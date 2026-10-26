# Bug audit (pre-fix baseline)

Audit performed against `561c6bc`. The full temp reproduction suite was run
separately; this file records the findings that are being fixed next.

## High

1. `resolver.py` — `"Artist - Song"` can resolve to an album and
   `"Artist - Album"` to a single track. `_pick_between_track_and_album`
   checks `second_part == track.album` before the album name, and defaults to
   the album when the artist matches.
2. `scripts/fix_metadata.py` — tagless files are matched with
   `best_track_match(tracks, artist)` (no title hint), so a file can be
   retagged and moved using another song by the same artist.
3. `queue_manager.py` — `enqueue_unique()` dedupes on `(type, query)` only,
   ignoring `user`; a second user's identical link is dropped.
4. `queue_manager.py` — `purge_all()` deletes every user's queue; `/purge`
   is reachable by any allowed user.

## Medium

5. `m3u8.py` — tracks found only under SpotiFLAC's sanitized path are listed
   in the playlist with our (missing) path.
6. `worker.py` — the crash path marks the item failed but tells the user
   "will retry"; the item is never requeued.
7. `bot.py` — `/fixmetadata --lyrics` is only recognised as the first token.
8. `track_utils.py` — `partition_tracks()` treats any existing path (even a
   0-byte/truncated file) as a completed download.

## Low

9. `resolver.py` — accent-less queries do not match accented metadata.
10. `resolver.py` — album matching ignores the album-name hint.
11. `library.py`/`bot.py` — users without a username but with the same first
    name share one library folder.
12. `queue_manager.py` — `purge_all()` leaves orphaned `failed_tracks` rows.

Post-download duration validation in SpotiFLAC is intentionally loose
(preview/large-mismatch only); the provider title/artist guards remain the
real defence and are not changed here.
