#!/usr/bin/env python3
"""
Generate an M3U8 playlist file for a Spotify playlist.

Scans ~/Music for already-downloaded tracks and creates an .m3u8 file
with relative paths suitable for Navidrome / any music player.

Usage:
    python m3u8.py <playlist_url> [playlist_name]
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import httpx
from SpotiFLAC import TrackMetadata
from SpotiFLAC.client import SpotifyMetadataClient
from SpotiFLAC.providers.spotify_metadata import parse_spotify_url

from config import load_config
from track_utils import sanitize, track_relative_path


def build_m3u8_lines(tracks: list[TrackMetadata], cfg: dict) -> tuple[list[str], int, list[tuple[str, str, str]]]:
    lines = ["#EXTM3U"]
    count = 0
    seen_ids: set[str] = set()
    missing: list[tuple[str, str, str]] = []
    for t in tracks:
        if t.id in seen_ids:
            continue
        seen_ids.add(t.id)
        rel = track_relative_path(t, cfg)
        full = Path(cfg["output_dir"]) / rel
        if full.exists():
            count += 1
            lines.append(f"#EXTINF:{t.duration_seconds or 0:.0f},{t.first_artist} - {t.title}")
            lines.append(rel)
        else:
            missing.append((t.first_artist, t.title, rel))
    return lines, count, missing


def write_m3u8(name: str, lines: list[str], cfg: dict):
    out = Path(cfg["output_dir"]) / f"{sanitize(name, fallback='playlist')}.m3u8"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def write_missing_log(name: str, missing: list, cfg: dict) -> Path | None:
    if not missing:
        return None
    temp_dir = Path(cfg["output_dir"]) / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    out = temp_dir / f"{sanitize(name, fallback='playlist')}_missing.txt"
    lines = [f"Missing ({len(missing)}):"]
    for artist, title, rel in missing:
        lines.append(f"  \u2022 {artist} - {title} \u2192 {rel}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


async def _download_cover(url: str, path: Path) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        path.write_bytes(resp.content)


async def build_m3u8(url: str, name: str | None = None, cfg: dict | None = None):
    parsed = parse_spotify_url(url)
    if parsed["type"] != "playlist":
        raise ValueError(f"Not a playlist URL: {url}")

    if cfg is None:
        cfg = load_config(logging.getLogger("m3u8"))

    mc = SpotifyMetadataClient()
    collection_name, tracks, cover_url, info = await mc.get_url_async(url)

    playlist_name = name or info.get("name", collection_name)
    tracks = list(tracks)

    lines, included_count, missing = build_m3u8_lines(tracks, cfg)
    path = write_m3u8(playlist_name, lines, cfg)
    missing_log = write_missing_log(playlist_name, missing, cfg)

    cover_path = None
    if cover_url:
        cover_path = path.with_suffix(".jpg")
        try:
            await _download_cover(cover_url, cover_path)
        except Exception:
            logger = logging.getLogger("m3u8")
            logger.warning("Failed to download cover for %s", playlist_name)
            cover_path = None

    return {
        "path": str(path),
        "playlist_name": playlist_name,
        "total_tracks": included_count + len(missing),
        "exist_on_disk": included_count,
        "missing_count": len(missing),
        "missing_log_path": str(missing_log) if missing_log else None,
        "cover_path": str(cover_path) if cover_path else None,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate M3U8 for a Spotify playlist")
    parser.add_argument("url", help="Spotify playlist URL")
    parser.add_argument("name", nargs="?", default=None, help="Playlist name (default: Spotify name)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("m3u8")

    try:
        result = asyncio.run(build_m3u8(args.url, args.name))
        logger.info(
            "Wrote %s — %d/%d tracks on disk",
            result["path"], result["exist_on_disk"], result["total_tracks"],
        )
    except Exception as e:
        logger.error("Error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
