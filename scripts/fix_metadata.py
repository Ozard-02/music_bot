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
    python scripts/fix_metadata.py Albums/MADAME --dry-run
    python scripts/fix_metadata.py Albums/MADAME --apply
    python scripts/fix_metadata.py Albums/MADAME --apply --lyrics   # also fetch lyrics
    python scripts/fix_metadata.py /mnt/server/files/Albums --apply   # whole library
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mutagen.flac import FLAC

from config import SCRIPT_MAX_CONCURRENT as MAX_CONCURRENT
from flac_utils import get_spotify_id_from_file, resolve_cover_data
from resolver import best_track_match
from spotiflac_patch import reset_progress_manager, silence_spotiflac_loggers
from track_utils import sanitize

log = logging.getLogger("fix_metadata")

# Provider order = priority. Apple first, then Deezer, then SoundCloud.
ENRICH_PROVIDERS = ["apple", "deezer", "soundcloud"]


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


def _read_lyrics(filepath: str | Path) -> str:
    try:
        audio = FLAC(str(filepath))
        for tag in ("LYRICS", "UNSYNCEDLYRICS"):
            val = audio.get(tag, [""])[0] if audio.get(tag) else ""
            if val and val.strip():
                return val
    except Exception:
        pass
    return ""


def _write_lyrics(filepath: str | Path, text: str) -> None:
    if not text or not text.strip():
        return
    audio = FLAC(str(filepath))
    audio["LYRICS"] = text
    audio.save()


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
    lyrics: bool = False,
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
    from SpotiFLAC.core.tagger import embed_metadata_async, EmbedOptions

    reset_progress_manager()

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
                sid = get_spotify_id_from_file(fpath)
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
                    track = best_track_match(tracks, artist)
                if not track or not getattr(track, "id", None):
                    res["failed"] += 1
                    res["failed_files"].append(fpath.name)
                    res["details"].append(f"FAIL {fpath.name} — no track metadata")
                    return

                if apply:
                    old_lyrics = _read_lyrics(fpath)
                    cover_data = await resolve_cover_data(track)
                    await embed_metadata_async(
                        filepath=str(fpath),
                        metadata=track,
                        opts=EmbedOptions(
                            enrich=True,
                            enrich_providers=list(ENRICH_PROVIDERS),
                            embed_lyrics=lyrics,
                            cover_url=track.cover_url or "",
                        ),
                        cover_data=cover_data,
                    )
                    res["fixed"] += 1
                else:
                    res["would_fix"] += 1

                res["details"].append(
                    f"OK   {fpath.name} — {track.album} / {track.album_artist}"
                )

                final_path = fpath
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
                            final_path = new_path
                        else:
                            res["details"].append(
                                f"  (dry-run) would move into {target_dir.name}/"
                            )
                    else:
                        res["details"].append(f"  (stays in {folder.name}/)")

                if apply and old_lyrics and not _read_lyrics(final_path):
                    _write_lyrics(final_path, old_lyrics)

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
    lyrics: bool = False,
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
            folder, apply=apply, progress=None, logger=llog, lyrics=lyrics
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
    parser.add_argument(
        "--lyrics", action="store_true",
        help="Also fetch and embed lyrics (slow). Default keeps existing lyrics as-is.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    silence_spotiflac_loggers()

    target = Path(args.folder).expanduser()
    if not target.is_dir():
        log.error("Not a directory: %s", target)
        sys.exit(1)

    mode = "APPLY" if args.apply else "DRY-RUN"
    log.info("=== fix_metadata %s: %s ===", mode, target)

    res = await fix_library(target, apply=args.apply, logger=log, lyrics=args.lyrics)

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
