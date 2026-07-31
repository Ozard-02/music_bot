from __future__ import annotations

import asyncio
import builtins
from contextlib import contextmanager
import logging
import re
from pathlib import Path

from config import (
    MAX_CONCURRENT, PER_TRACK_TIMEOUT, PER_TRACK_RETRIES, SCRIPT_MAX_CONCURRENT,
)
from track_utils import spotiflac_track_relative_path, track_relative_path, _get_jpeg_dimensions


def _disable_progress_manager():
    """Neutralize SpotiFLAC's ProgressManager + console interception once.

    ProgressManager keeps class-level asyncio state (_event_queue, _worker_task)
    bound to the first event loop that touched it.  The bot runs each download
    in its own thread/loop (asyncio.to_thread + asyncio.run), so the shared
    queue ends up 'bound to a different event loop' on every subsequent job,
    flooding the log.  We never use its tqdm bars, so make it a no-op.

    SpotiFLAC's install_console_interception() (called once per track
    download) strips every StreamHandler — including ours — off the root
    logger and adds a TqdmLoggingHandler that is never removed.  Handlers
    pile up on root one per track, so every log line prints N times in
    SpotiFLAC's format and spoty_loop.log stops growing.  Neutralize it in
    both modules that reference it (core.progress and downloader).
    """
    try:
        from SpotiFLAC.core import progress
        from SpotiFLAC.core.progress import ProgressManager
        import SpotiFLAC.downloader as sf_downloader
    except ImportError:
        return
    ProgressManager._event_queue = None
    ProgressManager._worker_task = None
    ProgressManager._bars = {}
    ProgressManager._slot_map = {}
    ProgressManager._master_bar = None
    ProgressManager.enqueue_progress = lambda *a, **kw: None
    ProgressManager.start_worker = lambda *a, **kw: None
    ProgressManager.initialize_master_bar = lambda *a, **kw: None

    for _mod in (progress, sf_downloader):
        _mod.install_console_interception = lambda *a, **kw: None
        _mod.uninstall_console_interception = lambda *a, **kw: None


_disable_progress_manager()


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


async def run_url(url: str, cfg: dict, logger: logging.Logger, skip_titles: set[str] | None = None) -> dict:
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
            return await _download_tracks(client, tracks, cfg, logger, skip_titles)


_SPOTIFY_COVER_UPGRADE = re.compile(r"(ab67616d0000)1e02")


def _upgrade_cover_url(url: str) -> str:
    """Upgrade Spotify CDN URL from 300×300 (1e02) to 640×640 (b273)."""
    return _SPOTIFY_COVER_UPGRADE.sub(r"\g<1>b273", url)


_TRACK_ID_RE = re.compile(r"open\.spotify\.com/track/([a-zA-Z0-9]+)")


def _get_spotify_id_from_file(fpath: Path) -> str | None:
    from mutagen.flac import FLAC
    try:
        audio = FLAC(str(fpath))
    except Exception:
        return None
    for tag in ("URL", "comment"):
        val = audio.get(tag, [None])[0]
        if val:
            m = _TRACK_ID_RE.search(val)
            if m:
                return m.group(1)
    return None


async def _fix_cover(track, cfg: dict, logger: logging.Logger) -> None:
    import httpx
    from mutagen.flac import FLAC, Picture

    rel = track_relative_path(track, cfg)
    fpath = Path(cfg["output_dir"]) / rel
    if not fpath.exists():
        return

    cover_url = getattr(track, "cover_url", None)
    if not cover_url:
        return

    cover_url = _upgrade_cover_url(cover_url)

    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(cover_url)
            if resp.status_code != 200:
                return

        data = resp.content

        def _embed():
            audio = FLAC(str(fpath))
            pic = Picture()
            pic.data = data
            pic.type = 3
            pic.mime = "image/jpeg"
            w, h = _get_jpeg_dimensions(data)
            pic.width, pic.height = w, h
            pic.depth = 0
            audio.clear_pictures()
            audio.add_picture(pic)
            audio.save()

        await asyncio.to_thread(_embed)
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


async def _download_tracks(client, tracks: list, cfg: dict, logger: logging.Logger, skip_titles: set[str] | None = None) -> dict:
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

    async def _dl(track):
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


async def rescan_library(
    cfg: dict,
    logger: logging.Logger,
    progress=None,
) -> dict:
    from SpotiFLAC.client import SpotifyMetadataClient
    from SpotiFLAC.core.progress import ProgressManager
    import httpx
    from mutagen.flac import FLAC, Picture

    ProgressManager._event_queue = None
    ProgressManager._worker_task = None

    output_dir = Path(cfg["output_dir"])
    flacs = list(output_dir.rglob("*.flac"))
    total = len(flacs)

    if progress:
        await progress(0, total, f"Scanning {total} FLACs…")
    logger.info("Rescan: found %d FLACs", total)

    spotify = SpotifyMetadataClient()
    sem = asyncio.Semaphore(SCRIPT_MAX_CONCURRENT)
    ok = failed = skipped = 0

    async def process(fpath: Path):
        nonlocal ok, failed, skipped
        async with sem:
            try:
                sid = _get_spotify_id_from_file(fpath)
                if not sid:
                    skipped += 1
                    return

                track = await spotify.get_track_async(sid)
                if not track or not track.cover_url:
                    skipped += 1
                    return

                cover_url = _upgrade_cover_url(track.cover_url)

                async with httpx.AsyncClient(timeout=10) as http:
                    resp = await http.get(cover_url)
                    if resp.status_code != 200:
                        failed += 1
                        return

                data = resp.content

                def _embed():
                    audio = FLAC(str(fpath))
                    pic = Picture()
                    pic.data = data
                    pic.type = 3
                    pic.mime = "image/jpeg"
                    w, h = _get_jpeg_dimensions(data)
                    pic.width, pic.height = w, h
                    pic.depth = 0
                    audio.clear_pictures()
                    audio.add_picture(pic)
                    audio.save()

                await asyncio.to_thread(_embed)
                ok += 1
            except Exception:
                failed += 1

        done = ok + failed + skipped
        if progress and done % 10 == 0:
            await progress(done, total, f"{ok} fixed, {failed} failed")

    await asyncio.gather(*[process(f) for f in flacs])

    if progress:
        await progress(total, total, f"Done — {ok} fixed, {skipped} skipped, {failed} failed")
    logger.info("Rescan done: %d fixed, %d skipped, %d failed", ok, skipped, failed)
    return {"ok": ok, "skipped": skipped, "failed": failed}


