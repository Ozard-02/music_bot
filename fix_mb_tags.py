#!/usr/bin/env python3
"""Remove MUSICBRAINZ_* tags from all FLAC files under ~/Music.

These tags come from Qobuz metadata enrichment and often contain bogus IDs
that cause Navidrome to group unrelated albums together.
"""

import os
import subprocess
import sys

MUSIC_DIR = os.path.expanduser("~/Music")
BAD_TAGS = [
    "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ_ALBUMARTISTID",
    "MUSICBRAINZ_TRACKID",
    "MUSICBRAINZ_ARTISTID",
    "MUSICBRAINZ_RELEASEGROUPID",
]

count = 0
for root, _dirs, files in os.walk(MUSIC_DIR):
    for fn in files:
        if not fn.lower().endswith(".flac"):
            continue
        path = os.path.join(root, fn)
        for tag in BAD_TAGS:
            result = subprocess.run(
                ["metaflac", "--remove-tag=" + tag, path],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                pass
            elif "No such tag" in result.stderr:
                pass
            else:
                print(f"Error removing {tag} from {path}: {result.stderr}")

        count += 1
        if count % 500 == 0:
            print(f"[{count}] files processed...")

print(f"Done. Processed {count} FLAC files.")
