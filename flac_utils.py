"""Shared FLAC tag/cover helpers used by the downloader and maintenance scripts."""

from __future__ import annotations

import re
from pathlib import Path

from mutagen.flac import FLAC, Picture

from track_utils import get_jpeg_dimensions

_TRACK_ID_RE = re.compile(r"open\.spotify\.com/track/([a-zA-Z0-9]+)")

_SPOTIFY_COVER_UPGRADE = re.compile(r"(ab67616d0000)1e02")


def get_spotify_id_from_file(fpath: str | Path) -> str | None:
    try:
        audio = FLAC(str(fpath))
    except Exception:
        return None
    for tag in ("URL", "comment"):
        val = audio.get(tag, [None])[0]
        if val:
            m = _TRACK_ID_RE.search(val)
            if m:
                return m.group(1)
    return None


def upgrade_cover_url(url: str) -> str:
    """Upgrade Spotify CDN URL from 300x300 (1e02) to 640x640 (b273)."""
    return _SPOTIFY_COVER_UPGRADE.sub(r"\g<1>b273", url)


def embed_cover(fpath: str | Path, data: bytes) -> None:
    """Replace the FLAC's pictures with `data` as a JPEG front cover."""
    audio = FLAC(str(fpath))
    pic = Picture()
    pic.data = data
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.width, pic.height = get_jpeg_dimensions(data)
    pic.depth = 0
    audio.clear_pictures()
    audio.add_picture(pic)
    audio.save()


def iter_flacs(root: str | Path):
    """Yield every FLAC file under `root` (case-insensitive, sorted)."""
    root = Path(root)
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.suffix.lower() == ".flac":
            yield f
