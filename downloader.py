from __future__ import annotations

import asyncio
import logging

from config import (
    SERVICES, MAX_CONCURRENT, PER_TRACK_TIMEOUT, PER_TRACK_RETRIES,
    load_config, setup_logger, bridge_community_session,
)


async def run_url(url: str, cfg: dict, logger: logging.Logger) -> dict:
    from SpotiFLAC import AsyncSpotiFLAC
    from SpotiFLAC.providers.spotify_metadata import parse_spotify_url

    async with AsyncSpotiFLAC(
        output_dir=cfg["output_dir"],
        services=SERVICES,
        quality=cfg["quality"],
        filename_format=cfg["filename_format"],
        use_artist_subfolders=cfg["use_artist_subfolders"],
        use_album_subfolders=cfg["use_album_subfolders"],
        first_artist_only=cfg["first_artist_only"],
        embed_lyrics=cfg["embed_lyrics"],
        enrich_providers=["deezer", "apple", "soundcloud"],
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


def run_url_sync(url: str, cfg: dict, logger: logging.Logger) -> dict:
    return asyncio.run(run_url(url, cfg, logger))
