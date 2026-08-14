"""Background queue processor."""

import asyncio
import ctypes
import gc
import logging
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from telegram import Bot

from config import (
    MAX_PARALLEL_JOBS,
    MAX_QUEUE_RETRIES,
    MAX_TRACK_RETRIES,
    MAX_DOWNLOAD_TIMEOUT,
    MAX_QUEUE_AGE,
    STALL_TIMEOUT,
    RETRY_BACKOFF_BASE,
    MAX_RETRY_BACKOFF,
    esc,
)
from m3u8 import build_m3u8
from queue_manager import QueueManager
from library import user_cfg
from resolver import resolve_search
from downloader import DownloadResult, run_url
from SpotiFLAC.providers.spotify_metadata import parse_spotify_url


def _run_url_sync(
    url: str,
    cfg: dict,
    logger: logging.Logger,
    skip_titles: set[str] | None = None,
    progress_cb=None,
    failure_cb=None,
) -> DownloadResult:
    """Run the download on its own loop without blocking teardown on leaked
    threads.

    asyncio.run() tears down via shutdown_default_executor(300s) — a leaked
    download thread (e.g. a dead qobuz community session stuck in its
    verification) stalls teardown the full 300s, blocking the worker slot and
    spamming 'executor did not finish joining its threads'.  We own the loop
    instead: cancel pending tasks, flush asyncgens, then shutdown the default
    executor without waiting — the leaked thread finishes on its own and
    pre-check picks up whatever it writes on a future run."""
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            run_url(url, cfg, logger, skip_titles, progress_cb, failure_cb),
        )
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True),
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            executor = getattr(loop, "_default_executor", None)
            loop.close()
            if executor is not None:
                executor.shutdown(wait=False)
    return result


def _trim_rss() -> None:
    """Return freed heap pages to the OS so idle RSS settles instead of staying
    at the last download peak.

    Each job runs in a throwaway thread (`asyncio.to_thread` → `asyncio.run`).
    When the thread exits, glibc/Python allocator arenas keep the pages
    resident, so RSS = peak RSS forever.  gc.collect() releases Python cycles,
    then malloc_trim(0) hands free glibc heap pages back.  No-op on platforms
    without glibc's malloc_trim.
    """
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).malloc_trim(0)
    except Exception:
        pass  # ponytail: non-glibc — nothing to trim, keep going


@dataclass(frozen=True)
class FailureDecision:
    """Outcome for a failed item: action + detail (error, count, or delay)."""

    action: str  # "fail" | "done" | "requeue"
    detail: object = None


def item_age(item: dict, now: datetime | None = None) -> float:
    """Age in seconds since `created_at`; 0 if the timestamp is unparseable."""
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        created = datetime.fromisoformat(item["created_at"])
        return (now - created).total_seconds()
    except (ValueError, TypeError, KeyError):
        return 0


def is_expired(item: dict, now: datetime | None = None) -> bool:
    """True when an item is past the queue age or retry limit."""
    return item_age(item, now) > MAX_QUEUE_AGE or item.get("retries", 0) >= MAX_QUEUE_RETRIES


def backoff_delay(retries: int, floor: int = 0) -> int:
    """Exponential backoff in seconds, capped at MAX_RETRY_BACKOFF."""
    return max(floor, min(MAX_RETRY_BACKOFF, RETRY_BACKOFF_BASE * (2 ** retries)))


def decide_failure(item: dict, result: DownloadResult, gave_up_titles: set[str], *, now: datetime | None = None) -> FailureDecision:
    """Pure decision logic for failed downloads. Checks, in order:
    1. age > MAX_QUEUE_AGE            → fail ("Timed out in queue")
    2. retries >= MAX_QUEUE_RETRIES    → fail ("Max retries exceeded")
    3. no still-trying failures        → done with partial result (gave-up count)
    4. otherwise                       → requeue with exponential backoff delay
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if is_expired(item, now):
        reason = "Timed out in queue (>24h)" if item_age(item, now) > MAX_QUEUE_AGE else "Max retries exceeded"
        return FailureDecision("fail", reason)

    still_trying = [
        title for _track_id, title, _err in result.failed_tracks
        if title not in gave_up_titles
    ]
    if not still_trying:
        return FailureDecision("done", len(gave_up_titles))

    return FailureDecision("requeue", backoff_delay(item.get("retries", 0)))


def _result_summary(result: DownloadResult) -> str:
    parts = []
    if result.ok:
        parts.append(f"✅ {result.ok} ok")
    if result.skipped:
        parts.append(f"⏭ {result.skipped} skipped")
    if result.failed:
        parts.append(f"❌ {result.failed} failed")
    if result.providers:
        prov = " · ".join(f"{k} {v}" for k, v in sorted(result.providers.items()))
        parts.append(f"🧪 {prov}")
    return " | ".join(parts)


def _done_message(display: str, result: DownloadResult, given_up: int) -> str:
    """Completion message shared by the clean-success and all-gave-up paths."""
    parts = [_result_summary(result)]
    if given_up:
        parts.append(f"❌ {given_up} given up")
    msg = f"✅ <b>{esc(display)}</b>\n  {' | '.join(parts)}"
    if given_up:
        msg += f"\n  🧊 Tracks gave up after {MAX_TRACK_RETRIES} attempts"
    return msg


class Worker:
    def __init__(
        self,
        queue: QueueManager,
        bot: Bot,
        chat_id: int,
        cfg: dict,
        logger: logging.Logger,
        wake_event: asyncio.Event,
        max_parallel: int = MAX_PARALLEL_JOBS,
    ):
        self._queue = queue
        self._bot = bot
        self._chat_id = chat_id
        self._cfg = cfg
        self._logger = logger
        self._wake_event = wake_event
        self._poll = 5
        self._sem = asyncio.Semaphore(max_parallel)
        self._max_parallel = max_parallel
        self._shutdown = False
        self._tasks: set[asyncio.Task] = set()
        self._active: dict[int, int] = {}

    async def run(self):
        self._logger.info("Worker started (max_parallel=%d)", self._max_parallel)
        while not self._shutdown:
            slot = False
            try:
                await self._sem.acquire()
                slot = True
                item = await asyncio.to_thread(self._queue.dequeue)
                if item:
                    self._poll = 5
                    asyncio.create_task(self._run_with_sem(item))
                    slot = False
                else:
                    next_retry = await asyncio.to_thread(self._queue.get_next_retry_at)
                    if next_retry:
                        remaining = (next_retry - datetime.now(timezone.utc)).total_seconds()
                        self._poll = max(5, min(300, remaining))
                    else:
                        self._poll = min(300, self._poll * 2)
                        if self._poll == 300:
                            await asyncio.to_thread(_trim_rss)
            except Exception as e:
                self._logger.error("Worker error: %s", e)
                self._poll = min(300, self._poll * 2)
            finally:
                if slot:
                    self._sem.release()

            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=self._poll)
            except asyncio.TimeoutError:
                pass
            else:
                self._wake_event.clear()

    async def _run_with_sem(self, item: dict):
        task = asyncio.current_task()
        item_id = item["id"]
        if item_id in self._active:
            self._logger.info("#%d: already in flight, deferring", item_id)
            await asyncio.to_thread(self._queue.requeue, item_id, 1800)
            self._sem.release()
            return
        self._active[item_id] = item.get("retries", 0)
        self._tasks.add(task)
        try:
            await self._process(item)
        finally:
            self._active.pop(item_id, None)
            self._tasks.discard(task)
            self._sem.release()
            self._wake_event.set()
            await asyncio.to_thread(_trim_rss)

    async def shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        self._wake_event.set()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _notify(self, text: str, chat_id: int | None = None) -> None:
        """Send a message to the item's owner (or the default chat). A failed
        send must never affect the item's DB status or crash the worker —
        log and move on."""
        try:
            await self._bot.send_message(chat_id=chat_id or self._chat_id, text=text, parse_mode="HTML")
        except Exception as e:
            self._logger.warning("Failed to send notification: %s", e)

    async def _item_cfg(self, item: dict) -> dict:
        """Per-item cfg: output_dir resolved to the user's folder and quality
        set to the user's stored preference. Falls back to the base cfg for
        legacy items without a user."""
        cfg = self._cfg
        uid = item.get("user")
        if uid:
            row = await asyncio.to_thread(self._queue.get_user, uid)
            if row:
                cfg = user_cfg(self._cfg, row["folder"])
                quality = row.get("quality")
                if quality:
                    cfg = {**cfg, "quality": quality}
        return cfg

    @staticmethod
    def _item_chat(item: dict) -> int | None:
        """Where to notify about this item: the user who queued it, or the
        default chat for legacy items."""
        return item.get("user") or None

    async def _process(self, item: dict):
        self._logger.info("Processing #%d: %s", item["id"], item["query"])
        chat = self._item_chat(item)
        try:
            cfg = await self._item_cfg(item)
            url, display = await self._resolve(item, cfg)
            skip_titles = await asyncio.to_thread(
                self._queue.get_give_up_titles, item["id"], MAX_TRACK_RETRIES,
            )
            if skip_titles:
                self._logger.info(
                    "#%d: skipping %d given-up tracks",
                    item["id"], len(skip_titles),
                )

            result = await self._run_download(item, url, display, skip_titles, cfg, chat)
            if result is None:
                return  # timed out — requeued by _handle_timeout

            if result.failed == 0:
                await self._handle_no_failures(item, display, result, chat)
            else:
                await self._handle_failure(item, display, result, chat)

        except Exception as e:
            self._logger.error("Failed #%d: %s\n%s", item["id"], e, traceback.format_exc())
            await asyncio.to_thread(self._queue.mark_failed, item["id"], str(e))
            await self._notify(
                f"❌ <b>Failed #{item['id']}</b>\n  <code>{esc(item['query'])}</code>\n  Internal error — check logs",
                chat,
            )

    async def _run_download(
        self,
        item: dict,
        url: str,
        display: str,
        skip_titles: set[str],
        cfg: dict,
        chat: int | None,
    ) -> DownloadResult | None:
        """Run the download with callbacks wired to the queue and a whole-job
        timeout plus a no-progress stall watchdog. Returns the result, or None
        when the job timed out / stalled (already marked failed — the leaked
        download thread keeps running in the background and pre-check picks up
        whatever it writes on a future run)."""

        stall = {"last": time.monotonic()}

        def progress_cb(done, total, title, provider=None):
            stall["last"] = time.monotonic()
            status = f"{done}/{total} · Now: {title}"
            if provider:
                status += f" · via {provider}"
            self._queue.set_progress(item["id"], status)

        def failure_cb(title, err):
            self._queue.log_failed_track(item["id"], title, err)

        timeout = cfg.get("max_download_timeout", MAX_DOWNLOAD_TIMEOUT)
        stall_timeout = cfg.get("stall_timeout", STALL_TIMEOUT)
        task = asyncio.create_task(asyncio.to_thread(
            _run_url_sync, url, cfg, self._logger, skip_titles, progress_cb, failure_cb,
        ))
        try:
            while True:
                wait = min(timeout, stall_timeout)
                try:
                    return await asyncio.wait_for(asyncio.shield(task), timeout=wait)
                except asyncio.TimeoutError:
                    if time.monotonic() - stall["last"] >= stall_timeout:
                        await self._handle_timeout(item, display, chat, reason="stall")
                        return None
                    timeout -= wait
                    if timeout <= 0:
                        await self._handle_timeout(item, display, chat, reason="timeout")
                        return None
        except asyncio.CancelledError:
            raise

    async def _handle_no_failures(self, item: dict, display: str, result: DownloadResult, chat: int | None):
        await self._mark_done_and_notify(
            item, display, result, len(result.gave_up_tracks), chat,
        )

    async def _handle_timeout(self, item: dict, display: str, chat: int | None, reason: str = "timeout"):
        """A whole-job timeout or stall means the download thread is still
        running in the background (wait_for can't cancel a thread).  Mark the
        item failed so the queue advances to the next item; the leaked thread
        keeps downloading and pre-check will pick up whatever it writes on a
        future run.  Never requeues — a stuck item must not hog the queue."""
        if reason == "stall":
            detail = "stalled (no progress for a long time)"
        else:
            detail = "download timed out"
        await asyncio.to_thread(self._queue.mark_failed, item["id"], detail)
        await self._notify(
            f"❌ <b>{esc(display)}</b>\n  ⏰ {detail} — failed, next item in queue",
            chat,
        )

    async def _mark_done_and_notify(self, item: dict, display: str, result: DownloadResult, given_up: int, chat: int | None):
        await asyncio.to_thread(
            self._queue.mark_done,
            item["id"],
            result.ok,
            result.skipped,
            given_up,
        )
        await self._notify(_done_message(display, result, given_up), chat)
        await self._auto_build_m3u8(item, await self._item_cfg(item), chat)

    async def _resolve(self, item: dict, cfg: dict) -> tuple[str, str]:
        if item["input_type"] == "search":
            from SpotiFLAC import AsyncSpotiFLAC
            async with AsyncSpotiFLAC(output_dir=cfg["output_dir"]) as client:
                url, display, _kind = await resolve_search(client, item["query"])
            self._logger.info("Resolved #%d: %s → %s", item["id"], item["query"], display)
            return url, display
        return item["query"], item["query"]

    async def _auto_build_m3u8(self, item: dict, cfg: dict, chat: int | None):
        if item["input_type"] != "link":
            return
        parsed = parse_spotify_url(item["query"])
        if parsed.get("type") != "playlist":
            return
        try:
            result = await build_m3u8(item["query"], cfg=cfg)
            msg = f"📋 <b>Playlist: {esc(result['playlist_name'])}</b>\n  {result['exist_on_disk']}/{result['total_tracks']} tracks on disk"
            if result.get("cover_path"):
                msg += "\n  🖼️ Cover"
            await self._notify(msg, chat)
        except Exception as e:
            self._logger.warning("Auto m3u8 failed for %s: %s", item["query"], e)

    async def _handle_failure(self, item: dict, display: str, result: DownloadResult, chat: int | None):
        gave_up = await asyncio.to_thread(
            self._queue.get_give_up_titles, item["id"], MAX_TRACK_RETRIES,
        )
        decision = decide_failure(item, result, gave_up)
        retries = item.get("retries", 0)

        if decision.action == "fail":
            await asyncio.to_thread(self._queue.mark_failed, item["id"], decision.detail)
            await self._auto_build_m3u8(item, await self._item_cfg(item), chat)
            if decision.detail.startswith("Timed out"):
                msg = f"❌ <b>{esc(display)}</b>\n  ⏰ In queue over 24h — gave up"
            else:
                msg = f"❌ <b>{esc(display)}</b>\n  ❌ Failed after {MAX_QUEUE_RETRIES} retries"
            await self._notify(msg, chat)
            return

        if decision.action == "done":
            await self._mark_done_and_notify(item, display, result, decision.detail, chat)
            return

        delay = decision.detail
        await asyncio.to_thread(self._queue.requeue, item["id"], delay)
        when = f"retry in {delay}s" if delay > 0 else "retrying now"
        msg = (
            f"🔄 <b>{esc(display)}</b>\n  {_result_summary(result)}\n"
            f"  🔄 Re-queued (#{item['id']}, retry {retries + 1}/{MAX_QUEUE_RETRIES}, {when})"
        )
        await self._notify(msg, chat)

