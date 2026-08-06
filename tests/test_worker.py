"""Tests for worker.py — Worker._process."""

import asyncio
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone, timedelta

import pytest

from config import MAX_QUEUE_RETRIES, MAX_TRACK_RETRIES, RETRY_BACKOFF_BASE
from downloader import DownloadResult
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
        return DownloadResult(failed=len(failed_tracks), failed_tracks=failed_tracks)

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


class TestWorkerProcess:
    """Tests Worker._process with mocked run_url and bot."""

    @pytest.mark.asyncio
    async def test_success_marks_done(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        with patch("worker.run_url", return_value=DownloadResult(ok=5, skipped=2, total=7)):
            await worker._process(item)

        s = qm.get_status()
        assert s["done"] == 1
        assert s["running"] == 0
        h = qm.get_history(1)
        assert h[0]["result_ok"] == 5
        assert h[0]["result_skipped"] == 2
        assert h[0]["result_failed"] == 0

        bot.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_progress_cb_wired_into_run_url(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        captured = {}

        def fake_run_url(url, cfg, logger, skip_titles=None, progress_cb=None, failure_cb=None):
            captured["progress_cb"] = progress_cb
            captured["failure_cb"] = failure_cb
            return DownloadResult(ok=1, total=1)

        with patch("worker.run_url", side_effect=fake_run_url):
            await worker._process(item)

        cb = captured["progress_cb"]
        assert cb is not None
        with patch.object(qm, "set_progress", wraps=qm.set_progress) as spy:
            cb(3, 10, "Song X")
        spy.assert_called_once_with(item["id"], "3/10 · Now: Song X")

    @pytest.mark.asyncio
    async def test_failure_cb_logs_failed_tracks_live(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        captured = {}

        def fake_run_url(url, cfg, logger, skip_titles=None, progress_cb=None, failure_cb=None):
            captured["failure_cb"] = failure_cb
            return DownloadResult(failed=2, total=2)

        with patch("worker.run_url", side_effect=fake_run_url):
            await worker._process(item)

        fb = captured["failure_cb"]
        assert fb is not None
        fb("Broken Song", "Qobuz 500")
        fb("Another Fail", "Tidal 410")
        tracks = qm.get_failed_tracks(item_id=item["id"])
        assert len(tracks) == 2
        titles = {t["track_title"] for t in tracks}
        assert titles == {"Broken Song", "Another Fail"}

    @pytest.mark.asyncio
    async def test_timeout_marks_failed_not_requeued(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        async def slow_run_url(*args, **kwargs):
            await asyncio.sleep(0.2)
            return {"ok": 1, "skipped": 0, "failed": 0, "total": 1}

        worker._cfg = dict(worker._cfg, max_download_timeout=0.05)
        with patch("worker.run_url", new=slow_run_url):
            await worker._process(item)

        s = qm.get_status()
        assert s["failed"] == 1
        assert s["queued"] == 0
        item_from_db = qm.get_item(item["id"])
        assert item_from_db["status"] == "failed"

        bot.send_message.assert_awaited()
        msg = bot.send_message.call_args[1]["text"]
        assert "timed out" in msg

    @pytest.mark.asyncio
    async def test_timeout_persists_failures_from_failure_cb(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        async def slow_run_url(url, cfg, logger, skip_titles=None, progress_cb=None, failure_cb=None):
            failure_cb("Broken Song", "Qobuz 500")
            await asyncio.sleep(0.2)
            return {"ok": 0, "skipped": 0, "failed": 0, "failed_tracks": [], "total": 1}

        worker._cfg = dict(worker._cfg, max_download_timeout=0.05)
        with patch("worker.run_url", new=slow_run_url):
            await worker._process(item)

        tracks = qm.get_failed_tracks(item_id=item["id"])
        assert len(tracks) == 1
        assert tracks[0]["track_title"] == "Broken Song"

    @pytest.mark.asyncio
    async def test_timeout_after_max_retries_fails(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        for _ in range(MAX_QUEUE_RETRIES):
            item = qm.dequeue()
            qm.requeue(item["id"])
        item = qm.dequeue()

        async def slow_run_url(*args, **kwargs):
            await asyncio.sleep(0.2)
            return {"ok": 1, "skipped": 0, "failed": 0, "total": 1}

        worker._cfg = dict(worker._cfg, max_download_timeout=0.05)
        with patch("worker.run_url", new=slow_run_url):
            await worker._process(item)

        s = qm.get_status()
        assert s["failed"] == 1
        assert s["queued"] == 0

    @pytest.mark.asyncio
    async def test_success_sends_correct_message(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        with patch("worker.run_url", return_value=DownloadResult(ok=3, total=3)):
            await worker._process(item)

        msg = bot.send_message.call_args[1]["text"]
        assert "3 ok" in msg

    @pytest.mark.asyncio
    async def test_failure_requeues_within_limits(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        with patch("worker.run_url", return_value=DownloadResult(
            failed=3,
            failed_tracks=[
                ("id1", "Broken Song", "Qobuz 500"),
                ("id2", "Another Fail", "Tidal 410"),
                ("id3", "Last One", "Deezer 404"),
            ],
            total=3,
        )):
            await worker._process(item)

        s = qm.get_status()
        assert s["queued"] == 1
        assert s["running"] == 0
        item_from_db = qm.get_item(item["id"])
        assert item_from_db["retries"] == 1
        assert item_from_db["retry_at"] is not None

        bot.send_message.assert_awaited()
        msg = bot.send_message.call_args[1]["text"]
        assert "Re-queued" in msg

    @pytest.mark.asyncio
    async def test_track_gives_up_after_max_attempts(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        for _ in range(MAX_TRACK_RETRIES):
            qm.log_failed_track(item["id"], "Broken Song", "Qobuz 500")

        with patch("worker.run_url", return_value=DownloadResult(
            failed=1,
            failed_tracks=[("id1", "Broken Song", "Qobuz 500")],
            total=1,
        )):
            await worker._process(item)

        s = qm.get_status()
        assert s["done"] == 1
        assert s["queued"] == 0

        msg = bot.send_message.call_args[1]["text"]
        assert "given up" in msg.lower()

    @pytest.mark.asyncio
    async def test_requeues_when_other_tracks_still_trying(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        for _ in range(MAX_TRACK_RETRIES):
            qm.log_failed_track(item["id"], "Broken Song", "Qobuz 500")

        with patch("worker.run_url", return_value=DownloadResult(
            failed=2,
            failed_tracks=[
                ("id1", "Broken Song", "Qobuz 500"),
                ("id2", "Fresh Fail", "Deezer 404"),
            ],
            total=2,
        )):
            await worker._process(item)

        s = qm.get_status()
        assert s["queued"] == 1
        msg = bot.send_message.call_args[1]["text"]
        assert "Re-queued" in msg

    @pytest.mark.asyncio
    async def test_max_retries_permanent_fail(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        for _ in range(MAX_QUEUE_RETRIES):
            item = qm.dequeue()
            qm.requeue(item["id"])
        item = qm.dequeue()

        with patch("worker.run_url", return_value=DownloadResult(failed=3, total=3)):
            await worker._process(item)

        s = qm.get_status()
        assert s["failed"] == 1
        assert s["queued"] == 0

        msg = bot.send_message.call_args[1]["text"]
        assert "Failed" in msg or "retries" in msg

    @pytest.mark.asyncio
    async def test_exception_during_download_fails_item(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        with patch("worker.run_url", side_effect=RuntimeError("Connection failed")):
            await worker._process(item)

        s = qm.get_status()
        assert s["failed"] == 1
        assert s["running"] == 0

        bot.send_message.assert_awaited()
        msg = bot.send_message.call_args[1]["text"]
        assert "Failed" in msg

    @pytest.mark.asyncio
    async def test_logs_failed_tracks_to_db(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        def fake_run_url(url, cfg, logger, skip_titles=None, progress_cb=None, failure_cb=None):
            failure_cb("Broken Song", "Qobuz 500")
            failure_cb("Another Fail", "Tidal 410")
            return DownloadResult(failed=2, total=2)

        with patch("worker.run_url", side_effect=fake_run_url):
            await worker._process(item)

        tracks = qm.get_failed_tracks(item_id=item["id"])
        assert len(tracks) == 2
        titles = {t["track_title"] for t in tracks}
        assert "Broken Song" in titles
        assert "Another Fail" in titles

    @pytest.mark.asyncio
    async def test_overnight_timeout_gives_up(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        item["created_at"] = old

        with patch("worker.run_url", return_value=DownloadResult(failed=3, total=3)):
            await worker._process(item)

        s = qm.get_status()
        assert s["failed"] == 1
        assert s["queued"] == 0

        msg = bot.send_message.call_args[1]["text"]
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
        worker._resolve = AsyncMock(return_value=(self.TRACK, "Guns N' Roses & Friends"))

        with patch("worker.run_url", return_value=DownloadResult(ok=3, total=3)):
            await worker._process(item)

        msg = bot.send_message.call_args[1]["text"]
        assert "Guns N' Roses &amp; Friends" in msg
        assert "Roses & Friends" not in msg
        assert bot.send_message.call_args[1]["parse_mode"] == "HTML"
        s = qm.get_status()
        assert s["done"] == 1
        assert s["failed"] == 0

    @pytest.mark.asyncio
    async def test_send_failure_does_not_flip_done_to_failed(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", self.TRACK)
        item = qm.dequeue()
        bot.send_message.side_effect = RuntimeError("Telegram parse error")

        with patch("worker.run_url", return_value=DownloadResult(ok=3, total=3)):
            await worker._process(item)  # must not raise

        s = qm.get_status()
        assert s["done"] == 1
        assert s["failed"] == 0

    @pytest.mark.asyncio
    async def test_send_failure_does_not_crash_requeue_path(self, worker, bot):
        qm = worker._queue
        qm.enqueue_unique("link", self.TRACK)
        item = qm.dequeue()
        bot.send_message.side_effect = RuntimeError("Telegram network error")

        with patch("worker.run_url", return_value=DownloadResult(
            failed=2,
            failed_tracks=[("id1", "Broken", "Qobuz 500"), ("id2", "Another", "Tidal 410")],
            total=2,
        )):
            await worker._process(item)

        s = qm.get_status()
        assert s["queued"] == 1  # requeued, not failed
        assert s["failed"] == 0


class TestWorkerPerUser:
    """Items carry the queueing user; the worker must download into that
    user's folder at their quality and notify the user who queued it."""

    @pytest.mark.asyncio
    async def test_user_item_uses_user_folder_quality_and_chat(self, worker, bot, tmp_path):
        worker._cfg["output_dir"] = str(tmp_path)
        qm = worker._queue
        qm.upsert_user(777, "guest", "guest_Music", "LOW")
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc", user=777)
        item = qm.dequeue()

        captured = {}

        def fake_run_url(url, cfg, logger, skip_titles=None, progress_cb=None, failure_cb=None):
            captured["cfg"] = cfg
            return DownloadResult(ok=1, total=1)

        with patch("worker.run_url", side_effect=fake_run_url):
            await worker._process(item)

        assert captured["cfg"]["output_dir"] == str(tmp_path / "guest_Music")
        assert captured["cfg"]["quality"] == "LOW"
        bot.send_message.assert_awaited_once()
        assert bot.send_message.call_args.kwargs["chat_id"] == 777

    @pytest.mark.asyncio
    async def test_legacy_item_uses_base_cfg_and_default_chat(self, worker, bot, tmp_path):
        worker._cfg["output_dir"] = str(tmp_path)
        qm = worker._queue
        qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        captured = {}

        def fake_run_url(url, cfg, logger, skip_titles=None, progress_cb=None, failure_cb=None):
            captured["cfg"] = cfg
            return DownloadResult(ok=1, total=1)

        with patch("worker.run_url", side_effect=fake_run_url):
            await worker._process(item)

        assert captured["cfg"]["output_dir"] == str(tmp_path)
        bot.send_message.assert_awaited_once()
        assert bot.send_message.call_args.kwargs["chat_id"] == 12345


def test_trim_rss_runs_without_raising():
    """RSS trim must never crash the worker — even on platforms without
    glibc's malloc_trim."""
    from worker import _trim_rss
    with patch("worker.ctypes.CDLL", side_effect=OSError("no libc")):
        _trim_rss()
    _trim_rss()
