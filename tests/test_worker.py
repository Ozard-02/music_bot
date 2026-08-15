"""Tests for worker.py — Worker._process (subprocess-based)."""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from config import MAX_QUEUE_RETRIES, MAX_TRACK_RETRIES, RETRY_BACKOFF_BASE
from worker import Worker, decide_failure


class TestDecideFailure:
    """Pure decision logic for failed items — no DB, no bot."""

    def _item(self, retries=0, created=None):
        return {
            "id": 1,
            "retries": retries,
            "created_at": created or datetime.now(timezone.utc).isoformat(),
        }

    def _result(self, failed_tracks):
        return {"failed": len(failed_tracks), "failed_tracks": failed_tracks}

    def test_older_than_24h_fails_timeout(self):
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        d = decide_failure(self._item(created=old.isoformat()), self._result([]), set())
        assert d.action == "fail"
        assert "24h" in d.detail

    def test_max_retries_fails(self):
        d = decide_failure(self._item(retries=MAX_QUEUE_RETRIES), self._result([("id1", "A", "err")]), set())
        assert d.action == "fail"
        assert d.detail == "Max retries exceeded"

    def test_all_failures_given_up_marks_done(self):
        d = decide_failure(self._item(), self._result([("id1", "Broken", "err")]), {"Broken"})
        assert d.action == "done"
        assert d.detail == 1

    def test_mix_of_given_up_and_fresh_requeues(self):
        d = decide_failure(
            self._item(),
            self._result([("id1", "Broken", "err"), ("id2", "Fresh", "err")]),
            {"Broken"},
        )
        assert d.action == "requeue"
        assert d.detail == RETRY_BACKOFF_BASE

    def test_requeue_backoff_doubles_with_retries(self):
        d = decide_failure(self._item(retries=2), self._result([("id1", "A", "err")]), set())
        assert d.detail == RETRY_BACKOFF_BASE * 4

    def test_bad_created_at_treated_as_fresh(self):
        d = decide_failure(self._item(created="not-a-date"), self._result([("id1", "A", "err")]), set())
        assert d.action == "requeue"


@pytest.fixture
def bot():
    return AsyncMock()


@pytest.fixture
def worker(queue_manager, bot, config, logger):
    return Worker(queue_manager, bot, 12345, config, logger, asyncio.Event())


def _ok_result(**kw):
    base = {"ok": 0, "skipped": 0, "failed": 0, "failed_tracks": [], "gave_up_tracks": [], "total": 0, "providers": {}}
    base.update(kw)
    return base


class TestWorkerProcess:
    """Tests Worker._process with mocked _run_job (subprocess boundary)."""

    @pytest.mark.asyncio
    async def test_success_marks_done(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        with patch("worker.Worker._run_job", return_value=(_ok_result(ok=5, skipped=2, total=7), "display")):
            await worker._process(item)

        s = qm.get_status()
        assert s["done"] == 1
        assert s["running"] == 0
        h = qm.get_history(1)
        assert h[0]["result_ok"] == 5
        assert h[0]["result_skipped"] == 2
        assert h[0]["result_failed"] == 0

        bot.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timeout_marks_failed_not_requeued(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        async def slow_job(self, item, skip, cfg, chat):
            await asyncio.to_thread(qm.mark_failed, item["id"], "download timed out")
            await worker._safe_notify(
                f"❌ <b>X</b>\n  ⏰ download timed out — failed, next item in queue",
                chat,
            )
            await asyncio.sleep(0.01)
            return None

        with patch("worker.Worker._run_job", new=slow_job):
            await worker._process(item)

        s = qm.get_status()
        assert s["failed"] == 1
        assert s["queued"] == 0

        bot.assert_awaited()
        msg = bot.call_args[0][0]
        assert "timed out" in msg

    @pytest.mark.asyncio
    async def test_success_sends_correct_message(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        with patch("worker.Worker._run_job", return_value=(_ok_result(ok=3, total=3), "Album X")):
            await worker._process(item)

        msg = bot.call_args[0][0]
        assert "3 ok" in msg
        assert "Album X" in msg

    @pytest.mark.asyncio
    async def test_failure_requeues_within_limits(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        with patch("worker.Worker._run_job", return_value=(_ok_result(
            failed=3,
            failed_tracks=[
                ("id1", "Broken Song", "Qobuz 500"),
                ("id2", "Another Fail", "Tidal 410"),
                ("id3", "Last One", "Deezer 404"),
            ],
            total=3,
        ), "Album X")):
            await worker._process(item)

        s = qm.get_status()
        assert s["queued"] == 1
        assert s["running"] == 0
        item_from_db = qm.get_item(item["id"])
        assert item_from_db["retries"] == 1
        assert item_from_db["retry_at"] is not None

        msg = bot.call_args[0][0]
        assert "Re-queued" in msg

    @pytest.mark.asyncio
    async def test_track_gives_up_after_max_attempts(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        for _ in range(MAX_TRACK_RETRIES):
            qm.log_failed_track(item["id"], "Broken Song", "Qobuz 500")

        with patch("worker.Worker._run_job", return_value=(_ok_result(
            failed=1,
            failed_tracks=[("id1", "Broken Song", "Qobuz 500")],
            total=1,
        ), "Album X")):
            await worker._process(item)

        s = qm.get_status()
        assert s["done"] == 1
        assert s["queued"] == 0

        msg = bot.call_args[0][0]
        assert "given up" in msg.lower()

    @pytest.mark.asyncio
    async def test_requeues_when_other_tracks_still_trying(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        for _ in range(MAX_TRACK_RETRIES):
            qm.log_failed_track(item["id"], "Broken Song", "Qobuz 500")

        with patch("worker.Worker._run_job", return_value=(_ok_result(
            failed=2,
            failed_tracks=[
                ("id1", "Broken Song", "Qobuz 500"),
                ("id2", "Fresh Fail", "Deezer 404"),
            ],
            total=2,
        ), "Album X")):
            await worker._process(item)

        s = qm.get_status()
        assert s["queued"] == 1
        msg = bot.call_args[0][0]
        assert "Re-queued" in msg

    @pytest.mark.asyncio
    async def test_max_retries_permanent_fail(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        for _ in range(MAX_QUEUE_RETRIES):
            item = qm.dequeue()
            qm.requeue(item["id"])
        item = qm.dequeue()

        with patch("worker.Worker._run_job", return_value=(_ok_result(failed=3, total=3), "Album X")):
            await worker._process(item)

        s = qm.get_status()
        assert s["failed"] == 1
        assert s["queued"] == 0

        msg = bot.call_args[0][0]
        assert "Failed" in msg or "retries" in msg

    @pytest.mark.asyncio
    async def test_exception_during_download_fails_item(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        with patch("worker.Worker._run_job", side_effect=RuntimeError("Connection failed")):
            await worker._process(item)

        s = qm.get_status()
        assert s["failed"] == 1
        assert s["running"] == 0

        msg = bot.call_args[0][0]
        assert "Failed" in msg

    @pytest.mark.asyncio
    async def test_overnight_timeout_gives_up(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        item["created_at"] = old

        with patch("worker.Worker._run_job", return_value=(_ok_result(failed=3, total=3), "Album X")):
            await worker._process(item)

        s = qm.get_status()
        assert s["failed"] == 1
        assert s["queued"] == 0

        msg = bot.call_args[0][0]
        assert "24h" in msg or "gave up" in msg.lower()


class TestNotificationSafety:
    """Notifications must never affect the item's DB status, and remote
    content (display names) must be HTML-escaped before sending."""

    TRACK = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"

    @pytest.mark.asyncio
    async def test_display_with_special_chars_is_escaped(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", self.TRACK)
        item = qm.dequeue()

        with patch("worker.Worker._run_job", return_value=(_ok_result(ok=3, total=3), "Guns N' Roses & Friends")):
            await worker._process(item)

        msg = bot.call_args[0][0]
        assert "Guns N' Roses &amp; Friends" in msg
        assert "Roses & Friends" not in msg
        assert bot.call_args[0][0]
        s = qm.get_status()
        assert s["done"] == 1
        assert s["failed"] == 0

    @pytest.mark.asyncio
    async def test_send_failure_does_not_flip_done_to_failed(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", self.TRACK)
        item = qm.dequeue()
        bot.side_effect = RuntimeError("Telegram parse error")

        with patch("worker.Worker._run_job", return_value=(_ok_result(ok=3, total=3), "X")):
            await worker._process(item)  # must not raise

        s = qm.get_status()
        assert s["done"] == 1
        assert s["failed"] == 0

    @pytest.mark.asyncio
    async def test_send_failure_does_not_crash_requeue_path(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", self.TRACK)
        item = qm.dequeue()
        bot.side_effect = RuntimeError("Telegram network error")

        with patch("worker.Worker._run_job", return_value=(_ok_result(
            failed=2,
            failed_tracks=[("id1", "Broken", "Qobuz 500"), ("id2", "Another", "Tidal 410")],
            total=2,
        ), "X")):
            await worker._process(item)

        s = qm.get_status()
        assert s["queued"] == 1  # requeued, not failed
        assert s["failed"] == 0


class TestWorkerPerUser:
    """Items carry the queueing user; the worker must use that user's folder
    and quality and notify the user who queued it."""

    @pytest.mark.asyncio
    async def test_user_item_uses_user_folder_quality_and_chat(self, worker, bot, tmp_path):
        worker._cfg["output_dir"] = str(tmp_path)
        qm = worker._queue
        qm.upsert_user(777, "guest", "guest_Music", "LOW")
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc", user=777)
        item = qm.dequeue()

        captured = {}

        async def fake_job(self, item, skip, cfg, chat):
            captured["cfg"] = cfg
            return _ok_result(ok=1, total=1), "X"

        with patch("worker.Worker._run_job", new=fake_job):
            await worker._process(item)

        assert captured["cfg"]["output_dir"] == str(tmp_path / "guest_Music")
        assert captured["cfg"]["quality"] == "LOW"
        bot.assert_awaited_once()
        assert bot.call_args[0][1] == 777

    @pytest.mark.asyncio
    async def test_legacy_item_uses_base_cfg_and_default_chat(self, worker, bot, tmp_path):
        worker._cfg["output_dir"] = str(tmp_path)
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        captured = {}

        async def fake_job(self, item, skip, cfg, chat):
            captured["cfg"] = cfg
            return _ok_result(ok=1, total=1), "X"

        with patch("worker.Worker._run_job", new=fake_job):
            await worker._process(item)

        assert captured["cfg"]["output_dir"] == str(tmp_path)
        bot.assert_awaited_once()
        assert bot.call_args[0][1] == 12345