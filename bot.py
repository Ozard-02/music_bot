#!/usr/bin/env python3
"""Telegram bot for queueing Spotify downloads (stdlib-only parent)."""

import asyncio
import gc
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import (
    STALL_TIMEOUT,
    allowed_user_ids,
    esc,
    file_log_path,
    load_config,
    load_env,
    setup_logger,
)
from library import QUALITY_CHOICES, user_cfg, user_folder_name
from playlists import create_empty, empty_playlist
from queue_manager import QueueManager
import spotify_credentials as spotify_creds
from resolver import parse_inputs, format_help
from telegram_client import TelegramClient
from worker import Worker, stream_job


load_env(Path(__file__).parent / ".env")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


ALLOWED_USER_IDS = allowed_user_ids()

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
    username = user.get("username")
    if username:
        return user_folder_name(username)
    # No username: include the user id, otherwise two "Andrea"s share a folder.
    name = f"{user.get('first_name') or 'user'}_{user['id']}"
    return user_folder_name(name)


def _get_or_create_user(qm: QueueManager, user: dict, default_quality: str) -> dict:
    row = qm.get_user(user["id"])
    if row:
        return row
    qm.upsert_user(user["id"], user.get("username"), _user_folder(user), default_quality)
    return qm.get_user(user["id"])


def _user_cfg(qm: QueueManager, cfg: dict, user: dict) -> dict:
    row = _get_or_create_user(qm, user, cfg["quality"])
    return user_cfg(cfg, row["folder"])


# Per-file lines in a command summary; beyond this only a count is shown
# (keeps the message under Telegram's 4096-character limit).
_MAX_LISTED_FILES = 15


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
        self._chat_id = min(ALLOWED_USER_IDS) if ALLOWED_USER_IDS else None

    def _is_allowed(self, user: dict) -> bool:
        return bool(user) and user.get("id") in ALLOWED_USER_IDS

    async def _handle_command(
        self, chat_id: int, user: dict, command: str, args: str, message_id: int | None = None
    ) -> None:
        if command == "start":
            await self._client.send_message(
                chat_id,
                "🎵 <b>SpotiLoop Bot</b>\n  Turns Spotify links into FLAC files.\n\n" + format_help(),
            )
        elif command == "help":
            await self._client.send_message(chat_id, format_help())
        elif command == "status":
            await self._status(chat_id, user)
        elif command == "quality":
            await self._quality(chat_id, user, args)
        elif command == "purge":
            count = await asyncio.to_thread(self._qm.purge_all, user["id"])
            await self._client.send_message(
                chat_id, f"🗑️ <b>Purged {count} item{'s' if count != 1 else ''}</b>"
            )
        elif command == "retry":
            await self._retry(chat_id, user, args)
        elif command == "mkplaylist":
            await self._mkplaylist(chat_id, user, args)
        elif command == "fixmetadata":
            await self._fixmetadata(chat_id, user, args)
        elif command == "synclyrics":
            await self._synclyrics(chat_id, user, args)
        elif command == "rmplaylist":
            await self._rmplaylist(chat_id, user, args)
        elif command == "spotify":
            await self._spotify(chat_id, args, message_id)

    async def _status(self, chat_id: int, user: dict) -> None:
        uid = user["id"]
        s = await asyncio.to_thread(self._qm.get_status, uid)
        history = await asyncio.to_thread(self._qm.get_history, 5, uid)
        running = await asyncio.to_thread(self._qm.get_running, uid)
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

        next_retry = await asyncio.to_thread(self._qm.get_next_retry_at, uid)
        if next_retry:
            wait = (next_retry - datetime.now(timezone.utc)).total_seconds()
            if wait > 0:
                lines.append(f"  ⏱ Next retry in: {_format_duration(wait)}")

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
                if h["status"] == "failed" and h.get("error"):
                    lines.append(f"    <code>{esc(h['error'][:160])}</code>")
                elif h["status"] == "done" and h.get("result_failed"):
                    lines.append(f"    🧊 {h['result_failed']} gave up")
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
            lines.append("  Send <code>/quality &lt;value&gt;</code> to change.")
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
            f"✅ <b>Quality set to</b> <code>{esc(value)}</code>\n  Applies to new downloads only.",
        )

    async def _spotify(
        self, chat_id: int, args: str, message_id: int | None = None
    ) -> None:
        parts = args.split()
        action = parts[0].lower() if parts else "status"

        if action in ("status", "show"):
            await self._client.send_message(chat_id, spotify_creds.describe_credentials())
            return

        if action in ("token", "access_token"):
            if len(parts) != 2:
                await self._client.send_message(
                    chat_id,
                    "🔑 <b>Usage:</b> /spotify token &lt;access_token&gt;\n"
                    "  Short-lived; for a durable setup use /spotify client.",
                )
                return
            token = parts[1].strip()
            if len(token) < 20:
                await self._client.send_message(chat_id, "❌ That does not look like a token.")
                return
            try:
                await asyncio.to_thread(spotify_creds.set_access_token, None, token)
            except (OSError, ValueError) as exc:
                self._logger.warning("Could not save Spotify token: %s", exc)
                await self._client.send_message(chat_id, "❌ Could not save the token.")
                return
            await self._client.delete_message(chat_id, message_id)
            await self._client.send_message(
                chat_id,
                "✅ <b>Spotify access token saved</b>\n"
                "  Searches use the official API; the command message was deleted.\n"
                "  It expires; /spotify clear falls back to anonymous.",
            )
            return

        if action == "client":
            if len(parts) != 3:
                await self._client.send_message(
                    chat_id,
                    "🔑 <b>Usage:</b> /spotify client &lt;client_id&gt; &lt;client_secret&gt;\n"
                    "  Create a free app at developer.spotify.com; no user login needed.",
                )
                return
            client_id, client_secret = parts[1].strip(), parts[2].strip()
            if len(client_id) < 10 or len(client_secret) < 10:
                await self._client.send_message(
                    chat_id, "❌ Client ID or secret looks too short."
                )
                return
            try:
                await asyncio.to_thread(
                    spotify_creds.set_client_credentials, None, client_id, client_secret
                )
            except (OSError, ValueError) as exc:
                self._logger.warning("Could not save Spotify client credentials: %s", exc)
                await self._client.send_message(chat_id, "❌ Could not save the credentials.")
                return
            await self._client.delete_message(chat_id, message_id)
            await self._client.send_message(
                chat_id,
                "✅ <b>Spotify client credentials saved</b>\n"
                "  The API refreshes the token automatically; the command message was deleted.\n"
                "  /spotify test checks them, /spotify clear falls back to anonymous.",
            )
            return

        if action == "market":
            if len(parts) != 2 or len(parts[1]) != 2 or not parts[1].isalpha():
                await self._client.send_message(
                    chat_id, "🔑 <b>Usage:</b> /spotify market &lt;IT&gt;"
                )
                return
            market = parts[1].upper()
            try:
                await asyncio.to_thread(spotify_creds.set_market, None, market)
            except (OSError, ValueError) as exc:
                self._logger.warning("Could not save Spotify market: %s", exc)
                await self._client.send_message(chat_id, "❌ Could not save the market.")
                return
            await self._client.send_message(
                chat_id, f"✅ <b>Spotify market set to</b> <code>{esc(market)}</code>"
            )
            return

        if action == "clear":
            try:
                await asyncio.to_thread(spotify_creds.clear_credentials, None)
            except spotify_creds.CredentialsError as exc:
                await self._client.send_message(chat_id, f"❌ {esc(exc)}")
                return
            await self._client.send_message(
                chat_id,
                "🧹 <b>Spotify credentials cleared</b>\n"
                "  Searches fall back to the anonymous web token (or .env, if configured).",
            )
            return

        if action == "test":
            await self._client.send_message(chat_id, "⏳ Testing Spotify credentials…")
            try:
                result = await asyncio.to_thread(spotify_creds.test_credentials, None)
            except spotify_creds.CredentialsError as exc:
                await self._client.send_message(chat_id, f"❌ <b>Spotify test failed</b>\n  {esc(exc)}")
                return
            await self._client.send_message(chat_id, f"✅ {esc(result)}")
            return

        await self._client.send_message(
            chat_id,
            "🔑 <b>Spotify credentials</b>\n"
            "  /spotify — status\n"
            "  /spotify token &lt;token&gt; — static access token\n"
            "  /spotify client &lt;id&gt; &lt;secret&gt; — client credentials\n"
            "  /spotify market &lt;IT&gt; — optional market\n"
            "  /spotify test — validate the official API\n"
            "  /spotify clear — anonymous fallback",
        )

    async def _retry(self, chat_id: int, user: dict, args: str) -> None:
        raw = args.strip().lstrip("#")
        if not raw.isdigit():
            await self._client.send_message(
                chat_id,
                "🔄 <b>Usage:</b> /retry &lt;item_id&gt;\n"
                "  Re-queues one failed item and resets its retry counter.",
            )
            return
        item_id = int(raw)
        ok = await asyncio.to_thread(self._qm.retry_item, item_id, user["id"])
        if not ok:
            await self._client.send_message(
                chat_id,
                f"❌ <b>Cannot retry #{item_id}</b>\n"
                "  Not found, not yours, or currently running.",
            )
            return
        self._wake_event.set()
        await self._client.send_message(
            chat_id, f"🔄 <b>Re-queued #{item_id}</b>\n  Retry counter reset."
        )

    async def _run_command_job(self, chat_id: int, spec: dict) -> tuple[dict | None, int | None]:
        """Run a one-shot command (m3u8 / fix_metadata / sync_lyrics) via
        stream_job — the same IPC loop downloads use — streaming progress into
        a single Telegram message.

        Returns (result_dict, message_id); (None, message_id) on
        stall/timeout/crash/error with the message already edited."""
        header = spec.get("header", "⏳ Running…")
        msg = await self._client.send_message(chat_id, header)
        message_id = msg.get("message_id") if msg else None

        # edit_message is a coroutine: it must be awaited, or no progress
        # update and no failure notice ever reaches the chat (the message
        # stayed on the "Running…" header forever).
        async def _edit(text: str) -> None:
            if message_id:
                await self._client.edit_message(chat_id, message_id, text)

        spec = {**spec, "log_path": file_log_path(self._logger)}

        last_edit = 0.0

        async def on_event(event: dict):
            nonlocal last_edit
            if event.get("event") != "progress":
                return
            now = time.monotonic()
            done = event.get("done", 0)
            total = event.get("total", 0)
            # Telegram rate-limits edits; one update every 2s is plenty.
            if now - last_edit < 2.0 and done < total:
                return
            last_edit = now
            pct = f" \u00b7 {done * 100 // max(1, total)}%" if total else ""
            await _edit(
                f"⏳ {header}\n  {done}/{total}{pct}\n"
                f"  <code>{esc((event.get('title') or '')[:200])}</code>"
            )

        try:
            result, reason = await stream_job(
                spec,
                logger=self._logger,
                on_event=on_event,
                stall_timeout=STALL_TIMEOUT,
            )
        except Exception as e:
            self._logger.exception("Command job error: %s", e)
            await _edit(f"❌ <b>Command error:</b> <code>{esc(e)}</code>")
            return None, message_id

        if reason == "crash":
            await _edit("❌ <b>Command failed — check logs</b>")
        elif reason is not None:
            await _edit("❌ <b>Command stalled — killed after no progress for a long time</b>")
        return result, message_id

    async def _mkplaylist(self, chat_id: int, user: dict, args: str) -> None:
        raw = args.strip()
        if not raw:
            await self._client.send_message(
                chat_id,
                "📋 <b>Usage:</b> /mkplaylist &lt;playlist_url&gt; [playlist_name]\n"
                "  /mkplaylist &lt;name&gt; — create empty <code>&lt;name&gt;.m3u8</code> (no url)\n"
                "  Builds a .m3u8 from tracks already on disk (no downloads).",
            )
            return
        # keep current bot state: url default name, plus empty name-only shortcut
        parts = raw.split()
        # treat as url if looks like spotify link (keep test short ids like /playlist/abc)
        first = parts[0]
        first_is_url = first.startswith("https://") or first.startswith("http://") or "open.spotify.com/" in first
        if not first_is_url:
            # name-only → create empty playlist (no Spotify lookup)
            name = raw  # preserve spaces: "My Playlist" not just parts[0]
            ucfg = _user_cfg(self._qm, self._cfg, user)
            root = Path(ucfg["output_dir"])
            result = await asyncio.to_thread(create_empty, root, name)
            if result.get("error") == "exists":
                await self._client.send_message(
                    chat_id,
                    f"⚠️ <b>Playlist already exists:</b> <code>{esc(result['name'])}</code>\n"
                    f"  <code>{esc(result['file_name'])}.m3u8</code>\n"
                    f"  Use /rmplaylist {esc(result['name'])} to delete it first.",
                )
                return
            if result.get("error") == "write_error":
                await self._client.send_message(
                    chat_id,
                    f"❌ <b>Failed to create playlist:</b> <code>{esc(result['name'])}</code> — <code>{esc(result.get('detail',''))}</code>",
                )
                return
            await self._client.send_message(
                chat_id,
                f"✅ <b>Empty playlist created: {esc(result['name'])}</b>\n"
                f"  0 tracks\n"
                f"  📄 <code>{esc(result['file_name'])}.m3u8</code>",
            )
            self._logger.info("mkplaylist empty %s -> %s", result["name"], result["path"])
            return

        # url mode: /mkplaylist <url> [name] — keep default Spotify name behaviour
        url = parts[0]
        name = " ".join(parts[1:]) if len(parts) > 1 else None

        ucfg = _user_cfg(self._qm, self._cfg, user)
        result, message_id = await self._run_command_job(chat_id, {
            "type": "m3u8", "url": url, "name": name, "cfg": ucfg,
            "header": "📋 <b>Building playlist\u2026</b>",
        })
        if result is None:
            return
        if result.get("error"):
            text = f"❌ <b>Playlist failed</b>\n  <code>{esc(result['error'])}</code>"
            if message_id:
                await self._client.edit_message(chat_id, message_id, text)
            else:
                await self._client.send_message(chat_id, text)
            return
        missing = result.get("missing_count", 0)
        lines = [
            f"✅ <b>Playlist: {esc(result.get('playlist_name', ''))}</b>",
            f"  {result.get('exist_on_disk', 0)}/{result.get('total_tracks', 0)} tracks on disk",
        ]
        if missing:
            lines.append(f"  ❌ {missing} missing \u2014 see <code>{esc(result.get('missing_log_path') or '')}</code>")
        if result.get("cover_path"):
            lines.append("  🖼️ Cover saved")
        lines.append(f"  📄 <code>{esc(result.get('path', ''))}</code>")
        if message_id:
            await self._client.edit_message(chat_id, message_id, "\n".join(lines))

    async def _fixmetadata(self, chat_id: int, user: dict, args: str) -> None:
        ucfg = _user_cfg(self._qm, self._cfg, user)
        root = Path(ucfg["output_dir"])

        lyrics = any(p.lower() == "--lyrics" for p in args.split())
        parts = [p for p in args.split() if p.lower() != "--lyrics"]

        if not parts:
            folder = root
        else:
            folder = Path(" ".join(parts)).expanduser()
            if not folder.is_absolute():
                folder = root / folder
            # fix_metadata re-tags AND moves files: keep it inside this
            # user's own library (no "../other_Music", no absolute paths
            # into another user's folder or the rest of the disk).
            if not folder.resolve().is_relative_to(root.resolve()):
                await self._client.send_message(
                    chat_id, f"❌ Folder must be inside your library: <code>{esc(root)}</code>"
                )
                return
            if not folder.is_dir():
                await self._client.send_message(
                    chat_id, f"❌ Not a folder: <code>{esc(folder)}</code>"
                )
                return

        mode = "fetch lyrics" if lyrics else "keep lyrics as-is"
        result, message_id = await self._run_command_job(chat_id, {
            "type": "fix_metadata", "folder": str(folder), "lyrics": lyrics,
            "providers": ucfg.get("lyrics_providers") or [], "cfg": ucfg,
            "header": f"🔧 <b>Fix metadata</b>\n  Scanning <code>{esc(folder)}</code>\u2026\n  🎤 {mode}",
        })
        if result is None:
            return
        if result.get("error"):
            text = f"❌ <b>Fix metadata failed</b>\n  <code>{esc(result['error'])}</code>"
            if message_id:
                await self._client.edit_message(chat_id, message_id, text)
            else:
                await self._client.send_message(chat_id, text)
            return
        lines = [
            "✅ <b>Fix metadata done</b>",
            f"  Folders: {result.get('folders', 0)}",
            f"  Re-tagged: {result.get('fixed', 0)}",
            f"  Moved: {result.get('moved', 0)}",
            f"  Failed: {result.get('failed', 0)}",
            f"  🎤 Lyrics: {'fetched' if lyrics else 'kept as-is'}",
        ]
        # Telegram rejects messages over 4096 chars: on a big library an
        # uncapped file list made the final summary edit fail silently.
        failed_files = result.get("failed_files") or []
        if failed_files:
            lines.append("  ❌ <code>" + ", ".join(esc(f) for f in failed_files[:_MAX_LISTED_FILES]) + "</code>")
            if len(failed_files) > _MAX_LISTED_FILES:
                lines.append(f"  … and {len(failed_files) - _MAX_LISTED_FILES} more failed (see logs)")
        moved_files = result.get("moved_files") or []
        if moved_files:
            lines.append("  📦 Moved to their album folder:")
            for f in moved_files[:_MAX_LISTED_FILES]:
                lines.append(f"  <code>{esc(Path(f).name)} \u2192 {esc(Path(f).parent.name)}/</code>")
            if len(moved_files) > _MAX_LISTED_FILES:
                lines.append(f"  … and {len(moved_files) - _MAX_LISTED_FILES} more moved")
        if message_id:
            await self._client.edit_message(chat_id, message_id, "\n".join(lines))

    async def _synclyrics(self, chat_id: int, user: dict, args: str) -> None:
        """Fetch/refresh lyrics only — never re-tags or moves files."""
        ucfg = _user_cfg(self._qm, self._cfg, user)
        root = Path(ucfg["output_dir"])
        force = any(p.lower() == "--force" for p in args.split())
        parts = [p for p in args.split() if p.lower() != "--force"]

        if not parts:
            folder = root
        else:
            folder = Path(" ".join(parts)).expanduser()
            if not folder.is_absolute():
                folder = root / folder
            if not folder.resolve().is_relative_to(root.resolve()):
                await self._client.send_message(
                    chat_id, f"❌ Folder must be inside your library: <code>{esc(root)}</code>"
                )
                return
            if not folder.is_dir():
                await self._client.send_message(
                    chat_id, f"❌ Not a folder: <code>{esc(folder)}</code>"
                )
                return

        mode = "re-fetch all" if force else "missing or unsynced"
        result, message_id = await self._run_command_job(chat_id, {
            "type": "sync_lyrics", "folder": str(folder), "force": force,
            "providers": ucfg.get("lyrics_providers") or [], "cfg": ucfg,
            "header": f"🎤 <b>Sync lyrics</b>\n  Scanning <code>{esc(folder)}</code>…\n  Mode: {mode}",
        })
        if result is None:
            return
        if result.get("error"):
            text = f"❌ <b>Sync lyrics failed</b>\n  <code>{esc(result['error'])}</code>"
            if message_id:
                await self._client.edit_message(chat_id, message_id, text)
            else:
                await self._client.send_message(chat_id, text)
            return
        lines = [
            "✅ <b>Lyrics sync done</b>",
            f"  Files: {result.get('total', 0)}",
            f"  Fetched: {result.get('fixed', 0)}",
            f"  .lrc sidecars: {result.get('sidecars', 0)}",
            f"  Kept: {result.get('kept', 0)}",
            f"  No lyrics found: {result.get('no_lyrics', 0)}",
            f"  Skipped: {result.get('skipped', 0)}",
            f"  Failed: {result.get('failed', 0)}",
        ]
        failed_files = result.get("failed_files") or []
        if failed_files:
            lines.append("  ❌ <code>" + ", ".join(esc(f) for f in failed_files[:_MAX_LISTED_FILES]) + "</code>")
            if len(failed_files) > _MAX_LISTED_FILES:
                lines.append(f"  … and {len(failed_files) - _MAX_LISTED_FILES} more failed (see logs)")
        if message_id:
            await self._client.edit_message(chat_id, message_id, "\n".join(lines))

    async def _rmplaylist(self, chat_id: int, user: dict, args: str) -> None:
        """Delete every FLAC listed in a local .m3u8 (default Trash) and prune empty album/artist folders.

        Local-only: `playlists.empty_playlist()` reads
        ``<output_dir>/<sanitized name>.m3u8`` (as created by /mkplaylist),
        deletes each listed ``.flac`` + ``.lrc`` sidecar, prunes empty parents
        and removes the missing log. The .m3u8 is truncated to its header and
        kept, together with its cover. Navidrome picks the deletions up on the
        next scan — hence the trash-bin workflow.
        """
        name = args.strip() or "Trash"
        ucfg = _user_cfg(self._qm, self._cfg, user)
        root = Path(ucfg["output_dir"])
        result = await asyncio.to_thread(empty_playlist, root, name)
        if result.get("error") == "not_found":
            avail = result.get("avail") or []
            if avail:
                avail_str = ", ".join(f"<code>{esc(a)}</code>" for a in avail[:20])
                await self._client.send_message(
                    chat_id,
                    f"❌ <b>Playlist not found:</b> <code>{esc(result['name'])}</code>\n"
                    f"  Available: {avail_str}\n"
                    f"  Create it with /mkplaylist &lt;url&gt; {esc(result['name'])}",
                )
            else:
                await self._client.send_message(
                    chat_id,
                    f"❌ <b>Playlist not found:</b> <code>{esc(result['name'])}</code>\n"
                    f"  No .m3u8 playlists in <code>{esc(root)}</code>\n"
                    f"  Create one with /mkplaylist &lt;url&gt; {esc(result['name'])}",
                )
            return
        if result.get("error") == "read_error":
            await self._client.send_message(
                chat_id, f"❌ <b>Failed to read playlist:</b> <code>{esc(result['name'])}</code> — <code>{esc(result.get('detail',''))}</code>"
            )
            return
        # success – keep file, truncate to empty
        lines = [
            f"🗑️ <b>Emptied playlist: {esc(result['name'])}</b>",
            f"  🧹 {result['deleted']}/{result['total']} tracks deleted",
        ]
        if result["not_found"]:
            lines.append(f"  ⚠️ {result['not_found']} not on disk (already gone)")
        if result["pruned"]:
            lines.append(f"  📂 {result['pruned']} empty folder{'s' if result['pruned']!=1 else ''} pruned")
        lines.append(f"  📄 <code>{esc(result['file_name'])}.m3u8</code> kept empty")
        await self._client.send_message(chat_id, "\n".join(lines))
        self._logger.info(
            "rmplaylist %s: %d/%d deleted, %d not_found, %d pruned",
            result["name"], result["deleted"], result["total"], result["not_found"], result["pruned"],
        )

    async def _handle_text(self, chat_id: int, user: dict, text: str) -> None:
        entries = parse_inputs(text)
        if not entries:
            entries = [("invalid", text.strip())]
        valid = [(t, v) for t, v in entries if t != "invalid"]
        invalid = [v for t, v in entries if t == "invalid"]
        if not valid:
            await self._client.send_message(chat_id, "❓ Didn't understand that.\n\n" + format_help())
            return

        _get_or_create_user(self._qm, user, self._cfg.get("quality", "LOSSLESS"))

        queued: list[tuple[int, str]] = []
        dupes: list[tuple[int, str]] = []
        for input_type, value in valid:
            item_id, is_new = await asyncio.to_thread(
                self._qm.enqueue_unique, input_type, value, user["id"],
            )
            if is_new:
                queued.append((item_id, value))
                self._logger.info("Enqueued #%d: %s (%s)", item_id, value, input_type)
            else:
                dupes.append((item_id, value))

        if queued:
            self._wake_event.set()

        if len(entries) == 1:
            # Single entry: keep the personal per-item reply
            item_id, value = (queued + dupes)[0]
            if not queued:
                await self._client.send_message(
                    chat_id,
                    f"⚠️ <b>Already queued as #{item_id}</b>\n  <code>{esc(value[:80])}</code>",
                )
                return
            s = await asyncio.to_thread(self._qm.get_status, user["id"])
            pos = s["queued"] + s["running"]
            await self._client.send_message(
                chat_id,
                f"📥 <b>Queued #{item_id}</b>\n  Position: {pos}\n  <code>{esc(value[:80])}</code>",
            )
            return

        # A pasted list: one summary instead of a message storm.
        lines = [f"📥 <b>Queued {len(queued)} item{'s' if len(queued) != 1 else ''}</b>"]
        for item_id, value in queued[:_MAX_LISTED_FILES]:
            lines.append(f"  #{item_id} <code>{esc(value[:60])}</code>")
        if len(queued) > _MAX_LISTED_FILES:
            lines.append(f"  … and {len(queued) - _MAX_LISTED_FILES} more")
        if dupes:
            lines.append(
                f"  ⚠️ {len(dupes)} already queued (last: <code>{esc(dupes[-1][1][:60])}</code>)"
            )
        for value in invalid[:_MAX_LISTED_FILES]:
            lines.append(f"  ❓ Unrecognised: <code>{esc(value[:60])}</code>")
        if len(invalid) > _MAX_LISTED_FILES:
            lines.append(f"  … and {len(invalid) - _MAX_LISTED_FILES} more unrecognised")
        await self._client.send_message(chat_id, "\n".join(lines))

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
            await self._handle_command(
                chat_id, user, command, args, message.get("message_id")
            )
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
    logger.info("SpotiLoop build: %s", os.environ.get("BUILD_COMMIT", "dev"))
    cfg = load_config(logger)

    lock = SingleInstanceLock(QUEUE_DB.with_name(QUEUE_DB.name + ".lock"), logger)
    lock.acquire()

    qm = QueueManager(str(QUEUE_DB))
    qm.reset_running()  # the bot owns the queue; API restarts must not steal jobs
    qm.prune_history(30)  # API processes no longer prune the shared DB
    api_server = None
    if os.environ.get("RUN_API", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from api import start_api_thread

            api_server = start_api_thread(logger, queue=qm)
        except Exception:
            logger.exception("Could not start the HTTP API thread")

    bot = Bot(TOKEN, qm, cfg, logger)
    # Freeze after every startup import/object (including the API thread), so
    # GC never rescans permanent module/connection state.
    gc.freeze()
    try:
        asyncio.run(bot.run())
    finally:
        if api_server is not None:
            api_server.shutdown()
            api_server.server_close()
        lock.release()


if __name__ == "__main__":
    main()