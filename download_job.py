#!/usr/bin/env python3
"""Subprocess download worker: one job per process.

Reads a single JSON spec from stdin, runs the download (or search resolution +
download), and emits JSON-lines to stdout. Logging goes to stderr/rotating
file — stdout is protocol only. Exits 0 after a `result` event, nonzero on a
crash (parent treats that as a failed job).

Importing SpotiFLAC here (transitively via downloader) is exactly why the
parent stays lean: this process is short-lived and its RSS is reclaimed on
exit.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

import spotiflac_patch  # noqa: F401  (import-time side-effects)
from config import SPOTIFY_BLOCKED_ERROR, setup_logger
from deezer_meta import is_deezer_url
from downloader import DownloadResult, is_spotify_block, run_deezer, run_url


def _emit(event: dict) -> None:
    """Write one protocol line to stdout and flush immediately — the parent
    streams these and its stall watchdog counts silence as a hang."""
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def _job(spec: dict, logger: logging.Logger) -> DownloadResult:
    cfg = spec.get("cfg") or {}
    skip_titles = set(spec.get("skip_titles") or [])

    # Plain function: downloader calls progress_cb synchronously. (An async
    # def here was never awaited, so no progress line ever reached the
    # parent: /status stayed on "downloading…" and long playlists hit the
    # stall watchdog.)
    def _progress(done, total, title, provider=None):
        _emit({
            "event": "progress",
            "done": done,
            "total": total,
            "title": title,
            "provider": provider,
        })

    def _failure(track_id, title, err):
        _emit({
            "event": "failure",
            "track_id": track_id,
            "title": title,
            "error": err,
        })

    if spec.get("type") == "search":
        from SpotiFLAC import AsyncSpotiFLAC
        from resolver_match import resolve_search

        async with AsyncSpotiFLAC(output_dir=cfg["output_dir"]) as client:
            url, display, _kind = await resolve_search(client, spec["query"])
        logger.info("Resolved: %s -> %s", spec["query"], display)
        _emit({"event": "resolved", "url": url, "display": display})
        result = await run_url(url, cfg, logger, skip_titles, _progress, _failure)
        await _maybe_m3u8(spec, logger)
        return result

    url = spec["url"]
    if is_deezer_url(url):
        result = await run_deezer(url, cfg, logger, skip_titles, _progress, _failure)
    else:
        result = await run_url(url, cfg, logger, skip_titles, _progress, _failure)
    await _maybe_m3u8(spec, logger)
    return result


async def _maybe_m3u8(spec: dict, logger: logging.Logger) -> None:
    """Auto-build an .m3u8 for playlist downloads (SpotiFLAC lives here)."""
    if not spec.get("want_m3u8"):
        return
    from SpotiFLAC.providers.spotify_metadata import parse_spotify_url
    from m3u8 import build_m3u8

    url = spec.get("url")
    if not url or is_deezer_url(url):
        return
    if parse_spotify_url(url).get("type") == "playlist":
        try:
            result = await build_m3u8(url, cfg=spec.get("cfg") or {})
            logger.info("Auto m3u8: %s", result.get("path"))
        except Exception as e:
            logger.warning("Auto m3u8 failed for %s: %s", url, e)


async def _command(spec: dict, logger: logging.Logger) -> dict:
    """One-shot maintenance commands (run in-process here, SpotiFLAC available).

    Returns the result dict; long-running commands emit `progress` events via
    the same stdout protocol as downloads."""
    cmd = spec.get("type")
    cfg = spec.get("cfg") or {}

    if cmd == "m3u8":
        from m3u8 import build_m3u8
        return await build_m3u8(spec["url"], spec.get("name"), cfg)

    if cmd == "fix_metadata":
        from scripts.fix_metadata import fix_library

        async def progress(current, total, text):
            _emit({"event": "progress", "done": current, "total": total, "title": text})

        try:
            return await fix_library(
                spec["folder"], apply=True, progress=progress,
                lyrics=bool(spec.get("lyrics")),
                lyrics_providers=spec.get("providers") or None,
            )
        except Exception as exc:
            # fix_library() is defensive too, but keep the result shape valid
            # even if it raises before returning: the bot indexes these keys.
            logger.exception("fix_metadata failed")
            return {
                "error": str(exc),
                "folders": 0, "total": 0, "fixed": 0, "moved": 0,
                "failed": 0, "skipped": 0, "would_fix": 0,
                "details": [], "moved_files": [], "failed_files": [],
            }

    if cmd == "sync_lyrics":
        from scripts.sync_lyrics import sync_lyrics

        async def progress(current, total, text):
            _emit({"event": "progress", "done": current, "total": total, "title": text})

        try:
            return await sync_lyrics(
                spec["folder"],
                apply=True,
                progress=progress,
                providers=spec.get("providers") or None,
                force=bool(spec.get("force")),
            )
        except Exception as exc:
            logger.exception("sync_lyrics failed")
            return {
                "error": str(exc), "total": 0, "fixed": 0, "sidecars": 0,
                "kept": 0, "no_lyrics": 0, "skipped": 0, "failed": 0,
                "would_fix": 0, "details": [], "failed_files": [],
            }

    raise ValueError(f"unknown command type: {cmd}")


_COMMANDS = {"m3u8", "fix_metadata", "sync_lyrics"}


def main() -> int:
    spec_line = sys.stdin.readline()
    if not spec_line:
        return 1
    try:
        spec = json.loads(spec_line)
    except json.JSONDecodeError:
        return 1

    log_path = spec.get("log_path") or (Path(__file__).parent / "spoty_loop.log")
    logger = setup_logger(log_path)

    try:
        if spec.get("type") in _COMMANDS:
            result = asyncio.run(_command(spec, logger))
            _emit({"event": "result", "result": result})
            return 0
        result = asyncio.run(_job(spec, logger))
    except Exception as exc:
        logger.exception("Job crashed")
        err = SPOTIFY_BLOCKED_ERROR if is_spotify_block(exc) else "internal_error"
        _emit({"event": "result", "result": {
            "ok": 0, "skipped": 0, "failed": 1,
            "failed_tracks": [("", "job_crash", err)], "total": 1,
            "error": f"{type(exc).__name__}: {exc}",
        }})
        return 1

    _emit({"event": "result", "result": result.to_dict()})
    return 0


if __name__ == "__main__":
    sys.exit(main())