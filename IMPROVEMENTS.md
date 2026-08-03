# Improvements

## Telegram Bot + SQLite Queue

### Bot
- [x] **`bot.py`** — Telegram bot via `python-telegram-bot`
  - [x] `/start` — welcome + instructions
  - [x] `/help` — format reminder
  - [x] Messages consistently formatted (emoji headers, bold titles, `<code>` blocks, 2-space indent)
  - [x] `/fixmetadata` progress shows `N/M` + live `%` and renders HTML (no raw `<b>` tags)
  - [x] Link handler — validate Spotify URL, add to queue, reply "Queued (#N)"
  - [x] Search handler — `"Artist - Album"`, `"Album - Song"`, `"Artist - Song"`
  - [x] `/status` — current download + queue position + history stats
  - [x] `/quality [value]` — set per-user download quality (no arg lists options)
  - [x] `/mkplaylist <url> [name]` — generate .m3u8 for a Spotify playlist from tracks on disk
  - [x] `/fixmetadata [folder]` — re-tag all FLACs in a folder (whole library if omitted) via SpotiFLAC (Apple-first enrichment); strips bogus MUSICBRAINZ_* and moves files to their real album folder
  - [x] `/fixmetadata --lyrics [folder]` — opt-in lyrics fetch; default re-tags WITHOUT fetching lyrics (slow), and never destroys existing LYRICS tags (restored after re-tag)
  - [x] `/purge` — remove all queued items
  - [x] Duplicate prevention — warns "Already queued as #N" when same input is already active
  - [x] Chat ID whitelist: only your user ID can interact; others silently ignored
  - [x] Invalid input → show format help
  - [x] Blocking async calls (`run_url`, `build_m3u8`) offloaded to thread executor — bot stays responsive

### Queue
- [x] **`queue_manager.py`** — SQLite persistence
  - [x] `queue` table: id, input_type, query, status, timestamps, result counts, error
  - [x] `failed_tracks` table: per-track failure logging with FK → queue
  - [x] Survives restarts: `_init_db()` resets stranded `running` items → `queued` on startup
  - [x] Methods: `enqueue_unique()`, `dequeue()`, `requeue()`, `mark_done()`, `mark_failed()`, `purge_all()`, `log_failed_track()`, `get_failed_tracks()`, `get_status()`, `get_history()`

### Worker
- [x] **`worker.py`** — sequential queue processor
  - [x] Polls queue for next `queued` item
  - [x] Resolves search queries via SpotiFLAC search
  - [x] Calls `run_url()` (shared core) in thread executor — doesn't block bot polling
  - [x] Logs per-track failures to `failed_tracks` table
  - [x] Requeues with retry counter (up to 15× or 24h), then permanent fail
  - [x] Sends Telegram notification on completion (summary: X ok, Y skipped, Z failed)
  - [x] Whole-job timeout is graceful: `asyncio.TimeoutError` → requeue with 30-min floor + "⏳ Still downloading" (never "Internal error"); per-track failures logged live via `failure_cb` so give-up advances even when a job times out; timeout budget from `cfg.max_download_timeout` (default 8h)

### M3U8
- [x] **`m3u8.py`** — standalone .m3u8 generator for Spotify playlists
  - [x] Fetches playlist metadata + track list via SpotiFLAC
  - [x] Reconstructs expected output paths using same `_sanitize()` rules as SpotiFLAC
  - [x] Scans `~/Music` for already-downloaded tracks (no re-downloading)
  - [x] Writes `~/Music/{name}.m3u8` with relative paths
  - [x] Importable: `build_m3u8(url, name, cfg)` used by `/mkplaylist` bot command
  - [x] Dedup by `track.id` in `build_m3u8_lines()` — duplicate playlist entries produce one line
  - [x] CLI: `python m3u8.py <url> [name]`

### Resolver
- [x] **`resolver.py`** — search resolution
  - [x] `parse_input()` — link vs `"X - Y"` vs invalid
  - [x] `resolve_search()` — Spotify search, picks best track/album match
  - [x] `format_help()` — format instructions in HTML

### Refactor
- [x] **`downloader.py`** — extracted `run_url(url) → DownloadResult{ok, skipped, failed, failed_tracks, gave_up_tracks, total}`
  - [x] Handles tracks, albums, playlists
  - [x] `_download_once` takes optional `services` param; CLI retry loop passes `wait_for_providers()` result (only healthy providers used)
  - [x] Uses `client.download_track(track.external_url)` per track — collections are split into per-track downloads limited by `asyncio.Semaphore(MAX_CONCURRENT)` (gives SpotiFLAC per-track metadata for correct path resolution), singles via `download_track(url)`
  - [x] Removed `download_single_track`, `download_collection`, `_download_track_with_retry` — SpotiFLAC internal retry/timeout/parallelism used instead
  - [x] Passes `track_max_retries`, `timeout_s`, `max_concurrent_downloads` to `AsyncSpotiFLAC`
  - [x] Standalone `main()` still works for CLI use

## Docker
- [x] **`Dockerfile`** — container image
  - [x] `python:3.14-slim` base
  - [x] Install `chromium` (SpotiFLAC needs a real browser for
        Qobuz session auto-verification via `nodriver` CDP)
  - [x] `ENV CHROME_PATH=/usr/bin/chromium`
  - [x] `ENV CHROME_FLAGS="--no-sandbox --disable-dev-shm-usage"`
  - [x] Install SpotiFLAC + python-telegram-bot
  - [x] Copy `spoty_loop/` into container
  - [x] CMD: run `bot.py`
- [x] **`docker-compose.yml`** — single service
  - [x] Mount `~/Music:/root/Music` (FLAC output)
  - [x] Mount `~/.spotiflac:/root/.spotiflac` (session + config + queue.db)
  - [x] Mount `.:/app` (code, for testing — remove for production)
  - [x] `env_file: .env` + `QUEUE_DB_PATH` env var
- [x] **`.dockerignore`** — exclude venv, caches, logs, secrets
- [x] **`bot.py`** — `QUEUE_DB_PATH` env var redirects queue.db to mounted volume
- [x] **`requirements.txt`** — `python-telegram-bot` (done)

### CI/CD
- [x] **`.github/workflows/docker.yml`** — GitHub Actions
  - [x] Trigger: push to `main`
  - [x] Build image via `docker/build-push-action`
  - [x] Push to `ghcr.io/ozard-02/music_loop:latest`
  - [x] Auth via `secrets.GHCR_TOKEN` (PAT with `write:packages` scope)

---

## Preserve Original Symbols in Filenames

SpotiFLAC replaces `<>:"/\\|?*` with `_` in filenames and directories. On Linux ext4, only `/` and `\0` are forbidden — everything else (`? : " < > | *`) is valid.

### Proposed approach (no SpotiFLAC patches)

1. **Fix `sanitize()`** — only replace `/` (use `∕` U+2215 as substitute), preserve all other chars
2. **Post-download rename** — after SpotiFLAC saves (with `_`), move to original-symbols path
3. **Pre-check** — looks at original-symbols path → finds already-downloaded files ✓

### One-time rename script

Scan all FLACs in `~/Music`, read `ALBUM`/`ARTIST`/`TITLE` Vorbis tags, reconstruct original-symbols paths, rename directories/files from `_`-paths.

### Flow

```
pre-check: look for .../WHEN WE ALL FALL ASLEEP, WHERE DO WE GO?/bad guy.flac
  → not found (SpotiFLAC saved with _) → download
SpotiFLAC → saves to .../WHEN WE ALL FALL ASLEEP, WHERE DO WE GO_/bad guy.flac
post-download rename → .../WHEN WE ALL FALL ASLEEP, WHERE DO WE GO?/bad guy.flac
next time: pre-check finds it → skip ✓
```

### Status
- [x] Fix `sanitize()` — only replace `/` with `∕`, preserve all other chars
- [x] Add post-download rename in `_download_tracks()` — async via `asyncio.to_thread`
- [x] One-time rename script — `fix_original_filenames.py` (reads Vorbis tags, renames files + empty dirs)
- [ ] Test with real downloads

## Tests
- [x] **222 tests** across bot, downloader, fix_metadata, m3u8, queue_manager, resolver, worker
  - [x] All mock-based, no network, no real SpotiFLAC calls
  - [x] Run: `pytest tests/ -v`

## Code Quality

- [x] Sanitize exception before sending to Telegram (`worker.py:141` — log full traceback locally, send safe message)
- [x] Remove dead functions from `track_utils.py` — `fetch_tracks`, `dedup_tracks`, `classify_tracks`, `remove_empty_parents` (uncalled anywhere)
- [x] Extract `_get_jpeg_dimensions()` to shared utility (triplicated in `backfill_urls.py`, `retag_missing.py`, `fix_covers.py`)
- [x] Replace `client._get_metadata_client()` with public SpotiFLAC API in `m3u8.py:82`
- [x] Remove unused imports: `MAX_QUEUE_RETRIES` in `queue_manager.py:7`, `re` in `backfill_urls.py:4`, `spotiflac_sanitize`/`spotiflac_track_relative_path` in `m3u8.py:23`
- [x] Eliminate double-logging in `worker._process()` (inner try removed, single catch)
- [x] Move `MAX_QUEUE_AGE` from `worker.py:17` to `config.py`
- [x] Import `sanitize` from `track_utils` in `fix_original_filenames.py` (was via `m3u8` re-export chain)
- [x] Wrap `FLAC(path)` in `fix_mb_tags.py` with try/except for corrupted files
- [x] Add return type annotations to all `bot.py` handlers (9 functions)
- [x] Centralize `MAX_CONCURRENT = 5` constant (defined independently in 3 scripts)

## Existing (done before bot)
- [x] Skip-race fix: per-track-ID `_in_progress` guard inside semaphore
- [x] Config reading from `~/.spotiflac/config.json`
- [x] `[N/M]` progress counter on outcome logs
- [x] Retry loop: retries until zero failures (no fast-fail)
- [x] Removed `track_file_exists` — relies on SpotiFLAC internal skip detection
- [x] MusicBrainz tag strip — patched tagger.py to avoid Navidrome misgrouping
- [x] Cover fix — `enrich_providers` excludes qobuz; `fix_covers.py` re-embeds Spotify covers
- [x] Add tidal to `enrich_providers` — Tidal provides 1280×1280 covers (vs SoundCloud 500×500). (Tidal later dropped from downloads — enrich is now `["apple","deezer","soundcloud"]`.)
