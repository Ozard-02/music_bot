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
from functools import lru_cache
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

def _load_config() -> dict:
    path = os.path.expanduser("~/.spotiflac/config.json")
    cfg = {}
    try:
        with open(path) as f:
            cfg = json.load(f)
    except Exception:
        pass
    folder_template = cfg.get("folderTemplate", "{album_artist}/{album}")
    return {
        "output_dir": cfg.get("downloadPath", "/home/espo/Music"),
        "filename_format": cfg.get("filenameTemplate", "{artist} - {title}"),
        "use_artist_subfolders": "{album_artist}" in folder_template,
        "use_album_subfolders": "{album}" in folder_template,
        "first_artist_only": cfg.get("useFirstArtistOnly", True),
        "embed_lyrics": cfg.get("embedLyrics", True),
        "quality": cfg.get("tidalQuality", "LOSSLESS"),
    }


CFG = _load_config()

OUTPUT_DIR = CFG["output_dir"]
FILENAME_FORMAT = CFG["filename_format"]
USE_ARTIST_SUBFOLDERS = CFG["use_artist_subfolders"]
USE_ALBUM_SUBFOLDERS = CFG["use_album_subfolders"]
FIRST_ARTIST_ONLY = CFG["first_artist_only"]
EMBED_LYRICS = CFG["embed_lyrics"]
QUALITY = CFG["quality"]

SERVICES = ["qobuz", "tidal", "amazon"]
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


@lru_cache(maxsize=128)
def _scan_dir(path: Path) -> tuple[str, ...]:
    resolved = path.resolve()
    if resolved.is_dir():
        return tuple(f.stem.lower() for f in resolved.iterdir() if f.is_file())
    return ()


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
_progress_total: int = 0
_progress_done: int = 0
_last_heartbeat: float = time.time()
_in_progress: set[str] = set()
_in_progress_lock = asyncio.Lock()


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
    global _progress_done
    track_url = f"https://open.spotify.com/track/{track.id}"

    for attempt in range(1 + MAX_RETRIES):
        try:
            async with sem:
                if track_file_exists(track):
                    _stats["skipped"] += 1
                    _progress_done += 1
                    logger.info(
                        "[%d/%d] SKIP %s — already on disk",
                        _progress_done, _progress_total, track.title,
                    )
                    return
                async with _in_progress_lock:
                    if track.id in _in_progress:
                        _stats["skipped"] += 1
                        _progress_done += 1
                        logger.info(
                            "[%d/%d] SKIP %s — already downloading in another task",
                            _progress_done, _progress_total, track.title,
                        )
                        return
                    _in_progress.add(track.id)
                try:
                    await asyncio.wait_for(
                        client.download_track(track_url),
                        timeout=PER_TRACK_TIMEOUT,
                    )
                finally:
                    async with _in_progress_lock:
                        _in_progress.discard(track.id)
            _stats["ok"] += 1
            _progress_done += 1
            logger.info(
                "[%d/%d] OK %s",
                _progress_done, _progress_total, track.title,
            )
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
    _progress_done += 1
    logger.error(
        "[%d/%d] GAVE UP %s after %d attempts",
        _progress_done, _progress_total, track.title, 1 + MAX_RETRIES,
    )


async def download_playlist(client: AsyncSpotiFLAC, url: str):
    _stats.update(skipped=0, ok=0, failed=0)
    logger.info("Fetching playlist: %s", url)
    info, tracks = await client.get_playlist(url)
    logger.info(
        "Playlist '%s' — %d tracks", info.get("name", "?"), len(tracks)
    )

    seen_ids: set[str] = set()
    unique_tracks: list[TrackMetadata] = []
    dup_counts: dict[str, int] = {}
    for t in tracks:
        if t.id in seen_ids:
            dup_counts[t.id] = dup_counts.get(t.id, 1) + 1
        else:
            seen_ids.add(t.id)
            unique_tracks.append(t)
    if dup_counts:
        dup_titles = {
            t.title: c
            for t in tracks
            if (c := dup_counts.get(t.id))
        }
        logger.warning(
            "Dropped %d duplicate track IDs from playlist",
            len(tracks) - len(unique_tracks),
        )
        lines = [f"  {title} x{count}" for title, count in dup_titles.items()]
        joined = "\n".join(lines)
        logger.warning("Duplicates:\n%s", joined)
        dup_path = Path(__file__).parent / "duplicates.log"
        with open(dup_path, "w") as f:
            f.write(joined + "\n")
        logger.warning("Full list written to %s", dup_path)
    global _progress_total, _progress_done
    _progress_total = len(unique_tracks)
    _progress_done = 0
    logger.info("Unique tracks to process: %d", _progress_total)

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
