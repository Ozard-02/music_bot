#!/usr/bin/env python3
"""Re-embed Spotify cover art into existing FLACs with correct dimensions.

Reads the Spotify track URL from each FLAC's URL tag, fetches the cover
from Spotify's CDN, and embeds it with proper JPEG width/height so
Navidrome displays it correctly.
"""

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from mutagen.flac import FLAC, Picture

from config import load_config


def _get_jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if data[:2] != b"\xff\xd8":
        return (0, 0)
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            if i + 10 > len(data):
                break
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return (w, h)
        if marker in (0xD9,):
            break
        seg_len = ((data[i + 2] << 8) | data[i + 3]) & 0xFFFF
        i += 2 + seg_len
    return (0, 0)

MAX_CONCURRENT = 5

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("fix_covers")

_TRACK_ID_RE = re.compile(r"open\.spotify\.com/track/([a-zA-Z0-9]+)")


def _get_spotify_id(filepath: str) -> str | None:
    audio = FLAC(filepath)
    for tag in ("URL", "comment"):
        val = audio.get(tag, [None])[0]
        if val:
            m = _TRACK_ID_RE.search(val)
            if m:
                return m.group(1)
    return None


async def fix_covers(dry_run: bool = False):
    from SpotiFLAC.client import SpotifyMetadataClient
    from SpotiFLAC.core.progress import ProgressManager
    ProgressManager._event_queue = None
    ProgressManager._worker_task = None

    cfg = load_config(log)
    output_dir = cfg["output_dir"]

    flacs = []
    for root, _dirs, files in os.walk(output_dir):
        for fn in files:
            if fn.endswith(".flac"):
                fpath = os.path.join(root, fn)
                sid = _get_spotify_id(fpath)
                if sid:
                    flacs.append((fpath, sid))

    log.info("Found %d FLACs with Spotify track IDs", len(flacs))

    spotify = SpotifyMetadataClient()

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    ok = 0
    failed = 0

    async def process(fpath: str, sid: str):
        nonlocal ok, failed
        async with sem:
            try:
                track = await spotify.get_track_async(sid)
                if not track:
                    log.warning("  SKIP %s — no track metadata", Path(fpath).name)
                    failed += 1
                    return

                cover_url = track.cover_url
                if not cover_url:
                    log.warning("  SKIP %s — no cover URL from Spotify", Path(fpath).name)
                    failed += 1
                    return

                if dry_run:
                    log.info("  WOULD FIX %s — %s (spotify 300)", Path(fpath).name, track.album or "?")
                    ok += 1
                    return

                import httpx
                async with httpx.AsyncClient(timeout=10) as http:
                    resp = await http.get(cover_url)
                    if resp.status_code != 200:
                        log.warning("  SKIP %s — HTTP %d", Path(fpath).name, resp.status_code)
                        failed += 1
                        return

                    audio = FLAC(fpath)
                    pic = Picture()
                    pic.data = resp.content
                    pic.type = 3
                    pic.mime = "image/jpeg"
                    pic.width, pic.height = _get_jpeg_dimensions(resp.content)
                    pic.depth = 0
                    audio.clear_pictures()
                    audio.add_picture(pic)
                    audio.save()
                    log.info("  OK %s — %s (%s bytes)", Path(fpath).name, track.album or "?", len(resp.content))

                ok += 1

            except Exception as e:
                log.warning("  FAIL %s — %s", Path(fpath).name, e)
                failed += 1

    tasks = [process(fpath, sid) for fpath, sid in flacs]
    await asyncio.gather(*tasks)

    log.info("")
    log.info("Done: %d OK, %d failed (dry_run=%s)", ok, failed, dry_run)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    asyncio.run(fix_covers(dry_run=dry))
