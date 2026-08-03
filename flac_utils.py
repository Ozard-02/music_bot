"""Shared FLAC tag/cover helpers used by the downloader and maintenance scripts."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from mutagen.flac import FLAC, Picture

from track_utils import get_jpeg_dimensions

_TRACK_ID_RE = re.compile(r"open\.spotify\.com/track/([a-zA-Z0-9]+)")

_SPOTIFY_COVER_UPGRADE = re.compile(r"(ab67616d0000)1e02")


def upgrade_apple_cover(url: str, size: str = "3000x3000") -> str:
    """Scale an iTunes artwork URL to the requested size (Apple serves up to 3000x3000)."""
    return url.replace("100x100", size)


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


async def fetch_cover(url: str, timeout: float = 10) -> bytes | None:
    """Fetch an (upgraded 640x640) Spotify cover image, or None on failure."""
    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.get(upgrade_cover_url(url))
        if resp.status_code != 200:
            return None
        return resp.content


async def _fetch_bytes(url: str, timeout: float = 10) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
            resp = await http.get(url)
            if resp.status_code != 200:
                return None
            return resp.content
    except Exception:
        return None


async def resolve_cover_data(track, timeout: float = 10) -> bytes | None:
    """Fetch the best available cover for `track`: Apple HD (3000x3000)
    via ISRC enrichment when it can, otherwise the upgraded Spotify 640x640
    cover. Returns raw bytes, or None when neither source works."""
    from SpotiFLAC.core.metadata_enrichment import enrich_metadata_async

    try:
        enriched = await enrich_metadata_async(
            track.title,
            track.first_artist,
            isrc=track.isrc or "",
            providers=["apple"],
        )
        if enriched.cover_url_hd:
            data = await _fetch_bytes(upgrade_apple_cover(enriched.cover_url_hd), timeout)
            if data:
                return data
    except Exception:
        pass

    if track.cover_url:
        return await _fetch_bytes(upgrade_cover_url(track.cover_url), timeout)
    return None


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
