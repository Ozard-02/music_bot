"""Background queue processor."""

import asyncio
import logging
import traceback
from datetime import datetime, timezone
from telegram import Bot

from config import (
    MAX_PARALLEL_JOBS,
    MAX_QUEUE_RETRIES,
    MAX_DOWNLOAD_TIMEOUT,
    MAX_QUEUE_AGE,
    RETRY_BACKOFF_BASE,
    MAX_RETRY_BACKOFF,
)
from m3u8 import build_m3u8
from queue_manager import QueueManager
from resolver import resolve_search
from downloader import run_url
from SpotiFLAC.providers.spotify_metadata import parse_spotify_url


def _run_url_sync(url: str, cfg: dict, logger: logging.Logger) -> dict:
    async def _inner():
        return await run_url(url, cfg, logger)
    return asyncio.run(_inner())


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
        self._tasks.add(task)
        try:
            await self._process(item)
        finally:
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

    async def _process(self, item: dict):
        self._logger.info("Processing #%d: %s", item["id"], item["query"])
        try:
            url, display = await self._resolve(item)

            result = await asyncio.wait_for(
                asyncio.to_thread(_run_url_sync, url, self._cfg, self._logger),
                timeout=MAX_DOWNLOAD_TIMEOUT,
            )

            if result["failed"] > 0:
                for _track_id, title, err in result.get("failed_tracks", []):
                    await asyncio.to_thread(
                        self._queue.log_failed_track, item["id"], title, err,
                    )

            if result["failed"] == 0:
                await asyncio.to_thread(
                    self._queue.mark_done,
                    item["id"],
                    result["ok"],
                    result["skipped"],
                    0,
                )
                parts = []
                if result["ok"]:
                    parts.append(f"✅ {result['ok']} ok")
                if result["skipped"]:
                    parts.append(f"⏭ {result['skipped']} skipped")
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=f"<b>{display}</b>\n{' | '.join(parts)}",
                    parse_mode="HTML",
                )
                await self._auto_build_m3u8(item)
            else:
                await self._handle_failure(item, display, result)

        except Exception as e:
            self._logger.error("Failed #%d: %s\n%s", item["id"], e, traceback.format_exc())
            await asyncio.to_thread(self._queue.mark_failed, item["id"], str(e))
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=f"<b>Failed</b> #{item['id']}: {item['query']}\nInternal error — check logs",
                parse_mode="HTML",
            )

    async def _resolve(self, item: dict) -> tuple[str, str]:
        if item["input_type"] == "search":
            from SpotiFLAC import AsyncSpotiFLAC
            async with AsyncSpotiFLAC(output_dir=self._cfg["output_dir"]) as client:
                url, display, _kind = await resolve_search(client, item["query"])
            self._logger.info("Resolved #%d: %s → %s", item["id"], item["query"], display)
            return url, display
        return item["query"], item["query"]

    async def _auto_build_m3u8(self, item: dict):
        if item["input_type"] != "link":
            return
        parsed = parse_spotify_url(item["query"])
        if parsed.get("type") != "playlist":
            return
        try:
            result = await build_m3u8(item["query"], cfg=self._cfg)
            parts = [f"📋 <b>{result['playlist_name']}</b>",
                     f"{result['exist_on_disk']}/{result['total_tracks']} tracks on disk"]
            if result.get("cover_path"):
                parts.append("🖼️ Cover")
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=" | ".join(parts),
                parse_mode="HTML",
            )
        except Exception as e:
            self._logger.warning("Auto m3u8 failed for %s: %s", item["query"], e)

    async def _handle_failure(self, item: dict, display: str, result: dict):
        retries = item.get("retries", 0)
        try:
            created = datetime.fromisoformat(item["created_at"])
            age = (datetime.now(timezone.utc) - created).total_seconds()
        except (ValueError, TypeError, KeyError):
            age = 0

        if age > MAX_QUEUE_AGE:
            await asyncio.to_thread(self._queue.mark_failed, item["id"], "Timed out in queue (>24h)")
            await self._auto_build_m3u8(item)
            msg = f"<b>{display}</b>\n⏰ In queue over 24h — gave up"
            await self._bot.send_message(chat_id=self._chat_id, text=msg, parse_mode="HTML")
            return

        if retries >= MAX_QUEUE_RETRIES:
            await asyncio.to_thread(self._queue.mark_failed, item["id"], "Max retries exceeded")
            await self._auto_build_m3u8(item)
            msg = f"<b>{display}</b>\n❌ Failed after {MAX_QUEUE_RETRIES} retries"
            await self._bot.send_message(chat_id=self._chat_id, text=msg, parse_mode="HTML")
            return

        delay = min(MAX_RETRY_BACKOFF, RETRY_BACKOFF_BASE * (2 ** retries))
        await asyncio.to_thread(self._queue.requeue, item["id"], delay)
        parts = []
        if result["ok"]:
            parts.append(f"✅ {result['ok']} ok")
        if result["skipped"]:
            parts.append(f"⏭ {result['skipped']} skipped")
        if result["failed"]:
            parts.append(f"❌ {result['failed']} failed")
        when = f"retry in {delay}s" if delay > 0 else "retrying now"
        msg = f"<b>{display}</b>\n{' | '.join(parts)}\n🔄 Re-queued (#{item['id']}, retry {retries + 1}/{MAX_QUEUE_RETRIES}, {when})"
        await self._bot.send_message(chat_id=self._chat_id, text=msg, parse_mode="HTML")

