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
from config import setup_logger
from downloader import DownloadResult, run_url


def _emit(event: dict) -> None:
    """Write one protocol line to stdout and flush immediately — the parent
    streams these and its stall watchdog counts silence as a hang."""
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def _job(spec: dict, logger: logging.Logger) -> DownloadResult:
    cfg = spec.get("cfg") or {}
    skip_titles = set(spec.get("skip_titles") or [])

    async def _progress(done, total, title, provider=None):
        _emit({
            "event": "progress",
            "done": done,
            "total": total,
            "title": title,
            "provider": provider,
        })

    def _failure(title, err):
        _emit({"event": "failure", "title": title, "error": err})

    if spec.get("type") == "search":
        from SpotiFLAC import AsyncSpotiFLAC
        from resolver import resolve_search

        async with AsyncSpotiFLAC(output_dir=cfg["output_dir"]) as client:
            url, display, _kind = await resolve_search(client, spec["query"])
        logger.info("Resolved: %s -> %s", spec["query"], display)
        _emit({"event": "resolved", "url": url, "display": display})
        result = await run_url(url, cfg, logger, skip_titles, _progress, _failure)
        await _maybe_m3u8(spec, logger)
        return result

    result = await run_url(spec["url"], cfg, logger, skip_titles, _progress, _failure)
    await _maybe_m3u8(spec, logger)
    return result


async def _maybe_m3u8(spec: dict, logger: logging.Logger) -> None:
    """Auto-build an .m3u8 for playlist downloads (SpotiFLAC lives here)."""
    if not spec.get("want_m3u8"):
        return
    from SpotiFLAC.providers.spotify_metadata import parse_spotify_url
    from m3u8 import build_m3u8

    url = spec.get("url")
    if url and parse_spotify_url(url).get("type") == "playlist":
        try:
            result = await build_m3u8(url, cfg=spec.get("cfg") or {})
            _emit({"event": "m3u8", "result": result})
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

        return await fix_library(
            spec["folder"], apply=True, progress=progress, lyrics=bool(spec.get("lyrics")),
        )

    raise ValueError(f"unknown command type: {cmd}")


_COMMANDS = {"m3u8", "fix_metadata"}


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
    except Exception:
        logger.exception("Job crashed")
        _emit({"event": "result", "result": {"ok": 0, "skipped": 0, "failed": 1,
               "failed_tracks": [("", "job_crash", "internal_error")], "total": 1}})
        return 1

    _emit({"event": "result", "result": result.to_dict()})
    return 0


if __name__ == "__main__":
    sys.exit(main())