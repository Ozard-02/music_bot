#!/usr/bin/env python3
"""Telegram bot for queueing Spotify downloads (stdlib-only parent)."""

import asyncio
import functools
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import JOB_SCRIPT, STALL_TIMEOUT, setup_logger, load_config, esc
from library import QUALITY_CHOICES, user_cfg, user_folder_name
from queue_manager import QueueManager
from resolver import parse_input, format_help
from telegram_client import TelegramClient
from worker import Worker


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

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


def _parse_allowed_user_ids() -> set[int]:
    """Parse the allowlist: comma-separated TELEGRAM_ALLOWED_USER_IDS, with
    the legacy single TELEGRAM_ALLOWED_USER_ID as a fallback."""
    ids = set()
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS")
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
    if os.environ.get("TELEGRAM_ALLOWED_USER_ID"):
        try:
            ids.add(int(os.environ["TELEGRAM_ALLOWED_USER_ID"]))
        except ValueError:
            pass
    return ids


ALLOWED_USER_IDS = _parse_allowed_user_ids()

QUEUE_DB = Path(os.environ.get("QUEUE_DB_PATH", str(Path(__file__).parent / "queue.db")))


class SingleInstanceLock:
    """flock-based single-instance lock with standby + takeover."""

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


def _user_folder(user: dict) -> str:
    return user_folder_name(user.get("username"), fallback=user.get("first_name") or "user")


def _get_or_create_user(qm: QueueManager, user: dict, default_quality: str) -> dict:
    row = qm.get_user(user["id"])
    if row:
        return row
    qm.upsert_user(user["id"], user.get("username"), _user_folder(user), default_quality)
    return qm.get_user(user["id"])


def _user_cfg(qm: QueueManager, cfg: dict, user: dict) -> dict:
    row = _get_or_create_user(qm, user, cfg["quality"])
    return user_cfg(cfg, row["folder"])


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class Bot:
    def __init__(self, token: str, qm: QueueManager, cfg: dict, logger: logging.Logger):
        self._client = TelegramClient(token, logger)
        self._qm = qm
        self._cfg = cfg
        self._logger = logger
        self._wake_event = asyncio.Event()
        self._worker: Worker | None = None
        self._offset: int | None = None
        # Default notification chat: the first allowed user (single-owner use).
        self._chat_id = next(iter(sorted(ALLOWED_USER_IDS)), None)

    def _is_allowed(self, user: dict) -> bool:
        return bool(user) and user.get("id") in ALLOWED_USER_IDS

    async def _handle_command(self, chat_id: int, user: dict, command: str, args: str) -> None:
        if command == "start":
            await self._client.send_message(
                chat_id,
                "🎵 <b>SpotiLoop Bot</b>\n  Turns Spotify links into FLAC files.\n\n" + format_help(),
            )
        elif command == "help":
            await self._client.send_message(chat_id, format_help())
        elif command == "status":
            await self._status(chat_id)
        elif command == "quality":
            await self._quality(chat_id, user, args)
        elif command == "purge":
            count = await asyncio.to_thread(self._qm.purge_all)
            await self._client.send_message(
                chat_id, f"🗑️ <b>Purged {count} item{'s' if count != 1 else ''}</b>"
            )
        elif command == "mkplaylist":
            await self._mkplaylist(chat_id, user, args)
        elif command == "fixmetadata":
            await self._fixmetadata(chat_id, user, args)

    async def _status(self, chat_id: int) -> None:
        s = await asyncio.to_thread(self._qm.get_status)
        history = await asyncio.to_thread(self._qm.get_history, 5)
        running = await asyncio.to_thread(self._qm.get_running)
        user_names = {
            r["telegram_user_id"]: (r.get("username") or r["folder"])
            for r in await asyncio.to_thread(self._qm.get_users)
        }

        def _who(item) -> str:
            uid = item.get("user")
            name = user_names.get(uid) if uid else None
            return f" · {esc(name)}" if name else ""

        lines = [
            "📊 <b>Queue Status</b>",
            f"  ⏳ Queued: {s['queued']}",
            f"  🔄 Running: {s['running']}",
            f"  ✅ Done: {s['done']}",
            f"  ❌ Failed: {s['failed']}",
        ]

        if running:
            now = datetime.now(timezone.utc)
            lines.append("")
            lines.append("<b>Running:</b>")
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
                detail = r.get("progress") or "downloading…"
                lines.append(
                    f"  #{r['id']} 🔄 <b>{esc(r['query'][:60])}</b>{_who(r)}\n"
                    f"    {esc(detail)}{elapsed}"
                )

        done_ids = {r["id"] for r in running}
        if history:
            lines.append("")
            lines.append("<b>Recent:</b>")
            for h in history:
                if h["id"] in done_ids:
                    continue
                icon = {"done": "✅", "failed": "❌", "running": "🔄", "queued": "⏳"}.get(
                    h["status"], "❓"
                )
                label = esc(h["query"][:60]) + _who(h)
                lines.append(f"  #{h['id']} {icon} <b>{label}</b>")
        await self._client.send_message(chat_id, "\n".join(lines))

    async def _quality(self, chat_id: int, user: dict, args: str) -> None:
        row = _get_or_create_user(self._qm, user, self._cfg.get("quality", "LOSSLESS"))
        if not args:
            current = row.get("quality", "LOSSLESS")
            lines = [
                f"🎚️ <b>Quality</b> — current: <code>{esc(current)}</code>",
                "  Available:",
            ]
            lines += [f"  {'✅' if q == current else '•'} <code>{esc(q)}</code>" for q in QUALITY_CHOICES]
            lines.append("  Send /quality <value> to change.")
            await self._client.send_message(chat_id, "\n".join(lines))
            return

        value = args.strip().upper()
        if value not in QUALITY_CHOICES:
            await self._client.send_message(
                chat_id,
                f"❌ <b>Unknown quality</b> <code>{esc(value)}</code>\n\n"
                f"  Available: {', '.join(f'<code>{esc(q)}</code>' for q in QUALITY_CHOICES)}",
            )
            return

        await asyncio.to_thread(self._qm.set_user_quality, user["id"], value)
        await self._client.send_message(
            chat_id,
            f"✅ <b>Quality set to <code>{esc(value)}</code></b>\n  Applies to new downloads only.",
        )

    async def _run_command_job(self, chat_id: int, spec: dict) -> tuple[dict | None, int | None]:
        """Spawn download_job.py for a one-shot command (m3u8 / fix_metadata),
        stream its JSON-lines into a single progress-then-final Telegram message.

        Returns (result_dict, message_id) so the caller can edit the final
        message; (None, message_id) on timeout/crash/error — the message is
        already edited to show the failure."""
        header = spec.get("header", "⏳ Running…")
        msg = await self._client.send_message(chat_id, header)
        message_id = msg.get("message_id") if msg else None

        def _edit(text: str) -> None:
            if message_id:
                self._client.edit_message(chat_id, message_id, text)

        spec = {**spec, "log_path": self._logger.handlers[0].baseFilename if self._logger.handlers else None}
        result = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, JOB_SCRIPT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            proc.stdin.write((json.dumps(spec) + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()

            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=STALL_TIMEOUT)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    _edit("❌ <b>Command stalled — killed after no progress for a long time</b>")
                    return None, message_id
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("event")
                if kind == "progress":
                    p = event
                    pct = f" \u00b7 {p.get('done', 0) * 100 // max(1, p.get('total', 1))}%" if p.get("total") else ""
                    _edit(f"⏳ {header}\n  {p.get('done', 0)}/{p.get('total', 0)}{pct}\n  <code>{esc((p.get('title') or '')[:200])}</code>")
                elif kind == "result":
                    result = event.get("result") or {}
            await proc.wait()
        except Exception as e:
            self._logger.exception("Command job error: %s", e)
            _edit(f"❌ <b>Command error:</b> <code>{esc(e)}</code>")
            return None, message_id

        if result is None:
            _edit("❌ <b>Command failed — check logs</b>")
        return result, message_id

    async def _mkplaylist(self, chat_id: int, user: dict, args: str) -> None:
        parts = args.split()
        if not parts:
            await self._client.send_message(
                chat_id,
                "📋 <b>Usage:</b> /mkplaylist &lt;playlist_url&gt; [playlist_name]\n"
                "  Builds a .m3u8 from tracks already on disk (no downloads).",
            )
            return
        url = parts[0]
        name = " ".join(parts[1:]) if len(parts) > 1 else None

        ucfg = _user_cfg(self._qm, self._cfg, user)
        result, message_id = await self._run_command_job(chat_id, {
            "type": "m3u8", "url": url, "name": name, "cfg": ucfg,
            "header": "📋 <b>Building playlist\u2026</b>",
        })
        if result is None:
            return
        missing = result.get("missing_count", 0)
        lines = [
            f"✅ <b>Playlist: {esc(result['playlist_name'])}</b>",
            f"  {result['exist_on_disk']}/{result['total_tracks']} tracks on disk",
        ]
        if missing:
            lines.append(f"  ❌ {missing} missing \u2014 see <code>{esc(result['missing_log_path'])}</code>")
        if result.get("cover_path"):
            lines.append("  🖼️ Cover saved")
        lines.append(f"  📄 <code>{esc(result['path'])}</code>")
        if message_id:
            await self._client.edit_message(chat_id, message_id, "\n".join(lines))

    async def _fixmetadata(self, chat_id: int, user: dict, args: str) -> None:
        ucfg = _user_cfg(self._qm, self._cfg, user)
        root = Path(ucfg["output_dir"])

        lyrics = False
        parts = args.split()
        if parts and parts[0].lower() == "--lyrics":
            lyrics = True
            parts = parts[1:]

        if not parts:
            folder = root
        else:
            folder = Path(" ".join(parts)).expanduser()
            if not folder.is_absolute():
                folder = root / folder
            if not folder.is_dir():
                await self._client.send_message(
                    chat_id, f"❌ Not a folder: <code>{esc(folder)}</code>"
                )
                return

        mode = "fetch lyrics" if lyrics else "keep lyrics as-is"
        result, message_id = await self._run_command_job(chat_id, {
            "type": "fix_metadata", "folder": str(folder), "lyrics": lyrics, "cfg": ucfg,
            "header": f"🔧 <b>Fix metadata</b>\n  Scanning <code>{esc(folder)}</code>\u2026\n  🎤 {mode}",
        })
        if result is None:
            return
        lines = [
            "✅ <b>Fix metadata done</b>",
            f"  Folders: {result['folders']}",
            f"  Re-tagged: {result['fixed']}",
            f"  Moved: {result['moved']}",
            f"  Failed: {result['failed']}",
            f"  🎤 Lyrics: {'fetched' if lyrics else 'kept as-is'}",
        ]
        if result.get("failed_files"):
            lines.append("  ❌ <code>" + ", ".join(esc(f) for f in result["failed_files"]) + "</code>")
        if result.get("moved_files"):
            lines.append("  📦 Moved to their album folder:")
            for f in result["moved_files"]:
                lines.append(f"  <code>{esc(Path(f).name)} \u2192 {esc(Path(f).parent.name)}/</code>")
        if message_id:
            await self._client.edit_message(chat_id, message_id, "\n".join(lines))

    async def _handle_text(self, chat_id: int, user: dict, text: str) -> None:
        input_type, value = parse_input(text)
        if input_type == "invalid":
            await self._client.send_message(chat_id, "❓ Didn't understand that.\n\n" + format_help())
            return

        _get_or_create_user(self._qm, user, self._cfg.get("quality", "LOSSLESS"))
        item_id, is_new = await asyncio.to_thread(
            self._qm.enqueue_unique, input_type, value, user["id"],
        )
        if not is_new:
            await self._client.send_message(
                chat_id,
                f"⚠️ <b>Already queued as #{item_id}</b>\n  <code>{esc(value[:80])}</code>",
            )
            return

        self._wake_event.set()
        self._logger.info("Enqueued #%d: %s (%s)", item_id, value, input_type)

        s = await asyncio.to_thread(self._qm.get_status)
        pos = s["queued"] + s["running"]
        await self._client.send_message(
            chat_id,
            f"📥 <b>Queued #{item_id}</b>\n  Position: {pos}\n  <code>{esc(value[:80])}</code>",
        )

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not message:
            return
        chat_id = message["chat"]["id"]
        user = message.get("from") or {}
        if not self._is_allowed(user):
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            parts = text[1:].split(maxsplit=1)
            command = parts[0].split("@")[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            await self._handle_command(chat_id, user, command, args)
        else:
            await self._handle_text(chat_id, user, text)

    async def _notify(self, text: str, chat_id: int | None = None) -> None:
        await self._client.send_message(chat_id or self._chat_id, text)

    async def run(self):
        logger = self._logger
        worker = Worker(self._qm, self._notify, self._chat_id, self._cfg, logger, self._wake_event)
        self._worker = worker
        worker_task = asyncio.create_task(worker.run())
        logger.info("Bot starting...")
        try:
            while True:
                updates = await self._client.get_updates(self._offset)
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    try:
                        await self._handle_update(upd)
                    except Exception as e:
                        logger.exception("Update handling error: %s", e)
        finally:
            await worker.shutdown()
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass


def main() -> None:
    if not TOKEN:
        print("TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    if not ALLOWED_USER_IDS:
        print("TELEGRAM_ALLOWED_USER_IDS (or TELEGRAM_ALLOWED_USER_ID) not set")
        sys.exit(1)

    logger = setup_logger()
    cfg = load_config(logger)

    lock = SingleInstanceLock(QUEUE_DB.with_name(QUEUE_DB.name + ".lock"), logger)
    lock.acquire()

    qm = QueueManager(str(QUEUE_DB))
    try:
        asyncio.run(Bot(TOKEN, qm, cfg, logger).run())
    finally:
        lock.release()


if __name__ == "__main__":
    main()