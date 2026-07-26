#!/usr/bin/env python3
"""
Resilient parallel playlist downloader using SpotiFLAC.
Matches ~/Music/{Artist}/{Album}/{Artist} - {title}.flac structure.
Never deletes existing files — only skips or adds new ones.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from SpotiFLAC import AsyncSpotiFLAC, TrackMetadata
from SpotiFLAC.core.health_check import run_health_check, get_working_providers
from SpotiFLAC.core.models import sanitize


def _bridge_community_session():
    """Copy desktop app's session to module's expected path if valid."""
    desktop = os.path.expanduser("~/.spotiflac/community_session.json")
    module_dir = os.path.expanduser("~/.spotiflac/signed_sessions")
    module = os.path.join(module_dir, "community_sessions.json")
    if not os.path.exists(desktop):
        return
    try:
        with open(desktop) as f:
            data = json.load(f)
        if data.get("session_id"):
            os.makedirs(module_dir, exist_ok=True)
            with open(module, "w") as f:
                json.dump(data, f, indent=2)
    except Exception:
        pass


_bridge_community_session()

OUTPUT_DIR = "/home/espo/Music"
SERVICES = ["qobuz", "tidal", "amazon"]
QUALITY = "LOSSLESS"
FILENAME_FORMAT = "{artist} - {title}"
USE_ARTIST_SUBFOLDERS = True
USE_ALBUM_SUBFOLDERS = True
FIRST_ARTIST_ONLY = True
EMBED_LYRICS = True
MAX_CONCURRENT = 3
CHECK_INTERVAL = 300
PER_TRACK_TIMEOUT = 180
MAX_RETRIES = 3

_UNSAFE_FOLDER_RE = re.compile(r'[<>:"/\\|?*]')

logger = logging.getLogger("spoty_loop")


def _safe_folder(name: str) -> str:
    return _UNSAFE_FOLDER_RE.sub("_", name.strip())


def _get_first_artist(artists: str) -> str:
    """Extract first artist, handling commas inside parentheses correctly."""
    result = []
    depth = 0
    for ch in artists:
        if ch == "(":
            depth += 1
            result.append(ch)
        elif ch == ")":
            depth -= 1
            result.append(ch)
        elif ch == "," and depth == 0:
            break
        else:
            result.append(ch)
    return "".join(result).strip()


_dir_cache: dict[str, tuple[float, list[str]]] = {}
_DIR_CACHE_TTL = 60

def _scan_dir(path: Path) -> list[str]:
    """Memoized directory listing with TTL — same dir is only scanned once per 60s."""
    key = str(path.resolve())
    now = time.time()
    cached = _dir_cache.get(key)
    if cached and (now - cached[0]) < _DIR_CACHE_TTL:
        return cached[1]
    if path.is_dir():
        stems = [f.stem.lower() for f in path.iterdir() if f.is_file()]
    else:
        stems = []
    _dir_cache[key] = (now, stems)
    return stems


def track_file_exists(track: TrackMetadata) -> bool:
    """Check if a track already exists on disk. Never returns a false negative."""
    title = sanitize(track.title)
    first_artist = _get_first_artist(track.artists) if FIRST_ARTIST_ONLY else track.artists
    artist_name = sanitize(first_artist)
    if not title or not artist_name:
        return False

    folder_artist = _safe_folder(first_artist)
    folder_album = _safe_folder(track.album)
    ext = ".flac"

    expected_name = f"{artist_name} - {title}{ext}"

    if USE_ARTIST_SUBFOLDERS and USE_ALBUM_SUBFOLDERS and folder_album:
        album_dir = Path(OUTPUT_DIR) / folder_artist / folder_album
        expected_path = album_dir / expected_name

        if expected_path.exists():
            return True

        title_lower = title.lower()
        for stem in _scan_dir(album_dir):
            if title_lower in stem:
                return True
    elif USE_ARTIST_SUBFOLDERS:
        artist_dir = Path(OUTPUT_DIR) / folder_artist
        expected_path = artist_dir / expected_name
        if expected_path.exists():
            return True
        title_lower = title.lower()
        for stem in _scan_dir(artist_dir):
            if title_lower in stem:
                return True
    else:
        expected_path = Path(OUTPUT_DIR) / expected_name
        if expected_path.exists():
            return True

    return False


async def wait_for_providers():
    logger.info("Health-checking providers every %ds...", CHECK_INTERVAL)
    while True:
        try:
            results = await run_health_check(SERVICES)
            working = get_working_providers(results)
            if working:
                logger.info("Providers UP: %s", ", ".join(working))
                return working
        except Exception as e:
            logger.warning("Health check error: %s", e)

        logger.info(
            "All providers down. Next check in %ds...", CHECK_INTERVAL
        )
        await _heartbeat_sleep(CHECK_INTERVAL, "Waiting for providers")


_stats: dict[str, int] = {"skipped": 0, "ok": 0, "failed": 0}
_last_heartbeat: float = time.time()


async def _heartbeat_sleep(seconds: int, msg: str = "Waiting..."):
    """Sleep for `seconds`, logging a heartbeat every 60s."""
    global _last_heartbeat
    for _ in range(seconds):
        await asyncio.sleep(1)
        now = time.time()
        if now - _last_heartbeat >= 60:
            logger.info("[heartbeat] %s (%ds left)", msg, seconds)
            _last_heartbeat = now
        seconds -= 1


async def download_track_with_retry(
    client: AsyncSpotiFLAC,
    track: TrackMetadata,
    sem: asyncio.Semaphore,
):
    if track_file_exists(track):
        _stats["skipped"] += 1
        logger.info("SKIP %s — already on disk", track.title)
        return

    track_url = f"https://open.spotify.com/track/{track.id}"

    for attempt in range(1 + MAX_RETRIES):
        try:
            async with sem:
                await asyncio.wait_for(
                    client.download_track(track_url),
                    timeout=PER_TRACK_TIMEOUT,
                )
            _stats["ok"] += 1
            logger.info("OK %s", track.title)
            return
        except asyncio.TimeoutError:
            logger.warning(
                "TIMEOUT %s (attempt %d/%d)",
                track.title,
                attempt + 1,
                1 + MAX_RETRIES,
            )
        except Exception as e:
            logger.warning(
                "FAIL %s (attempt %d/%d): %s",
                track.title,
                attempt + 1,
                1 + MAX_RETRIES,
                e,
            )
        await asyncio.sleep(min(30, 2**attempt))

    _stats["failed"] += 1
    logger.error("GAVE UP %s after %d attempts", track.title, 1 + MAX_RETRIES)


async def download_playlist(client: AsyncSpotiFLAC, url: str):
    _stats.update(skipped=0, ok=0, failed=0)
    logger.info("Fetching playlist: %s", url)
    info, tracks = await client.get_playlist(url)
    logger.info(
        "Playlist '%s' — %d tracks", info.get("name", "?"), len(tracks)
    )

    seen_ids: set[str] = set()
    unique_tracks: list[TrackMetadata] = []
    dups = 0
    for t in tracks:
        if t.id in seen_ids:
            dups += 1
        else:
            seen_ids.add(t.id)
            unique_tracks.append(t)
    if dups:
        logger.warning("Dropped %d duplicate track IDs from playlist", dups)
    logger.info("Unique tracks to process: %d", len(unique_tracks))

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    tasks = [download_track_with_retry(client, t, sem) for t in unique_tracks]
    await asyncio.gather(*tasks)

    s = _stats
    logger.info(
        "SUMMARY — %d skipped, %d downloaded, %d failed",
        s["skipped"], s["ok"], s["failed"],
    )


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else None
    if not url:
        print("Usage: python downloader.py <spotify_playlist_url>")
        sys.exit(1)

    log_path = Path(__file__).parent / "spoty_loop.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(),
        ],
    )
    logger.info("Logging to %s", log_path)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("SpotiFLAC"):
            logging.getLogger(name).setLevel(logging.WARNING)

    logger.info("SpotiLoop starting")
    logger.info("  Playlist: %s", url)
    logger.info("  Output:   %s", OUTPUT_DIR)
    logger.info("  Services: %s", SERVICES)
    logger.info("  Max parallel: %d", MAX_CONCURRENT)
    logger.info(
        "  Health check interval: %ds (%.1f min)",
        CHECK_INTERVAL,
        CHECK_INTERVAL / 60,
    )

    while True:
        try:
            logger.info("--- Stage: waiting for providers ---")
            working = await wait_for_providers()
            logger.info("--- Stage: downloading playlist ---")
            async with AsyncSpotiFLAC(
                output_dir=OUTPUT_DIR,
                services=working,
                quality=QUALITY,
                filename_format=FILENAME_FORMAT,
                use_artist_subfolders=USE_ARTIST_SUBFOLDERS,
                use_album_subfolders=USE_ALBUM_SUBFOLDERS,
                first_artist_only=FIRST_ARTIST_ONLY,
                embed_lyrics=EMBED_LYRICS,
            ) as client:
                await download_playlist(client, url)
            s = _stats
            if s["ok"] == 0:
                logger.info(
                    "All providers failed — going back to health check loop..."
                )
                await asyncio.sleep(5)
                continue
            logger.info("All tracks processed. Exiting.")
            return
        except Exception as e:
            logger.error(
                "Session crashed: %s. Restarting in 30s...", e, exc_info=True
            )
            logger.info("Restarting in 30s...")
            await _heartbeat_sleep(30, "Restarting after crash")


if __name__ == "__main__":
    asyncio.run(main())
