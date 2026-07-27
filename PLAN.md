# Plan: SpotyLoop — Resilient Playlist Downloader + Telegram Bot

## Goal
Download Spotify content to FLAC matching `~/Music/{Artist}/{Album}/{Artist} - {title}.flac`.
Telegram bot for queueing downloads. Never delete existing files.

## Architecture

```
User → Telegram Bot (bot.py)
         ↓
      Queue Manager (queue_manager.py) — SQLite persistence
         ↓
      Worker (worker.py) — polls queue, processes items
         ├─ Link: pass directly to run_url()
         └─ Search: resolver.py → search Spotify → get URL → run_url()
                ↓
         downloader.run_url() — full retry loop, parallel downloads
```

### Files

| File | Job |
|---|---|
| `downloader.py` | Core download engine. `run_url(url)` handles tracks/albums/playlists with retry. Still works as standalone CLI. |
| `queue_manager.py` | SQLite queue table (status, timestamps, results). Thread-safe. |
| `resolver.py` | `parse_input()` detects link vs "Artist - Album" search. `resolve_search()` queries Spotify via SpotiFLAC. |
| `worker.py` | Background async loop — dequeues, resolves, calls `run_url()`, sends Telegram notification. |
| `bot.py` | Telegram bot — /start, /help, /status, /purge, /mkplaylist, link handler, text handler. Chat ID whitelist. |

### Resolver Logic

`"X - Y"` search:
1. Search Spotify for track `"X Y"` and album `"X Y"`
2. If track's artist matches X → likely "Artist - Song" → download track
3. If album's artist matches X → likely "Artist - Album" → download album
4. If track's album name matches Y → likely "Album - Song" → download track
5. Default to track if both match

### Bot Format Reminder

Shown on /start, /help, and invalid input:
- Spotify **link** → download it
- **Artist — Album** → download album
- **Album — Song** → download song
- **Artist — Song** → download song

## Setup

Requires env vars:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_ID`

Run: `bot.py` (starts worker + polling in one process)

Standalone CLI still works: `python downloader.py <spotify_url>`

## Done

1. **Environment** — uv venv, SpotiFLAC installed, nodriver patched (utf-8 header).
2. **Session bridge** — copy desktop `community_session.json` → module path (fixes Cloudflare).
3. **`downloader.py`** — retry loop with 3-parallel downloads, 180s timeout, 3 retries, crash-restart.
4. **Services reorder** — `SERVICES = ["qobuz", "tidal", "amazon"]` (Qobuz primary).
5. **SpotiFLAC child loggers** — iterate all `SpotiFLAC.*` loggers → WARNING level.
6. **Dedup** — filter duplicates by `track.id` before download, write to `duplicates.log`.
7. **Skip race fix** — per-track `_in_progress` guard inside semaphore prevents parallel-dup downloads.
8. **Loop until complete** — inner retry loop runs until zero failures.
9. **Removed `track_file_exists`** — SpotiFLAC handles skip detection internally.
10. **Removed dead code** — `_safe_folder`, `_get_first_artist`, `_scan_dir`, unused imports cleaned up.
11. **MusicBrainz tag fix** — patched `SpotiFLAC/core/tagger.py` to strip `MUSICBRAINZ_*` tags.
12. **`fix_mb_tags.py`** — one-time script to strip existing `MUSICBRAINZ_*` tags from all FLACs.
13. **Cover fix** — `enrich_providers` excludes qobuz.
14. **`fix_covers.py`** — one-time script to re-embed Spotify cover art into all FLACs.
15. **Refactor** — `RunState` dataclass, clean imports, specific exception handling.
16. **`run_url()` extracted** — shared between CLI and bot worker. Handles tracks, albums, playlists.
17. **SQLite queue** — `queue_manager.py` with enqueue/dequeue/status/history.
18. **Search resolver** — `resolver.py` parses `"X - Y"` via Spotify search, picks best match.
19. **Telegram bot** — `bot.py` with /start, /help, /status, link+text handlers, whitelist.
20. **Background worker** — `worker.py` polls queue, resolves searches, downloads, notifies.
21. **`wait_for_providers` fix** — `_download_once` does single health check (no blocking). CLI retry loop calls `wait_for_providers()` at loop top.
22. **`failed_tracks` table** — `queue_manager.py` logs per-track failures with `log_failed_track()`. Captured in `RunState` and persisted from worker.
23. **`m3u8.py`** — standalone script to generate `.m3u8` for a Spotify playlist. Scans `~/Music` for already-downloaded tracks, writes relative paths. Importable for bot use later.
24. **Duplicate prevention** — `queue_manager.find_existing()` checks for active jobs with same input before enqueueing. Bot warns "Already queued as #N".
25. **`/purge` command** — `queue_manager.purge_queued()` deletes all queued items. Bot confirms count.
26. **M3U8 dedup** — `build_m3u8_lines()` deduplicates by `track.id` to match downloader behavior.

## Remaining

- **Docker** — `Dockerfile` + `docker-compose.yml` for containerized deployment.
- **Tune parallelism** — adjust `MAX_CONCURRENT` (3→?) if rate-limited.
- **Tidal v1 API retired** — permanent 410 error, requires SpotiFLAC update or tidal-web extension.
