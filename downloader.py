"""Core download engine: run_url() handles tracks, albums, playlists.

Runs inside the download subprocess (download_job.py) — never imported by the
parent. Cross-job overlap protection uses flock lockfiles because each job is
its own process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import MAX_CONCURRENT, PER_TRACK_TIMEOUT, PER_TRACK_RETRIES
from flac_utils import embed_cover, get_spotify_id_from_file, read_lrc, resolve_cover_data, write_lrc_sidecar
from spotiflac_patch import _AsyncLockAdapter, pop_track_provider
from track_utils import partition_tracks, prune_empty_parents, spotiflac_track_relative_path, track_relative_path


@dataclass(frozen=True)
class DownloadResult:
    """Typed outcome of a download job (tracks/albums/playlists)."""

    ok: int = 0
    skipped: int = 0
    failed: int = 0
    failed_tracks: list[tuple] = field(default_factory=list)
    gave_up_tracks: list[tuple] = field(default_factory=list)
    total: int = 0
    providers: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DownloadResult":
        return cls(
            ok=data.get("ok", 0),
            skipped=data.get("skipped", 0),
            failed=data.get("failed", 0),
            failed_tracks=[tuple(t) for t in data.get("failed_tracks", [])],
            gave_up_tracks=[tuple(t) for t in data.get("gave_up_tracks", [])],
            total=data.get("total", 0),
            providers=data.get("providers", {}),
        )


async def run_url(
    url: str,
    cfg: dict,
    logger: logging.Logger,
    skip_titles: set[str] | None = None,
    progress_cb=None,
    failure_cb=None,
) -> DownloadResult:
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
    """Write a .lrc sidecar from lyrics already embedded by SpotiFLAC."""
    rel = track_relative_path(track, cfg)
    fpath = Path(cfg["output_dir"]) / rel
    if not fpath.exists():
        return
    try:
        lrc = read_lrc(fpath)
        if lrc:
            write_lrc_sidecar(fpath, lrc)
            logger.debug("Lyrics sidecar written for %s", rel)
    except Exception:
        logger.debug("Lyrics sidecar failed for %s", rel)


# Cross-process in-flight guard: each job is its own process, so process-local
# state can't coordinate. A flock lockfile per track id (LOCK_EX|LOCK_NB) under
# <output_dir>/.inflight/<id>.lock does. flock releases automatically on
# process exit — no stale locks, no cleanup needed.
_in_flight_lock = _AsyncLockAdapter()


def _inflight_lockfile(track_id: str, cfg: dict) -> Path:
    d = Path(cfg["output_dir"]) / ".inflight"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{track_id}.lock"


def _try_lock_track(track_id: str, cfg: dict) -> object | None:
    """Return an open locked fd, or None if another job holds the lock."""
    import fcntl

    try:
        fd = os.open(_inflight_lockfile(track_id, cfg), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def _rename_after_download(track, cfg: dict, logger: logging.Logger, started: float | None = None):
    """Normalize a freshly downloaded file from SpotiFLAC's naming to ours."""
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
    providers: dict[str, int] = {}

    async def _dl(track):
        nonlocal done_count
        async with sem:
            lock_fd = None
            async with _in_flight_lock:
                lock_fd = _try_lock_track(track.id, cfg)
                if lock_fd is None:
                    skipped_inflight.append(track)
                    return
            try:
                started = time.time()
                fl = await client.download_track(track.external_url)
                if fl:
                    failed_list.append(track)
                    if failure_cb:
                        for f in fl:
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
                if lock_fd is not None:
                    try:
                        os.close(lock_fd)  # releases flock
                    except OSError:
                        pass
            done_count += 1
            provider = pop_track_provider(track.id)
            if provider:
                providers[provider] = providers.get(provider, 0) + 1
            if progress_cb:
                await asyncio.to_thread(progress_cb, done_count, len(missing), track.title, provider)

    await asyncio.gather(*[_dl(t) for t in missing], return_exceptions=True)

    # Reconcile: a track counts as ok only if a file actually exists on disk
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
        providers=providers,
    )