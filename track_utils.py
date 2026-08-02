from __future__ import annotations

import re
from pathlib import Path

from SpotiFLAC import TrackMetadata

_RE_SPOTIFLAC = re.compile(r'[<>:"/\\|?*]')


def sanitize(text: str, fallback: str = "Unknown", *, spotiflac_mode: bool = False) -> str:
    if not text:
        return fallback
    if spotiflac_mode:
        cleaned = _RE_SPOTIFLAC.sub("_", text)
    else:
        cleaned = re.sub(r"/", "\u2215", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or fallback


def spotiflac_sanitize(text: str, fallback: str = "Unknown") -> str:
    return sanitize(text, fallback=fallback, spotiflac_mode=True)


def _make_relative_path(track: TrackMetadata, cfg: dict, *, spotiflac_mode: bool = False) -> str:
    _san = lambda s: sanitize(s, spotiflac_mode=spotiflac_mode)
    artist = _san(track.first_artist if cfg["first_artist_only"] else track.artists)
    album_artist = _san(track.album_artist)
    album = _san(track.album)
    title = _san(track.title)
    filename = cfg["filename_format"].format(artist=artist, title=title)
    return str(Path(album_artist) / album / f"{filename}.flac")


def track_relative_path(track: TrackMetadata, cfg: dict) -> str:
    return _make_relative_path(track, cfg, spotiflac_mode=False)


def spotiflac_track_relative_path(track: TrackMetadata, cfg: dict) -> str:
    return _make_relative_path(track, cfg, spotiflac_mode=True)


def partition_tracks(
    tracks: list[TrackMetadata],
    cfg: dict,
    skip_titles: set[str] | None = None,
) -> tuple[list[TrackMetadata], list[TrackMetadata], list[TrackMetadata]]:
    """Dedupe by `track.id`, then split into (existing, given_up, missing).

    `existing` are already on disk, `given_up` have a title in `skip_titles`
    (never downloaded again), everything else is `missing`. Order preserved.
    """
    seen: set[str] = set()
    unique: list[TrackMetadata] = []
    for t in tracks:
        if t.id not in seen:
            seen.add(t.id)
            unique.append(t)

    skip = skip_titles or set()
    existing: list[TrackMetadata] = []
    given_up: list[TrackMetadata] = []
    missing: list[TrackMetadata] = []
    for t in unique:
        full = Path(cfg["output_dir"]) / track_relative_path(t, cfg)
        if full.exists():
            existing.append(t)
        elif t.title in skip:
            given_up.append(t)
        else:
            missing.append(t)
    return existing, given_up, missing


def get_jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if data[:2] != b"\xff\xd8":
        return (0, 0)
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            if i + 10 > len(data):
                break
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return (w, h)
        if marker in (0xD9,):
            break
        seg_len = ((data[i + 2] << 8) | data[i + 3]) & 0xFFFF
        i += 2 + seg_len
    return (0, 0)


def prune_empty_parents(path: Path, root: Path) -> None:
    """Remove empty ancestor directories of `path` up to (but excluding) `root`."""
    parent = path.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent

