from __future__ import annotations

import re
from pathlib import Path

from SpotiFLAC import AsyncSpotiFLAC, TrackMetadata
from SpotiFLAC.providers.spotify_metadata import parse_spotify_url

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


async def fetch_tracks(url: str, cfg: dict) -> list[TrackMetadata]:
    parsed = parse_spotify_url(url)
    async with AsyncSpotiFLAC(output_dir=cfg["output_dir"]) as client:
        if parsed["type"] == "track":
            track = await client.get_track_metadata(url)
            return [track]
        _, tracks = await client.get_playlist(url)
    return list(tracks)


def dedup_tracks(tracks: list[TrackMetadata]) -> list[TrackMetadata]:
    seen: set[str] = set()
    result = []
    for t in tracks:
        if t.id not in seen:
            seen.add(t.id)
            result.append(t)
    return result


def classify_tracks(
    tracks: list[TrackMetadata], cfg: dict
) -> tuple[list[TrackMetadata], list[TrackMetadata]]:
    existing = []
    missing = []
    for t in tracks:
        rel = track_relative_path(t, cfg)
        if (Path(cfg["output_dir"]) / rel).exists():
            existing.append(t)
        else:
            missing.append(t)
    return existing, missing


def remove_empty_parents(path: str | Path, stop_at: str | Path):
    path = Path(path).parent
    stop_at = Path(stop_at)
    while path != stop_at:
        try:
            path.rmdir()
            path = path.parent
        except OSError:
            break
