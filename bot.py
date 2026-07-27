#!/usr/bin/env python3
"""Telegram bot for queueing Spotify downloads."""

import asyncio
import functools
import logging
import os
import sys
from pathlib import Path


def _load_env(path: str | Path):
    """Simple .env loader — no dependency needed."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_env(Path(__file__).parent / ".env")

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from config import setup_logger, load_config, bridge_community_session
from m3u8 import build_m3u8
from queue_manager import QueueManager
from resolver import parse_input, format_help
from worker import Worker

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "0"))

QUEUE_DB = Path(__file__).parent / "queue.db"


def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    return user and user.id == ALLOWED_USER_ID


def require_auth(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context):
        if not _is_allowed(update):
            return
        return await func(update, context)
    return wrapper


@require_auth
async def start(update: Update, _context):
    await update.message.reply_html(
        "🎵 <b>SpotiLoop Bot</b>\n\n" + format_help()
    )


@require_auth
async def help_cmd(update: Update, _context):
    await update.message.reply_html(format_help())


@require_auth
async def status_cmd(update: Update, context):
    qm: QueueManager = context.application.bot_data["queue_manager"]
    s = await asyncio.to_thread(qm.get_status)
    history = await asyncio.to_thread(qm.get_history, 5)

    lines = [
        f"📊 <b>Queue Status</b>",
        f"  Queued: {s['queued']}",
        f"  Running: {s['running']}",
        f"  Done: {s['done']}",
        f"  Failed: {s['failed']}",
    ]
    if history:
        lines.append("\n<b>Recent:</b>")
        for h in history:
            icon = {"done": "✅", "failed": "❌", "running": "🔄", "queued": "⏳"}.get(
                h["status"], "❓"
            )
            label = h["query"][:60]
            lines.append(f"  #{h['id']} {icon} {label}")
    await update.message.reply_html("\n".join(lines))


@require_auth
async def mkplaylist_cmd(update: Update, context):
    if not context.args:
        await update.message.reply_html(
            "Usage: /mkplaylist &lt;playlist_url&gt; [playlist_name]"
        )
        return
    url = context.args[0]
    name = " ".join(context.args[1:]) if len(context.args) > 1 else None

    msg = await update.message.reply_html("⏳ Scanning…")
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: asyncio.run(build_m3u8(url, name)),
        )
        await msg.edit_text(
            f"✅ <b>{result['playlist_name']}</b>\n"
            f"{result['exist_on_disk']}/{result['total_tracks']} tracks on disk\n"
            f"<code>{result['path']}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


@require_auth
async def purge_cmd(update: Update, context):
    qm: QueueManager = context.application.bot_data["queue_manager"]
    count = await asyncio.to_thread(qm.purge_queued)
    await update.message.reply_html(f"🗑️ <b>Purged {count} queued item{'s' if count != 1 else ''}</b>")


@require_auth
async def handle_message(update: Update, context):
    text = update.message.text.strip()
    qm: QueueManager = context.application.bot_data["queue_manager"]
    logger: logging.Logger = context.application.bot_data["logger"]

    input_type, value = parse_input(text)

    if input_type == "invalid":
        await update.message.reply_html(
            "❓ Didn't understand that.\n\n" + format_help()
        )
        return

    existing = await asyncio.to_thread(qm.find_existing, input_type, value)
    if existing is not None:
        await update.message.reply_html(
            f"⚠️ <b>Already queued as #{existing}</b>\n"
            f"<code>{value[:80]}</code>"
        )
        return

    item_id = await asyncio.to_thread(qm.enqueue, input_type, value)
    context.application.bot_data["wake_event"].set()
    logger.info("Enqueued #%d: %s (%s)", item_id, value, input_type)

    s = await asyncio.to_thread(qm.get_status)
    pos = s["queued"] + s["running"]
    await update.message.reply_html(
        f"📥 <b>Queued #{item_id}</b>\n"
        f"Position: {pos}\n"
        f"<code>{value[:80]}</code>"
    )


async def post_init(application: Application):
    qm: QueueManager = application.bot_data["queue_manager"]
    cfg = application.bot_data["cfg"]
    logger: logging.Logger = application.bot_data["logger"]
    wake_event: asyncio.Event = application.bot_data["wake_event"]
    worker = Worker(qm, application.bot, ALLOWED_USER_ID, cfg, logger, wake_event)
    task = asyncio.create_task(worker.run())
    application.bot_data["worker_task"] = task


async def post_stop(application: Application):
    task = application.bot_data.get("worker_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main():
    if not TOKEN:
        print("TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    if not ALLOWED_USER_ID:
        print("TELEGRAM_ALLOWED_USER_ID not set")
        sys.exit(1)

    logger = setup_logger()
    bridge_community_session(logger)
    cfg = load_config(logger)

    qm = QueueManager(str(QUEUE_DB))

    wake_event = asyncio.Event()

    application = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(15)
        .read_timeout(30)
        .write_timeout(15)
        .pool_timeout(15)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    application.bot_data["queue_manager"] = qm
    application.bot_data["cfg"] = cfg
    application.bot_data["logger"] = logger
    application.bot_data["wake_event"] = wake_event

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("purge", purge_cmd))
    application.add_handler(CommandHandler("mkplaylist", mkplaylist_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    application.run_polling(bootstrap_retries=3)


if __name__ == "__main__":
    main()
