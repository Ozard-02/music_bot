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
| `Dockerfile` | Container image: python:3.14-slim + chromium + spotiflac + bot.py |
| `docker-compose.yml` | Single-service compose with TrueNAS pool mounts and env vars |
| `.dockerignore` | Excludes venv, caches, logs, .env from image |

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

### Bare-metal
```bash
python bot.py
```
Standalone CLI still works: `python downloader.py <spotify_url>`

### Docker
```bash
docker compose up -d
```

## Done

1. **Pre-check before download** — `downloader.py` constructs expected file paths via `track_relative_path()` before calling SpotiFLAC. Existing tracks sorted into skipped, only missing passed to `_run_once_async(target_tracks=...)`. Early exit when everything exists.
2. **Download timeout** — `MAX_DOWNLOAD_TIMEOUT = 7200` kills stuck downloads in `worker.py` via `asyncio.wait_for`.
3. **Amazon first** — `SERVICES = ["amazon", "qobuz"]` matching `autoOrder` in SpotiFLAC config. `PER_TRACK_TIMEOUT` reduced 180 → 100s.
4. **Environment** — uv venv, SpotiFLAC installed, nodriver patched (utf-8 header).
5. **Session bridge** — copy desktop `community_session.json` → module path (fixes Cloudflare).
6. **`downloader.py`** — retry loop with 3-parallel downloads, 180s timeout, 3 retries, crash-restart.
7. **Services reorder** — `SERVICES = ["qobuz", "tidal", "amazon"]` (Qobuz primary).
8. **SpotiFLAC child loggers** — iterate all `SpotiFLAC.*` loggers → WARNING level.
9. **Dedup** — filter duplicates by `track.id` before download, write to `duplicates.log`.
10. **Skip race fix** — per-track `_in_progress` guard inside semaphore prevents parallel-dup downloads.
11. **Loop until complete** — inner retry loop runs until zero failures.
12. **Removed `track_file_exists`** — SpotiFLAC handles skip detection internally.
13. **Removed dead code** — `_safe_folder`, `_get_first_artist`, `_scan_dir`, unused imports cleaned up.
14. **MusicBrainz tag fix** — patched `SpotiFLAC/core/tagger.py` to strip `MUSICBRAINZ_*` tags.
15. **`fix_mb_tags.py`** — one-time script to strip existing `MUSICBRAINZ_*` tags from all FLACs.
16. **Cover fix** — `enrich_providers` excludes qobuz.
17. **`fix_covers.py`** — one-time script to re-embed Spotify cover art into all FLACs.
18. **Refactor** — `RunState` dataclass, clean imports, specific exception handling.
19. **`run_url()` extracted** — shared between CLI and bot worker. Handles tracks, albums, playlists.
20. **SQLite queue** — `queue_manager.py` with enqueue/dequeue/status/history.
21. **Search resolver** — `resolver.py` parses `"X - Y"` via Spotify search, picks best match.
22. **Telegram bot** — `bot.py` with /start, /help, /status, link+text handlers, whitelist.
23. **Background worker** — `worker.py` polls queue, resolves searches, downloads, notifies.
24. **`wait_for_providers` fix** — `_download_once` does single health check (no blocking). CLI retry loop calls `wait_for_providers()` at loop top.
25. **`failed_tracks` table** — `queue_manager.py` logs per-track failures with `log_failed_track()`. Captured in `RunState` and persisted from worker.
26. **`m3u8.py`** — standalone script to generate `.m3u8` for a Spotify playlist. Scans `~/Music` for already-downloaded tracks, writes relative paths. Importable for bot use later.
27. **Duplicate prevention** — `queue_manager.find_existing()` checks for active jobs with same input before enqueueing. Bot warns "Already queued as #N".
28. **`/purge` command** — `queue_manager.purge_queued()` deletes all queued items. Bot confirms count.
29. **M3U8 dedup** — `build_m3u8_lines()` deduplicates by `track.id` to match downloader behavior.
30. **use healthy providers** — `wait_for_providers()` result now passed to `_download_once()` as `services=` param, instead of hardcoded `SERVICES`. Dead Tidal v1 no longer polled on every track.
31. **stranded running items** — `_init_db()` resets `running` → `queued` on startup so items in-flight during a kill are recovered on restart.
32. **album download fix** — `client.download_track(url)` used for album/playlist as single batch. _Later reverted — see #34._
33. **per-track parallel download for collections** — `_run_collection` downloads missing tracks individually via `client.download_track(track.external_url)` with `asyncio.Semaphore(MAX_CONCURRENT)` instead of a single batch call. Gives SpotiFLAC per-track metadata for correct path resolution. Pre-check (skip existing) still runs upfront.
34. **removed Retry Failed Tracks folder** — deleted `_move_playlist_files()`, `os`/`re`/`shutil` imports. No more playlist-named directory or file-moving logic. Non-flac/non-m3u8 aux files go to `~/Music/temp/`.
35. **120 tests** — 3 new tests for per-track download assertions (album_all_new, album_partial_exist, album_dedup_counts_unique).
36. **pre-check path mismatch fixed** — `sanitize()` now replaces `<>:"/\\|?*` with `_` (matching SpotiFLAC's filesystem behavior) instead of removing them. Paths like `WHEN WE ALL FALL ASLEEP, WHERE DO WE GO_/...` now match what SpotiFLAC actually writes to disk, so the pre-check correctly identifies existing files and skips them.
37. **Original symbols in filenames** — `sanitize()` now only replaces `/` with `∕` (U+2215), preserving all other special characters (`? : " < > | *`). Post-download rename converts SpotiFLAC's `_`-paths to original-symbols paths. One-time `fix_original_filenames.py` script migrates existing files. Pre-check looks at original-symbols paths → finds already-downloaded files. 121 tests.
38. **`.part` file cleanup** — `bot.py:post_init()` deletes leftover `*.enc.part` files from interrupted downloads on startup.
39. **Playlist cover sidecar** — `build_m3u8()` downloads cover as `{playlist}.jpg` next to `.m3u8` file via `httpx`. Uses `mc.get_url_async(url)` instead of `client.get_playlist(url)` to get `cover_url`. `_download_cover()` helper in `m3u8.py`. Old `.playlist_covers/` migrated to sidecar location on bot startup.
40. **Auto m3u8 on playlist download** — `worker.py:_auto_build_m3u8()` runs after successful playlist download, after 24h timeout, and after max-retries failure. Checks `input_type == "link"` + `parse_spotify_url(...)["type"] == "playlist"`. Not run on requeue retries.
41. **Retry/timeout tuning** — `MAX_QUEUE_RETRIES` 50→15, `MAX_DOWNLOAD_TIMEOUT` 7200→3600. Less patience for stuck items.
42. **Refactor: remove cumulative tracking** — removed `_pre_check()`, `store_cumulative_tracking()`, DB columns `total`/`initial_skipped`. `run_url()` result used directly. Eliminated redundant API call per job.
43. **Refactor: deduplicate path utils** — `m3u8.py` now imports `sanitize`/`track_relative_path` from `track_utils.py` instead of defining its own copies. Single source of truth.
44. **Refactor: remove dead code** — deleted `run_url_sync()`, fixed test patches → 7 previously-failing worker tests now pass (121/121).
45. **Refactor: clean up lazy imports** — moved `parse_spotify_url`/`build_m3u8`/`track_relative_path`/`Path` to top-level imports in `worker.py`. Only `AsyncSpotiFLAC` stays lazy.
46. **Suppress SpotiFLAC output noise** — `downloader.py:_silence_spotiflac()` context manager monkey-patches `SpotiFLAC.core.console.*` banner/error/fallback functions and `builtins.input` to no-ops during download. `config.py:setup_logger()` sets all `SpotiFLAC.*` loggers to `CRITICAL`. Kills SOURCE banners, tracebacks, interactive prompts (`Incolla qui il grant`), and fallback spam. 128 tests.
47. **Docker deployment** — `Dockerfile` (python:3.14-slim + chromium + spotiflac), `.dockerignore`, `docker-compose.yml` with `~/.spotiflac` + `~/Music` mounts. `QUEUE_DB_PATH` env var redirects queue.db to the mounted volume. Containerized and bare-metal both work.
48. **CI/CD auto-build** — GitHub Actions workflow: on push to `main`, builds image and pushes to `ghcr.io/ozard-02/music_loop:latest`. TrueNAS pulls the updated image automatically.

## Remaining

- **Tune parallelism** — adjust `MAX_CONCURRENT` (3→?) if rate-limited.
- **Tidal v1 API retired** — permanent 410 error, requires SpotiFLAC update or tidal-web extension.
