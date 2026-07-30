from __future__ import annotations

import asyncio
import builtins
from contextlib import contextmanager
import logging
from pathlib import Path

from config import (
    MAX_CONCURRENT, PER_TRACK_TIMEOUT, PER_TRACK_RETRIES,
)
from track_utils import spotiflac_track_relative_path, track_relative_path


@contextmanager
def _silence_spotiflac():
    import SpotiFLAC.core.console as _console
    import SpotiFLAC.core.progress as _progress

    _originals = {
        "print_source_banner": _console.print_source_banner,
        "print_api_failure": _console.print_api_failure,
        "print_quality_fallback": _console.print_quality_fallback,
        "print_track_header": _console.print_track_header,
        "print_summary": _console.print_summary,
        "print_official_source": _console.print_official_source,
        "safe_tqdm_write": _progress.safe_tqdm_write,
        "input": builtins.input,
    }

    _console.print_source_banner = lambda *a, **kw: None
    _console.print_api_failure = lambda *a, **kw: None
    _console.print_quality_fallback = lambda *a, **kw: None
    _console.print_track_header = lambda *a, **kw: None
    _console.print_summary = lambda *a, **kw: None
    _console.print_official_source = lambda *a, **kw: None
    _progress.safe_tqdm_write = lambda *a, **kw: None
    builtins.input = lambda *a, **kw: ""

    try:
        yield
    finally:
        _console.print_source_banner = _originals["print_source_banner"]
        _console.print_api_failure = _originals["print_api_failure"]
        _console.print_quality_fallback = _originals["print_quality_fallback"]
        _console.print_track_header = _originals["print_track_header"]
        _console.print_summary = _originals["print_summary"]
        _console.print_official_source = _originals["print_official_source"]
        _progress.safe_tqdm_write = _originals["safe_tqdm_write"]
        builtins.input = _originals["input"]


async def run_url(url: str, cfg: dict, logger: logging.Logger) -> dict:
    from SpotiFLAC import AsyncSpotiFLAC
    from SpotiFLAC.providers.spotify_metadata import parse_spotify_url

    parsed = parse_spotify_url(url)

    with _silence_spotiflac():
        async with AsyncSpotiFLAC(
            output_dir=cfg["output_dir"],
            services=cfg["services"],
            quality=cfg["quality"],
            filename_format=cfg["filename_format"],
            use_artist_subfolders=cfg["use_artist_subfolders"],
            use_album_subfolders=cfg["use_album_subfolders"],
            first_artist_only=cfg["first_artist_only"],
            embed_lyrics=cfg["embed_lyrics"],
            enrich_providers=["apple", "deezer", "tidal", "soundcloud"],
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
            return await _download_tracks(client, tracks, cfg, logger)


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


async def _download_tracks(client, tracks: list, cfg: dict, logger: logging.Logger) -> dict:
    seen = set()
    unique = []
    for t in tracks:
        if t.id not in seen:
            seen.add(t.id)
            unique.append(t)
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
        return {"ok": 0, "skipped": total, "failed": 0, "failed_tracks": [], "total": total}

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    failed_list = []

    async def _dl(track):
        async with sem:
            try:
                fl = await client.download_track(track.external_url)
                if fl:
                    failed_list.extend(fl)
                else:
                    await asyncio.to_thread(_rename_after_download, track, cfg, logger)
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
        "total": total,
    }


