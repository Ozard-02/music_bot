#!/usr/bin/env python3
"""Re-embed correct cover art from Spotify into existing FLACs.

Reads the Spotify track URL from each FLAC's URL tag, fetches the correct
album art from Spotify, and replaces whatever Qobuz enrichment put there.
"""

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import httpx
from mutagen.flac import FLAC, Picture

MUSIC_DIR = os.path.expanduser("~/Music")
MAX_CONCURRENT = 10

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

    flacs = []
    for root, _dirs, files in os.walk(MUSIC_DIR):
        for fn in files:
            if fn.endswith(".flac"):
                fpath = os.path.join(root, fn)
                sid = _get_spotify_id(fpath)
                if sid:
                    flacs.append((fpath, sid))

    log.info("Found %d FLACs with Spotify track IDs", len(flacs))

    spotify = SpotifyMetadataClient()
    http = httpx.AsyncClient(timeout=10)

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    ok = 0
    failed = 0

    async def process(fpath: str, sid: str):
        nonlocal ok, failed
        async with sem:
            try:
                track = await spotify.get_track_async(sid)
                if not track or not track.cover_url:
                    log.warning("  SKIP %s — no cover URL", Path(fpath).name)
                    failed += 1
                    return

                if dry_run:
                    log.info("  WOULD FIX %s", Path(fpath).name)
                    ok += 1
                    return

                cover_url = track.cover_url

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
                pic.width = 0
                pic.height = 0
                pic.depth = 0
                audio.clear_pictures()
                audio.add_picture(pic)
                audio.save()
                log.info("  OK %s — %s", Path(fpath).name, track.album or "?")
                ok += 1

            except Exception as e:
                log.warning("  FAIL %s — %s", Path(fpath).name, e)
                failed += 1

    try:
        tasks = [process(fpath, sid) for fpath, sid in flacs]
        await asyncio.gather(*tasks)
    finally:
        await http.aclose()

    log.info("")
    log.info("Done: %d OK, %d failed (dry_run=%s)", ok, failed, dry_run)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    asyncio.run(fix_covers(dry_run=dry))
