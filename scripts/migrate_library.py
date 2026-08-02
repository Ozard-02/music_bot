#!/usr/bin/env python3
"""One-time migration: move the root-level library into the owner's folder.

Before multi-user folders existed, everything landed at ~/Music root
(~/Music/{AlbumArtist}/{Album}/...). This script moves those entries into
~/{owner}_Music/ so the owner's library is scoped like every other user's.

.m3u8 files and their .jpg covers are moved along with the tracks — the
relative paths inside stay valid because the tracks move with them.

Usage:
    python scripts/migrate_library.py --username espo --dry-run   # preview
    python scripts/migrate_library.py --username espo             # apply
"""

import argparse
import logging
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from library import FOLDER_SUFFIX

log = logging.getLogger("migrate_library")


def _is_ignored(name: str) -> bool:
    """Entries that stay at root: user folders, the future shared folder,
    hidden files/dirs (playlist covers migration handled by the bot)."""
    if name.endswith(FOLDER_SUFFIX):
        return True
    if name == "shared_Music":
        return True
    return name.startswith(".")


def main(username: str, dry_run: bool = False):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config(log)
    root = Path(cfg["output_dir"])
    target = root / f"{username}{FOLDER_SUFFIX}"

    if not root.is_dir():
        log.error("Output dir does not exist: %s", root)
        sys.exit(1)
    if not username:
        log.error("--username is required")
        sys.exit(1)

    entries = [e for e in root.iterdir() if not _is_ignored(e.name)]
    if not entries:
        log.info("Nothing to migrate in %s", root)
        return

    if dry_run:
        log.info("Would move %d entries into %s/:", len(entries), target)
        for e in entries:
            log.info("  %s", e.name)
        return

    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for e in entries:
        dest = target / e.name
        if dest.exists():
            log.warning("  SKIP (already exists) %s", e.name)
            continue
        shutil.move(str(e), str(dest))
        log.info("  MOVED %s -> %s", e.name, dest)
        moved += 1

    log.info("Done: %d entries moved into %s", moved, target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", help="Owner's telegram username (folder becomes {username}_Music)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()
    main(args.username, dry_run=args.dry_run)
