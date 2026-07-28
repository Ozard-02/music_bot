from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from config import (
    SERVICES, MAX_CONCURRENT, PER_TRACK_TIMEOUT, PER_TRACK_RETRIES,
    load_config, setup_logger, bridge_community_session,
)
from m3u8 import track_relative_path


async def run_url(url: str, cfg: dict, logger: logging.Logger) -> dict:
    from SpotiFLAC import AsyncSpotiFLAC
    from SpotiFLAC.providers.spotify_metadata import parse_spotify_url

    parsed = parse_spotify_url(url)

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
        if parsed["type"] == "track":
            track = await client.get_track_metadata(url)
            tracks = [track]
        else:
            _, tracks = await client.get_playlist(url)
        return await _download_tracks(client, tracks, cfg, logger)


async def _download_tracks(client, tracks: list, cfg: dict, logger: logging.Logger) -> dict:
    seen = set()
    unique = [t for t in tracks if not (t.id in seen or seen.add(t.id))]
    total = len(unique)

    existing = []
    missing = []
    for t in unique:
        rel = track_relative_path(t, cfg)
        full = Path(cfg["output_dir"]) / rel
        if full.exists():
            existing.append(t)
        else:
            missing.append(t)

    existing_count = len(existing)
    logger.info(
        "Pre-check: %d/%d tracks exist on disk (%d new)",
        existing_count, total, len(missing),
    )

    if not missing:
        logger.info("All %d tracks already on disk — nothing to do", total)
        return {"ok": 0, "skipped": total, "failed": 0, "failed_tracks": []}

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    failed_list = []

    async def _dl(track):
        async with sem:
            try:
                fl = await client.download_track(track.external_url)
                if fl:
                    failed_list.extend(fl)
            except Exception:
                failed_list.append(track)

    await asyncio.gather(*[_dl(t) for t in missing], return_exceptions=True)

    failed = len(failed_list)
    ok = len(missing) - failed
    skipped = existing_count
    logger.info("PASS — %d ok, %d skipped, %d failed", ok, skipped, failed)
    return {
        "ok": ok,
        "skipped": skipped,
        "failed": failed,
        "failed_tracks": [(t.id, t.title, "download_failed") for t in failed_list],
    }


def run_url_sync(url: str, cfg: dict, logger: logging.Logger) -> dict:
    return asyncio.run(run_url(url, cfg, logger))
