# Bug audit

Audit performed against `561c6bc`; fixes below are on `main` after that commit.

## Fixed

1. **Resolver album/song inversion** (`resolver.py`) — `"Artist - Song"`
   resolved to an album and `"Artist - Album"` to a single track. The picker
   now prefers the album only when the second part equals the album name;
   otherwise it keeps the track. Album matching also honours the album-name
   hint.
2. **Accent-insensitive matching** (`resolver.py`) — queries like
   `Beyonce - Cafe` now match `Beyoncé - Café` (ASCII fold + punctuation
   normalisation).
3. **`fix_metadata` wrong-song retag** (`scripts/fix_metadata.py`) — the
   tagless-file path now passes the title hint to `best_track_match`.
4. **Queue user scoping** (`queue_manager.py`, `bot.py`) —
   `enqueue_unique` dedupes per user; `/purge` and `/status` (plus queue
   position) are per user; purging also removes that user's
   `failed_tracks` rows.
5. **m3u8 path drift** (`track_utils.py`, `m3u8.py`) — playlists now write
   the path that really exists (ours, or SpotiFLAC's sanitized fallback).
6. **Crash retry** (`worker.py`) — a crashed subprocess is requeued with
   backoff (bounded by the normal retry/age limits) instead of being marked
   failed while the user is told it will retry.
7. **`/fixmetadata --lyrics` placement** (`bot.py`) — accepted anywhere in
   the arguments, not just as the first token.
8. **User-folder collision** (`bot.py`) — users without a username get a
   folder that includes their Telegram id, so two "Andrea"s no longer share
   a library.

## Not fixed (documented)

- `partition_tracks()` is still existence-based: a 0-byte/truncated file at
  the expected path counts as downloaded. A content/duration check is the
  next candidate.
- `failed_tracks` is still keyed by title, so two different songs sharing a
  title can be skipped together.
- SpotiFLAC's post-download validation is intentionally loose (preview/large
  mismatch only); provider title/artist guards remain the real defence.

## Known external limitation (reported track `5Q1CqDubt01LzIsZUTDoWm`)

`No Flash` by Wemme Flow / Mr. Monkey is **only on Amazon Music** (Songlink
resolves the ASIN, Deezer/Qobuz/Apple/Tidal do not have it). Qobuz's wrong
text candidate is correctly rejected. Amazon's fallbacks currently fail in
this environment:

- community `amz-oss.spotbye.qzz.io` → 503
- Antra `amazon.anandserver.cfd` → 401
- MusicDL `dl.musicdl.me` → timeout
- `amz.squid.wtf` → DNS failure
- mono API → needs a Chromium-based browser (not installed)

This is an upstream/source availability problem, not a spoty_loop matching
bug.
