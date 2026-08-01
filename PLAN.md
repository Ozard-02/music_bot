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
| `downloader.py` | Core download engine. `run_url(url)` handles tracks/albums/playlists with retry. |
| `maintenance.py` | Library maintenance: `rescan_library()` re-embeds Spotify covers (`/rescan`, `scripts/fix_covers.py`). |
| `spotiflac_patch.py` | All SpotiFLAC monkey-patching in one place (ProgressManager, console interception, log silencing). |
| `flac_utils.py` | Shared FLAC helpers: `get_spotify_id_from_file`, `upgrade_cover_url`, `embed_cover`, `iter_flacs`. |
| `track_utils.py` | Shared path utilities (`sanitize`, `track_relative_path`, `get_jpeg_dimensions`). |
| `queue_manager.py` | SQLite queue table (status, timestamps, results). Thread-safe. |
| `resolver.py` | `parse_input()` detects link vs "Artist - Album" search. `resolve_search()` queries Spotify via SpotiFLAC. |
| `worker.py` | Background async loop — dequeues, resolves, calls `run_url()`, sends Telegram notification. Pure `decide_failure()` for retry decisions. |
| `bot.py` | Telegram bot — /start, /help, /status, /purge, /mkplaylist, link handler, text handler. Chat ID whitelist. |
| `scripts/` | Maintenance CLIs: fix_metadata (also bot-called), fix_covers, fix_mb_tags, fix_original_filenames, retag_missing, backfill_urls, fix_qvc. |
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

Maintenance CLIs live in `scripts/` (`python scripts/fix_metadata.py <folder> [--apply]`, etc.).

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
47. **Docker deployment** — `Dockerfile` (python:3.14-slim + chromium + pydoll-python + spotiflac), `.dockerignore`, `docker-compose.yml` with `~/.spotiflac` + `~/Music` mounts. `QUEUE_DB_PATH` env var redirects queue.db to the mounted volume. Containerized and bare-metal both work.
48. **CI/CD auto-build** — GitHub Actions workflow: on push to `main`, builds image and pushes to `ghcr.io/ozard-02/music_loop:latest`. Auth via `secrets.GHCR_TOKEN` (PAT with `write:packages` scope). TrueNAS pulls the updated image automatically.
49. **Spotify `-` → `‑` (non-breaking hyphen) path fix** — pre-check used `sanitize()` to replace `-` with Unicode `‑`, but SpotiFLAC writes literal `-`. Reverted: `sanitize()` now preserves `-`. Pre-check paths now match what SpotiFLAC actually writes.
50. **`BaseException` catch for SpotiFLAC `sys.exit(0)`** — SpotiFLAC calls `sys.exit(0)` on success in some code paths, which raises `SystemExit`. Changed `except Exception` → `except BaseException` chain to prevent silent crash. Worker interprets `SystemExit` as success.
51. **Silent metadata failure detection** — SpotiFLAC's `download_track(url)` returns `[]` (not exception) when metadata resolution fails. Added file-existence check in `_dl`: if `download_track` returns `[]` and no file appears on disk, counts as failure with "SILENT FAIL" log. Added post-download `(disk: N/total)` sanity line.
52. **All platforms verified** — bare-metal (Arch Linux), Docker on laptop (macOS), Docker on TrueNAS SCALE all tested working.
53. **Post-download cover overwrite** — `downloader.py:_fix_cover()` fetches Spotify cover (640×640 via URL upgrade `1e02`→`b273`) and embeds via mutagen after each successful download. Keeps enrich_providers for genre/label, only cover gets corrected.
54. **`/rescan` bot command** — `rescan_library()` in `downloader.py` walks output dir, reads `URL` tag for Spotify track ID, fetches fresh metadata + cover, embeds it. Reports via Telegram progress callback. Uses `asyncio.Semaphore(SCRIPT_MAX_CONCURRENT=5)`.
55. **conftest.py cleanup** — `config` fixture now `rmtree`s `/tmp/test_music` before returning, preventing stale-file cross-test pollution.
56. **`fix_metadata.py` + `/fixmetadata` bot command** — re-tags every FLAC in a folder (or whole library root) through the SpotiFLAC pipeline: deletes old tags, strips all `MUSICBRAINZ_*` (fixes "same album name split into multiple Navidrome albums"), writes clean Spotify metadata with Apple-first enrichment (`["apple","deezer","soundcloud"]`). Files whose real album differs from their folder are moved into the real album's folder (never deleted). CLI `--dry-run` default, `--apply` writes. 149 tests.
57. **Kill SpotiFLAC ProgressManager log flood** — `downloader.py:_disable_progress_manager()` runs once at import: neutralizes `ProgressManager`'s class-level asyncio state (`_event_queue`/`_worker_task`) and makes `enqueue_progress`/`start_worker` no-ops. Root cause was the "Queue bound to a different event loop" RuntimeError flood (476× in one log) from `asyncio.to_thread + asyncio.run` reusing shared class-level state across 3 parallel jobs.
58. **Per-track give-up after 10 attempts** — `MAX_TRACK_RETRIES=10` in `config.py`. `queue_manager.get_give_up_titles()` returns titles with ≥N failures; `worker._process` computes them before each run and passes `skip_titles` to `run_url()`. `_download_tracks` reports them separately as `gave_up_tracks` (never re-downloaded, never re-logged). `_handle_failure` stops requeueing once all remaining failures are at/over threshold → marks item done with partial results instead of looping 15×. Previously a dead-Qobuz track caused the whole album to requeue 15×, each pass rewriting covers (ZFS block-I/O amplification ~7 GiB).
59. **Log rotation** — `config.setup_logger()` uses `RotatingFileHandler` (5 MB × 3 backups) instead of unbounded `FileHandler`.
60. **Single-instance lock (durable)** — `bot.py:SingleInstanceLock` uses `flock(LOCK_EX|LOCK_NB)` on `queue.db.lock` (next to the queue DB on the shared volume). Acquired in `main()` before `QueueManager`/PTB build; only the lock holder polls `getUpdates`, so two instances can never run the bot together (kills the Telegram Conflict errors). A standby instance polls every 30s and takes over when the holder dies — no stale locks (flock auto-releases on process exit), no Docker restart loop. 158 tests.

## Remaining

- **Tidal v1 API retired** — permanent 410 error, requires SpotiFLAC update or tidal-web extension.
- **Album metadata quality** — investigate whether enrich_providers (Apple/Deezer/Tidal/SoundCloud) contaminate album-level metadata (genre, label, year, etc.). Possibly drop enrich_providers entirely and use only Spotify metadata for consistency.
- **Cross-folder album merging** — `/fixmetadata` moves single-track folders (e.g. `OK`, `ROSSO COME IL FANGO`) into their real album folder only when run on the library root or on that folder; folders are never auto-deleted when emptied.
61. **Kill 3× duplicate log lines (root logger pollution)** — SpotiFLAC 1.5.9's `core/progress.py:install_console_interception()` runs once per track download (`client.download_track` → `SpotiflacDownloader.run_async` → `DownloadWorker.run_async`). It strips every `StreamHandler` off the root logger (including our asctime stdout handler AND the `RotatingFileHandler`, which is a StreamHandler subclass — the file log froze after the first track) and adds a `TqdmLoggingHandler` (`[%(levelname)s] %(name)s: %(message)s`) that `uninstall_console_interception()` never removes. Root handlers piled up one per track → every record (incl. ours, name `spoty_loop`) printed N× foreign-format with µs-identical timestamps. This was never parallelism/containers (the single-instance lock #60 was still needed, but was not the cause). `_disable_progress_manager()` now also no-ops `install_/uninstall_console_interception` in both `SpotiFLAC.core.progress` and `SpotiFLAC.downloader` (the module-level name actually called at downloader.py:436), plus `initialize_master_bar`. `Dockerfile` pins `SpotiFLAC==1.5.9` so the image can't drift from what we test. Reproduced locally before fixing (3 installs → 3 root handlers → 3× output). 160 tests.
62. **Refactor: complexity reduction (no behavior change)** —
    - `spotiflac_patch.py` — all monkey-patching (`disable_progress_manager`, `reset_progress_manager`, `silence_spotiflac`, `silence_spotiflac_loggers`) moved out of `downloader.py`/`fix_metadata.py`. Import-time side-effect preserved (regression-tested).
    - `flac_utils.py` — shared helpers `get_spotify_id_from_file` (was duplicated ×3), `embed_cover` (×3), `upgrade_cover_url`, `iter_flacs`; `_get_jpeg_dimensions` → public `get_jpeg_dimensions`.
    - `maintenance.py` — `rescan_library` moved out of `downloader.py` (bot's `/rescan` + `scripts/fix_covers.py` CLI now share it; `dry_run` param added to keep fix_covers' `--dry-run`).
    - `worker.py` — 4-outcome failure state machine extracted into pure `decide_failure(item, result, gave_up_titles) → FailureDecision` (fail-timeout / fail-max-retries / done-partial / requeue-backoff), tested directly (6 new tests); `_handle_no_failures`/`_handle_failure` are thin dispatchers with a shared `_result_summary()`.
    - `scripts/` — package with the 7 maintenance/one-off CLIs (fix_metadata, fix_covers, fix_mb_tags, fix_original_filenames, retag_missing, backfill_urls, fix_qvc), each with a `sys.path` bootstrap for standalone use; bot + tests import updated (`from scripts.fix_metadata import fix_library`). `fix_qvc.py` stays gitignored at its new path.
    - 166 tests.
63. **`MAX_CONCURRENT` 3→2 + bot message consistency** —
    - `config.MAX_CONCURRENT = 2` (per downloader semaphore + `max_concurrent_downloads`).
    - `/fixmetadata` with no folder now defaults to the whole library (consistent with `/rescan`); folder args joined with spaces so multi-word folders like `Noyz Narcos` resolve correctly.
    - One message convention everywhere: `{emoji} <b>Title</b>` + 2-space-indented body lines (paths in `<code>`). Applied to queued/already-queued, `/mkplaylist`, worker notifications (success, partial, timeout, max-retries, requeue, internal error), and the auto-m3u8 message.
    - `format_help()` now lists `/help` and `/rescan` (were missing). 161 tests.
