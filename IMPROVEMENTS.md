# Improvements

## Telegram Bot + SQLite Queue

### Bot
- [x] **`bot.py`** — Telegram bot via `python-telegram-bot`
  - [x] `/start` — welcome + instructions
  - [x] `/help` — format reminder
  - [x] Link handler — validate Spotify URL, add to queue, reply "Queued (#N)"
  - [x] Search handler — `"Artist - Album"`, `"Album - Song"`, `"Artist - Song"`
  - [x] `/status` — current download + queue position + history stats
  - [x] `/mkplaylist <url> [name]` — generate .m3u8 for a Spotify playlist from tracks on disk
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
  - [x] Methods: `enqueue()`, `find_existing()`, `dequeue()`, `requeue()`, `mark_done()`, `mark_failed()`, `purge_queued()`, `log_failed_track()`, `get_failed_tracks()`, `get_status()`, `get_history()`

### Worker
- [x] **`worker.py`** — sequential queue processor
  - [x] Polls queue for next `queued` item
  - [x] Resolves search queries via SpotiFLAC search
  - [x] Calls `run_url()` (shared core) in thread executor — doesn't block bot polling
  - [x] Logs per-track failures to `failed_tracks` table
  - [x] Requeues with retry counter (up to 50× or 24h), then permanent fail
  - [x] Sends Telegram notification on completion (summary: X ok, Y skipped, Z failed)

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
- [x] **`downloader.py`** — extracted `run_url(url) -> dict{ok, skipped, failed, failed_tracks}`
  - [x] Handles tracks, albums, playlists
  - [x] `_download_once` takes optional `services` param; CLI retry loop passes `wait_for_providers()` result (only healthy providers used)
  - [x] `RunState.failed_tracks` list captures per-track (id, title, error) on GAVE UP
  - [x] Standalone `main()` still works for CLI use

## Docker
- [ ] **`Dockerfile`** — container image
  - [ ] `python:3.14-slim` base
  - [ ] Install SpotiFLAC + python-telegram-bot
  - [ ] Copy `spoty_loop/` into container
  - [ ] CMD: run `bot.py`
- [ ] **`docker-compose.yml`** — single service
  - [ ] Mount `~/Music:~/Music`
  - [ ] Mount `~/.spotiflac:/root/.spotiflac`
  - [ ] Env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`
- [ ] **`requirements.txt`** — `python-telegram-bot` (done)

---

## Tests
- [x] **119 tests** across bot, downloader, m3u8, queue_manager, resolver, worker
  - [x] All mock-based, no network, no real SpotiFLAC calls
  - [x] Run: `pytest tests/ -v`

## Existing (done before bot)
- [x] Skip-race fix: per-track-ID `_in_progress` guard inside semaphore
- [x] Config reading from `~/.spotiflac/config.json`
- [x] `[N/M]` progress counter on outcome logs
- [x] Retry loop: retries until zero failures (no fast-fail)
- [x] Removed `track_file_exists` — relies on SpotiFLAC internal skip detection
- [x] MusicBrainz tag strip — patched tagger.py to avoid Navidrome misgrouping
- [x] Cover fix — `enrich_providers` excludes qobuz; `fix_covers.py` re-embeds Spotify covers
