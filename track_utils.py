from __future__ import annotations

import re
from pathlib import Path

# SpotiFLAC folder sanitizer: `re.sub(r'[<>:"/\\|?*]', "_", ...)` with no
# whitespace normalization (SpotiFLAC/downloader.py `_track_output_dir_async`).
_SPOTIFLAC_FOLDER_RE = re.compile(r'[<>:"/\\|?*]')


def sanitize(text: str, fallback: str = "Unknown") -> str:
    if not text:
        return fallback
    cleaned = re.sub(r"/", "\u2215", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or fallback


def _make_relative_path(track: TrackMetadata, cfg: dict) -> str:
    _san = sanitize
    artist = _san(track.first_artist if cfg["first_artist_only"] else track.artists)
    album_artist = _san(track.album_artist)
    album = _san(track.album)
    title = _san(track.title)
    filename = cfg["filename_format"].format(artist=artist, title=title)
    return str(Path(album_artist) / album / f"{filename}.flac")


def track_relative_path(track: TrackMetadata, cfg: dict) -> str:
    return _make_relative_path(track, cfg)


def spotiflac_track_relative_path(track: TrackMetadata, cfg: dict) -> str:
    """The path SpotiFLAC really writes, mirroring its own code exactly.

    Folders: first_artist/album with `[<>:"/\\|?*]` replaced by `_` (no
    whitespace normalization).  Filename: SpotiFLAC's `build_filename()`
    (chars *removed*, whitespace collapsed).  Reusing the installed package's
    own functions keeps parity by construction.
    """
    from SpotiFLAC.core.models import build_filename

    parts: list[str] = []
    if cfg.get("use_artist_subfolders", True):
        parts.append(_SPOTIFLAC_FOLDER_RE.sub("_", track.first_artist or ""))
    if cfg.get("use_album_subfolders", True):
        parts.append(_SPOTIFLAC_FOLDER_RE.sub("_", track.album or ""))
    filename = build_filename(
        track,
        cfg["filename_format"],
        first_artist_only=cfg["first_artist_only"],
    )
    return str(Path(*parts) / filename) if parts else filename


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
        elif (Path(cfg["output_dir"]) / spotiflac_track_relative_path(t, cfg)).exists():
            # present under SpotiFLAC's sanitized naming (older downloads,
            # naming drift) — treat as existing, never re-download
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

