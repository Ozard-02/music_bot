#!/usr/bin/env python3
"""Remove MUSICBRAINZ_* tags from all FLAC files under ~/Music.

These tags come from Qobuz metadata enrichment and often contain bogus IDs
that cause Navidrome to group unrelated albums together.
"""

import os
import sys

from mutagen.flac import FLAC

MUSIC_DIR = os.path.expanduser("~/Music")
BAD_TAGS = {
    "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ_ALBUMARTISTID",
    "MUSICBRAINZ_TRACKID",
    "MUSICBRAINZ_ARTISTID",
    "MUSICBRAINZ_RELEASEGROUPID",
}

count = 0
for root, _dirs, files in os.walk(MUSIC_DIR):
    for fn in files:
        if not fn.lower().endswith(".flac"):
            continue
        path = os.path.join(root, fn)
        audio = FLAC(path)
        modified = False
        for tag in BAD_TAGS & set(audio):
            del audio[tag]
            modified = True
        if modified:
            audio.save()
        count += 1
        if count % 500 == 0:
            print(f"[{count}] files processed...")

print(f"Done. Processed {count} FLAC files.")
