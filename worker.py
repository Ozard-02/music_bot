"""Background queue processor."""

import asyncio
import logging
from datetime import datetime, timezone

from telegram import Bot

from config import MAX_QUEUE_RETRIES
from queue_manager import QueueManager
from resolver import resolve_search
from downloader import run_url_sync


MAX_QUEUE_AGE = 86400  # 24h — give up if item has been in queue this long


class Worker:
    def __init__(
        self,
        queue: QueueManager,
        bot: Bot,
        chat_id: int,
        cfg: dict,
        logger: logging.Logger,
        wake_event: asyncio.Event,
    ):
        self._queue = queue
        self._bot = bot
        self._chat_id = chat_id
        self._cfg = cfg
        self._logger = logger
        self._wake_event = wake_event
        self._poll = 5

    async def run(self):
        self._logger.info("Worker started")
        while True:
            try:
                item = await asyncio.to_thread(self._queue.dequeue)
                if item:
                    self._poll = 5
                    await self._process(item)
                else:
                    self._poll = min(300, self._poll * 2)
            except Exception as e:
                self._logger.error("Worker error: %s", e)
                self._poll = min(300, self._poll * 2)

            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=self._poll)
            except asyncio.TimeoutError:
                pass
            else:
                self._wake_event.clear()

    async def _process(self, item: dict):
        self._logger.info("Processing #%d: %s", item["id"], item["query"])
        try:
            url, display = await self._resolve(item)

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, run_url_sync, url, self._cfg, self._logger,
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
                    result["failed"],
                )
                summary = _format_summary(display, result)
                await self._bot.send_message(
                    chat_id=self._chat_id, text=summary, parse_mode="HTML",
                )
            else:
                await self._handle_failure(item, display, result)

        except Exception as e:
            self._logger.error("Failed #%d: %s", item["id"], e)
            await asyncio.to_thread(self._queue.mark_failed, item["id"], str(e))
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=f"<b>Failed</b> #{item['id']}: {item['query']}\n<code>{e}</code>",
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

    async def _handle_failure(self, item: dict, display: str, result: dict):
        retries = item.get("retries", 0)
        age = _age_seconds(item["created_at"])

        if age > MAX_QUEUE_AGE:
            await asyncio.to_thread(self._queue.mark_failed, item["id"], "Timed out in queue (>24h)")
            msg = f"<b>{display}</b>\n⏰ In queue over 24h — gave up"
            await self._bot.send_message(chat_id=self._chat_id, text=msg, parse_mode="HTML")
            return

        if retries >= MAX_QUEUE_RETRIES:
            await asyncio.to_thread(self._queue.mark_failed, item["id"], "Max retries exceeded")
            msg = f"<b>{display}</b>\n❌ Failed after {MAX_QUEUE_RETRIES} retries"
            await self._bot.send_message(chat_id=self._chat_id, text=msg, parse_mode="HTML")
            return

        await asyncio.to_thread(self._queue.requeue, item["id"])
        summary = _format_summary(display, result)
        msg = f"{summary}\n🔄 Re-queued (#{item['id']}, retry {retries + 1}/{MAX_QUEUE_RETRIES})"
        await self._bot.send_message(chat_id=self._chat_id, text=msg, parse_mode="HTML")


def _age_seconds(created_at: str) -> float:
    try:
        created = datetime.fromisoformat(created_at)
        now = datetime.now(timezone.utc)
        return (now - created).total_seconds()
    except (ValueError, TypeError):
        return 0


def _format_summary(display: str, result: dict) -> str:
    parts = []
    if result["ok"]:
        parts.append(f"✅ {result['ok']} ok")
    if result["skipped"]:
        parts.append(f"⏭ {result['skipped']} skipped")
    if result["failed"]:
        parts.append(f"❌ {result['failed']} failed")
    return f"<b>{display}</b>\n{' | '.join(parts)}"
