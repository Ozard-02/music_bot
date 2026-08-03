# Code Structure

## Core download engine

### `config.py` — shared constants + setup utilities

```
Constants: SERVICES=["qobuz","deezer","amazon"], MAX_CONCURRENT=2,
           MAX_PARALLEL_JOBS=3, PER_TRACK_TIMEOUT=100, PER_TRACK_RETRIES=3,
           MAX_QUEUE_RETRIES=15, MAX_TRACK_RETRIES=10,
           MAX_DOWNLOAD_TIMEOUT=28800, MAX_QUEUE_AGE=86400

load_config(logger) → dict               # read ~/.spotiflac/config.json (incl. maxDownloadTimeout override)
setup_logger(log_path) → logger          # RotatingFileHandler (5MB×3) + stream
bridge_community_session(logger)         # copy desktop Tidal session
```

### `downloader.py` — download engine only
```
DownloadResult(ok, skipped, failed, failed_tracks, gave_up_tracks, total)  # frozen dataclass, to_dict()
run_url(url, cfg, logger, skip_titles=None, progress_cb=None, failure_cb=None) → DownloadResult  ← entry point
  progress_cb(done, total, title) — called per completed track (via asyncio.to_thread) for live /status
  failure_cb(title, err)          — called per failed track (live, so give-up advances even if the job times out)
├─ parse_spotify_url(url)
├─ if "track":
│  ├─ get_track_metadata(url)
│  ├─ construct path via track_relative_path()
│  ├─ if path exists → early return DownloadResult(skipped=1, total=1)
│  └─ else → client.download_track(url)
└─ if "album"|"playlist":
   ├─ get_playlist(url) → (info, tracks)  (info ignored for collection downloads)
   ├─ partition_tracks(tracks, cfg, skip_titles) → (existing, given_up, missing)
   │  (dedup by track.id; path check via track_relative_path() original-symbols form)
   ├─ if all exist → early return DownloadResult(skipped=N, gave_up_tracks=[...])
   ├─ log "Pre-check: N/M exist (X new, Y given up)"
   └─ download each missing track in parallel:
      asyncio.gather(*[download_track(t.external_url) for t in missing])
      with asyncio.Semaphore(MAX_CONCURRENT) limiting concurrency
       after each successful download → `_rename_after_download()` + `_fix_cover()`
       (moves SpotiFLAC's `_`-path to original-symbols path, overwrites cover with Spotify art)
```
Cover/rename helpers use `flac_utils.embed_cover()` + `upgrade_cover_url()`. The
SpotiFLAC monkey-patches live in `spotiflac_patch.py` (imported here for its
import-time side-effect). Downloads enrich with `["apple","deezer","soundcloud"]`
(no tidal) and embed no MusicBrainz tags (see `_patch_musicbrainz`), so fresh
downloads already match `/fixmetadata` output.

### `maintenance.py` — library maintenance
```
rescan_library(cfg, logger, progress=None, dry_run=False) → {ok, skipped, failed}
  ├─ walk output_dir for *.flac (flac_utils.iter_flacs)
  ├─ for each: read URL tag → parse Spotify track ID (flac_utils.get_spotify_id_from_file)
  ├─ SpotifyMetadataClient → get_track_async(id) → fetch 640px cover → embed (flac_utils.embed_cover)
  ├─ asyncio.Semaphore(SCRIPT_MAX_CONCURRENT=5) limiting concurrency
  └─ progress(current, total, text) callback for UI updates
```
Backs the `scripts/fix_covers.py` CLI (with `--dry-run`). The bot no longer has a `/rescan`
command — cover refresh now lives inside `/fixmetadata` (see `scripts/fix_metadata.py`).

### `spotiflac_patch.py` — all SpotiFLAC monkey-patching in one place
```
disable_progress_manager()   — runs once at import; neutralizes ProgressManager's
  class-level asyncio state (_event_queue/_worker_task), makes
  enqueue_progress/start_worker/initialize_master_bar no-ops (kills the
  "Queue bound to a different event loop" RuntimeError flood), and no-ops
  install/uninstall_console_interception in SpotiFLAC.core.progress AND
  SpotiFLAC.downloader (that function strips every StreamHandler off the root
  logger per track download — handlers piled up, freezing spoty_loop.log).
reset_progress_manager()     — detach class-level state before using SpotiFLAC in a new loop
install_console_silencing()   — runs once at import; permanently no-ops console.print_*,
  progress.safe_tqdm_write and builtins.input, then overwrites the module-level
  copies in every already-imported SpotiFLAC module (they do `from .core.console
  import print_summary` — patching console attrs alone is useless). Never
  restored → no multi-thread restore race (kills SESSION SUMMARY boxes,
  ✗/⚠️/⏱ lines and the "Incolla qui il grant" prompt in production logs).
_patch_qobuz_lock()           — runs once at import; wraps QobuzProvider.__init__ so
  _creds_lock becomes a loop-agnostic _AsyncLockAdapter (threading.Lock behind
  async-with). SpotiFLAC awaits the provider's asyncio.Lock from fresh loops
  (to_thread + asyncio.run) → "bound to a different event loop" → 100s timeouts.
_patch_musicbrainz()          — runs once at import; no-ops SpotiFLAC's per-track
  MusicBrainz lookup (every provider writes MUSICBRAINZ_* ids + extras via
  extra_tags=mb_tags → Navidrome splits albums into multiple releases).
  mb_result_to_tags → {} always, AsyncMBFetch never spawns a thread,
  fetch_mb_metadata_async → {} (saves ~12s/track), then overwrites the
  import-copied names in already-imported SpotiFLAC modules (same sweep as
  console silencing). Downloads now match /fixmetadata output out of the box.
silence_spotiflac_loggers()  — set all SpotiFLAC.* loggers to CRITICAL + httpx/httpcore to WARNING
```

### `flac_utils.py` — shared FLAC/tag/cover helpers
```
get_spotify_id_from_file(path) → str | None   # read URL/comment tag, extract track ID
upgrade_cover_url(url)                        # Spotify CDN 300×300 (1e02) → 640×640 (b273)
upgrade_apple_cover(url, size="3000x3000")    # iTunes artwork 100×100 → HD size
resolve_cover_data(track) → bytes | None      # best cover: Apple HD (3000², ISRC enrichment) else upgraded Spotify 640
fetch_cover(url, timeout=10) → bytes | None   # async GET of upgraded Spotify cover (used by maintenance.py)
embed_cover(path, data)                       # replace pictures with JPEG front cover + dimensions
iter_flacs(root)                              # yield every .flac under root (sorted)
```

### `track_utils.py` — shared path utilities
```
sanitize(text, spotiflac_mode=False)  # preserve special chars; only `/` → `∕`
spotiflac_sanitize / spotiflac_track_relative_path   # SpotiFLAC's `_`-sanitized variant
track_relative_path(track, cfg)       # {AlbumArtist}/{Album}/{Artist} - {Title}.flac
partition_tracks(tracks, cfg, skip_titles=None) → (existing, given_up, missing)
                                      # dedup by track.id, split by on-disk path check +
                                      # title in skip_titles (shared by downloader + m3u8)
get_jpeg_dimensions(data) → (w, h)    # JPEG SOF0/1/2 scan (0,0 if not JPEG)
prune_empty_parents(path, root)       # rmdir empty ancestor dirs up to root (shared
                                      # by downloader rename + fix_original_filenames)
```

### `library.py` — per-user library layout
```
QUALITY_CHOICES = ["DOLBY_ATMOS","HI_RES_LOSSLESS","LOSSLESS","HIGH","LOW"]  # SpotiFLAC set
FOLDER_SUFFIX = "_Music"
user_folder_name(username, fallback="user") → "espo_Music"   # sanitize + suffix
user_cfg(cfg, folder) → cfg copy with output_dir = root/folder
```
Each allowed Telegram user owns `~/Music/{username}_Music/`; the worker
resolves every item's cfg through `user_cfg()` so downloads, pre-checks,
m3u8 files and `/fixmetadata` all stay inside the user's folder.

## Bot system

### `bot.py` — Telegram bot entry point
```
main()
├─ TOKEN + ALLOWED_USER_IDS (comma-separated TELEGRAM_ALLOWED_USER_IDS, legacy
│  TELEGRAM_ALLOWED_USER_ID fallback) from env
├─ setup_logger(), bridge_community_session(), load_config()  (from config.py)
├─ SingleInstanceLock(queue.db.lock).acquire() — flock-based, blocks in standby
│  until sole instance, then proceeds (prevents two bots / Telegram conflicts)
├─ require_auth decorator — all handlers guarded by _is_allowed (id ∈ ALLOWED_USER_IDS)
├─ QueueManager(queue.db)
├─ creates asyncio.Event() — shared wake signal
├─ Application.builder().post_init(post_init)  # migrates .playlist_covers/, starts Worker(wake_event)
├─ handlers: /start, /help, /status, /quality, /purge, /mkplaylist, /fixmetadata, text
│  ├─ _get_or_create_user() — upsert user row on first interaction (sticky folder)
│  │  folder = user_folder_name(username, fallback=first_name); default quality = cfg quality
│  ├─ handle_message upserts the user, stamps enqueue_unique(..., user.id), wakes worker
│  ├─ status_cmd shows counts + a "Running:" section (per-job progress like
│  │  "3/10 · Now: Song X · 2m 05s" from queue.progress + started_at, via
│  │  qm.get_running() and _format_duration) — running items omitted from Recent;
│  │  each item labelled with its owning user
│  ├─ quality_cmd — no arg: list QUALITY_CHOICES + current; arg: validate + set_user_quality
│  ├─ mkplaylist_cmd runs build_m3u8 directly (async) with the user's cfg (user_cfg)
│  │  → .m3u8 + cover land in the user's folder; relative paths resolve there
│  └─ fixmetadata_cmd runs fix_library() (from scripts/fix_metadata.py) on the
│     calling user's folder (no-arg) or a folder arg resolved against it,
│     with progress callback (summary edit_text carries parse_mode="HTML")
│  all HTML messages escape user/remote content via config.esc() (html.escape) —
│  a raw & < > in a track/playlist name makes Telegram reject the message
└─ run_polling()  (finally → lock.release())
```

### `queue_manager.py` — SQLite persistence
```
QueueManager(db_path)
  Persistent connection (check_same_thread=False) with threading.Lock
  enqueue_unique(type, query, user=None) → (id, is_new)  # atomic dedup + insert
  dequeue() → item | None                   # atomically set status='running'
  get_item(id) → item | None                # fetch current DB row
  requeue(id)                               # increment retries, set 'queued' (clears progress)
  get_next_retry_at() → datetime | None     # earliest retry_at of queued items
  mark_done(id, ok, skipped, failed)        # clears progress
  mark_failed(id, error)                    # clears progress
  set_progress(id, text)                    # live "3/10 · Now: Song" for /status
  get_status() → {queued, running, done, failed, next_id}
  get_running() → [item, ...]               # status='running' rows
  log_failed_track(item_id, title, error)   # per-track failures
  get_failed_tracks(item_id=None, limit=50)
  get_give_up_titles(item_id, threshold) → set   # titles failed ≥ threshold×
  purge_all() → count                       # DELETE all rows
  get_history(limit) → [item, ...]
  upsert_user(id, username, folder, quality)  # folder sticky; username/quality refresh
  get_user(id) → row | None
  get_users() → [row, ...]
  set_user_quality(id, quality)

Tables:
  queue       — id, input_type, query, status, retries, created_at,
                result_*, completed_at, error, retry_at, progress, user
  users       — telegram_user_id PK, username, folder, quality, created_at, updated_at
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

best_track_match(tracks, artist_hint, title_hint) → track  # shared "best result" picker
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
  Pure helpers (no DB/bot):
    item_age(item, now=None) → float        # seconds since created_at (0 if unparseable)
    is_expired(item, now=None) → bool       # age > MAX_QUEUE_AGE or retries >= MAX_QUEUE_RETRIES
    backoff_delay(retries, floor=0) → int   # min(MAX_RETRY_BACKOFF, BASE * 2**retries), floored
    decide_failure(item, result, gave_up_titles) → FailureDecision  # fail-timeout/fail-max/done-partial/requeue
  Per-item context:
    _item_cfg(item) → dict                  # user_cfg(base, user.folder) + user.quality;
                                            # legacy items (no user) keep the base cfg
    _item_chat(item) → int | None           # notify the user who queued it (default chat for legacy)
    _notify(text, chat_id=None)             # send to item owner; failed send never touches DB status
  _auto_build_m3u8(item, cfg, chat):
    ├─ skip if input_type != "link"
    ├─ skip if parsed type != "playlist"
    ├─ build_m3u8(item["query"], cfg=per-item cfg) → notify via bot
    └─ called after success, 24h timeout, max-retries failure (not on requeue)
  _process(item):
    chat = _item_chat(item); cfg = _item_cfg(item)
    "link"   → url = item.query
    "search" → AsyncSpotiFLAC → resolve_search() → url
    skip_titles = get_give_up_titles(item, MAX_TRACK_RETRIES=10)  # tracks that gave up
    result = _run_download(item, url, display, skip_titles, cfg, chat) → DownloadResult | None
    ├─ None → timeout (already requeued by _handle_timeout)
    ├─ any failed → _handle_failure() → decide_failure(...) (see pure helpers)
    │  ├─ "fail" (age >24h)     → mark_failed("Timed out") + _auto_build_m3u8()
    │  ├─ "fail" (retries ≥MAX) → mark_failed("Max retries") + _auto_build_m3u8()
    │  ├─ "done" (all gave up)  → mark_done(partial) + notify + m3u8
    │  └─ "requeue" (delay)     → requeue with backoff_delay
    └─ all ok → _handle_no_failures(): mark_done + _auto_build_m3u8() + send summary
       (given-up tracks reported separately as "❌ N given up")
  _run_download(item, url, display, skip_titles, cfg, chat): wires progress_cb
    (→set_progress) + failure_cb (→log_failed_track live) into _run_url_sync, runs it
    via asyncio.wait_for (timeout=cfg.max_download_timeout); on asyncio.TimeoutError
    → _handle_timeout() and returns None
  _handle_timeout(): is_expired() → mark_failed + give-up msg; else requeue w/ backoff_delay(retries, floor=1800)
  _run_with_sem(item): in-flight guard via self._active {item_id: retries} — an item already
    running (or requeued after a timeout, its leaked thread still alive) is deferred 30 min, never duplicated
  _mark_done_and_notify(item, display, result, given_up, chat) — shared completion path
    (mark_done + `_done_message()` + notify + auto-m3u8) for clean success AND all-gave-up partials
    on exception → mark_failed() → send error
  _notify(text, chat_id) — all notifications go through this; a failed send (e.g. Telegram
    reject / network) only logs a warning and never touches the DB status, so a
    successful item can't be flipped to failed. All remote content (display names,
    queries, playlist names) HTML-escaped via config.esc()
```

## SpotiFLAC patches
- `SpotiFLAC/core/tagger.py`: `_embed_flac` strips `MUSICBRAINZ_*` before writing Vorbis comments.
- `spotiflac_patch.py`: runtime monkey-patches (ProgressManager, console interception, logger noise) — see section above.

## Helper scripts — `scripts/`

Maintenance/one-off CLIs live in `scripts/` (a package, so `bot.py` can do
`from scripts.fix_metadata import fix_library`). Each script bootstraps
`sys.path` so it also runs standalone: `python scripts/<name>.py`.

- `scripts/fix_metadata.py` — re-tag FLAC metadata via SpotiFLAC pipeline (Apple-first enrichment), strip bogus `MUSICBRAINZ_*`, move files to their real album folder. `fix_album_folder()` for one folder, `fix_library()` to walk a whole root. When a track has a Spotify `cover_url`, the 640×640 cover is fetched (`upgrade_cover_url` 1e02→b273, httpx timeout=10) and passed as `cover_data` to `embed_metadata_async`, so the **upgraded Spotify cover is embedded** and enrichment covers can't override it; a failed fetch still retags. CLI: `python scripts/fix_metadata.py <folder> [--apply]`. Also used by the bot's `/fixmetadata`.
- `scripts/fix_covers.py` — thin CLI over `maintenance.rescan_library()` (re-embed Spotify cover art); `--dry-run` supported.
- `scripts/fix_original_filenames.py` — one-time rename: SpotiFLAC `_`-paths → original-symbols paths (regression-tested, incl. dry-run + empty-dir pruning)
- `scripts/migrate_library.py` — one-time move of root-level `~/Music/{Album}/...` entries into the owner's `{username}_Music/` folder (excludes `*_Music`, `shared_Music`, dotfiles; `.m3u8` files move with their tracks so relative paths stay valid). CLI: `python scripts/migrate_library.py --username espo [--dry-run]`.
- `scripts/archive/` — superseded one-off scripts kept for reference: `fix_mb_tags.py` (strip MUSICBRAINZ_* — superseded by `/fixmetadata`), `retag_missing.py` (hardcoded tagless files — predecessor of fix_metadata), `backfill_urls.py` (write Spotify URL tags). Not wired into the bot or tests.

## Other root modules
- `m3u8.py` — generate .m3u8 for a Spotify playlist from tracks already on disk, download cover sidecar

  ```
  python m3u8.py <playlist_url> [playlist_name]
  build_m3u8(url, name=None, cfg=None) → {path, playlist_name, total_tracks, exist_on_disk, missing_count, cover_path, missing_log_path}
  _download_cover(url, path) — httpx GET → write image to path
  build_m3u8_lines(tracks, cfg) → (lines, count, missing)  # via partition_tracks (dedup by track.id)
  ```

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
