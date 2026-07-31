#!/usr/bin/env python3
"""Re-tag FLAC files via the SpotiFLAC metadata pipeline.

Fixes the "one album name split into multiple Navidrome albums" problem:
re-embedding through SpotiFLAC deletes the old tags, strips every
MUSICBRAINZ_* ID (which came from Qobuz enrichment and were often bogus,
causing Navidrome to group the same album into several releases) and writes
clean Spotify metadata with Apple-first enrichment (Deezer/SoundCloud as
fallbacks).

Files whose real album (from Spotify) differs from the album folder they sit
in are moved into the folder of their real album. Nothing is ever deleted.

Examples:
    python fix_metadata.py Albums/MADAME --dry-run
    python fix_metadata.py Albums/MADAME --apply
    python fix_metadata.py /mnt/server/files/Albums --apply   # whole library
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from mutagen.flac import FLAC

from config import SCRIPT_MAX_CONCURRENT as MAX_CONCURRENT
from track_utils import sanitize

log = logging.getLogger("fix_metadata")

_TRACK_ID_RE = re.compile(r"open\.spotify\.com/track/([a-zA-Z0-9]+)")

# Provider order = priority. Apple first, then Deezer, then SoundCloud.
ENRICH_PROVIDERS = ["apple", "deezer", "soundcloud"]


def _silence_spotiflac_loggers() -> None:
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("SpotiFLAC"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _get_spotify_id(filepath: str | Path) -> str | None:
    try:
        audio = FLAC(str(filepath))
    except Exception:
        return None
    for tag in ("URL", "comment"):
        val = audio.get(tag, [None])[0]
        if val:
            m = _TRACK_ID_RE.search(val)
            if m:
                return m.group(1)
    return None


def _guess_metadata(filepath: str | Path) -> tuple[str, str] | None:
    """Guess (artist, title) from a '<Artist> - <Title>.flac' filename."""
    name = Path(filepath).stem
    if " - " in name:
        artist, _, title = name.partition(" - ")
        if artist.strip() and title.strip():
            return artist.strip(), title.strip()
    return None


def _read_existing(filepath: str | Path) -> dict[str, str]:
    try:
        audio = FLAC(str(filepath))
        return {
            "album": (audio.get("ALBUM", [""])[0] or "").strip(),
            "albumartist": (audio.get("ALBUMARTIST", [""])[0] or "").strip(),
        }
    except Exception:
        return {"album": "", "albumartist": ""}


def _majority(values: list[str]) -> str:
    counts: dict[str, int] = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _is_foreign(track, folder: Path, majority_album: str) -> bool:
    """True if the track's real album doesn't belong to this album folder."""
    album = (getattr(track, "album", "") or "").strip().lower()
    if not album:
        return False

    candidates = {folder.name.lower()}
    if " - " in folder.name:
        candidates.add(folder.name.split(" - ", 1)[1].strip().lower())
    if album in candidates:
        return False
    if majority_album and album == majority_album.strip().lower():
        return False
    return True


def _move_file(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{src.stem} ({n}){src.suffix}"
        n += 1
    os.replace(src, dest)
    return dest


async def fix_album_folder(
    folder: Path,
    *,
    apply: bool = False,
    progress=None,
    logger: logging.Logger | None = None,
) -> dict:
    """Re-tag every FLAC directly inside `folder`."""
    llog = logger or log
    folder = Path(folder)
    flacs = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".flac")
    total = len(flacs)
    if total == 0:
        return {"total": 0, "fixed": 0, "moved": 0, "failed": 0, "skipped": 0,
                "would_fix": 0, "details": [], "moved_files": [], "failed_files": []}

    from SpotiFLAC.client import SpotifyMetadataClient
    from SpotiFLAC.core.progress import ProgressManager
    from SpotiFLAC.core.tagger import embed_metadata_async, EmbedOptions

    ProgressManager._event_queue = None
    ProgressManager._worker_task = None

    majority_album = _majority([_read_existing(f)["album"] for f in flacs])
    if apply:
        llog.info("Folder %s — majority album: %r", folder.name, majority_album or "(none)")

    spotify = SpotifyMetadataClient()
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    res = {
        "total": total,
        "fixed": 0,
        "moved": 0,
        "failed": 0,
        "skipped": 0,
        "would_fix": 0,
        "details": [],
        "moved_files": [],
        "failed_files": [],
    }

    async def process(fpath: Path) -> None:
        async with sem:
            try:
                sid = _get_spotify_id(fpath)
                if sid:
                    track = await spotify.get_track_async(sid)
                else:
                    guess = _guess_metadata(fpath)
                    if not guess:
                        res["skipped"] += 1
                        res["details"].append(f"SKIP {fpath.name} — cannot resolve identity")
                        return
                    artist, title = guess
                    tracks = await spotify.search_tracks_async(f"{artist} {title}", limit=5)
                    if not tracks:
                        res["skipped"] += 1
                        res["details"].append(f"SKIP {fpath.name} — no Spotify match")
                        return
                    track = tracks[0]
                    for t in tracks:
                        if artist.lower() in t.artists.lower():
                            track = t
                            break

                if not track or not getattr(track, "id", None):
                    res["failed"] += 1
                    res["failed_files"].append(fpath.name)
                    res["details"].append(f"FAIL {fpath.name} — no track metadata")
                    return

                if apply:
                    await embed_metadata_async(
                        filepath=str(fpath),
                        metadata=track,
                        opts=EmbedOptions(
                            enrich=True,
                            enrich_providers=list(ENRICH_PROVIDERS),
                            embed_lyrics=False,
                            cover_url=track.cover_url or "",
                        ),
                    )
                    res["fixed"] += 1
                else:
                    res["would_fix"] += 1

                res["details"].append(
                    f"OK   {fpath.name} — {track.album} / {track.album_artist}"
                )

                if _is_foreign(track, folder, majority_album):
                    target_dir = folder.parent / sanitize(track.album, "Unknown Album")
                    if target_dir.resolve() != folder.resolve():
                        res["details"].append(
                            f"MOVED {fpath.name} -> {target_dir.name}/"
                        )
                        if apply:
                            new_path = _move_file(fpath, target_dir)
                            res["moved"] += 1
                            res["moved_files"].append(str(new_path))
                        else:
                            res["details"].append(
                                f"  (dry-run) would move into {target_dir.name}/"
                            )
                    else:
                        res["details"].append(f"  (stays in {folder.name}/)")

            except Exception as exc:
                res["failed"] += 1
                res["failed_files"].append(fpath.name)
                res["details"].append(f"FAIL {fpath.name} — {exc}")
                llog.warning("  FAIL %s — %s", fpath.name, exc)

        done = res["fixed"] + res["failed"] + res["skipped"]
        if progress and total:
            await progress(done, total, fpath.name)

    await asyncio.gather(*[process(f) for f in flacs])

    if progress and total:
        await progress(total, total, "Done")
    llog.info(
        "Folder %s — %d files: %d fixed, %d moved, %d failed, %d skipped",
        folder.name, total, res["fixed"], res["moved"], res["failed"], res["skipped"],
    )
    return res


async def fix_library(
    root: Path,
    *,
    apply: bool = False,
    progress=None,
    logger: logging.Logger | None = None,
) -> dict:
    """Walk `root` and re-tag every album folder (dir containing FLACs)."""
    llog = logger or log
    root = Path(root)
    folders = [d for d in [root, *root.rglob("*")] if d.is_dir()]
    todo = [d for d in folders if any(p.suffix.lower() == ".flac" for p in d.iterdir())]

    if not todo:
        llog.info("No FLAC folders found under %s", root)
        return {"folders": 0, "total": 0, "fixed": 0, "moved": 0,
                "failed": 0, "skipped": 0, "would_fix": 0,
                "details": [], "moved_files": [], "failed_files": []}

    agg = {"folders": len(todo), "total": 0, "fixed": 0, "moved": 0,
           "failed": 0, "skipped": 0, "would_fix": 0,
           "details": [], "moved_files": [], "failed_files": []}

    for i, folder in enumerate(todo, start=1):
        if progress:
            await progress(i, len(todo), f"Folder: {folder.relative_to(root)}")
        sub = await fix_album_folder(
            folder, apply=apply, progress=None, logger=llog
        )
        for key in ("total", "fixed", "moved", "failed", "skipped", "would_fix"):
            agg[key] += sub[key]
        agg["details"].extend(sub["details"])
        agg["moved_files"].extend(sub["moved_files"])
        agg["failed_files"].extend(sub["failed_files"])

    if progress:
        await progress(len(todo), len(todo), "Done")
    return agg


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-tag FLAC metadata via SpotiFLAC (Apple-first enrichment)."
    )
    parser.add_argument("folder", help="Album folder or library root to process")
    parser.add_argument(
        "--apply", action="store_true",
        help="Write changes. Default is a read-only dry run.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _silence_spotiflac_loggers()

    target = Path(args.folder).expanduser()
    if not target.is_dir():
        log.error("Not a directory: %s", target)
        sys.exit(1)

    mode = "APPLY" if args.apply else "DRY-RUN"
    log.info("=== fix_metadata %s: %s ===", mode, target)

    if args.apply:
        res = await fix_library(target, apply=True, logger=log)
    else:
        res = await fix_library(target, apply=False, logger=log)

    log.info("")
    log.info("=== Results ===")
    for key in ("folders", "total", "fixed", "would_fix", "moved", "failed", "skipped"):
        if key in res:
            log.info("  %s: %d", key, res[key])
    if res["details"]:
        log.info("")
        log.info("  Per-file plan:")
        for d in res["details"]:
            log.info("    %s", d)
    if res["failed_files"]:
        log.info("")
        log.info("  Failed files:")
        for f in res["failed_files"]:
            log.info("    %s", f)
    if res["moved_files"]:
        log.info("")
        log.info("  Moved files:")
        for f in res["moved_files"]:
            log.info("    %s", f)


if __name__ == "__main__":
    asyncio.run(_main())
