"""Library maintenance: re-embed Spotify cover art into existing FLACs.

Used by the bot's /rescan command and the fix_covers.py CLI script.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from config import SCRIPT_MAX_CONCURRENT
from flac_utils import embed_cover, fetch_cover, get_spotify_id_from_file
from spotiflac_patch import reset_progress_manager


async def rescan_library(
    cfg: dict,
    logger: logging.Logger,
    progress=None,
    dry_run: bool = False,
) -> dict:
    from SpotiFLAC.client import SpotifyMetadataClient

    reset_progress_manager()

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
                sid = get_spotify_id_from_file(fpath)
                if not sid:
                    skipped += 1
                    return

                track = await spotify.get_track_async(sid)
                if not track or not track.cover_url:
                    skipped += 1
                    return

                data = await fetch_cover(track.cover_url)
                if data is None:
                    failed += 1
                    return

                if not dry_run:
                    await asyncio.to_thread(embed_cover, fpath, data)
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
