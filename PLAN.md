# Plan: SpotyLoop — Resilient Playlist Downloader

## Goal
Download Spotify playlists to FLAC matching `~/Music/{Artist}/{Album}/{Artist} - {title}.flac`.
Never delete existing files. Loop until every track is on disk. Parallel downloads.

## Done

1. **Environment** — uv venv, SpotiFLAC installed, nodriver patched (utf-8 header).
2. **Session bridge** — copy desktop `community_session.json` → module path (fixes Cloudflare).
3. **`downloader.py`** — retry loop with 3-parallel downloads, 180s timeout, 3 retries, crash-restart.
4. **Services reorder** — `SERVICES = ["qobuz", "tidal", "amazon"]` (Qobuz primary).
5. **SpotiFLAC child loggers** — iterate all `SpotiFLAC.*` loggers → WARNING level.
6. **Dedup** — filter duplicates by `track.id` before download, write to `duplicates.log`.
7. **Skip race fix** — per-track `_in_progress` guard inside semaphore prevents parallel-dup downloads.
8. **Loop until complete** — inner retry loop runs until zero failures:
   - All failed → 5 min wait (server likely down)
   - Some failed → 60s wait
   - Zero failed → exit
9. **Removed `track_file_exists`** — SpotiFLAC handles skip detection internally (path-matching), eliminating re-download bugs on restart.
10. **Removed dead code** — `_safe_folder`, `_get_first_artist`, `_scan_dir`, unused imports cleaned up.
11. **MusicBrainz tag fix** — Qobuz enrichment returns bogus `MUSICBRAINZ_ALBUMID` (same fake ID for unrelated albums), causing Navidrome to merge tracks into one album. Patched `SpotiFLAC/core/tagger.py` to strip all `MUSICBRAINZ_*` tags before writing Vorbis comments.
12. **`fix_mb_tags.py`** — one-time script to strip existing `MUSICBRAINZ_*` tags from all 1224 FLAC files in `~/Music`.
13. **Cover fix** — Qobuz enrichment returns wrong HD covers for some albums. `enrich_providers` changed to `["deezer", "apple", "tidal", "soundcloud"]` (excludes qobuz) in downloader.py.
14. **`fix_covers.py`** — one-time script to re-embed correct Spotify cover art into all 1025 FLACs with Spotify track IDs.
15. **Refactor** — `RunState` dataclass replaces mutable globals; `bridge_community_session`/`load_config` moved into `main()`; specific exception handling; chunked heartbeat sleep; flattened control flow.

## Remaining

- **Tune parallelism** — adjust `MAX_CONCURRENT` (3→?) if rate-limited.
- **Tidal v1 API retired** — permanent 410 error, requires SpotiFLAC update or tidal-web extension (Node.js not installed).
