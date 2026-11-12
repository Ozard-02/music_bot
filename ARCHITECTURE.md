# Architecture

The system is a **lean front ends + short-lived workers** split: the Telegram
bot and the HTTP API are both stdlib-only processes, and every job that needs
SpotiFLAC runs in its own subprocess.

```
Telegram ──► bot.py (stdlib) ──────────────┐
                                          ├─► SQLite queue (WAL)
Laila ─────► api.py (stdlib) ──search─────┘         │
                 │                                  ▼
                 └─spawn─► search_job.py      worker.py ─spawn─► download_job.py
                           (SpotiFLAC)                          (SpotiFLAC)
                                │                                   │
                                └─► JSON results                    └─► FLAC on disk
```

## Why two processes

The old single-process bot (python-telegram-bot + SpotiFLAC) idled at ~252MiB;
after removing the browser stack it dropped to ~89MiB. The floor for one
process is ~46MiB (telegram + httpx stack) plus SpotiFLAC's ~57MiB once
imported. Idle RSS is a hard requirement (see AGENTS.md), so we split:

- **Front ends** (`bot.py`, `api.py`) import stdlib only: `asyncio`, `json`,
  `urllib`, `sqlite3`, `http.server`, `fcntl`, `logging`, `pathlib`. Bot idle
  target **~15-20MiB**; the API idles in the same ballpark.
- **Subprocesses** (`download_job.py`, `search_job.py`) import SpotiFLAC +
  patches. Spawned per job, they exit when the job ends, so peak RSS is
  **reclaimed** — no lingering threads, no `_trim_rss` needed for the heavy
  part.

## Process split (what runs where)

### Front ends (stdlib only)
| Module | Role |
|--------|------|
| `telegram_client.py` | Raw Telegram Bot API client on `http.client` with two persistent keep-alive connections (one for the long-poll, one locked-shared for sends) |
| `bot.py` | Message dispatch, command handlers, allowlist, main loop |
| `api.py` | HTTP API for Laila: health/search/queue/cover, Bearer auth, CORS allowlist, search cache + single-flight |
| `queue_manager.py` | SQLite queue (`queue`, `users`, `failed_tracks`); WAL + busy timeout for two processes |
| `worker.py` | Job orchestration; owns `stream_job()`, the shared spawn/stream/watchdog IPC loop used by both downloads and one-shot commands |
| `config.py` | Constants, config.json loading, `.env` loading, logger setup (`silence_spotiflac_loggers` inlined) |
| `resolver.py` | Input parsing + search resolution (`parse_spotify_url`); no SpotiFLAC import in the parent |
| `library.py` | Library paths (user folders, per-user output dir) |
| `track_utils.py` | Filename/path sanitization; `TrackMetadata` imported `TYPE_CHECKING` only |

### Subprocesses (SpotiFLAC; spawned per job)
| Module | Role |
|--------|------|
| `download_job.py` | Entry point: reads one JSON spec from stdin, runs the job, emits JSON-lines |
| `search_job.py` | Entry point: reads one JSON search spec, emits one JSON result object |
| `downloader.py` | Download engine (`run_url`): resolution, partition, parallel download, rename, cover |
| `spotiflac_patch.py` | Import-time monkey-patches (GitHub version-check skip, console silencing, community-dead, provider IO timeout cap, MB no-op, provider tracking, provider matching guards) |
| `m3u8.py` | Playlist m3u8 + cover generation (auto-build happens here, not in parent) |
| `flac_utils.py` | FLAC tag read/write, cover embed/dedupe (httpx/mutagen/pillow live here) |

### Provider matching guards

SpotiFLAC falls back to fuzzy text search when an ISRC is missing from a
provider catalog.  Qobuz's fallbacks are unsafe: `_search_by_text_async`
returns its best candidate even below its own threshold, and
`_search_by_text_with_duration_async` picks the closest duration without
checking title or artist.  That downloaded a random unrelated track while
Spotify tags were applied on top (right metadata, wrong audio).

`spotiflac_patch._patch_provider_matching()` wraps those searches (plus
Deezer's text search and Qobuz's ISRC search) and rejects candidates whose
title/artist — and ISRC/duration where available — do not plausibly match.
Rejections are logged to `spoty_loop.patch` at `WARNING`; the provider then
fails and the next service runs (Deezer does an exact ISRC lookup).

## IPC protocol

**Job request** — parent writes one JSON line to the child's **stdin**:

```json
{"id": 12, "url": "https://open.spotify.com/...", "type": "link",
 "cfg": {"output_dir": "...", "quality": "LOSSLESS", ...},
 "skip_titles": ["x"], "want_m3u8": true}
```

`type` is `"link"` (URL, spotify: URI, album/playlist/track) or `"search"`
(`artist - song`, any dash kind). Parent closes stdin after writing; the child
reads one line, then runs.

**Events** — child writes JSON lines to **stdout** (one object per line):

| Event | Payload |
|-------|---------|
| `{"event":"progress","done":3,"total":12,"title":"...","provider":"qobuz"}` | one per completed track |
| `{"event":"failure","title":"...","error":"..."}` | one per failed track |
| `{"event":"result","result":{...}}` | **final** line: `DownloadResult.to_dict()` |

The parent treats `result` as the job's end. A nonzero child exit without a
`result` line counts as a crash (failed job, retried by the worker).

**File descriptors** — strict separation:
- **stdout = protocol only.** Logging never goes to stdout.
- **stderr = logs.** Logger writes to stderr and the rotating log file.
- **stdin = job spec** (one line).

This is why `setup_logger` in the child must keep its StreamHandler on stderr.

## Search IPC (HTTP API)

`api.py` spawns `search_job.py` for every cache miss:

- **stdin**: one JSON line `{"query": "...", "limit": 20}`.
- **stdout**: exactly one JSON object:
  `{"ok": true, "tracks": [...], "albums": [...], "playlists": [...]}`, or
  `{"ok": false, "kind": "spotify_blocked", "error": "..."}`.
- **stderr**: logs only. The API captures them and never forwards them.
- The child sets `sync_extensions=False` and uses `spotiflac_patch` (no GitHub
  version check) only in anonymous mode. It wraps the metadata client's
  `query` so a swallowed Spotify 406/429 is reported as `spotify_blocked`
  instead of "no results".
- **Official mode:** if `~/.spotiflac/spotify.json` (managed by Telegram
  `/spotify`) or `.env` has client credentials or an access token, `api.py`
  sends `spotify_mode=official` + the token to the child, which calls the
  official Web API with stdlib `urllib`. Client credentials are refreshed in
  the API process and retried once on a 401. The file is re-read on every
  search, so `/spotify` changes need no restart. Without credentials, the
  child uses SpotiFLAC's anonymous web token.
- The API caches results for `SEARCH_CACHE_TTL` (default 60s) and serializes
  cache misses, so typing does not fan out into parallel Spotify requests.

## HTTP API

| Endpoint | Auth | Notes |
|----------|------|-------|
| `GET /api/health` | Bearer | Token/connectivity check |
| `GET /api/search?q=&limit=` | Bearer | Tracks/albums/playlists in Spotify's order |
| `POST /api/queue` | Bearer | Canonical `track/album/playlist` links, dedupes |
| `GET /api/cover?u=` | none | Spotify CDN image proxy |

- Queue writes share `queue.db` with the bot through WAL; `busy_timeout=5000`
  makes the two processes wait instead of failing on a writer lock.
- **Single-container mode:** `RUN_API=1` starts the API in a daemon thread
  inside `bot.py` (used by the TrueNAS `ix-app`, which already runs host
  networking with a portal on 8099). It opens its own `QueueManager`
  connection, and searches still run in `search_job.py` subprocesses. With
  `RUN_API` unset, the API is a separate `python api.py` process as in the
  local compose.
- `API_QUEUE_USER` (default: first Telegram allowlist id, including the legacy
  `TELEGRAM_ALLOWED_USER_ID`) decides whose folder/quality API downloads use.
- Covers are restricted to `*.scdn.co` / `*.spotifycdn.com`, fetched without
  following redirects, capped at 5MiB, and kept in a small in-memory LRU.
  The endpoint has no auth so `<img>`/expo-image can load it; never expose the
  API port publicly.
- CORS only echoes origins listed in `API_CORS_ORIGINS`.

## Stall watchdog

`worker.stream_job()` reads the child's stdout asynchronously. If no line
arrives for `stall_timeout_seconds` (config, default 1800), or the job exceeds
its overall deadline, the child is SIGKILLed and reaped. The same loop kills
on task cancellation. This permanently fixes the old "leaked straggler thread"
bug: a dead child's RSS is reclaimed by the kernel, no orphan threads survive.

## Cross-process in-flight guard

The old `_in_flight` set was process-local. Now each job is its own process,
so overlap protection moves to **`fcntl.flock` lockfiles**:

- One lockfile per track id under `<output_dir>/.inflight/<id>.lock`.
- Child acquires `LOCK_EX | LOCK_NB` before downloading a track; a second job
  for the same track gets `EWOULDBLOCK` -> reports `skipped`, never double-writes.
- Lockfile remains (harmless); `flock` releases automatically on child exit.

## Energy profile (idle)

- Telegram long-poll: one held HTTPS request (`getUpdates?timeout=50`) on a
  persistent keep-alive connection — zero TLS handshakes while idle (the old
  urllib client shook hands on every API call, ~1700/day from polling alone).
- Worker: `asyncio.Event` — wakes instantly when the bot enqueues, otherwise
  sleeps with idle backoff 5s -> 10 -> 20 -> 40 -> 80 -> 160 -> 300s cap.
- Spotify block: a 406/429 from `*.spotify.com` is tagged `spotify_blocked`
  by the job; the worker then stops dequeuing for `SPOTIFY_BLOCK_PAUSE`
  (30min) instead of letting every item retry against a rate-limited IP.
- Download subprocesses skip SpotiFLAC's import-time GitHub version check
  (one useless HTTPS request per job).
- QueueManager: one persistent SQLite connection, no polling.
- `gc.freeze()` after startup: permanent objects are never rescanned.
- Idle cost is one long-poll request + zero DB polls. Near-zero CPU.

## RSS budget

Measured (host, import chain): interpreter ~12.6MiB + logging ~4.3MiB +
asyncio ~5.5MiB + http.client/ssl ~2.5MiB + app code — **parent idle
~26MiB** (was ~29.4MiB on `urllib.request`). `MALLOC_ARENA_MAX=2` (Dockerfile/
compose) trims glibc arena overhead under threads; container idle lands in the
high-20s MiB.

| Component | RSS |
|-----------|-----|
| python interpreter base | ~12-13MiB |
| stdlib (`logging`, `asyncio`, `sqlite3`, `http.client`, `json`) | ~12MiB |
| parent app code | ~1-2MiB |
| **parent idle total** | **~26MiB** |
| subprocess peak (during job) | ~150-250MiB (transient, reclaimed at exit) |
| idle container total | **high-20s MiB** |