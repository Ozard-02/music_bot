"""Core download engine: run_url() handles tracks, albums, playlists.

Standalone CLI: `python downloader.py <spotify_url>` still works.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import MAX_CONCURRENT, PER_TRACK_TIMEOUT, PER_TRACK_RETRIES
from track_utils import partition_tracks, prune_empty_parents, spotiflac_track_relative_path, track_relative_path
import spotiflac_loader


@dataclass(frozen=True)
class DownloadResult:
    """Typed outcome of a download job (tracks/albums/playlists)."""

    ok: int = 0
    skipped: int = 0
    failed: int = 0
    failed_tracks: list[tuple] = field(default_factory=list)
    gave_up_tracks: list[tuple] = field(default_factory=list)
    total: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@spotiflac_loader.wrap
async def run_url(
    url: str,
    cfg: dict,
    logger: logging.Logger,
    skip_titles: set[str] | None = None,
    progress_cb=None,
    failure_cb=None,
) -> DownloadResult:
    global _in_flight_lock
    if _in_flight_lock is None:
        import spotiflac_patch
        _in_flight_lock = spotiflac_patch._AsyncLockAdapter()
    from SpotiFLAC import AsyncSpotiFLAC
    from SpotiFLAC.providers.spotify_metadata import parse_spotify_url

    parsed = parse_spotify_url(url)

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
                if failure_cb:
                    failure_cb(url, "metadata_error")
                return DownloadResult(failed=1, failed_tracks=[("", url, "metadata_error")], total=1)
        else:
            _, tracks = await client.get_playlist(url)
        return await _download_tracks(client, tracks, cfg, logger, skip_titles, progress_cb, failure_cb)


async def _fix_cover(track, cfg: dict, logger: logging.Logger) -> None:
    from flac_utils import resolve_cover_data, embed_cover

    rel = track_relative_path(track, cfg)
    fpath = Path(cfg["output_dir"]) / rel
    if not fpath.exists():
        return

    try:
        data = await resolve_cover_data(track)
        if data is None:
            return
        await asyncio.to_thread(embed_cover, fpath, data)
        logger.debug("Cover overwritten for %s", rel)
    except Exception:
        logger.debug("Cover overwrite failed for %s", rel)


def _write_lyrics_sidecar(track, cfg: dict, logger: logging.Logger) -> None:
    """Write a .lrc sidecar from lyrics already embedded by SpotiFLAC.

    SpotiFLAC stores fetched (possibly line-synced) lyrics in the FLAC LYRICS
    tag as plain text — no player reads that as 'synchronized'.  Reading the
    tag back and emitting a real `.lrc` sidecar makes the timing usable.
    Reuses the just-emitted tag: no extra network fetch.
    """
    rel = track_relative_path(track, cfg)
    fpath = Path(cfg["output_dir"]) / rel
    if not fpath.exists():
        return
    try:
        from flac_utils import read_lrc, write_lrc_sidecar
        lrc = read_lrc(fpath)
        if lrc:
            write_lrc_sidecar(fpath, lrc)
            logger.debug("Lyrics sidecar written for %s", rel)
    except Exception:
        logger.debug("Lyrics sidecar failed for %s", rel)


# Cross-job in-flight guard: overlapping jobs (same track/album/playlist in the
# queue in parallel) download each track exactly once; the second job counts
# the track as skipped instead of writing a competing .part file.
# asyncio.Lock would bind to the first event loop that touches it, but each job
# runs on its own loop (asyncio.run per worker thread) — a threading-based
# adapter is cross-loop-safe (same fix as the qobuz lock, spotiflac_patch.py).
_in_flight: set[str] = set()
_in_flight_lock = None  # lazily created in run_url() (spotiflac_patch is heavy)


def _rename_after_download(track, cfg: dict, logger: logging.Logger, started: float | None = None):
    """Normalize a freshly downloaded file from SpotiFLAC's naming to ours.

    Only touches files this download actually wrote: `started` (wall clock
    captured before the download) is compared against the file's mtime.  A
    file that pre-dates the download is left completely untouched — never
    deleted or moved, since naming drift can make `spoti_path` point at an
    older, unrelated file (data-loss hazard).  Never raises.
    """
    from flac_utils import get_spotify_id_from_file

    spoti_rel = spotiflac_track_relative_path(track, cfg)
    orig_rel = track_relative_path(track, cfg)
    if spoti_rel == orig_rel:
        return
    spoti_path = Path(cfg["output_dir"]) / spoti_rel
    orig_path = Path(cfg["output_dir"]) / orig_rel
    if not spoti_path.exists():
        return
    try:
        if started is None or spoti_path.stat().st_mtime < started:
            logger.warning(
                "Not renaming %s — file pre-dates this download",
                spoti_rel,
            )
            return
        if orig_path.exists():
            # The fresh download duplicated an existing canonical file.
            # Delete the duplicate ONLY when BOTH files provably are the same
            # track (embedded Spotify ID matches the expected one); never
            # delete what we can't prove.
            if (
                get_spotify_id_from_file(spoti_path) == track.id
                and get_spotify_id_from_file(orig_path) == track.id
            ):
                logger.warning("Target exists, removing duplicate: %s", spoti_path)
                spoti_path.unlink()
                return
            logger.warning(
                "Target exists but ID mismatch, keeping both: %s (skip unlink)",
                spoti_path,
            )
            return
        orig_path.parent.mkdir(parents=True, exist_ok=True)
        spoti_path.rename(orig_path)
        logger.info("Renamed %s -> %s", spoti_rel, orig_rel)
        prune_empty_parents(spoti_path, Path(cfg["output_dir"]))
    except Exception as exc:
        logger.warning("Rename %s -> %s failed: %s", spoti_rel, orig_rel, exc)


async def _download_tracks(
    client,
    tracks: list,
    cfg: dict,
    logger: logging.Logger,
    skip_titles: set[str] | None = None,
    progress_cb=None,
    failure_cb=None,
) -> DownloadResult:
    existing, given_up, missing = partition_tracks(tracks, cfg, skip_titles)
    total = len(existing) + len(given_up) + len(missing)
    existing_count = len(existing)
    given_up_count = len(given_up)
    logger.info(
        "Pre-check: %d/%d tracks exist on disk (%d new, %d given up)",
        existing_count, total, len(missing), given_up_count,
    )

    if not missing:
        logger.info("All %d tracks already on disk — nothing to do", total)
        return DownloadResult(
            skipped=existing_count,
            gave_up_tracks=[(t.id, t.title, "gave_up") for t in given_up],
            total=total,
        )

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    failed_list = []
    skipped_inflight = []
    done_count = 0

    async def _dl(track):
        nonlocal done_count
        async with sem:
            async with _in_flight_lock:
                if track.id in _in_flight:
                    skipped_inflight.append(track)
                    return
                _in_flight.add(track.id)
            try:
                started = time.time()
                fl = await client.download_track(track.external_url)
                if fl:
                    failed_list.append(track)
                    if failure_cb:
                        for f in fl:
                            # SpotiFLAC's download_track returns the failed tracks,
                            # as TrackMetadata (1.5.x/1.6.x) or (id, title, artists,
                            # error) tuples (older versions). Normalize both shapes.
                            if isinstance(f, tuple):
                                title, err = f[1], f[3] or "download_failed"
                            else:
                                title, err = f.title, "download_failed"
                            failure_cb(title, err)
                else:
                    await asyncio.to_thread(_rename_after_download, track, cfg, logger, started)
                    await _fix_cover(track, cfg, logger)
                    if cfg.get("embed_lyrics"):
                        await asyncio.to_thread(_write_lyrics_sidecar, track, cfg, logger)
            except Exception as exc:
                failed_list.append(track)
                if failure_cb:
                    failure_cb(track.title, str(exc))
            finally:
                async with _in_flight_lock:
                    _in_flight.discard(track.id)
            done_count += 1
            if progress_cb:
                await asyncio.to_thread(progress_cb, done_count, len(missing), track.title)

    await asyncio.gather(*[_dl(t) for t in missing], return_exceptions=True)

    # Reconcile: a track counts as ok only if a file actually exists on disk
    # (canonical layout or SpotiFLAC layout) — "success but no file" (naming
    # drift, provider glitch) is reported as failed, not silently ok.
    failed_ids = {t.id for t in failed_list}
    skip_ids = {t.id for t in skipped_inflight}
    base = Path(cfg["output_dir"])
    for t in missing:
        if t.id in failed_ids or t.id in skip_ids:
            continue
        if not (base / track_relative_path(t, cfg)).exists() and not (
            base / spotiflac_track_relative_path(t, cfg)
        ).exists():
            failed_list.append(t)
            failed_ids.add(t.id)
            logger.warning("Reconcile: no file on disk after download: %s", t.title)
            if failure_cb:
                failure_cb(t.title, "no_file_after_download")

    failed = len(failed_list)
    ok = len(missing) - failed - len(skipped_inflight)
    skipped = existing_count + len(skipped_inflight)
    if skipped_inflight:
        logger.info("Skipped %d track(s) already in flight in another job", len(skipped_inflight))
    logger.info("PASS — %d ok, %d skipped, %d failed", ok, skipped, failed)
    return DownloadResult(
        ok=ok,
        skipped=skipped,
        failed=failed,
        failed_tracks=[(t.id, t.title, "download_failed") for t in failed_list],
        gave_up_tracks=[(t.id, t.title, "gave_up") for t in given_up],
        total=total,
    )
