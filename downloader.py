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

from config import (
    SERVICES, MAX_CONCURRENT, PER_TRACK_TIMEOUT, PER_TRACK_RETRIES,
    MAX_RETRY_DURATION, CHECK_INTERVAL,
    load_config, setup_logger, bridge_community_session,
)


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


async def _download_once(url: str, cfg: dict, logger: logging.Logger, services: list[str] | None = None) -> dict:
    """One download pass — single attempt, no retry loop."""
    if services is None:
        services = SERVICES
    from SpotiFLAC import AsyncSpotiFLAC
    from SpotiFLAC.providers.spotify_metadata import parse_spotify_url

    async with AsyncSpotiFLAC(
        output_dir=cfg["output_dir"],
        services=services,
        quality=cfg["quality"],
        filename_format=cfg["filename_format"],
        use_artist_subfolders=cfg["use_artist_subfolders"],
        use_album_subfolders=cfg["use_album_subfolders"],
        first_artist_only=cfg["first_artist_only"],
        embed_lyrics=cfg["embed_lyrics"],
        enrich_providers=["deezer", "apple", "tidal", "soundcloud"],
        track_max_retries=PER_TRACK_RETRIES,
        timeout_s=PER_TRACK_TIMEOUT,
        max_concurrent_downloads=MAX_CONCURRENT,
    ) as client:
        parsed = parse_spotify_url(url)

        total = 0
        if parsed["type"] != "track":
            info, tracks = await client.get_playlist(url)
            seen = set()
            unique = [t for t in tracks if not (t.id in seen or seen.add(t.id))]
            total = len(unique)
            logger.info("Collection '%s' — %d tracks (%d unique)",
                        info.get("name", "?"), len(tracks), total)

        try:
            failed_list = await client.download_track(url)
        except Exception as e:
            logger.error("Download failed: %s", e)
            return {"ok": 0, "skipped": 0, "failed": 1, "failed_tracks": []}

        failed = len(failed_list)
        ok = (total or 1) - failed

    logger.info("PASS — %d ok, %d failed", ok, failed)
    return {
        "ok": ok,
        "skipped": 0,
        "failed": failed,
        "failed_tracks": [(t.id, t.title, "download_failed") for t in failed_list],
    }


async def run_url(url: str, cfg: dict, logger: logging.Logger, single_pass: bool = False) -> dict:
    """Download a Spotify URL. Returns {ok, skipped, failed}.

    single_pass=True — one attempt, no retry. Use for queue processing.
    single_pass=False — full retry loop (24h cap). Use for standalone CLI.
    """
    if single_pass:
        return await _download_once(url, cfg, logger, services=SERVICES)

    deadline = time.monotonic() + MAX_RETRY_DURATION

    def _past() -> bool:
        return time.monotonic() >= deadline

    while True:
        try:
            working = await wait_for_providers(logger)
            result = await _download_once(url, cfg, logger, services=working)
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
