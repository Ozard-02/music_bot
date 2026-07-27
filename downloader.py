#!/usr/bin/env python3
"""
Resilient parallel downloader using SpotiFLAC.
~/Music/{Artist}/{Album}/{Artist} - {title}.flac
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field

from config import (
    SERVICES, MAX_CONCURRENT, PER_TRACK_TIMEOUT, PER_TRACK_RETRIES,
    MAX_RETRY_DURATION, CHECK_INTERVAL,
    load_config, setup_logger, bridge_community_session,
)


@dataclass
class RunState:
    skipped: int = 0
    ok: int = 0
    failed: int = 0
    total: int = 0
    done: int = 0
    in_progress: set = field(default_factory=set)
    failed_tracks: list = field(default_factory=list)


async def wait_for_providers(logger: logging.Logger) -> list[str]:
    while True:
        try:
            results = await run_health_check(SERVICES)
            working = get_working_providers(results)
            if working:
                return working
        except Exception as e:
            logger.warning("Health check error: %s", e)
        logger.info("All providers down. Next check in %ds...", CHECK_INTERVAL)
        await heartbeat_sleep(CHECK_INTERVAL, "Waiting for providers", logger)


async def heartbeat_sleep(seconds: int, msg: str, logger: logging.Logger):
    if seconds > 60:
        logger.info("[heartbeat] %s (%ds)", msg, seconds)
    await asyncio.sleep(seconds)


async def download_single_track(
    client: AsyncSpotiFLAC,
    url: str,
    state: RunState,
    logger: logging.Logger,
):
    state.total = 1
    try:
        track = await client.get_track_metadata(url)
    except Exception as e:
        logger.error("Failed to get track metadata: %s", e)
        state.failed += 1
        state.done += 1
        return

    sem = asyncio.Semaphore(1)
    await _download_track_with_retry(client, track, sem, state, logger)


async def download_collection(
    client: AsyncSpotiFLAC,
    url: str,
    state: RunState,
    logger: logging.Logger,
):
    logger.info("Fetching: %s", url)
    info, tracks = await client.get_playlist(url)
    logger.info("Collection '%s' — %d tracks", info.get("name", "?"), len(tracks))

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
        logger.warning("Dropped %d duplicate track IDs — %d unique remain",
                       len(tracks) - len(unique), len(unique))

    state.total = len(unique)
    logger.info("Unique tracks to process: %d", state.total)

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    tasks = [_download_track_with_retry(client, t, sem, state, logger) for t in unique]
    await asyncio.gather(*tasks)


async def _download_track_with_retry(
    client: AsyncSpotiFLAC,
    track: TrackMetadata,
    sem: asyncio.Semaphore,
    state: RunState,
    logger: logging.Logger,
):
    last_error: str | None = None
    for attempt in range(1 + PER_TRACK_RETRIES):
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
            last_error = "Timeout"
            logger.warning(
                "TIMEOUT %s (attempt %d/%d)",
                track.title, attempt + 1, 1 + PER_TRACK_RETRIES,
            )
        except Exception as e:
            last_error = str(e)
            logger.warning(
                "FAIL %s (attempt %d/%d): %s",
                track.title, attempt + 1, 1 + PER_TRACK_RETRIES, e,
            )
        await asyncio.sleep(min(30, 2**attempt))

    state.failed += 1
    state.done += 1
    state.failed_tracks.append((track.id, track.title, last_error or "Unknown"))
    logger.error("[%d/%d] GAVE UP %s", state.done, state.total, track.title)


async def _download_once(url: str, cfg: dict, logger: logging.Logger) -> dict:
    """One download pass — single attempt, no retry loop."""
    from SpotiFLAC import AsyncSpotiFLAC
    from SpotiFLAC.providers.spotify_metadata import parse_spotify_url

    state = RunState()
    async with AsyncSpotiFLAC(
        output_dir=cfg["output_dir"],
        services=SERVICES,
        quality=cfg["quality"],
        filename_format=cfg["filename_format"],
        use_artist_subfolders=cfg["use_artist_subfolders"],
        use_album_subfolders=cfg["use_album_subfolders"],
        first_artist_only=cfg["first_artist_only"],
        embed_lyrics=cfg["embed_lyrics"],
        enrich_providers=["deezer", "apple", "tidal", "soundcloud"],
    ) as client:
        parsed = parse_spotify_url(url)
        if parsed["type"] == "track":
            await download_single_track(client, url, state, logger)
        else:
            await download_collection(client, url, state, logger)
    logger.info(
        "PASS — %d skipped, %d ok, %d failed",
        state.skipped, state.ok, state.failed,
    )
    return {
        "ok": state.ok,
        "skipped": state.skipped,
        "failed": state.failed,
        "failed_tracks": state.failed_tracks,
    }


async def run_url(url: str, cfg: dict, logger: logging.Logger, single_pass: bool = False) -> dict:
    """Download a Spotify URL. Returns {ok, skipped, failed}.

    single_pass=True — one attempt, no retry. Use for queue processing.
    single_pass=False — full retry loop (24h cap). Use for standalone CLI.
    """
    if single_pass:
        return await _download_once(url, cfg, logger)

    deadline = time.monotonic() + MAX_RETRY_DURATION

    def _past() -> bool:
        return time.monotonic() >= deadline

    while True:
        try:
            working = await wait_for_providers(logger)
            result = await _download_once(url, cfg, logger)
            if result["failed"] == 0:
                return result
            if _past():
                logger.error(
                    "Max retry duration (%ds) exceeded. %d still failed.",
                    MAX_RETRY_DURATION, result["failed"],
                )
                return result
            if result["ok"] == 0:
                wait = min(300, deadline - time.monotonic())
                logger.warning("All %d failed. Waiting %ds...", result["failed"], wait)
                await heartbeat_sleep(max(1, wait), "Waiting before retry", logger)
            else:
                wait = min(60, deadline - time.monotonic())
                logger.warning("%d failed. Retrying in %ds...", result["failed"], wait)
                await heartbeat_sleep(max(1, wait), "Waiting before retry", logger)
        except Exception as e:
            logger.error("Session crashed: %s", e, exc_info=True)
            if _past():
                logger.error("Max retry duration exceeded after crash.")
                return {"ok": 0, "skipped": 0, "failed": 1}
            await heartbeat_sleep(min(30, deadline - time.monotonic()), "Restarting after crash", logger)


def run_url_sync(url: str, cfg: dict, logger: logging.Logger, single_pass: bool = False) -> dict:
    """Synchronous wrapper of run_url for use in thread pools."""
    return asyncio.run(run_url(url, cfg, logger, single_pass=single_pass))


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else None
    if not url:
        print("Usage: python downloader.py <spotify_url>")
        sys.exit(1)

    logger = setup_logger()
    bridge_community_session(logger)
    cfg = load_config(logger)

    logger.info("SpotiLoop starting")
    logger.info("  URL:     %s", url)
    logger.info("  Output:  %s", cfg["output_dir"])
    logger.info("  Services: %s", SERVICES)
    logger.info("  Parallel: %d", MAX_CONCURRENT)

    result = await run_url(url, cfg, logger)
    logger.info(
        "FINAL — %d ok, %d skipped, %d failed",
        result["ok"], result["skipped"], result["failed"],
    )
    sys.exit(1 if result["failed"] > 0 else 0)


if __name__ == "__main__":
    asyncio.run(main())
