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

from config import STALL_TIMEOUT, setup_logger, load_config, esc
from library import QUALITY_CHOICES, user_cfg, user_folder_name
from queue_manager import QueueManager
from resolver import parse_input, format_help
from telegram_client import TelegramClient
from track_utils import sanitize
from worker import Worker, stream_job


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
        elif command == "rmplaylist":
            await self._rmplaylist(chat_id, user, args)

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
        """Run a one-shot command (m3u8 / fix_metadata) via stream_job —
        the same IPC loop downloads use — streaming progress into a single
        Telegram message.

        Returns (result_dict, message_id); (None, message_id) on
        stall/timeout/crash/error with the message already edited."""
        header = spec.get("header", "⏳ Running…")
        msg = await self._client.send_message(chat_id, header)
        message_id = msg.get("message_id") if msg else None

        def _edit(text: str) -> None:
            if message_id:
                self._client.edit_message(chat_id, message_id, text)

        spec = {**spec, "log_path": self._logger.handlers[0].baseFilename if self._logger.handlers else None}

        async def on_event(event: dict):
            if event.get("event") != "progress":
                return
            pct = f" \u00b7 {event.get('done', 0) * 100 // max(1, event.get('total', 1))}%" if event.get("total") else ""
            _edit(
                f"⏳ {header}\n  {event.get('done', 0)}/{event.get('total', 0)}{pct}\n"
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
            _edit(f"❌ <b>Command error:</b> <code>{esc(e)}</code>")
            return None, message_id

        if reason == "crash":
            _edit("❌ <b>Command failed — check logs</b>")
        elif reason is not None:
            _edit("❌ <b>Command stalled — killed after no progress for a long time</b>")
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
            file_name = sanitize(name, fallback="playlist")
            ucfg = _user_cfg(self._qm, self._cfg, user)
            root = Path(ucfg["output_dir"])
            m3u = root / f"{file_name}.m3u8"

            def _do_create() -> dict:
                if m3u.is_file():
                    return {"error": "exists", "name": name, "file_name": file_name, "path": str(m3u)}
                try:
                    root.mkdir(parents=True, exist_ok=True)
                    m3u.write_text("#EXTM3U\n", encoding="utf-8")
                except Exception as e:
                    return {"error": "write_error", "name": name, "detail": str(e)}
                return {"name": name, "file_name": file_name, "path": str(m3u)}

            result = await asyncio.to_thread(_do_create)
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

    async def _rmplaylist(self, chat_id: int, user: dict, args: str) -> None:
        """Delete every FLAC listed in a local .m3u8 (default Trash) and prune empty album/artist folders.

        Local-only: reads ``<output_dir>/<sanitize(name)>.m3u8`` (as created by
        /mkplaylist), deletes each listed ``.flac`` + ``.lrc`` sidecar, then
        prunes empty parents and removes the .m3u8 + cover + missing log.
        Navidrome picks it up on next scan – hence the trash-bin workflow.
        """
        name = args.strip() or "Trash"
        # keep display name as user typed; file name uses sanitize
        file_name = sanitize(name, fallback="playlist")
        ucfg = _user_cfg(self._qm, self._cfg, user)
        root = Path(ucfg["output_dir"])
        m3u = root / f"{file_name}.m3u8"

        def _do_delete() -> dict:
            if not m3u.is_file():
                avail = sorted(p.stem for p in root.glob("*.m3u8"))
                return {"error": "not_found", "name": name, "path": str(m3u), "avail": avail}
            try:
                text = m3u.read_text(encoding="utf-8")
            except Exception as e:
                return {"error": "read_error", "name": name, "detail": str(e)}
            rels = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
            deleted = 0
            not_found = 0
            pruned = 0
            # need root resolved for traversal guard
            try:
                root_res = root.resolve()
            except Exception:
                root_res = root
            for rel in rels:
                # guard absolute / traversal – m3u should be relative
                if rel.startswith("/") or rel.startswith("\\"):
                    not_found += 1
                    continue
                p = root / rel
                # ensure p is inside root (even when p doesn't exist yet, resolve parent)
                try:
                    # resolve() follows symlinks; for missing files it resolves as far as possible
                    p_res = p.resolve()
                    if not p_res.is_relative_to(root_res):
                        not_found += 1
                        continue
                except Exception:
                    not_found += 1
                    continue
                if not p.is_file():
                    not_found += 1
                    continue
                try:
                    p.unlink()
                    deleted += 1
                except Exception as e:
                    self._logger.warning("rmplaylist: failed to delete %s: %s", p, e)
                    not_found += 1
                    continue
                # sidecar .lrc
                try:
                    lrc = p.with_suffix(".lrc")
                    if lrc.is_file():
                        lrc.unlink()
                except Exception:
                    pass
                # prune empty album/artist dirs, count how many were removed
                # duplicate prune_empty_parents logic but count
                parent = p.parent
                while True:
                    try:
                        # need to be inside root and not root itself
                        if parent == root or parent == root_res or not parent.is_relative_to(root_res):
                            break
                    except Exception:
                        break
                    try:
                        parent.rmdir()
                        pruned += 1
                    except OSError:
                        break
                    parent = parent.parent
            # keep playlist but be sure it's empty – truncate to header only
            try:
                m3u.write_text("#EXTM3U\n", encoding="utf-8")
            except Exception:
                pass
            try:
                cover = m3u.with_suffix(".jpg")
                if cover.is_file():
                    cover.unlink()
            except Exception:
                pass
            try:
                missing_log = root / "temp" / f"{file_name}_missing.txt"
                if missing_log.is_file():
                    missing_log.unlink()
                    # prune temp if empty
                    try:
                        Path(root / "temp").rmdir()
                    except OSError:
                        pass
            except Exception:
                pass
            return {
                "name": name,
                "file_name": file_name,
                "total": len(rels),
                "deleted": deleted,
                "not_found": not_found,
                "pruned": pruned,
                "path": str(m3u),
            }

        result = await asyncio.to_thread(_do_delete)
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
    gc.freeze()  # startup objects are permanent — stop GC rescanning them
    try:
        asyncio.run(Bot(TOKEN, qm, cfg, logger).run())
    finally:
        lock.release()


if __name__ == "__main__":
    main()