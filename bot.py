#!/usr/bin/env python3
"""Telegram bot for queueing Spotify downloads."""

import asyncio
import functools
import logging
import os
import sys
import time
from datetime import datetime, timezone
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

from config import setup_logger, load_config, bridge_community_session, esc
from m3u8 import build_m3u8
from queue_manager import QueueManager
from resolver import parse_input, format_help
from worker import Worker

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "0"))

QUEUE_DB = Path(os.environ.get("QUEUE_DB_PATH", str(Path(__file__).parent / "queue.db")))


class SingleInstanceLock:
    """flock-based single-instance lock with standby + takeover.

    The lock file sits next to the queue DB on the shared volume, so every
    instance (container or bare-metal) contends on the same file.  flock is
    advisory and released automatically when the holder's process exits, so
    there is never a stale lock.  A standby instance polls until the holder
    dies, then takes over.  Only the lock holder polls Telegram getUpdates,
    so two instances can never run the bot at the same time.
    """

    def __init__(self, lock_path: Path, logger: logging.Logger, poll: float = 30.0):
        self._lock_path = lock_path
        self._logger = logger
        self._poll = poll
        self._fd: int | None = None
        self._standby_log_every = 10
        self._attempt = 0

    def _try_lock(self) -> bool:
        import fcntl

        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def acquire(self) -> None:
        while not self._try_lock():
            self._attempt += 1
            if self._attempt == 1 or self._attempt % self._standby_log_every == 0:
                self._logger.warning(
                    "Another bot instance holds %s (attempt %d) — standing by",
                    self._lock_path, self._attempt,
                )
            time.sleep(self._poll)
        self._logger.info("Acquired single-instance lock %s", self._lock_path)

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # flock is released when the fd closes
            self._fd = None


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
async def start(update: Update, _context) -> None:
    await update.message.reply_html(
        "🎵 <b>SpotiLoop Bot</b>\n\n" + format_help()
    )


@require_auth
async def help_cmd(update: Update, _context) -> None:
    await update.message.reply_html(format_help())


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


@require_auth
async def status_cmd(update: Update, context) -> None:
    qm: QueueManager = context.application.bot_data["queue_manager"]
    s = await asyncio.to_thread(qm.get_status)
    history = await asyncio.to_thread(qm.get_history, 5)
    running = await asyncio.to_thread(qm.get_running)

    lines = [
        f"📊 <b>Queue Status</b>",
        f"  Queued: {s['queued']}",
        f"  Running: {s['running']}",
        f"  Done: {s['done']}",
        f"  Failed: {s['failed']}",
    ]

    if running:
        now = datetime.now(timezone.utc)
        lines.append("\n<b>Running:</b>")
        for r in running:
            started = r.get("started_at")
            elapsed = ""
            if started:
                try:
                    elapsed = " · " + _format_duration(
                        (now - datetime.fromisoformat(started)).total_seconds()
                    )
                except (ValueError, TypeError):
                    pass
            detail = r.get("progress") or "downloading"
            lines.append(
                f"  #{r['id']} 🔄 {esc(r['query'][:60])}\n"
                f"    {esc(detail)}{elapsed}"
            )

    done_ids = {r["id"] for r in running}
    if history:
        lines.append("\n<b>Recent:</b>")
        for h in history:
            if h["id"] in done_ids:
                continue
            icon = {"done": "✅", "failed": "❌", "running": "🔄", "queued": "⏳"}.get(
                h["status"], "❓"
            )
            label = esc(h["query"][:60])
            lines.append(f"  #{h['id']} {icon} {label}")
    await update.message.reply_html("\n".join(lines))


@require_auth
async def mkplaylist_cmd(update: Update, context) -> None:
    if not context.args:
        await update.message.reply_html(
            "Usage: /mkplaylist &lt;playlist_url&gt; [playlist_name]"
        )
        return
    url = context.args[0]
    name = " ".join(context.args[1:]) if len(context.args) > 1 else None

    msg = await update.message.reply_html("⏳ Scanning…")
    try:
        result = await build_m3u8(url, name)
        missing = result.get("missing_count", 0)
        parts = [f"✅ <b>Playlist: {esc(result['playlist_name'])}</b>",
                 f"  {result['exist_on_disk']}/{result['total_tracks']} tracks on disk"]
        if missing:
            parts.append(f"  ❌ {missing} missing — see <code>{esc(result['missing_log_path'])}</code>")
        if result.get("cover_path"):
            parts.append("  🖼️ Cover saved")
        parts.append(f"  <code>{esc(result['path'])}</code>")
        await msg.edit_text("\n".join(parts), parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


@require_auth
async def purge_cmd(update: Update, context) -> None:
    qm: QueueManager = context.application.bot_data["queue_manager"]
    count = await asyncio.to_thread(qm.purge_all)
    await update.message.reply_html(f"🗑️ <b>Purged {count} item{'s' if count != 1 else ''}</b>")


@require_auth
async def fixmetadata_cmd(update: Update, context) -> None:
    cfg = context.application.bot_data["cfg"]
    root = Path(cfg["output_dir"])

    if not context.args:
        folder = root
    else:
        folder = Path(" ".join(context.args)).expanduser()
        if not folder.is_absolute():
            folder = root / folder

        if not folder.is_dir():
            await update.message.reply_html(f"❌ Not a folder: <code>{esc(folder)}</code>")
            return

    from scripts.fix_metadata import fix_library

    msg = await update.message.reply_html(
        f"⏳ Fixing metadata in <code>{esc(folder)}</code>…"
    )

    async def progress(current, total, text):
        await msg.edit_text(
            f"⏳ <b>Fix metadata</b> {current}/{total}\n<code>{esc(text[:200])}</code>"
        )

    try:
        result = await fix_library(folder, apply=True, progress=progress)
        lines = [
            f"✅ <b>Fix metadata done</b>",
            f"  Folders: {result['folders']}",
            f"  Re-tagged: {result['fixed']}",
            f"  Moved: {result['moved']}",
            f"  Failed: {result['failed']}",
        ]
        if result["failed_files"]:
            lines.append("  ❌ <code>" + ", ".join(esc(f) for f in result["failed_files"]) + "</code>")
        if result["moved_files"]:
            lines.append("  📦 Moved to their album folder:")
            for f in result["moved_files"]:
                lines.append(f"  <code>{esc(Path(f).name)} → {esc(Path(f).parent.name)}/</code>")
        await msg.edit_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ Fix metadata error: {e}")


@require_auth
async def handle_message(update: Update, context) -> None:
    text = update.message.text.strip()
    qm: QueueManager = context.application.bot_data["queue_manager"]
    logger: logging.Logger = context.application.bot_data["logger"]

    input_type, value = parse_input(text)

    if input_type == "invalid":
        await update.message.reply_html(
            "❓ Didn't understand that.\n\n" + format_help()
        )
        return

    item_id, is_new = await asyncio.to_thread(
        qm.enqueue_unique, input_type, value,
    )
    if not is_new:
        await update.message.reply_html(
            f"⚠️ <b>Already queued as #{item_id}</b>\n"
            f"  <code>{esc(value[:80])}</code>"
        )
        return

    context.application.bot_data["wake_event"].set()
    logger.info("Enqueued #%d: %s (%s)", item_id, value, input_type)

    s = await asyncio.to_thread(qm.get_status)
    pos = s["queued"] + s["running"]
    await update.message.reply_html(
        f"📥 <b>Queued #{item_id}</b>\n"
        f"  Position: {pos}\n"
        f"  <code>{esc(value[:80])}</code>"
    )


def _cleanup_part_files(output: Path, logger: logging.Logger) -> None:
    """Remove leftover .part files from interrupted downloads."""
    if not output.exists():
        return
    cleaned = 0
    for p in output.rglob("*enc.part"):
        try:
            p.unlink()
            cleaned += 1
        except OSError:
            pass
    if cleaned:
        logger.info("Cleaned up %d leftover .part file(s)", cleaned)


def _migrate_playlist_covers(output: Path, logger: logging.Logger) -> None:
    """Migrate .playlist_covers/ to sidecar (Navidrome-compatible) location."""
    covers_dir = output / ".playlist_covers"
    if not covers_dir.is_dir():
        return
    moved = 0
    for f in list(covers_dir.iterdir()):
        if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
            dest = output / f.name
            if not dest.exists():
                f.rename(dest)
                moved += 1
    if moved:
        logger.info("Migrated %d playlist cover(s) from .playlist_covers/", moved)
    try:
        covers_dir.rmdir()
    except OSError:
        pass


async def post_init(application: Application) -> None:
    qm: QueueManager = application.bot_data["queue_manager"]
    cfg = application.bot_data["cfg"]
    logger: logging.Logger = application.bot_data["logger"]
    wake_event: asyncio.Event = application.bot_data["wake_event"]

    output = Path(cfg["output_dir"])
    _cleanup_part_files(output, logger)
    _migrate_playlist_covers(output, logger)

    worker = Worker(qm, application.bot, ALLOWED_USER_ID, cfg, logger, wake_event)
    application.bot_data["worker"] = worker
    task = asyncio.create_task(worker.run())
    application.bot_data["worker_task"] = task


async def post_stop(application: Application) -> None:
    worker: Worker = application.bot_data.get("worker")
    if worker:
        await worker.shutdown()
    task = application.bot_data.get("worker_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main() -> None:
    if not TOKEN:
        print("TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    if not ALLOWED_USER_ID:
        print("TELEGRAM_ALLOWED_USER_ID not set")
        sys.exit(1)

    logger = setup_logger()
    bridge_community_session(logger)
    cfg = load_config(logger)

    lock = SingleInstanceLock(QUEUE_DB.with_name(QUEUE_DB.name + ".lock"), logger)
    lock.acquire()

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
    application.add_handler(CommandHandler("fixmetadata", fixmetadata_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    try:
        application.run_polling(bootstrap_retries=3)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
