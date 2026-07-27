#!/usr/bin/env python3
"""
Resilient parallel playlist downloader using SpotiFLAC.
~/Music/{Artist}/{Album}/{Artist} - {title}.flac
"""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from SpotiFLAC import AsyncSpotiFLAC, TrackMetadata
from SpotiFLAC.core.health_check import run_health_check, get_working_providers


SERVICES = ["qobuz", "tidal", "amazon"]
MAX_CONCURRENT = 3
CHECK_INTERVAL = 300
PER_TRACK_TIMEOUT = 180
MAX_RETRIES = 3


@dataclass
class RunState:
    skipped: int = 0
    ok: int = 0
    failed: int = 0
    total: int = 0
    done: int = 0
    in_progress: set = field(default_factory=set)


def bridge_community_session(logger: logging.Logger):
    desktop = os.path.expanduser("~/.spotiflac/community_session.json")
    module_path = os.path.expanduser("~/.spotiflac/signed_sessions/community_sessions.json")
    try:
        with open(desktop) as f:
            data = json.load(f)
        if data.get("session_id"):
            os.makedirs(os.path.dirname(module_path), exist_ok=True)
            with open(module_path, "w") as f:
                json.dump(data, f, indent=2)
            logger.info("Community session bridged")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("No desktop session to bridge: %s", e)


DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.default.json"


def load_config(logger: logging.Logger) -> dict:
    path = os.path.expanduser("~/.spotiflac/config.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        logger.warning(
            "Config not found at %s — see %s for reference, falling back to defaults",
            path, DEFAULT_CONFIG_PATH,
        )
        cfg = {}
    except json.JSONDecodeError as e:
        logger.warning("Config parse error at %s: %s, using defaults", path, e)
        cfg = {}
    folder_template = cfg.get("folderTemplate", "{album_artist}/{album}")
    return {
        "output_dir": cfg.get("downloadPath", os.path.expanduser("~/Music")),
        "filename_format": cfg.get("filenameTemplate", "{artist} - {title}"),
        "use_artist_subfolders": "{album_artist}" in folder_template,
        "use_album_subfolders": "{album}" in folder_template,
        "first_artist_only": cfg.get("useFirstArtistOnly", True),
        "embed_lyrics": cfg.get("embedLyrics", True),
        "quality": cfg.get("tidalQuality", "LOSSLESS"),
    }


async def wait_for_providers(logger: logging.Logger) -> list[str]:
    while True:
        try:
            results = await run_health_check(SERVICES)
            working = get_working_providers(results)
            if working:
                logger.info("Providers UP: %s", ", ".join(working))
                return working
        except Exception as e:
            logger.warning("Health check error: %s", e)
        logger.info("All providers down. Next check in %ds...", CHECK_INTERVAL)
        await heartbeat_sleep(CHECK_INTERVAL, "Waiting for providers", logger)


async def heartbeat_sleep(seconds: int, msg: str, logger: logging.Logger):
    if seconds <= 60:
        await asyncio.sleep(seconds)
        return
    logger.info("[heartbeat] %s (%ds)", msg, seconds)
    while seconds > 0:
        chunk = min(60, seconds)
        await asyncio.sleep(chunk)
        seconds -= chunk
        if seconds > 0:
            logger.info("[heartbeat] %s (%ds left)", msg, seconds)


async def download_track_with_retry(
    client: AsyncSpotiFLAC,
    track: TrackMetadata,
    sem: asyncio.Semaphore,
    state: RunState,
    logger: logging.Logger,
):
    for attempt in range(1 + MAX_RETRIES):
        try:
            async with sem:
                if track.id in state.in_progress:
                    state.skipped += 1
                    state.done += 1
                    logger.info(
                        "[%d/%d] SKIP %s — already downloading",
                        state.done, state.total, track.title,
                    )
                    return
                state.in_progress.add(track.id)
                try:
                    url = f"https://open.spotify.com/track/{track.id}"
                    await asyncio.wait_for(
                        client.download_track(url),
                        timeout=PER_TRACK_TIMEOUT,
                    )
                finally:
                    state.in_progress.discard(track.id)
            state.ok += 1
            state.done += 1
            logger.info("[%d/%d] OK %s", state.done, state.total, track.title)
            return
        except asyncio.TimeoutError:
            logger.warning(
                "TIMEOUT %s (attempt %d/%d)",
                track.title, attempt + 1, 1 + MAX_RETRIES,
            )
        except Exception as e:
            logger.warning(
                "FAIL %s (attempt %d/%d): %s",
                track.title, attempt + 1, 1 + MAX_RETRIES, e,
            )
        await asyncio.sleep(min(30, 2**attempt))

    state.failed += 1
    state.done += 1
    logger.error("[%d/%d] GAVE UP %s", state.done, state.total, track.title)


async def download_playlist(
    client: AsyncSpotiFLAC,
    url: str,
    state: RunState,
    logger: logging.Logger,
):
    logger.info("Fetching playlist: %s", url)
    info, tracks = await client.get_playlist(url)
    logger.info("Playlist '%s' — %d tracks", info.get("name", "?"), len(tracks))

    seen_ids: set[str] = set()
    unique: list[TrackMetadata] = []
    dup_counts: dict[str, int] = {}
    for t in tracks:
        if t.id in seen_ids:
            dup_counts[t.id] = dup_counts.get(t.id, 1) + 1
        else:
            seen_ids.add(t.id)
            unique.append(t)
    if dup_counts:
        dup_titles = {t.title: c for t in tracks if (c := dup_counts.get(t.id))}
        logger.warning("Dropped %d duplicate track IDs", len(tracks) - len(unique))
        lines = [f"  {title} x{count}" for title, count in dup_titles.items()]
        logger.warning("Duplicates:\n%s", "\n".join(lines))
        dup_path = Path(__file__).parent / "duplicates.log"
        with open(dup_path, "w") as f:
            f.write("\n".join(lines) + "\n")
        logger.warning("Full list -> %s", dup_path)

    state.total = len(unique)
    logger.info("Unique tracks to process: %d", state.total)

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    tasks = [download_track_with_retry(client, t, sem, state, logger) for t in unique]
    await asyncio.gather(*tasks)

    logger.info(
        "SUMMARY — %d skipped, %d downloaded, %d failed",
        state.skipped, state.ok, state.failed,
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
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
    )
    logger = logging.getLogger("spoty_loop")
    logger.info("Logging to %s", log_path)

    bridge_community_session(logger)
    cfg = load_config(logger)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("SpotiFLAC"):
            logging.getLogger(name).setLevel(logging.WARNING)

    logger.info("SpotiLoop starting")
    logger.info("  Playlist: %s", url)
    logger.info("  Output:   %s", cfg["output_dir"])
    logger.info("  Services: %s", SERVICES)
    logger.info("  Max parallel: %d", MAX_CONCURRENT)

    while True:
        try:
            working = await wait_for_providers(logger)
            while True:
                state = RunState()
                async with AsyncSpotiFLAC(
                    output_dir=cfg["output_dir"],
                    services=working,
                    quality=cfg["quality"],
                    filename_format=cfg["filename_format"],
                    use_artist_subfolders=cfg["use_artist_subfolders"],
                    use_album_subfolders=cfg["use_album_subfolders"],
                    first_artist_only=cfg["first_artist_only"],
                    embed_lyrics=cfg["embed_lyrics"],
                    enrich_providers=["deezer", "apple", "tidal", "soundcloud"],
                ) as client:
                    await download_playlist(client, url, state, logger)

                if state.failed == 0:
                    logger.info("All tracks processed. Exiting.")
                    return
                if state.ok == 0:
                    logger.warning(
                        "All %d failed (server likely down). Waiting 5 min...",
                        state.failed,
                    )
                    await heartbeat_sleep(300, "Waiting before retry", logger)
                else:
                    logger.warning(
                        "%d failed. Retrying in 60s...", state.failed,
                    )
                    await heartbeat_sleep(60, "Waiting before retry", logger)
        except Exception as e:
            logger.error("Session crashed: %s", e, exc_info=True)
            await heartbeat_sleep(30, "Restarting after crash", logger)


if __name__ == "__main__":
    asyncio.run(main())
