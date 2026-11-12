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

from config import SCRIPT_MAX_CONCURRENT as MAX_CONCURRENT, silence_spotiflac_loggers
from flac_utils import embed_cover, get_spotify_id_from_file, read_lrc, resolve_cover_data, write_lrc_sidecar
from resolver import best_track_match
from spotiflac_patch import reset_progress_manager
from track_utils import sanitize

log = logging.getLogger("fix_metadata")

# Provider order = priority. Apple first, then Deezer, then SoundCloud.
ENRICH_PROVIDERS = ["apple", "deezer", "soundcloud"]

# Aggregate result lists are only used for CLI/human output; counts stay
# exact. Cap them so a whole-library rescan cannot build an unbounded payload.
_MAX_AGG_LIST = 10_000


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
    # os.rename fails if a target appeared after the dedup loop (instead of
    # os.replace silently overwriting it) — data-loss guard, see deletion audit.
    os.rename(src, dest)
    return dest


async def fix_album_folder(
    folder: Path,
    *,
    apply: bool = False,
    progress=None,
    logger: logging.Logger | None = None,
    lyrics: bool = False,
    lyrics_providers: list[str] | None = None,
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
                    track = best_track_match(tracks, artist, title)
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
                            lyrics_providers=list(lyrics_providers or []),
                            cover_url=track.cover_url or "",
                        ),
                        cover_data=cover_data,
                    )
                    if cover_data:
                        # SpotiFLAC's tagger appends pictures instead of replacing
                        # them, so old (possibly wrong) covers survive.  Force a
                        # single clean cover.
                        await asyncio.to_thread(embed_cover, fpath, cover_data)
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

                if lyrics:
                    lrc = read_lrc(final_path)
                    if lrc:
                        write_lrc_sidecar(final_path, lrc)

            except Exception as exc:
                res["failed"] += 1
                res["failed_files"].append(fpath.name)
                res["details"].append(f"FAIL {fpath.name} — {exc}")
                llog.warning("  FAIL %s — %s", fpath.name, exc)

        done = res["fixed"] + res["failed"] + res["skipped"] + res["would_fix"]
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
    lyrics_providers: list[str] | None = None,
    max_folders: int = 2,
) -> dict:
    """Walk `root` and re-tag every album folder (dir containing FLACs).

    Never raises: an unreadable folder or a failed folder is reported in the
    result (`failed` / `error`) so the bot's command message never dies with
    a KeyError on a missing result key.
    """
    llog = logger or log
    root = Path(root)
    agg = {"folders": 0, "total": 0, "fixed": 0, "moved": 0,
           "failed": 0, "skipped": 0, "would_fix": 0,
           "details": [], "moved_files": [], "failed_files": []}

    try:
        folders = [d for d in [root, *root.rglob("*")] if d.is_dir()]
        todo = []
        for d in folders:
            try:
                if any(p.suffix.lower() == ".flac" for p in d.iterdir()):
                    todo.append(d)
            except OSError as exc:
                llog.warning("Skipping unreadable folder %s: %s", d, exc)
    except Exception as exc:
        llog.exception("Failed to scan %s", root)
        agg["error"] = str(exc)
        return agg

    if not todo:
        llog.info("No FLAC folders found under %s", root)
        return agg

    agg["folders"] = len(todo)
    reset_progress_manager()  # once, before any concurrent folder starts

    sem = asyncio.Semaphore(max(1, max_folders))
    agg_lock = asyncio.Lock()
    completed = 0

    async def process_folder(folder: Path) -> None:
        nonlocal completed
        async with sem:
            if progress:
                try:
                    await progress(0, len(todo), f"Folder: {folder.relative_to(root)}")
                except Exception as exc:
                    llog.warning("Progress update failed: %s", exc)
            try:
                sub = await fix_album_folder(
                    folder, apply=apply, progress=None, logger=llog,
                    lyrics=lyrics, lyrics_providers=lyrics_providers,
                )
            except Exception as exc:
                llog.exception("Folder %s failed", folder)
                sub = {
                    "total": 0, "fixed": 0, "moved": 0, "failed": 1,
                    "skipped": 0, "would_fix": 0, "details": [],
                    "moved_files": [], "failed_files": [str(folder)],
                }
                async with agg_lock:
                    agg["failed"] += 1
                    agg["failed_files"].append(str(folder))
                    agg["details"].append(f"FAIL {folder} — {exc}")
            else:
                async with agg_lock:
                    for key in ("total", "fixed", "moved", "failed", "skipped", "would_fix"):
                        agg[key] += sub[key]
                    agg["details"].extend(sub["details"])
                    agg["moved_files"].extend(sub["moved_files"])
                    agg["failed_files"].extend(sub["failed_files"])
                    for key in ("details", "moved_files", "failed_files"):
                        if len(agg[key]) > _MAX_AGG_LIST:
                            del agg[key][:-_MAX_AGG_LIST]
            async with agg_lock:
                completed += 1
                done = completed
            if progress:
                try:
                    await progress(done, len(todo), f"Folder: {folder.relative_to(root)}")
                except Exception as exc:
                    llog.warning("Progress update failed: %s", exc)

    await asyncio.gather(*[process_folder(f) for f in todo])

    if progress:
        try:
            await progress(len(todo), len(todo), "Done")
        except Exception as exc:
            llog.warning("Progress update failed: %s", exc)
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
    parser.add_argument(
        "--parallel", type=int, default=2, metavar="N",
        help="Album folders processed concurrently (default: 2).",
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

    res = await fix_library(
        target, apply=args.apply, logger=log, lyrics=args.lyrics,
        max_folders=args.parallel,
    )

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
