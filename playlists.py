"""Local .m3u8 playlist helpers for the Telegram front end (stdlib only).

These functions only touch files under a user's output directory. They never
import SpotiFLAC, so the bot can call them without spawning a subprocess.
"""

from __future__ import annotations

import logging
from pathlib import Path

from track_utils import sanitize

log = logging.getLogger("spoty_loop.playlists")


def create_empty(root: str | Path, name: str) -> dict:
    """Create `<name>.m3u8` with only the header, or report why not."""
    root = Path(root).expanduser()
    file_name = sanitize(name, fallback="playlist")
    m3u = root / f"{file_name}.m3u8"
    if m3u.is_file():
        return {
            "error": "exists",
            "name": name,
            "file_name": file_name,
            "path": str(m3u),
        }
    try:
        root.mkdir(parents=True, exist_ok=True)
        m3u.write_text("#EXTM3U\n", encoding="utf-8")
    except OSError as exc:
        return {"error": "write_error", "name": name, "detail": str(exc)}
    return {"name": name, "file_name": file_name, "path": str(m3u)}


def empty_playlist(root: str | Path, name: str) -> dict:
    """Delete every FLAC listed in `<name>.m3u8` and truncate it to the header.

    The playlist file and its cover are kept; only the listed FLACs, their
    .lrc sidecars, the missing-log and empty album/artist folders go away.
    Paths are resolved and checked against `root`, so a hand-edited playlist
    can never delete outside the library.
    """
    root = Path(root).expanduser().resolve()
    file_name = sanitize(name, fallback="playlist")
    m3u = root / f"{file_name}.m3u8"
    if not m3u.is_file():
        available = sorted(p.stem for p in root.glob("*.m3u8"))
        return {
            "error": "not_found",
            "name": name,
            "path": str(m3u),
            "avail": available,
        }
    try:
        text = m3u.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": "read_error", "name": name, "detail": str(exc)}

    rels = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    deleted = not_found = pruned = 0
    for rel in rels:
        if rel.startswith(("/", "\\")):
            not_found += 1
            continue
        candidate = root / rel
        try:
            target = candidate.resolve()
        except OSError:
            not_found += 1
            continue
        if (
            not target.is_relative_to(root)
            or not candidate.is_file()
            or candidate.suffix.lower() != ".flac"
        ):
            not_found += 1
            continue
        try:
            candidate.unlink()
            deleted += 1
        except OSError as exc:
            log.warning("empty_playlist: failed to delete %s: %s", candidate, exc)
            not_found += 1
            continue
        try:
            sidecar = candidate.with_suffix(".lrc")
            if sidecar.is_file():
                sidecar.unlink()
        except OSError:
            pass
        pruned += _prune_empty_parents(candidate.parent, root)

    try:
        m3u.write_text("#EXTM3U\n", encoding="utf-8")
    except OSError:
        pass
    try:
        missing_log = root / "temp" / f"{file_name}_missing.txt"
        if missing_log.is_file():
            missing_log.unlink()
        temp = root / "temp"
        if temp.is_dir():
            temp.rmdir()
    except OSError:
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


def _prune_empty_parents(parent: Path, root: Path) -> int:
    pruned = 0
    while parent != root and parent.is_relative_to(root):
        try:
            parent.rmdir()
        except OSError:
            break
        pruned += 1
        parent = parent.parent
    return pruned
