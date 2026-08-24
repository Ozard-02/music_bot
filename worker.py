"""Background queue processor: spawns download_job.py subprocesses.

The parent never imports SpotiFLAC — each job runs in its own short-lived
subprocess whose RSS is reclaimed on exit. The worker streams the job's
JSON-lines (progress/failure/result), applies the stall watchdog via
`proc.kill()`, and updates the queue + notifies the user.
"""

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from config import (
    JOB_SCRIPT,
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
from queue_manager import QueueManager
from library import user_cfg


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


@dataclass(frozen=True)
class FailureDecision:
    """Outcome for a failed item: action + detail (error, count, or delay)."""

    action: str  # "fail" | "done" | "requeue"
    detail: object = None


def decide_failure(item: dict, result: dict, gave_up_titles: set[str], *, now: datetime | None = None) -> FailureDecision:
    """Pure decision logic for failed downloads (works on a DownloadResult dict
    from the subprocess's `result` event). Checks, in order:
    1. age > MAX_QUEUE_AGE            -> fail ("Timed out in queue")
    2. retries >= MAX_QUEUE_RETRIES    -> fail ("Max retries exceeded")
    3. no still-trying failures        -> done with partial result (gave-up count)
    4. otherwise                       -> requeue with exponential backoff delay
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if is_expired(item, now):
        reason = "Timed out in queue (>24h)" if item_age(item, now) > MAX_QUEUE_AGE else "Max retries exceeded"
        return FailureDecision("fail", reason)

    still_trying = [
        title for _tid, title, _err in result.get("failed_tracks", [])
        if title not in gave_up_titles
    ]
    if not still_trying:
        return FailureDecision("done", len(gave_up_titles))

    return FailureDecision("requeue", backoff_delay(item.get("retries", 0)))


def _result_summary(result: dict) -> str:
    parts = []
    if result.get("ok"):
        parts.append(f"✅ {result['ok']} ok")
    if result.get("skipped"):
        parts.append(f"⏭ {result['skipped']} skipped")
    if result.get("failed"):
        parts.append(f"❌ {result['failed']} failed")
    providers = result.get("providers") or {}
    if providers:
        prov = " · ".join(f"{k} {v}" for k, v in sorted(providers.items()))
        parts.append(f"🧪 {prov}")
    return " | ".join(parts)


def _done_message(display: str, result: dict, given_up: int) -> str:
    parts = [_result_summary(result)]
    if given_up:
        parts.append(f"❌ {given_up} given up")
    msg = f"✅ <b>{esc(display)}</b>\n  {' | '.join(parts)}"
    if given_up:
        msg += f"\n  🧊 Tracks gave up after {MAX_TRACK_RETRIES} attempts"
    return msg


async def stream_job(
    spec: dict,
    *,
    logger: logging.Logger,
    on_event,
    stall_timeout: float,
    deadline: float | None = None,
) -> tuple[dict | None, str | None]:
    """Spawn download_job.py, write `spec` to its stdin, stream its JSON-lines.

    Every event except `result` is passed to `on_event(event)`. The child is
    killed on stall (no line for `stall_timeout`), on `deadline` (monotonic
    timestamp), or when cancelled — killing it reclaims its RSS by
    construction.

    Returns (result, reason): reason is None iff a result event arrived;
    otherwise 'stall', 'timeout', or 'crash' (exit without a result).
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable, JOB_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    proc.stdin.write((json.dumps(spec) + "\n").encode())
    await proc.stdin.drain()
    proc.stdin.close()

    result = None
    reason = None
    last_event = time.monotonic()
    try:
        while result is None and reason is None:
            remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else stall_timeout
            wait = min(stall_timeout, remaining)
            if wait <= 0:
                reason = "timeout"
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=wait)
            except asyncio.TimeoutError:
                reason = "stall" if time.monotonic() - last_event >= stall_timeout else "timeout"
                break
            if not line:
                break  # EOF
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Bad line from job subprocess: %r", line[:200])
                continue
            last_event = time.monotonic()
            if event.get("event") == "result":
                result = event.get("result") or {}
            else:
                await on_event(event)
    except asyncio.CancelledError:
        proc.kill()
        raise
    finally:
        if reason is not None:
            proc.kill()
        if reason is not None or result is not None:
            # bounded reap; after SIGKILL this is instant unless the child is
            # stuck in uninterruptible I/O
            try:
                await asyncio.wait_for(proc.wait(), timeout=stall_timeout)
            except asyncio.TimeoutError:
                pass

    if result is None and reason is None:
        reason = "crash"
    return result, reason


class Worker:
    def __init__(
        self,
        queue: QueueManager,
        notify,
        chat_id: int,
        cfg: dict,
        logger: logging.Logger,
        wake_event: asyncio.Event,
        max_parallel: int = MAX_PARALLEL_JOBS,
    ):
        self._queue = queue
        self._notify = notify
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

    async def shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        self._wake_event.set()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

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
        return item.get("user") or None

    async def _safe_notify(self, text: str, chat: int | None) -> None:
        """Notify the item's owner (or default chat). A failed send must never
        affect the item's DB status or crash the worker — log and move on."""
        try:
            await self._notify(text, chat or self._chat_id)
        except Exception as e:
            self._logger.warning("Failed to send notification: %s", e)

    async def _process(self, item: dict):
        self._logger.info("Processing #%d: %s", item["id"], item["query"])
        chat = self._item_chat(item)
        try:
            cfg = await self._item_cfg(item)
            skip_titles = await asyncio.to_thread(
                self._queue.get_give_up_titles, item["id"], MAX_TRACK_RETRIES,
            )
            if skip_titles:
                self._logger.info(
                    "#%d: skipping %d given-up tracks",
                    item["id"], len(skip_titles),
                )

            outcome = await self._run_job(item, skip_titles, cfg, chat)
            if outcome is None:
                return  # timed out / stalled — handled by _run_job
            result, display = outcome
            if result.get("failed") == 0:
                await self._handle_no_failures(item, display, result, chat)
            else:
                await self._handle_failure(item, display, result, chat)

        except Exception as e:
            self._logger.exception("Failed #%d: %s", item["id"], e)
            await asyncio.to_thread(self._queue.mark_failed, item["id"], str(e))
            await self._safe_notify(
                f"❌ <b>Failed #{item['id']}</b>\n  <code>{esc(item['query'])}</code>\n  Internal error — check logs",
                chat,
            )

    async def _run_job(self, item: dict, skip_titles: set[str], cfg: dict, chat: int | None):
        """Run one download via stream_job (shared IPC loop) and translate its
        events into queue updates.

        Returns (result_dict, display) or None when killed/crashed. A killed
        child's RSS is reclaimed — no leaked straggler threads by construction."""
        display = item["query"]

        async def on_event(event: dict):
            nonlocal display
            kind = event.get("event")
            if kind == "progress":
                status = f"{event.get('done', 0)}/{event.get('total', 0)} · Now: {event.get('title', '')}"
                if event.get("provider"):
                    status += f" · via {event['provider']}"
                await asyncio.to_thread(self._queue.set_progress, item["id"], status)
            elif kind == "failure":
                await asyncio.to_thread(
                    self._queue.log_failed_track, item["id"], event.get("title", ""), event.get("error"),
                )
            elif kind == "resolved":
                display = event.get("display") or display

        spec = {
            "id": item["id"],
            "type": item["input_type"],
            "cfg": cfg,
            "skip_titles": sorted(skip_titles),
            "want_m3u8": item["input_type"] == "link",
            "log_path": self._logger.handlers[0].baseFilename if self._logger.handlers else None,
        }
        if item["input_type"] == "search":
            spec["query"] = item["query"]
        else:
            spec["url"] = item["query"]

        result, reason = await stream_job(
            spec,
            logger=self._logger,
            on_event=on_event,
            stall_timeout=cfg.get("stall_timeout", STALL_TIMEOUT),
            deadline=time.monotonic() + cfg.get("max_download_timeout", MAX_DOWNLOAD_TIMEOUT),
        )

        if reason in ("stall", "timeout"):
            return await self._kill_timeout(item, display, chat, reason)
        if reason == "crash":
            await asyncio.to_thread(self._queue.mark_failed, item["id"], "subprocess crashed")
            await self._safe_notify(
                f"❌ <b>{esc(display)}</b>\n  💥 Download subprocess crashed — will retry",
                chat,
            )
            return None
        return result, display

    async def _kill_timeout(self, item: dict, display: str, chat: int | None, reason: str):
        self._logger.warning("#%d: %s — killing subprocess", item["id"], reason)
        detail = "stalled (no progress for a long time)" if reason == "stall" else "download timed out"
        await asyncio.to_thread(self._queue.mark_failed, item["id"], detail)
        await self._safe_notify(
            f"❌ <b>{esc(display)}</b>\n  ⏰ {detail} — failed, next item in queue",
            chat,
        )
        return None

    async def _handle_no_failures(self, item: dict, display: str, result: dict, chat: int | None):
        await self._mark_done_and_notify(item, display, result, len(result.get("gave_up_tracks", [])), chat)

    async def _mark_done_and_notify(self, item: dict, display: str, result: dict, given_up: int, chat: int | None):
        await asyncio.to_thread(
            self._queue.mark_done,
            item["id"],
            result.get("ok", 0),
            result.get("skipped", 0),
            given_up,
        )
        await self._safe_notify(_done_message(display, result, given_up), chat)

    async def _handle_failure(self, item: dict, display: str, result: dict, chat: int | None):
        gave_up = await asyncio.to_thread(
            self._queue.get_give_up_titles, item["id"], MAX_TRACK_RETRIES,
        )
        decision = decide_failure(item, result, gave_up)
        retries = item.get("retries", 0)

        if decision.action == "fail":
            await asyncio.to_thread(self._queue.mark_failed, item["id"], decision.detail)
            if decision.detail.startswith("Timed out"):
                msg = f"❌ <b>{esc(display)}</b>\n  ⏰ In queue over 24h — gave up"
            else:
                msg = f"❌ <b>{esc(display)}</b>\n  ❌ Failed after {MAX_QUEUE_RETRIES} retries"
            await self._safe_notify(msg, chat)
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
        await self._safe_notify(msg, chat)