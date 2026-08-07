#!/usr/bin/env python3
"""Consolidate the library into the canonical album_artist/album/... layout.

SpotiFLAC writes files under first_artist/album/ with sanitized (char-removed)
names, while this project's canonical layout is album_artist/album/ with
original symbols — so tracks of compilations/special-char albums end up
scattered and the pre-check never sees them.  This script finds every FLAC
via its tags, moves it to the canonical path *inside the user folder it
already lives in* (`{username}_Music` — files never leave their user folder),
and removes only *provable* duplicates (same embedded Spotify ID at both
paths — never anything else).

Usage:
    python scripts/consolidate_library.py           # real consolidation
    python scripts/consolidate_library.py --dry-run # preview only

In docker:  docker compose exec spoty-loop python scripts/consolidate_library.py
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mutagen.flac import FLAC

from config import load_config
from flac_utils import get_spotify_id_from_file, iter_flacs
from scripts.fix_original_filenames import target_rel_path
from track_utils import prune_empty_parents

log = logging.getLogger("consolidate_library")


def _user_root(fpath: Path, output_dir: Path) -> Path:
    """The per-user folder (`{username}_Music`, library.py) containing `fpath`,
    else the output root itself.  Files must never be moved *out* of their
    user folder — each user's library stays self-contained."""
    for anc in fpath.parents:
        if anc.name.endswith("_Music"):
            return anc
    return output_dir


def consolidate(cfg: dict, dry_run: bool = False) -> dict:
    root = Path(cfg["output_dir"])
    moved = deduped = skipped = errors = 0

    for fpath in iter_flacs(root):
        try:
            audio = FLAC(str(fpath))
        except Exception as e:
            log.warning("  SKIP (read error) %s — %s", fpath, e)
            skipped += 1
            continue
        rel = target_rel_path(audio, cfg)
        if rel is None:
            log.warning("  SKIP (unreadable/incomplete tags) %s", fpath)
            skipped += 1
            continue
        user_root = _user_root(fpath, root)
        target = user_root / rel
        if fpath == target:
            continue

        try:
            if not target.exists():
                _move_with_sidecar(fpath, target, user_root, dry_run, log)
                moved += 1
                continue
            # Target exists: remove the duplicate ONLY when both files provably
            # carry the same Spotify track ID; never destroy an unproven file.
            src_id = get_spotify_id_from_file(fpath)
            dst_id = get_spotify_id_from_file(target)
            if src_id and src_id == dst_id:
                if dry_run:
                    log.info("  WOULD REMOVE duplicate\n    %s\n    (same track as %s)", fpath, target)
                else:
                    fpath.unlink()
                    prune_empty_parents(fpath, user_root)
                    log.info("  REMOVED duplicate\n    %s\n    (same track as %s)", fpath, target)
                deduped += 1
            else:
                log.warning("  SKIP (target exists, different/unknown ID)\n    %s\n    -> %s", fpath, target)
                skipped += 1
        except Exception as e:
            log.error("  FAIL %s — %s", fpath, e)
            errors += 1

    return {"moved": moved, "deduped": deduped, "skipped": skipped, "errors": errors}


def _move_with_sidecar(fpath: Path, target: Path, root: Path, dry_run: bool, log) -> None:
    lrc = fpath.with_suffix(".lrc")
    target_lrc = target.with_suffix(".lrc")
    if dry_run:
        log.info("  WOULD RENAME\n    %s\n    -> %s", fpath, target)
        return
    os.makedirs(target.parent, exist_ok=True)
    os.rename(fpath, target)
    if lrc.exists() and not target_lrc.exists():
        os.rename(lrc, target_lrc)
    prune_empty_parents(fpath, root)
    log.info("  RENAMED\n    %s\n    -> %s", fpath, target)


def main(dry_run: bool = False):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config(log)
    log.info("Config: output_dir=%s, filename_format=%s, first_artist_only=%s",
             cfg["output_dir"], cfg["filename_format"], cfg["first_artist_only"])
    log.info("Scanning %s ...", cfg["output_dir"])
    counts = consolidate(cfg, dry_run=dry_run)
    log.info("")
    log.info("Done: %d moved, %d duplicates removed, %d skipped, %d errors (dry_run=%s)",
             counts["moved"], counts["deduped"], counts["skipped"], counts["errors"], dry_run)


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
