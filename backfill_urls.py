import asyncio
import logging
import os
import re
from pathlib import Path

from mutagen.flac import FLAC

from config import load_config

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill_urls")

MAX_CONCURRENT = 5


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


def _find_no_url_flacs(output_dir: str) -> list[tuple[str, str, str, str]]:
    results = []
    for root, _dirs, files in os.walk(output_dir):
        for fn in files:
            if not fn.endswith(".flac"):
                continue
            fpath = os.path.join(root, fn)
            try:
                audio = FLAC(fpath)
            except Exception:
                continue

            tags = {k.lower(): v for k, v in audio.items()}
            artist = (tags.get("artist") or [None])[0]
            title = (tags.get("title") or [None])[0]
            album = (tags.get("album") or [None])[0]
            url = (tags.get("url") or [None])[0]

            has_tags = artist and title and album
            has_spotify_url = url and "open.spotify.com/track/" in url

            if has_tags and not has_spotify_url:
                results.append((fpath, artist, title, album))

    return results


async def backfill():
    from SpotiFLAC.client import SpotifyMetadataClient

    cfg = load_config(log)
    output_dir = cfg["output_dir"]

    flacs = _find_no_url_flacs(output_dir)
    log.info("Found %d FLACs without Spotify URL", len(flacs))
    if not flacs:
        log.info("Nothing to do.")
        return

    spotify = SpotifyMetadataClient()
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    url_written = 0
    cover_fixed = 0
    skipped = 0
    failed = 0

    async def process(fpath: str, artist: str, title: str, album: str):
        nonlocal url_written, cover_fixed, skipped, failed
        async with sem:
            query = f"{artist} {title}"
            try:
                tracks = await spotify.search_tracks_async(query, limit=5)
            except Exception as e:
                log.warning("  FAIL %s — search error: %s", Path(fpath).name, e)
                failed += 1
                return

            if not tracks:
                skipped += 1
                return

            track = None
            artist_lc = artist.lower().strip()
            for t in tracks:
                t_artists = t.artists.lower()
                if artist_lc in t_artists or t.title.lower() == title.lower():
                    track = t
                    break
            if not track:
                track = tracks[0]

            try:
                track_full = await spotify.get_track_async(track.id)
            except Exception as e:
                log.warning("  FAIL %s — get_track error: %s", Path(fpath).name, e)
                failed += 1
                return

            spotify_url = track_full.external_url or f"https://open.spotify.com/track/{track.id}"

            audio = FLAC(fpath)
            audio["url"] = spotify_url
            audio.save()
            url_written += 1

            pics = audio.pictures
            if pics:
                p = pics[0]
                if p.width == 0 or p.height == 0:
                    w, h = _get_jpeg_dimensions(p.data)
                    if w > 0 and h > 0:
                        p.width = w
                        p.height = h
                        audio.save()
                        cover_fixed += 1

            log.info("  OK %s — %s", Path(fpath).name, track_full.title)

    tasks = [process(f, a, t, al) for f, a, t, al in flacs]
    await asyncio.gather(*tasks)

    log.info("")
    log.info("=== Results ===")
    log.info("  URL written:    %d", url_written)
    log.info("  Cover dims fixed: %d", cover_fixed)
    log.info("  Skipped (no match): %d", skipped)
    log.info("  Failed:         %d", failed)


if __name__ == "__main__":
    asyncio.run(backfill())
