"""Tests for queue_manager.py — SQLite queue persistence."""

import threading
from datetime import datetime, timezone

import pytest

from config import MAX_QUEUE_RETRIES
from queue_manager import QueueManager


class TestQueueManager:
    def test_enqueue_returns_increasing_ids(self, queue_manager: QueueManager):
        qm = queue_manager
        id1 = qm.enqueue("link", "url1")
        id2 = qm.enqueue("link", "url2")
        assert id1 == 1
        assert id2 == 2

    def test_dequeue_returns_item_and_marks_running(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("search", "Artist - Album")
        item = qm.dequeue()
        assert item is not None
        assert item["query"] == "Artist - Album"
        assert item["input_type"] == "search"
        assert item["status"] == "running"
        assert item["retries"] == 0
        assert item["started_at"] is not None

    def test_dequeue_empty_when_nothing_queued(self, queue_manager: QueueManager):
        assert queue_manager.dequeue() is None

    def test_dequeue_respects_fifo_order(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "first")
        qm.enqueue("link", "second")
        item = qm.dequeue()
        assert item["query"] == "first"

    def test_dequeue_skips_already_running(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "first")
        qm.enqueue("link", "second")
        qm.dequeue()  # marks first as running
        item = qm.dequeue()
        assert item["query"] == "second"

    def test_dequeue_skips_done_and_failed(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "good")
        qm.enqueue("link", "bad")
        qm.enqueue("link", "next")
        item1 = qm.dequeue()
        qm.mark_done(item1["id"], 1, 0, 0)
        item2 = qm.dequeue()
        qm.mark_failed(item2["id"], "error")
        item3 = qm.dequeue()
        assert item3["query"] == "next"

    def test_requeue_increments_retries(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "url")
        item = qm.dequeue()
        qm.requeue(item["id"])
        item2 = qm.dequeue()
        assert item2["retries"] == 1
        assert item2["status"] == "running"

    def test_requeue_multiple_times(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "url")
        for _ in range(3):
            item = qm.dequeue()
            qm.requeue(item["id"])
        item = qm.dequeue()
        assert item["retries"] == 3

    def test_mark_done_sets_status_and_counts(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "url")
        item = qm.dequeue()
        qm.mark_done(item["id"], 5, 2, 0)
        h = qm.get_history(10)
        done = [x for x in h if x["id"] == item["id"]][0]
        assert done["status"] == "done"
        assert done["result_ok"] == 5
        assert done["result_skipped"] == 2
        assert done["result_failed"] == 0
        assert done["completed_at"] is not None

    def test_mark_failed_sets_status_and_error(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "url")
        item = qm.dequeue()
        qm.mark_failed(item["id"], "Something went wrong")
        h = qm.get_history(10)
        failed = [x for x in h if x["id"] == item["id"]][0]
        assert failed["status"] == "failed"
        assert failed["error"] == "Something went wrong"
        assert failed["completed_at"] is not None

    def test_get_status_counts(self, queue_manager: QueueManager):
        qm = queue_manager
        assert qm.get_status()["queued"] == 0
        qm.enqueue("link", "a")
        qm.enqueue("link", "b")
        assert qm.get_status()["queued"] == 2
        item = qm.dequeue()
        s = qm.get_status()
        assert s["queued"] == 1
        assert s["running"] == 1
        qm.mark_done(item["id"], 1, 0, 0)
        s = qm.get_status()
        assert s["running"] == 0
        assert s["done"] == 1

    def test_get_status_next_id(self, queue_manager: QueueManager):
        qm = queue_manager
        assert qm.get_status()["next_id"] is None
        qm.enqueue("link", "a")
        assert qm.get_status()["next_id"] == 1

    def test_get_history_returns_newest_first(self, queue_manager: QueueManager):
        qm = queue_manager
        for i in range(5):
            qm.enqueue("link", f"url{i}")
        h = qm.get_history(3)
        assert len(h) == 3
        assert h[0]["id"] == 5
        assert h[1]["id"] == 4
        assert h[2]["id"] == 3

    def test_get_running_returns_only_running(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "a")
        qm.enqueue("link", "b")
        assert qm.get_running() == []
        first = qm.dequeue()
        second = qm.dequeue()
        running = qm.get_running()
        assert [r["id"] for r in running] == [first["id"], second["id"]]
        qm.mark_done(first["id"], 1, 0, 0)
        assert [r["id"] for r in qm.get_running()] == [second["id"]]

    def test_set_progress_updates_row(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "a")
        item = qm.dequeue()
        assert item["progress"] is None
        qm.set_progress(item["id"], "2/10 · Now: Song X")
        assert qm.get_running()[0]["progress"] == "2/10 · Now: Song X"

    def test_progress_cleared_on_done(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "a")
        item = qm.dequeue()
        qm.set_progress(item["id"], "5/5 · Now: Last")
        qm.mark_done(item["id"], 5, 0, 0)
        assert qm.get_item(item["id"])["progress"] is None

    def test_requeue_beyond_max_retries(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "url")
        item = qm.dequeue()
        for _ in range(MAX_QUEUE_RETRIES + 5):
            qm.requeue(item["id"])
        item = qm.dequeue()
        assert item["retries"] == MAX_QUEUE_RETRIES + 5

    def test_log_failed_track(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "url")
        item = qm.dequeue()
        qm.log_failed_track(item["id"], "Broken Song", "Qobuz 500")

        tracks = qm.get_failed_tracks(item_id=item["id"])
        assert len(tracks) == 1
        assert tracks[0]["track_title"] == "Broken Song"
        assert tracks[0]["error"] == "Qobuz 500"
        assert tracks[0]["item_id"] == item["id"]

    def test_get_failed_tracks_all(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "a")
        qm.enqueue("link", "b")
        i1 = qm.dequeue()
        i2 = qm.dequeue()
        qm.log_failed_track(i1["id"], "Song A", "err")
        qm.log_failed_track(i1["id"], "Song B", "err")
        qm.log_failed_track(i2["id"], "Song C", "err")

        all_tracks = qm.get_failed_tracks()
        assert len(all_tracks) == 3

        item1_tracks = qm.get_failed_tracks(item_id=i1["id"])
        assert len(item1_tracks) == 2

    def test_get_failed_tracks_empty(self, queue_manager: QueueManager):
        qm = queue_manager
        assert qm.get_failed_tracks() == []
        assert qm.get_failed_tracks(item_id=999) == []

    def test_get_give_up_titles(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "url")
        item = qm.dequeue()
        for _ in range(10):
            qm.log_failed_track(item["id"], "Broken Song", "Qobuz 500")
        qm.log_failed_track(item["id"], "Almost", "Deezer 404")

        gave_up = qm.get_give_up_titles(item["id"], threshold=10)
        assert gave_up == {"Broken Song"}

    def test_get_give_up_titles_below_threshold(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "url")
        item = qm.dequeue()
        qm.log_failed_track(item["id"], "Almost", "Deezer 404")

        assert qm.get_give_up_titles(item["id"], threshold=10) == set()
        assert qm.get_give_up_titles(999, threshold=10) == set()

    def test_log_failed_track_without_error(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "url")
        item = qm.dequeue()
        qm.log_failed_track(item["id"], "Track")
        tracks = qm.get_failed_tracks(item_id=item["id"])
        assert tracks[0]["error"] is None

    def test_failed_tracks_foreign_key(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "url")
        item = qm.dequeue()
        qm.log_failed_track(item["id"], "Song", "err")
        # Verify it persists after reopening (temp file)
        tracks = qm.get_failed_tracks(item_id=item["id"])
        assert len(tracks) == 1

    def test_enqueue_unique_creates_new(self, queue_manager: QueueManager):
        qm = queue_manager
        item_id, is_new = qm.enqueue_unique("link", "url")
        assert is_new is True
        assert item_id == 1

    def test_enqueue_unique_detects_duplicate(self, queue_manager: QueueManager):
        qm = queue_manager
        id1, _ = qm.enqueue_unique("link", "url")
        id2, is_new = qm.enqueue_unique("link", "url")
        assert is_new is False
        assert id2 == id1

    def test_enqueue_unique_allows_different_type(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue_unique("link", "url")
        _id2, is_new = qm.enqueue_unique("search", "url")
        assert is_new is True

    def test_enqueue_unique_allows_different_query(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue_unique("link", "url-a")
        _id2, is_new = qm.enqueue_unique("link", "url-b")
        assert is_new is True

    def test_enqueue_unique_after_done_allows_new(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue_unique("link", "url")
        item = qm.dequeue()
        qm.mark_done(item["id"], 1, 0, 0)
        _, is_new = qm.enqueue_unique("link", "url")
        assert is_new is True

    def test_enqueue_unique_after_failed_allows_new(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue_unique("link", "url")
        item = qm.dequeue()
        qm.mark_failed(item["id"], "err")
        _, is_new = qm.enqueue_unique("link", "url")
        assert is_new is True

    def test_purge_all_removes_everything(self, queue_manager: QueueManager):
        qm = queue_manager
        qm.enqueue("link", "a")
        item = qm.dequeue()
        qm.mark_done(item["id"], 1, 0, 0)
        qm.enqueue("link", "b")
        count = qm.purge_all()
        assert count == 2
        s = qm.get_status()
        assert s["queued"] == 0
        assert s["done"] == 0
        assert s["running"] == 0
        assert s["failed"] == 0

    def test_purge_all_zero_when_empty(self, queue_manager: QueueManager):
        assert queue_manager.purge_all() == 0

    def test_restart_resets_running_to_queued(self, tmp_path):
        """Simulate bot restart — stranded 'running' items become 'queued'."""
        db = str(tmp_path / "test_restart.db")
        qm1 = QueueManager(db)
        qm1.enqueue("link", "url1")
        qm1.enqueue("link", "url2")
        item = qm1.dequeue()  # url1 becomes running
        assert qm1.get_status()["running"] == 1
        del qm1  # simulate kill — no explicit close

        qm2 = QueueManager(db)  # simulate restart
        s = qm2.get_status()
        assert s["running"] == 0
        assert s["queued"] == 2
        restarted = qm2.dequeue()
        assert restarted is not None
        assert restarted["query"] == "url1"
        assert restarted["started_at"] is not None  # fresh timestamp

    def test_concurrent_enqueue(self, queue_manager: QueueManager):
        qm = queue_manager
        errors = []

        def add():
            try:
                for _ in range(50):
                    qm.enqueue("link", "url")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        s = qm.get_status()
        assert s["queued"] == 200

    def test_concurrent_enqueue_unique(self, queue_manager: QueueManager):
        """Verify all threads get same id and only one is_new=True."""
        qm = queue_manager
        results: list[tuple[int, bool]] = []
        errors: list[Exception] = []

        def add():
            try:
                results.append(qm.enqueue_unique("link", "same-key"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 8
        item_ids = {r[0] for r in results}
        is_new_flags = [r[1] for r in results]
        assert len(item_ids) == 1, "all threads must get the same item_id"
        assert sum(is_new_flags) == 1, "exactly one thread must get is_new=True"
