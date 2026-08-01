#!/usr/bin/env python3
"""Rename FLAC files from SpotiFLAC's _-paths to original-symbols paths.

SpotiFLAC replaces <>:"/\\|?* with _ in filenames and directories.
On Linux ext4, only / and \\0 are forbidden — all other characters are valid.

This script finds all FLACs under ~/Music, reads their Vorbis tags,
reconstructs the original-symbols path, and renames files/directories accordingly.

Usage:
    python fix_original_filenames.py          # real rename
    python fix_original_filenames.py --dry-run  # preview only
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mutagen.flac import FLAC

from config import load_config
from flac_utils import iter_flacs
from track_utils import sanitize

log = logging.getLogger("fix_original_filenames")


def target_rel_path(audio: FLAC, cfg: dict) -> str | None:
    artist = audio.get("ARTIST", [None])[0]
    if cfg["first_artist_only"] and artist:
        first = artist.split(";")[0].split(",")[0].strip()
        artist = first or artist
    album_artist = audio.get("ALBUMARTIST", [None])[0] or audio.get("ALBUM ARTIST", [None])[0]
    album = audio.get("ALBUM", [None])[0]
    title = audio.get("TITLE", [None])[0]
    if not all([artist, album_artist, album, title]):
        return None
    artist_s = sanitize(artist)
    album_artist_s = sanitize(album_artist)
    album_s = sanitize(album)
    title_s = sanitize(title)
    filename = cfg["filename_format"].format(artist=artist_s, title=title_s)
    return str(Path(album_artist_s) / album_s / f"{filename}.flac")


def main(dry_run: bool = False):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config(log)
    log.info("Config: output_dir=%s, filename_format=%s, first_artist_only=%s",
             cfg["output_dir"], cfg["filename_format"], cfg["first_artist_only"])

    total = 0
    renamed = 0
    skipped = 0
    errors = 0

    for fpath in iter_flacs(cfg["output_dir"]):
        total += 1

        try:
            audio = FLAC(str(fpath))
        except Exception as e:
            log.warning("  SKIP (read error) %s — %s", fpath, e)
            skipped += 1
            continue

        rel = target_rel_path(audio, cfg)
        if rel is None:
            skipped += 1
            continue

        target = Path(cfg["output_dir"]) / rel
        if fpath == target:
            continue

        if dry_run:
            log.info("  WOULD RENAME\n    %s\n    -> %s", fpath, target)
            renamed += 1
            continue

        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.rename(fpath, target)
            log.info("  RENAMED\n    %s\n    -> %s", fpath, target)
            renamed += 1

            parent = os.path.dirname(fpath)
            while parent != cfg["output_dir"]:
                try:
                    os.rmdir(parent)
                except OSError:
                    break
                parent = os.path.dirname(parent)
        except Exception as e:
            log.error("  FAIL %s — %s", fpath, e)
            errors += 1

    log.info("")
    log.info("Done: %d files, %d renamed, %d skipped, %d errors (dry_run=%s)",
             total, renamed, skipped, errors, dry_run)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    main(dry_run=dry)
