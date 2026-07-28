# Code Structure

## Core download engine

### `config.py` — shared constants + setup utilities

```
Constants: SERVICES, MAX_CONCURRENT, PER_TRACK_TIMEOUT, PER_TRACK_RETRIES,
           MAX_QUEUE_RETRIES, MAX_DOWNLOAD_TIMEOUT

load_config(logger) → dict               # read ~/.spotiflac/config.json
setup_logger(log_path) → logger          # file+stream, suppress SpotiFLAC/httpx
bridge_community_session(logger)         # copy desktop Tidal session
```

### `downloader.py`

```
run_url(url, cfg, logger) → {ok, skipped, failed, failed_tracks}  ← entry point
├─ parse_spotify_url(url)
├─ if "track":
│  ├─ get_track_metadata(url)
│  ├─ construct path via track_relative_path()
│  ├─ if path exists → early return {ok=0, skipped=1}
│  └─ else → client.download_track(url)
└─ if "album"|"playlist":
   ├─ get_playlist(url) → info + tracks
   ├─ dedup by track.id
   ├─ pre-check: construct path for each track → split into existing/missing
   ├─ if all exist → early return {ok=0, skipped=N}
   ├─ log "Pre-check: N/M exist (X new)"
   └─ client._downloader._run_once_async(url, target_tracks=missing)

run_url_sync(url, cfg, logger) → dict   # sync wrapper (asyncio.run)
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
├─ Application.builder().post_init(post_init)  # starts Worker(wake_event)
├─ handlers: /start, /help, /status, /purge, /mkplaylist, text
│  ├─ handle_message sets wake_event after enqueue → worker wakes instantly
│  └─ mkplaylist_cmd uses run_in_executor (no wake needed)
└─ run_polling()
```

### `queue_manager.py` — SQLite persistence
```
QueueManager(db_path)
  Persistent connection (check_same_thread=False) with threading.Lock
  enqueue(type, query) → id
  find_existing(type, query) → id | None   # duplicate check
  dequeue() → item | None                   # atomically set status='running'
  requeue(id)                               # increment retries, set 'queued'
  mark_done(id, ok, skipped, failed)
  mark_failed(id, error)
  purge_all() → count                       # DELETE all rows
  log_failed_track(item_id, title, error)   # per-track failures
  get_failed_tracks(item_id=None, limit=50)
  get_status() → {queued, running, done, failed, next_id}
  get_history(limit) → [item, ...]

Tables:
  queue       — id, input_type, query, status, retries, created_at, ...
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
  _process(item):
    "link"   → url = item.query
    "search" → AsyncSpotiFLAC → resolve_search() → url
    run_url_sync(url) via executor (asyncio.wait_for, timeout=MAX_DOWNLOAD_TIMEOUT) → result
    ├─ if any failed → log_failed_track() per track → _handle_failure()
    │  ├─ age >24h → mark_failed("Timed out")
    │  ├─ retries ≥MAX_QUEUE_RETRIES → mark_failed("Max retries")
    │  └─ else → requeue()
    └─ if all ok → mark_done() → send summary
    on exception → mark_failed() → send error
```

## SpotiFLAC patch
`SpotiFLAC/core/tagger.py`: `_embed_flac` strips `MUSICBRAINZ_*` before writing Vorbis comments.

## Helper scripts
- `fix_mb_tags.py` — strip MUSICBRAINZ_* tags from all FLACs in ~/Music
- `fix_covers.py` — re-embed Spotify cover art into all FLACs with Spotify track IDs
- `m3u8.py` — generate .m3u8 for a Spotify playlist from tracks already on disk

  ```
  python m3u8.py <playlist_url> [playlist_name]
  build_m3u8(url, name, cfg) → {path, playlist_name, total_tracks, exist_on_disk}
  build_m3u8_lines(tracks, cfg) → (lines, count)  # dedup by track.id
  ```

- `config.default.json` — reference config (6 keys)
- `IMPROVEMENTS.md` — planned Docker + enhancements
