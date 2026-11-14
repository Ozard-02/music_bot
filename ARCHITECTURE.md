# Architecture

The system is a **lean front ends + short-lived workers** split: the Telegram
bot and the HTTP API are both stdlib-only processes, and every job that needs
SpotiFLAC runs in its own subprocess.

```
Telegram ──► bot.py (stdlib) ──────────────┐
                                          ├─► SQLite queue (WAL)
Laila ─────► api.py (stdlib) ──search─────┘         │
                 │                                  ▼
                 ├─► api.deezer.com (stdlib)   worker.py ─spawn─► download_job.py
                 │                                              (SpotiFLAC)
                 └─► cover CDN proxy                               │
                                                                  └─► FLAC on disk
```

`search_job.py` (Spotify search subprocess) is legacy: the Laila API no longer
spawns it. Telegram name search still resolves through Spotify inside the
download subprocess.

## Why the split

The old single-process bot (python-telegram-bot + SpotiFLAC) idled at ~252MiB;
after removing the browser stack it dropped to ~89MiB. The floor for one
process is ~46MiB (telegram + httpx stack) plus SpotiFLAC's ~57MiB once
imported. Idle RSS is a hard requirement (see AGENTS.md), so we split:

- **Front ends** (`bot.py`, optional `api.py`) import stdlib only: `asyncio`,
  `json`, `http.client`, `sqlite3`, `http.server`, `fcntl`, `logging`,
  `pathlib`. The API can be a separate process or, with `RUN_API=1`, a daemon
  thread inside the bot container.
- **Subprocesses** (`download_job.py`, `search_job.py`) import SpotiFLAC +
  patches. Spawned per job, they exit when the job ends, so peak RSS is
  **reclaimed** — no lingering threads, no `_trim_rss` needed for the heavy
  part.

The realistic floor for the stdlib front end on Python 3.14 is ~27-29MiB:
`asyncio` alone is ~6MiB, logging ~3MiB, SQLite ~1.5MiB. The old 15-20MiB
target predates this stack; the hard rule is still "well under the old 89MiB".

## Process split (what runs where)

### Front ends (stdlib only)
| Module | Role |
|--------|------|
| `telegram_client.py` | Raw Telegram Bot API client on `http.client` with two persistent keep-alive connections (one for the long-poll, one locked-shared for sends) |
| `bot.py` | Message dispatch, command handlers, allowlist, main loop |
| `api.py` | HTTP API for Laila: health/search/queue/cover, Bearer auth, CORS allowlist, Deezer search cache + single-flight; standalone process or `RUN_API` daemon thread in the bot |
| `deezer_search.py` | Stdlib Deezer client: three parallel public-API searches, normalised results, no credentials |
| `queue_manager.py` | SQLite queue (`queue`, `users`, `failed_tracks`); WAL + busy timeout for two processes |
| `worker.py` | Job orchestration; owns `stream_job()`, the shared spawn/stream/watchdog IPC loop used by both downloads and one-shot commands |
| `config.py` | Constants, config.json loading, `.env` loading, logger setup (`silence_spotiflac_loggers` inlined) |
| `resolver.py` | Input parsing only (`parse_inputs`/`format_help`); no SpotiFLAC/difflib in the parent |
| `library.py` | Library paths (user folders, per-user output dir) |
| `playlists.py` | Local `.m3u8` create/empty helpers; no Telegram or SpotiFLAC imports |
| `track_utils.py` | Filename/path sanitization; `TrackMetadata` imported `TYPE_CHECKING` only |

### Subprocesses (SpotiFLAC; spawned per job)
| Module | Role |
|--------|------|
| `download_job.py` | Entry point: reads one JSON spec from stdin, runs the job, emits JSON-lines |
| `search_job.py` | Legacy Spotify search entry point (tests/rollback; the Laila API no longer spawns it) |
| `resolver_match.py` | Fuzzy artist/title matching + `resolve_search` (imports difflib/unicodedata) |
| `deezer_meta.py` | Deezer track/album/playlist metadata → `TrackMetadata`, including ISRC enrichment |
| `downloader.py` | Download engine (`run_url` for Spotify, `run_deezer` for Deezer): resolution, partition, parallel download, rename, cover |
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

## Deezer search (HTTP API)

`api.py` calls `deezer_search.search()` in-process for every cache miss:

- Three parallel public-API requests: `/search`, `/search/album`,
  `/search/playlist` on `api.deezer.com`.
- No credentials, no SpotiFLAC import, no subprocess. The parent stays stdlib
  only (`http.client`), so the search peak RSS is the normal ~29MiB instead of
  the old 150–250MiB transient child.
- Results are normalised to `tracks` / `albums` / `playlists`; tracks keep
  their ISRC and Deezer URL.
- The API caches results for `SEARCH_CACHE_TTL` (default 60s, 32 entries;
  expired entries are dropped on insert) and serialises cache misses, so
  typing does not fan out into parallel requests.
- Failures map to `deezer_unavailable` (502) or `deezer_blocked` (503 plus
  `Retry-After: 1800`) when Deezer reports a quota/429.

The legacy Spotify search path (`search_job.py`, `SpotifyAuth`) remains in the
tree for tests and manual rollback, but `build_runtime` uses Deezer. Telegram
name search is separate and still resolves through Spotify in the download
subprocess.

## HTTP API

| Endpoint | Auth | Notes |
|----------|------|-------|
| `GET /api/health` | Bearer | Token/connectivity check |
| `GET /api/search?q=&limit=` | Bearer | Deezer tracks/albums/playlists, in Deezer's order |
| `POST /api/queue` | Bearer | Canonical Deezer (or Spotify) `track/album/playlist` links, dedupes |
| `GET /api/cover?u=` | none | Deezer/Spotify CDN image proxy |

- Queue writes share `queue.db` with the bot through WAL; `busy_timeout=5000`
  makes the two processes wait instead of failing on a writer lock.
- **Single-container mode:** `RUN_API=1` starts the API in a daemon thread
  inside `bot.py` (default in both compose files and used by the TrueNAS
  `ix-app`, which already runs host networking with a portal on 8099). It
  reuses the bot's `QueueManager` connection and searches run in-process in
  `api.py`. With `RUN_API` unset, the API can run as a separate `python api.py`
  process.
- `API_QUEUE_USER` (default: first Telegram allowlist id, including the legacy
  `TELEGRAM_ALLOWED_USER_ID`) decides whose folder/quality API downloads use.
- Covers are restricted to `*.dzcdn.net`, `*.scdn.co` / `*.spotifycdn.com`,
  fetched without following redirects, capped at 5MiB, and kept in an 8MiB /
  32-entry LRU.
  The endpoint has no auth so `<img>`/expo-image can load it; never expose the
  API port publicly.
- CORS only echoes origins listed in `API_CORS_ORIGINS`.

## Stall watchdog

`worker.stream_job()` reads the child's stdout asynchronously. If no line
arrives for `stall_timeout_seconds` (config, default 1800), or the job exceeds
its overall deadline, the child is SIGKILLed and reaped. Every other exit path
does the same: task cancellation, a broken stdin, or an exception in
`on_event()` (DB/Telegram). `result` is the final protocol line, so there is
nothing left to wait for. This permanently fixes the old "leaked straggler
thread" bug: a dead child's RSS is reclaimed by the kernel, no orphan
processes survive.

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
  sleeps with idle backoff 5s -> 10 -> 20 -> 30s cap. The API writes to SQLite
  from another process/thread and cannot set the event, so the 30s cap is what
  keeps API-queued links responsive (two tiny SELECTs per minute when idle).
- Spotify block: a 406/429 from `*.spotify.com` is tagged `spotify_blocked`
  by the job; the worker then stops dequeuing for `SPOTIFY_BLOCK_PAUSE`
  (30min) instead of letting every item retry against a rate-limited IP.
- Download subprocesses skip SpotiFLAC's import-time GitHub version check
  (one useless HTTPS request per job).
- QueueManager: one persistent SQLite connection, no polling.
- `gc.freeze()` after startup imports + API thread: permanent objects are
  never rescanned.
- Idle cost is one Telegram long-poll request plus at most one tiny queue
  SELECT every 30s (the API cannot wake the event; bot messages wake it
  instantly). Near-zero CPU.

## RSS budget

Measured after Phase 11 (host, import chain + QueueManager + Bot):
interpreter ~12.3MiB + logging ~3.3MiB + asyncio ~6.1MiB + sqlite ~1.5MiB +
http.client/ssl ~1.4MiB + app code — **parent idle ~29MiB**. `urllib.request`
is no longer in the graph (Spotify credentials use `http.client`), and
`difflib`/`unicodedata` live in `resolver_match.py` (subprocess only).
`MALLOC_ARENA_MAX=2` (Dockerfile/compose) trims glibc arena overhead under
threads.

| Component | RSS |
|-----------|-----|
| python interpreter base | ~12-13MiB |
| stdlib (`logging`, `asyncio`, `sqlite3`, `http.client`, `json`) | ~12MiB |
| parent app code | ~2-3MiB |
| **parent idle total (host)** | **~29MiB** |
| API standalone import | ~27MiB |
| **single container (docker stats, bot + API)** | **~22MiB** |
| subprocess peak (during job) | ~150-250MiB (transient, reclaimed at exit) |
