"""Tests for worker.py — Worker._process."""

import asyncio
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone, timedelta

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
        return {"ok": 0, "skipped": 0, "failed": len(failed_tracks), "failed_tracks": failed_tracks}

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


class TestWorkerProcess:
    """Tests Worker._process with mocked run_url and bot."""

    @pytest.fixture
    def bot(self):
        return AsyncMock()

    @pytest.fixture
    def worker(self, queue_manager, bot, config, logger):
        return Worker(queue_manager, bot, 12345, config, logger, asyncio.Event())

    @pytest.mark.asyncio
    async def test_success_marks_done(self, worker, bot):
        qm = worker._queue
        qm.enqueue("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        with patch("worker.run_url", return_value={"ok": 5, "skipped": 2, "failed": 0, "total": 7}):
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
    async def test_success_sends_correct_message(self, worker, bot):
        qm = worker._queue
        qm.enqueue("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        with patch("worker.run_url", return_value={"ok": 3, "skipped": 0, "failed": 0, "total": 3}):
            await worker._process(item)

        msg = bot.send_message.call_args[1]["text"]
        assert "3 ok" in msg

    @pytest.mark.asyncio
    async def test_failure_requeues_within_limits(self, worker, bot):
        qm = worker._queue
        qm.enqueue("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        with patch("worker.run_url", return_value={
            "ok": 0, "skipped": 0, "failed": 3, "total": 3,
            "failed_tracks": [
                ("id1", "Broken Song", "Qobuz 500"),
                ("id2", "Another Fail", "Tidal 410"),
                ("id3", "Last One", "Deezer 404"),
            ],
        }):
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
        qm.enqueue("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        for _ in range(MAX_TRACK_RETRIES):
            qm.log_failed_track(item["id"], "Broken Song", "Qobuz 500")

        with patch("worker.run_url", return_value={
            "ok": 0, "skipped": 0, "failed": 1, "total": 1,
            "failed_tracks": [("id1", "Broken Song", "Qobuz 500")],
        }):
            await worker._process(item)

        s = qm.get_status()
        assert s["done"] == 1
        assert s["queued"] == 0

        msg = bot.send_message.call_args[1]["text"]
        assert "given up" in msg.lower()

    @pytest.mark.asyncio
    async def test_requeues_when_other_tracks_still_trying(self, worker, bot):
        qm = worker._queue
        qm.enqueue("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        for _ in range(MAX_TRACK_RETRIES):
            qm.log_failed_track(item["id"], "Broken Song", "Qobuz 500")

        with patch("worker.run_url", return_value={
            "ok": 0, "skipped": 0, "failed": 2, "total": 2,
            "failed_tracks": [
                ("id1", "Broken Song", "Qobuz 500"),
                ("id2", "Fresh Fail", "Deezer 404"),
            ],
        }):
            await worker._process(item)

        s = qm.get_status()
        assert s["queued"] == 1
        msg = bot.send_message.call_args[1]["text"]
        assert "Re-queued" in msg

    @pytest.mark.asyncio
    async def test_max_retries_permanent_fail(self, worker, bot):
        qm = worker._queue
        qm.enqueue("link", "https://open.spotify.com/track/abc")
        for _ in range(MAX_QUEUE_RETRIES):
            item = qm.dequeue()
            qm.requeue(item["id"])
        item = qm.dequeue()

        with patch("worker.run_url", return_value={"ok": 0, "skipped": 0, "failed": 3, "total": 3}):
            await worker._process(item)

        s = qm.get_status()
        assert s["failed"] == 1
        assert s["queued"] == 0

        msg = bot.send_message.call_args[1]["text"]
        assert "Failed" in msg or "retries" in msg

    @pytest.mark.asyncio
    async def test_exception_during_download_fails_item(self, worker, bot):
        qm = worker._queue
        qm.enqueue("link", "https://open.spotify.com/track/abc")
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
        qm.enqueue("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()

        with patch("worker.run_url", return_value={
            "ok": 0, "skipped": 0, "failed": 2,
            "failed_tracks": [
                ("id1", "Broken Song", "Qobuz 500"),
                ("id2", "Another Fail", "Tidal 410"),
            ],
            "total": 2,
        }):
            await worker._process(item)

        tracks = qm.get_failed_tracks(item_id=item["id"])
        assert len(tracks) == 2
        titles = {t["track_title"] for t in tracks}
        assert "Broken Song" in titles
        assert "Another Fail" in titles

    @pytest.mark.asyncio
    async def test_overnight_timeout_gives_up(self, worker, bot):
        qm = worker._queue
        qm.enqueue("link", "https://open.spotify.com/track/abc")
        item = qm.dequeue()
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        item["created_at"] = old

        with patch("worker.run_url", return_value={"ok": 0, "skipped": 0, "failed": 3, "total": 3}):
            await worker._process(item)

        s = qm.get_status()
        assert s["failed"] == 1
        assert s["queued"] == 0

        msg = bot.send_message.call_args[1]["text"]
        assert "24h" in msg or "gave up" in msg.lower()
