#!/usr/bin/env python3
"""Fetch/refresh lyrics for existing FLACs without re-tagging metadata.

Unlike /fixmetadata --lyrics, this never changes tags other than LYRICS and
never moves files. It:
  * keeps existing synced lyrics unless --force,
  * tries to upgrade existing unsynced lyrics (never downgrading them),
  * backfills a .lrc sidecar from already-embedded timestamped lyrics,
  * fetches missing lyrics through SpotiFLAC's provider chain otherwise.

Examples:
    python scripts/sync_lyrics.py ~/Music --apply
    python scripts/sync_lyrics.py ~/Music/Album --apply --force
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mutagen.flac import FLAC

from config import SCRIPT_MAX_CONCURRENT
from flac_utils import (
    get_spotify_id_from_file,
    has_lrc_timestamps,
    iter_flacs,
    read_lrc,
    write_lrc_sidecar,
)
from scripts.fix_metadata import _read_lyrics

log = logging.getLogger("sync_lyrics")


def _write_lyrics(fpath: Path, text: str) -> None:
    if not text or not text.strip():
        return
    audio = FLAC(str(fpath))
    audio["LYRICS"] = text
    audio.save()


async def sync_lyrics(
    root: Path,
    *,
    apply: bool = False,
    progress=None,
    logger: logging.Logger | None = None,
    providers: list[str] | None = None,
    force: bool = False,
) -> dict:
    """Walk `root` and add/refresh lyrics. Never raises."""
    llog = logger or log
    root = Path(root)
    flacs = list(iter_flacs(root))
    total = len(flacs)

    res = {
        "total": total,
        "fixed": 0,
        "sidecars": 0,
        "kept": 0,
        "no_lyrics": 0,
        "skipped": 0,
        "failed": 0,
        "would_fix": 0,
        "details": [],
        "failed_files": [],
    }
    if not flacs:
        return res

    from SpotiFLAC.client import SpotifyMetadataClient
    from SpotiFLAC.core.lyrics import fetch_lyrics_async

    spotify = SpotifyMetadataClient()
    sem = asyncio.Semaphore(SCRIPT_MAX_CONCURRENT)
    done = 0
    lock = asyncio.Lock()

    async def process(fpath: Path) -> None:
        nonlocal done
        async with sem:
            try:
                existing = _read_lyrics(fpath)
                lrc = read_lrc(fpath) if existing else None
                if existing and not force:
                    if lrc:
                        if apply:
                            write_lrc_sidecar(fpath, lrc)
                            res["sidecars"] += 1
                            res["details"].append(f"LRC  {fpath.name}")
                        else:
                            res["kept"] += 1
                            res["details"].append(f"KEEP {fpath.name}")
                        return
                    # Existing lyrics are unsynced: fall through and try to
                    # replace them with a timestamped version.

                sid = get_spotify_id_from_file(fpath)
                if not sid:
                    if existing:
                        res["kept"] += 1
                        res["details"].append(
                            f"KEEP {fpath.name} — unsynced, no Spotify ID"
                        )
                    else:
                        res["skipped"] += 1
                        res["details"].append(f"SKIP {fpath.name} — no Spotify ID")
                    return

                track = await spotify.get_track_async(sid)
                if not track:
                    if existing:
                        res["kept"] += 1
                        res["details"].append(
                            f"KEEP {fpath.name} — unsynced, no track metadata"
                        )
                    else:
                        res["failed"] += 1
                        res["failed_files"].append(fpath.name)
                        res["details"].append(f"FAIL {fpath.name} — no track metadata")
                    return

                lyrics, provider = await fetch_lyrics_async(
                    track_name=track.title,
                    artist_name=track.first_artist,
                    album_name=track.album,
                    duration_s=(track.duration_ms or 0) // 1000,
                    track_id=track.id,
                    isrc=track.isrc or "",
                    providers=providers or None,
                )
                if not lyrics:
                    if existing:
                        res["kept"] += 1
                        res["details"].append(
                            f"KEEP {fpath.name} — unsynced, no replacement found"
                        )
                    else:
                        res["no_lyrics"] += 1
                        res["details"].append(f"NONE {fpath.name}")
                    return

                if existing and not force and not has_lrc_timestamps(lyrics):
                    # Don't replace unsynced lyrics with equally unsynced ones.
                    res["kept"] += 1
                    res["details"].append(
                        f"KEEP {fpath.name} — unsynced, {provider} returned plain lyrics"
                    )
                    return

                if not apply:
                    res["would_fix"] += 1
                    res["details"].append(f"OK   {fpath.name} — {provider} (dry-run)")
                    return

                _write_lyrics(fpath, lyrics)
                res["fixed"] += 1
                res["details"].append(f"OK   {fpath.name} — {provider}")
                lrc = read_lrc(fpath)
                if lrc:
                    write_lrc_sidecar(fpath, lrc)
                    res["sidecars"] += 1
            except Exception as exc:
                res["failed"] += 1
                res["failed_files"].append(fpath.name)
                res["details"].append(f"FAIL {fpath.name} — {exc}")
                llog.warning("  FAIL %s — %s", fpath.name, exc)
            finally:
                async with lock:
                    done += 1
                    current = done
                if progress and total:
                    await progress(current, total, fpath.name)

    await asyncio.gather(*[process(f) for f in flacs])

    if progress:
        await progress(total, total, "Done")
    llog.info(
        "Lyrics: %d fixed, %d sidecars, %d kept, %d none, %d skipped, %d failed",
        res["fixed"], res["sidecars"], res["kept"], res["no_lyrics"],
        res["skipped"], res["failed"],
    )
    return res


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Fetch/refresh lyrics in existing FLACs.")
    parser.add_argument("folder", help="Album folder or library root")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run).")
    parser.add_argument("--force", action="store_true", help="Re-fetch even when lyrics already exist.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    target = Path(args.folder).expanduser()
    if not target.is_dir():
        log.error("Not a directory: %s", target)
        sys.exit(1)

    res = await sync_lyrics(target, apply=args.apply, logger=log, force=args.force)
    for key in ("total", "fixed", "sidecars", "kept", "no_lyrics", "skipped", "failed", "would_fix"):
        log.info("  %s: %d", key, res[key])
    for line in res["details"]:
        log.info("    %s", line)


if __name__ == "__main__":
    asyncio.run(_main())
