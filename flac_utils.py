"""Shared FLAC tag/cover helpers used by the downloader and maintenance scripts."""

from __future__ import annotations

import io
import re
from pathlib import Path

import httpx
from mutagen.flac import FLAC, Picture

from track_utils import get_jpeg_dimensions

_IMAGE_SIMILAR_THRESHOLD = 0.005  # normalized mean-abs-diff: same art ~0.0001, different ~0.01+

_TRACK_ID_RE = re.compile(r"open\.spotify\.com/track/([a-zA-Z0-9]+)")

_SPOTIFY_COVER_UPGRADE = re.compile(r"(ab67616d0000)1e02")

# Matches an LRC timestamped line, e.g. [01:23.45] or [01:23:45]
_LRC_TS_RE = re.compile(r"\[\d{1,2}:\d{2}[.:]\d{2,3}\]")


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


def _images_similar(a: bytes, b: bytes, threshold: float = _IMAGE_SIMILAR_THRESHOLD) -> bool:
    """True if two JPEGs show the same artwork (perceptual similarity).

    Downscales both to a 32x32 RGB grid and compares normalized mean-abs-diff
    of channel values.  Same artwork at different resolutions/compression is
    ~0.0001; different artwork is ~0.01+.  Returns False on any decode error
    (fail-safe: a broken/foreign image never wins over the Spotify baseline).
    """
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        def _grid(data: bytes):
            return Image.open(io.BytesIO(data)).convert("RGB").resize((32, 32)).tobytes()
        ga, gb = _grid(a), _grid(b)
        if len(ga) != len(gb):
            return False
        total = sum(abs(x - y) for x, y in zip(ga, gb))
        return total / (len(ga) * 255) < threshold
    except Exception:
        return False


async def resolve_cover_data(track, timeout: float = 10) -> bytes | None:
    """Fetch the best available cover for `track`.

    The Spotify album cover (upgraded 640x640) is the always-correct baseline.
    Higher-resolution candidates from Apple and Deezer enrichment are only
    accepted when they are the *same artwork* as the baseline (perceptual
    similarity check) and at least as large — so a stray single-release image
    can never overwrite the album cover, but a genuine HD copy still upgrades
    quality.  Returns raw bytes, or None when no source works.
    """
    baseline = None
    if track.cover_url:
        baseline = await _fetch_bytes(upgrade_cover_url(track.cover_url), timeout)

    candidates = await _cover_candidates(track, timeout)
    if baseline is not None:
        base_w, base_h = get_jpeg_dimensions(baseline)
        base_area = base_w * base_h
        best = baseline
        best_area = base_area
        for data in candidates:
            w, h = get_jpeg_dimensions(data)
            if _images_similar(data, baseline) and (w or 0) * (h or 0) >= best_area:
                best, best_area = data, (w or 0) * (h or 0)
        return best
    return max(candidates, key=lambda d: (get_jpeg_dimensions(d)[0] or 0) * (get_jpeg_dimensions(d)[1] or 0), default=None)


async def _cover_candidates(track, timeout: float = 10) -> list[bytes]:
    """Apple then Deezer HD cover candidates; each independently best-effort."""
    candidates: list[bytes] = []
    from SpotiFLAC.core.metadata_enrichment import (
        _deezer_fetch_async,
        enrich_metadata_async,
    )

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
                candidates.append(data)
    except Exception:
        pass

    try:
        enriched = await _deezer_fetch_async(track.isrc or "")
        if enriched.cover_url_hd:
            data = await _fetch_bytes(upgrade_apple_cover(enriched.cover_url_hd), timeout)
            if data:
                candidates.append(data)
    except Exception:
        pass

    return candidates


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


def read_lrc(fpath: str | Path) -> str | None:
    """Return timestamped (LRC) lyrics from a file's LYRICS/UNSYNCEDLYRICS tag.

    SpotiFLAC embeds fetched lyrics verbatim into the FLAC `LYRICS` Vorbis
    comment — including any `[mm:ss.xx]` timestamps when the source was
    line-synced.  That text is valid LRC, but buried in a plain-text tag no
    player treats as "synced".  Reading it back here lets callers write a
    proper `.lrc` sidecar.  Returns None when there are no timestamps (plain
    lyrics carry no timing, so a sidecar would be pointless).
    """
    try:
        audio = FLAC(str(fpath))
    except Exception:
        return None
    for tag in ("LYRICS", "UNSYNCEDLYRICS"):
        val = audio.get(tag, [""])[0] if audio.get(tag) else ""
        if val and val.strip() and _LRC_TS_RE.search(val):
            return val.strip()
    return None


def write_lrc_sidecar(fpath: str | Path, lrc_text: str) -> None:
    """Write `lrc_text` as a `.lrc` sidecar next to `fpath`."""
    if not lrc_text or not lrc_text.strip():
        return
    sidecar = Path(fpath).with_suffix(".lrc")
    sidecar.write_text(lrc_text.strip() + "\n", encoding="utf-8")


def iter_flacs(root: str | Path):
    """Yield every FLAC file under `root` (case-insensitive, sorted)."""
    root = Path(root)
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.suffix.lower() == ".flac":
            yield f
