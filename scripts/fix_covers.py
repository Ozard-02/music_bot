#!/usr/bin/env python3
"""Re-embed Spotify cover art into existing FLACs with correct dimensions.

Reads the Spotify track URL from each FLAC's URL tag, fetches the cover
from Spotify's CDN, and embeds it with proper JPEG width/height so
Navidrome displays it correctly.

Thin CLI over maintenance.rescan_library() (also used by the bot's
/fixmetadata cover refresh).
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from maintenance import rescan_library
from spotiflac_patch import silence_spotiflac_loggers

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("fix_covers")


async def fix_covers(dry_run: bool = False):
    cfg = load_config(log)
    result = await rescan_library(cfg, log, dry_run=dry_run)
    log.info("")
    log.info("Done: %d OK, %d skipped, %d failed (dry_run=%s)",
             result["ok"], result["skipped"], result["failed"], dry_run)


if __name__ == "__main__":
    silence_spotiflac_loggers()
    dry = "--dry-run" in sys.argv
    asyncio.run(fix_covers(dry_run=dry))
