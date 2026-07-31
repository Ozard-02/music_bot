# Code Structure

## Core download engine

### `config.py` — shared constants + setup utilities

```
Constants: SERVICES=["amazon","qobuz"], MAX_CONCURRENT=3,
           PER_TRACK_TIMEOUT=100, PER_TRACK_RETRIES=3,
           MAX_QUEUE_RETRIES=15, MAX_TRACK_RETRIES=10,
           MAX_DOWNLOAD_TIMEOUT=3600

load_config(logger) → dict               # read ~/.spotiflac/config.json
setup_logger(log_path) → logger          # RotatingFileHandler (5MB×3) + stream
bridge_community_session(logger)         # copy desktop Tidal session
```

### `downloader.py`

```
run_url(url, cfg, logger, skip_titles=None) → {ok, skipped, failed, failed_tracks, gave_up_tracks}  ← entry point
├─ parse_spotify_url(url)
├─ if "track":
│  ├─ get_track_metadata(url)
│  ├─ construct path via track_relative_path()
│  ├─ if path exists → early return {ok=0, skipped=1}
│  └─ else → client.download_track(url)
└─ if "album"|"playlist":
   ├─ get_playlist(url) → (info, tracks)  (info ignored for collection downloads)
   ├─ dedup by track.id
    ├─ pre-check: construct path for each track via track_relative_path()
    │  (uses original-symbols path via `sanitize()` — only `/` → `∕`)
    │  → split into existing/missing/given_up (titles in skip_titles)
    ├─ if all exist → early return {ok=0, skipped=N}
    ├─ log "Pre-check: N/M exist (X new, Y given up)"
    └─ download each missing track in parallel:
       asyncio.gather(*[download_track(t.external_url) for t in missing])
       with asyncio.Semaphore(MAX_CONCURRENT) limiting concurrency
        after each successful download → `_rename_after_download()` + `_fix_cover()`
        (moves SpotiFLAC's `_`-path to original-symbols path, overwrites cover with Spotify art)

   rescan_library(cfg, logger, progress=None) → {fixed, skipped, failed, errors}
     ├─ walk output_dir for *.flac
     ├─ for each: read URL tag → parse Spotify track ID
     ├─ AsyncSpotiFLAC → get_track_async(id) → download + embed cover
     ├─ asyncio.Semaphore(SCRIPT_MAX_CONCURRENT=5) limiting concurrency
     └─ progress(current, total, text) callback for UI updates

   _disable_progress_manager() — runs once at import; neutralizes SpotiFLAC
     ProgressManager's class-level asyncio state (_event_queue/_worker_task) and
     makes enqueue_progress/start_worker no-ops. Fixes the "Queue bound to a
     different event loop" RuntimeError flood.
```

## Bot system

### `bot.py` — Telegram bot entry point
```
main()
├─ TOKEN + ALLOWED_USER_ID from env
├─ setup_logger(), bridge_community_session(), load_config()  (from config.py)
├─ require_auth decorator — all handlers guarded by _is_allowed
├─ QueueManager(queue.db)
├─ creates asyncio.Event() — shared wake signal
├─ Application.builder().post_init(post_init)  # migrates .playlist_covers/, starts Worker(wake_event)
├─ handlers: /start, /help, /status, /purge, /mkplaylist, /rescan, /fixmetadata, text
│  ├─ handle_message sets wake_event after enqueue → worker wakes instantly
│  ├─ mkplaylist_cmd runs build_m3u8 directly (async), shows "🖼️ Cover saved" if cover downloaded
│  ├─ rescan_cmd runs rescan_library() with Telegram progress callback ("🔍 Rescan N/M")
│  └─ fixmetadata_cmd runs fix_library() (from fix_metadata.py) with progress callback
│     folder arg resolved against cfg["output_dir"], applies changes, reports summary
└─ run_polling()
```

### `queue_manager.py` — SQLite persistence
```
QueueManager(db_path)
  Persistent connection (check_same_thread=False) with threading.Lock
  enqueue(type, query) → id
  find_existing(type, query) → id | None   # duplicate check
  dequeue() → item | None                   # atomically set status='running'
  get_item(id) → item | None                # fetch current DB row
  requeue(id)                               # increment retries, set 'queued'
  mark_done(id, ok, skipped, failed)
  mark_failed(id, error)
  purge_all() → count                       # DELETE all rows
  log_failed_track(item_id, title, error)   # per-track failures
  get_failed_tracks(item_id=None, limit=50)
  get_give_up_titles(item_id, threshold) → set   # titles failed ≥ threshold×
  get_status() → {queued, running, done, failed, next_id}
  get_history(limit) → [item, ...]

Tables:
  queue       — id, input_type, query, status, retries, created_at,
                result_*, completed_at, error
  failed_tracks — id, item_id (FK→queue), track_title, error, failed_at
```

### `resolver.py` — input parsing + Spotify search
```
parse_input(text) → (type, value)
  "link" — open.spotify.com URL
  "search" — "X - Y" format
  "invalid" — unrecognized

resolve_search(client, query) → (url, name, type)
  "Artist - Album"   → search album → album URL
  "Artist - Song"    → search track → track URL
  "Album - Song"     → search track → track URL
  picks best match via artist/album name heuristics

format_help() → HTML help text
```

### `worker.py` — background queue processor
```
Worker(queue, bot, chat_id, cfg, logger, wake_event)
  _poll = 5  (doubles on empty dequeues, caps at 300, resets on dequeue)
  run() — loop:
    dequeue → process → wait_for(wake_event, timeout=_poll)
    * when bot enqueues a new item, it sets wake_event
    * worker wakes instantly (even during 300s idle), clears event, polls
  _auto_build_m3u8(item):
    ├─ skip if input_type != "link"
    ├─ skip if parsed type != "playlist"
    ├─ build_m3u8(item["query"], cfg=self._cfg) → notify via bot
    └─ called after success, 24h timeout, max-retries failure (not on requeue)
  _process(item):
    "link"   → url = item.query
    "search" → AsyncSpotiFLAC → resolve_search() → url
    skip_titles = get_give_up_titles(item, MAX_TRACK_RETRIES=10)  # tracks that gave up
    run_url(url, skip_titles) via asyncio.wait_for(timeout=MAX_DOWNLOAD_TIMEOUT) → result
    ├─ if any failed → log_failed_track() per track → _handle_failure()
    │  ├─ age >24h → mark_failed("Timed out") + _auto_build_m3u8()
    │  ├─ retries ≥MAX_QUEUE_RETRIES → mark_failed("Max retries") + _auto_build_m3u8()
    │  ├─ remaining failures all ≥ MAX_TRACK_RETRIES → mark_done(partial) + notify + m3u8
    │  └─ else → requeue()
    └─ if all ok → _handle_no_failures(): mark_done + _auto_build_m3u8() + send summary
       (given-up tracks reported separately as "❌ N given up")
    on exception → mark_failed() → send error
```

## SpotiFLAC patch
`SpotiFLAC/core/tagger.py`: `_embed_flac` strips `MUSICBRAINZ_*` before writing Vorbis comments.

## Helper scripts
- `fix_metadata.py` — re-tag FLAC metadata via SpotiFLAC pipeline (Apple-first enrichment), strip bogus `MUSICBRAINZ_*`, move files to their real album folder. `fix_album_folder()` for one folder, `fix_library()` to walk a whole root. CLI: `python fix_metadata.py <folder> [--apply]`.
- `fix_mb_tags.py` — strip MUSICBRAINZ_* tags from all FLACs in ~/Music
- `fix_covers.py` — re-embed Spotify cover art into all FLACs with Spotify track IDs
- `fix_original_filenames.py` — one-time rename: SpotiFLAC `_`-paths → original-symbols paths
- `m3u8.py` — generate .m3u8 for a Spotify playlist from tracks already on disk, download cover sidecar
- `track_utils.py` — shared path utilities (`sanitize`, `spotiflac_sanitize`, `track_relative_path`, `spotiflac_track_relative_path`)

  ```
  python m3u8.py <playlist_url> [playlist_name]
  build_m3u8(url, name=None, cfg=None) → {path, playlist_name, total_tracks, exist_on_disk, cover_path, missing_log_path}
  _download_cover(url, path) — httpx GET → write image to path
  build_m3u8_lines(tracks, cfg) → (lines, count)  # dedup by track.id
  ```

  Path utilities live in `track_utils.py`; `m3u8.py` and `downloader.py` both import from there.

- `config.default.json` — reference config (6 keys)
- `IMPROVEMENTS.md` — planned Docker + enhancements

## Docker deployment files

### `Dockerfile`
```
FROM python:3.14-slim
├─ apt: chromium (for nodriver CDP)
├─ pip: spotiflac + python-telegram-bot
├─ COPY . /app
├─ ENV CHROME_PATH=/usr/bin/chromium
├─ ENV CHROME_FLAGS="--no-sandbox --disable-dev-shm-usage"
└─ CMD ["python", "bot.py"]
```

### `docker-compose.yml`
```
services:
  spoty-loop:
    build: .
    container_name: spoty-loop
    env_file: .env
    environment:
      - QUEUE_DB_PATH=/root/.spotiflac/queue.db
    volumes:
      - ~/Music:/root/Music                        # FLAC output
      - ~/.spotiflac:/root/.spotiflac            # config + session + queue.db
      - .:/app                                     # code (mount for testing)
    restart: unless-stopped
```

### `.dockerignore`
Excludes `.venv`, `__pycache__`, `.git`, `.env`, `*.log`, `*.db`, `AGENTS.md`.

### `bot.py` — `QUEUE_DB_PATH` env var
One-line change: `QUEUE_DB = Path(os.environ.get("QUEUE_DB_PATH", ...))` — points queue.db
to the mounted `~/.spotiflac/` volume when running in Docker, falls back to project root
for bare-metal usage.

## CI/CD

### `.github/workflows/docker.yml`
GitHub Actions workflow: on push to `main`, builds the Docker image and pushes to
`ghcr.io/ozard-02/music_loop:latest`. Auth uses `secrets.GHCR_TOKEN` (PAT with
`write:packages` scope, stored as repo secret).
