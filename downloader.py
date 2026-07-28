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
    from SpotiFLAC import AsyncSpotiFLAC, TrackMetadata
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
        is_single = parsed["type"] == "track"

        existing_count = 0
        total = 0
        missing_tracks: list[TrackMetadata] | None = None

        if is_single:
            track = await client.get_track_metadata(url)
            rel = track_relative_path(track, cfg)
            full = Path(cfg["output_dir"]) / rel
            if full.exists():
                logger.info("Pre-check: %s exists — skipping", rel)
                return {"ok": 0, "skipped": 1, "failed": 0, "failed_tracks": []}
            total = 1
            missing_tracks = [track]
        else:
            info, tracks = await client.get_playlist(url)
            seen = set()
            unique = [t for t in tracks if not (t.id in seen or seen.add(t.id))]
            total = len(unique)

            existing: list[TrackMetadata] = []
            missing: list[TrackMetadata] = []
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

            missing_tracks = missing

        try:
            if is_single:
                failed_list = await client.download_track(url)
            else:
                failed_list = await client._downloader._run_once_async(
                    url, target_tracks=missing_tracks,
                )
        except Exception as e:
            logger.error("Download failed: %s", e)
            return {
                "ok": 0,
                "skipped": existing_count,
                "failed": len(missing_tracks) if missing_tracks else 1,
                "failed_tracks": [],
            }

        failed = len(failed_list)
        downloaded = len(missing_tracks) - failed
        ok = downloaded
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
