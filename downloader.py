from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
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
            return await _run_single(client, url, cfg, logger)
        return await _run_collection(client, url, cfg, logger)


async def _run_single(client, url: str, cfg: dict, logger: logging.Logger) -> dict:
    from SpotiFLAC import TrackMetadata

    track = await client.get_track_metadata(url)
    rel = track_relative_path(track, cfg)
    full = Path(cfg["output_dir"]) / rel
    if full.exists():
        logger.info("Pre-check: %s exists — skipping", rel)
        return {"ok": 0, "skipped": 1, "failed": 0, "failed_tracks": []}

    try:
        failed_list = await client.download_track(url)
    except Exception as e:
        logger.error("Download failed: %s", e)
        return {"ok": 0, "skipped": 0, "failed": 1, "failed_tracks": []}

    failed = len(failed_list)
    ok = 1 - failed
    logger.info("PASS — %d ok, 0 skipped, %d failed", ok, failed)
    return {
        "ok": ok,
        "skipped": 0,
        "failed": failed,
        "failed_tracks": [(t.id, t.title, "download_failed") for t in failed_list],
    }


async def _run_collection(client, url: str, cfg: dict, logger: logging.Logger) -> dict:
    from SpotiFLAC import TrackMetadata

    info, tracks = await client.get_playlist(url)
    url_type = info.get("type", "")
    collection_name = info.get("name", "")

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

    try:
        failed_list = await client.download_track(url)
    except Exception as e:
        logger.error("Download failed: %s", e)
        return {
            "ok": 0,
            "skipped": existing_count,
            "failed": len(missing),
            "failed_tracks": [],
        }

    if url_type == "playlist" and collection_name:
        _move_playlist_files(cfg, collection_name, missing, failed_list, logger)

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


def _move_playlist_files(
    cfg: dict,
    collection_name: str,
    missing_tracks: list,
    failed_list: list,
    logger: logging.Logger,
):
    output_dir = Path(cfg["output_dir"])
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", collection_name.strip())
    if not safe_name:
        return
    playlist_dir = output_dir / safe_name
    if not playlist_dir.exists():
        return

    failed_ids = {t.id for t in failed_list} if failed_list else set()
    moved = 0
    for track in missing_tracks:
        if track.id in failed_ids:
            continue
        rel = track_relative_path(track, cfg)
        src = playlist_dir / rel
        dst = output_dir / rel

        if src.exists():
            if dst.exists():
                src.unlink()
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved += 1

    for dirpath, dirnames, filenames in os.walk(playlist_dir, topdown=False):
        if not dirnames and not filenames:
            os.rmdir(dirpath)

    if playlist_dir.exists() and not any(playlist_dir.iterdir()):
        playlist_dir.rmdir()

    if moved:
        logger.info("Moved %d files out of %s/", moved, safe_name)


def run_url_sync(url: str, cfg: dict, logger: logging.Logger) -> dict:
    return asyncio.run(run_url(url, cfg, logger))
