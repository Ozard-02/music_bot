import asyncio
import logging
import os
import re
from pathlib import Path

from mutagen.flac import FLAC, Picture

from config import load_config
from track_utils import _get_jpeg_dimensions

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("retag_missing")

MAX_CONCURRENT = 5


TAGLESS_FILES = [
    "/home/espo/Music/Jovanotti/Lorenzo 2015 CC./Jovanotti - Libera.flac",
    "/home/espo/Music/Jovanotti/Lorenzo 2015 CC./Jovanotti - Una Scintilla.flac",
    "/home/espo/Music/Prozac+/Acidoacida/Prozac+ - Piango.flac",
    "/home/espo/Music/La Crème/L'Alba/La Crème - Barre pt.2.flac",
]


def _guess_metadata(filepath: str) -> dict | None:
    rel = os.path.relpath(filepath, "/home/espo/Music")
    parts = rel.replace("\\", "/").split("/")
    if len(parts) < 2:
        return None
    fname = os.path.splitext(parts[-1])[0]
    album = parts[-2] if len(parts) >= 2 else ""
    artist = parts[-3] if len(parts) >= 3 else parts[-2]

    if " - " in fname:
        a, _, t = fname.partition(" - ")
        return {"artist": a.strip(), "title": t.strip(), "album": album, "dir_artist": artist}
    return {"artist": artist, "title": fname, "album": album, "dir_artist": artist}


async def fix_tagless():
    from SpotiFLAC.client import SpotifyMetadataClient
    from SpotiFLAC.core.tagger import embed_metadata_async, EmbedOptions

    cfg = load_config(log)
    spotify = SpotifyMetadataClient()
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    ok = 0
    failed = 0

    async def process(fpath: str):
        nonlocal ok, failed
        async with sem:
            meta = _guess_metadata(fpath)
            if not meta:
                log.warning("  SKIP %s — cannot guess metadata", Path(fpath).name)
                failed += 1
                return

            query = f"{meta['artist']} {meta['title']}"
            try:
                tracks = await spotify.search_tracks_async(query, limit=5)
            except Exception as e:
                log.warning("  SKIP %s — search error: %s", Path(fpath).name, e)
                failed += 1
                return

            if not tracks:
                log.warning("  SKIP %s — no Spotify results", Path(fpath).name)
                failed += 1
                return

            track = tracks[0]
            for t in tracks:
                if meta["artist"].lower() in t.artists.lower():
                    track = t
                    break

            try:
                track_full = await spotify.get_track_async(track.id)
            except Exception as e:
                log.warning("  SKIP %s — get_track error: %s", Path(fpath).name, e)
                failed += 1
                return

            try:
                await embed_metadata_async(
                    filepath=fpath,
                    metadata=track_full,
                    opts=EmbedOptions(
                        enrich=True,
                        enrich_providers=["apple", "deezer", "soundcloud"],
                        embed_lyrics=False,
                        cover_url=track_full.cover_url or "",
                    ),
                )
                log.info("  OK %s — %s / %s", Path(fpath).name, track_full.artists, track_full.title)
                ok += 1
            except Exception as e:
                log.warning("  FAIL %s — %s", Path(fpath).name, e)
                failed += 1

    tasks = [process(f) for f in TAGLESS_FILES]
    await asyncio.gather(*tasks)
    return ok, failed


async def fix_rosolo_cover():
    fpath = "/home/espo/Music/Rosolo Roso/Rosolo Rosa/Rosolo Roso - Rosolo Rosa.flac"
    try:
        audio = FLAC(fpath)
        pics = audio.pictures
        if not pics:
            log.info("  SKIP Rosolo — no cover picture found")
            return 0, 0

        p = pics[0]
        if p.width > 0 and p.height > 0:
            log.info("  OK Rosolo — cover already has dimensions (%dx%d)", p.width, p.height)
            return 1, 0

        w, h = _get_jpeg_dimensions(p.data)
        if w == 0 or h == 0:
            log.warning("  FAIL Rosolo — could not parse JPEG dimensions")
            return 0, 1

        p.width = w
        p.height = h
        audio.save()
        log.info("  OK Rosolo — cover dimensions set to %dx%d", w, h)
        return 1, 0
    except Exception as e:
        log.warning("  FAIL Rosolo — %s", e)
        return 0, 1


async def main():
    log.info("=== Fixing 4 tagless files ===")
    ok1, fail1 = await fix_tagless()

    log.info("")
    log.info("=== Fixing Rosolo Roso cover dimensions ===")
    ok2, fail2 = await fix_rosolo_cover()

    log.info("")
    log.info("=== Results ===")
    log.info("  Tagless files: %d OK, %d failed", ok1, fail1)
    log.info("  Rosolo cover:  %d OK, %d failed", ok2, fail2)


if __name__ == "__main__":
    asyncio.run(main())
