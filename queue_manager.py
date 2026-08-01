"""SQLite queue persistence for download jobs."""

import math
import sqlite3
import threading
from datetime import datetime, timezone



class QueueManager:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _connect(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self):
        conn = self._connect()
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    input_type TEXT NOT NULL,
                    query TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    result_ok INTEGER DEFAULT 0,
                    result_skipped INTEGER DEFAULT 0,
                    result_failed INTEGER DEFAULT 0,
                    retries INTEGER DEFAULT 0,
                    error TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS failed_tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    track_title TEXT NOT NULL,
                    error TEXT,
                    failed_at TEXT NOT NULL,
                    FOREIGN KEY (item_id) REFERENCES queue(id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_failed_tracks_item
                ON failed_tracks(item_id)
            """)

            # Schema migration: add retry_at for exponential backoff
            try:
                conn.execute("ALTER TABLE queue ADD COLUMN retry_at TEXT")
            except sqlite3.OperationalError:
                pass

            # Reset items stranded in 'running' from a killed process
            conn.execute(
                "UPDATE queue SET status='queued', started_at=NULL WHERE status='running'"
            )

    def enqueue(self, input_type: str, query: str) -> int:
        with self._lock:
            conn = self._connect()
            with conn:
                now = datetime.now(timezone.utc).isoformat()
                cursor = conn.execute(
                    "INSERT INTO queue (input_type, query, created_at) VALUES (?, ?, ?)",
                    (input_type, query, now),
                )
                return cursor.lastrowid

    def enqueue_unique(self, input_type: str, query: str) -> tuple[int, bool]:
        """Atomically check for existing + insert under a single lock.

        Returns (item_id, is_new) — closes the TOCTOU race of separate
        check-then-insert calls.
        """
        with self._lock:
            conn = self._connect()
            existing = conn.execute(
                "SELECT id FROM queue WHERE input_type=? AND query=? "
                "AND status IN ('queued', 'running')",
                (input_type, query),
            ).fetchone()
            if existing is not None:
                return existing["id"], False
            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                "INSERT INTO queue (input_type, query, created_at) VALUES (?, ?, ?)",
                (input_type, query, now),
            )
            return cursor.lastrowid, True

    def dequeue(self) -> dict | None:
        with self._lock:
            conn = self._connect()
            with conn:
                now = datetime.now(timezone.utc).isoformat()
                cursor = conn.execute(
                    "SELECT * FROM queue WHERE status='queued' AND (retry_at IS NULL OR retry_at <= ?) ORDER BY id LIMIT 1",
                    (now,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE queue SET status='running', started_at=? WHERE id=?",
                    (now, row["id"]),
                )
            item = dict(row)
            item["status"] = "running"
            item["started_at"] = now
            return item

    def get_item(self, item_id: int) -> dict | None:
        with self._lock:
            conn = self._connect()
            cursor = conn.execute("SELECT * FROM queue WHERE id=?", (item_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def requeue(self, item_id: int, delay: float = 0):
        """Move item back to queued, increment retry count, and optionally set retry_at."""
        with self._lock:
            conn = self._connect()
            with conn:
                if delay > 0:
                    retry_at = datetime.now(timezone.utc).timestamp() + delay
                    retry_at_iso = datetime.fromtimestamp(retry_at, tz=timezone.utc).isoformat()
                    conn.execute(
                        "UPDATE queue SET status='queued', retries=retries+1, retry_at=? WHERE id=?",
                        (retry_at_iso, item_id),
                    )
                else:
                    conn.execute(
                        "UPDATE queue SET status='queued', retries=retries+1, retry_at=NULL WHERE id=?",
                        (item_id,),
                    )

    def get_next_retry_at(self) -> datetime | None:
        """Return the earliest retry_at of all queued items, or None."""
        conn = self._connect()
        cursor = conn.execute(
            "SELECT MIN(retry_at) FROM queue WHERE status='queued' AND retry_at IS NOT NULL"
        )
        row = cursor.fetchone()
        if row and row[0]:
            return datetime.fromisoformat(row[0])
        return None

    def mark_done(self, item_id: int, ok: int, skipped: int, failed: int):
        with self._lock:
            conn = self._connect()
            with conn:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE queue SET status='done', completed_at=?, "
                    "result_ok=?, result_skipped=?, result_failed=? WHERE id=?",
                    (now, ok, skipped, failed, item_id),
                )

    def mark_failed(self, item_id: int, error: str):
        with self._lock:
            conn = self._connect()
            with conn:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE queue SET status='failed', completed_at=?, error=? WHERE id=?",
                    (now, error, item_id),
                )

    def get_status(self) -> dict:
        conn = self._connect()
        cur = conn.execute("SELECT status, COUNT(*) FROM queue GROUP BY status")
        counts = dict(cur.fetchall())
        cur = conn.execute(
            "SELECT id FROM queue WHERE status='queued' ORDER BY id LIMIT 1"
        )
        row = cur.fetchone()
        return {
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "done": counts.get("done", 0),
            "failed": counts.get("failed", 0),
            "next_id": row["id"] if row else None,
        }

    def log_failed_track(self, item_id: int, track_title: str, error: str | None = None):
        with self._lock:
            conn = self._connect()
            with conn:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO failed_tracks (item_id, track_title, error, failed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (item_id, track_title, error, now),
                )

    def get_failed_tracks(self, item_id: int | None = None, limit: int = 50) -> list[dict]:
        conn = self._connect()
        if item_id:
            cursor = conn.execute(
                "SELECT * FROM failed_tracks WHERE item_id=? ORDER BY id DESC LIMIT ?",
                (item_id, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM failed_tracks ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        return [dict(row) for row in cursor.fetchall()]

    def get_give_up_titles(self, item_id: int, threshold: int) -> set[str]:
        """Titles that have failed >= threshold times for this item."""
        conn = self._connect()
        cursor = conn.execute(
            "SELECT track_title, COUNT(*) AS n FROM failed_tracks "
            "WHERE item_id=? GROUP BY track_title HAVING n >= ?",
            (item_id, threshold),
        )
        return {row["track_title"] for row in cursor.fetchall()}

    def purge_all(self) -> int:
        with self._lock:
            conn = self._connect()
            with conn:
                cursor = conn.execute("DELETE FROM queue")
                return cursor.rowcount

    def get_history(self, limit: int = 10) -> list[dict]:
        conn = self._connect()
        cursor = conn.execute(
            "SELECT * FROM queue ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]
