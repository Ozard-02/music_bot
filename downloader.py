"""Core download engine: run_url() handles tracks, albums, playlists.

Standalone CLI: `python downloader.py <spotify_url>` still works.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from config import MAX_CONCURRENT, PER_TRACK_TIMEOUT, PER_TRACK_RETRIES
from flac_utils import embed_cover, fetch_cover
from spotiflac_patch import silence_spotiflac
from track_utils import spotiflac_track_relative_path, track_relative_path


async def run_url(
    url: str,
    cfg: dict,
    logger: logging.Logger,
    skip_titles: set[str] | None = None,
    progress_cb=None,
) -> dict:
    from SpotiFLAC import AsyncSpotiFLAC
    from SpotiFLAC.providers.spotify_metadata import parse_spotify_url

    parsed = parse_spotify_url(url)

    with silence_spotiflac():
        async with AsyncSpotiFLAC(
            output_dir=cfg["output_dir"],
            services=cfg["services"],
            quality=cfg["quality"],
            filename_format=cfg["filename_format"],
            use_artist_subfolders=cfg["use_artist_subfolders"],
            use_album_subfolders=cfg["use_album_subfolders"],
            first_artist_only=cfg["first_artist_only"],
            embed_lyrics=cfg["embed_lyrics"],
            enrich_providers=["apple", "deezer", "soundcloud"],
            track_max_retries=PER_TRACK_RETRIES,
            timeout_s=PER_TRACK_TIMEOUT,
            max_concurrent_downloads=MAX_CONCURRENT,
        ) as client:
            if parsed["type"] == "track":
                try:
                    track = await client.get_track_metadata(url)
                    if track is None:
                        raise TypeError("get_track_metadata returned None")
                    tracks = [track]
                except Exception:
                    logger.exception("Track metadata failed for %s", url)
                    return {"ok": 0, "skipped": 0, "failed": 1, "failed_tracks": [("", url, "metadata_error")], "total": 1}
            else:
                _, tracks = await client.get_playlist(url)
            return await _download_tracks(client, tracks, cfg, logger, skip_titles, progress_cb)


async def _fix_cover(track, cfg: dict, logger: logging.Logger) -> None:
    rel = track_relative_path(track, cfg)
    fpath = Path(cfg["output_dir"]) / rel
    cover_url = getattr(track, "cover_url", None)
    if not fpath.exists() or not cover_url:
        return

    try:
        data = await fetch_cover(cover_url)
        if data is None:
            return
        await asyncio.to_thread(embed_cover, fpath, data)
        logger.debug("Cover overwritten for %s", rel)
    except Exception:
        logger.debug("Cover overwrite failed for %s", rel)


def _rename_after_download(track, cfg: dict, logger: logging.Logger):
    spoti_rel = spotiflac_track_relative_path(track, cfg)
    orig_rel = track_relative_path(track, cfg)
    if spoti_rel == orig_rel:
        return
    spoti_path = Path(cfg["output_dir"]) / spoti_rel
    orig_path = Path(cfg["output_dir"]) / orig_rel
    if not spoti_path.exists():
        return
    if orig_path.exists():
        logger.warning("Target exists, removing duplicate: %s", spoti_path)
        spoti_path.unlink()
        return
    orig_path.parent.mkdir(parents=True, exist_ok=True)
    spoti_path.rename(orig_path)
    logger.info("Renamed %s -> %s", spoti_rel, orig_rel)
    parent = spoti_path.parent
    while parent != Path(cfg["output_dir"]):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


async def _download_tracks(
    client,
    tracks: list,
    cfg: dict,
    logger: logging.Logger,
    skip_titles: set[str] | None = None,
    progress_cb=None,
) -> dict:
    seen = set()
    unique = []
    for t in tracks:
        if t.id not in seen:
            seen.add(t.id)
            unique.append(t)
    total = len(unique)

    existing = []
    missing = []
    given_up = []
    skip_titles = skip_titles or set()
    for t in unique:
        rel = track_relative_path(t, cfg)
        full = Path(cfg["output_dir"]) / rel
        if full.exists():
            existing.append(t)
        elif t.title in skip_titles:
            given_up.append(t)
        else:
            missing.append(t)

    existing_count = len(existing)
    given_up_count = len(given_up)
    logger.info(
        "Pre-check: %d/%d tracks exist on disk (%d new, %d given up)",
        existing_count, total, len(missing), given_up_count,
    )

    if not missing:
        logger.info("All %d tracks already on disk — nothing to do", total)
        return {
            "ok": 0, "skipped": existing_count, "failed": 0,
            "failed_tracks": [],
            "gave_up_tracks": [(t.id, t.title, "gave_up") for t in given_up],
            "total": total,
        }

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    failed_list = []
    done_count = 0

    async def _dl(track):
        nonlocal done_count
        async with sem:
            try:
                fl = await client.download_track(track.external_url)
                if fl:
                    failed_list.extend(fl)
                else:
                    await asyncio.to_thread(_rename_after_download, track, cfg, logger)
                    await _fix_cover(track, cfg, logger)
            except Exception:
                failed_list.append(track)
            done_count += 1
            if progress_cb:
                await asyncio.to_thread(progress_cb, done_count, len(missing), track.title)

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
        "gave_up_tracks": [(t.id, t.title, "gave_up") for t in given_up],
        "total": total,
    }
