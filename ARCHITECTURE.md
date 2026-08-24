# Architecture

The bot is a **two-tier process split**: a lean, stdlib-only parent that never
imports SpotiFLAC, and a short-lived subprocess per download job that does.

```
Telegram ──► parent (stdlib only) ──spawn──► download_job.py (SpotiFLAC) ──► FLAC on disk
                │                                  ^
                │ SQLite queue                     | JSON-lines on stdout
                └──────────────────────────────────┘
```

## Why two processes

The old single-process bot (python-telegram-bot + SpotiFLAC) idled at ~252MiB;
after removing the browser stack it dropped to ~89MiB. The floor for one
process is ~46MiB (telegram + httpx stack) plus SpotiFLAC's ~57MiB once
imported. Idle RSS is a hard requirement (see AGENTS.md), so we split:

- **Parent** imports stdlib only: `asyncio`, `json`, `urllib`, `sqlite3`,
  `fcntl`, `logging`, `pathlib`. Idle target **~15-20MiB**.
- **Subprocess** (`download_job.py`) imports SpotiFLAC + patches. Spawned per
  job, exits when the job ends, so its peak RSS is **reclaimed** — no
  lingering threads, no `_trim_rss` needed for the heavy part.

## Process split (what runs where)

### Parent (stdlib only)
| Module | Role |
|--------|------|
| `telegram_client.py` | Raw Telegram Bot API client on `http.client` with two persistent keep-alive connections (one for the long-poll, one locked-shared for sends) |
| `bot.py` | Message dispatch, command handlers, allowlist, main loop |
| `queue_manager.py` | SQLite queue (`queue`, `users`, `failed_tracks`) |
| `worker.py` | Job orchestration; owns `stream_job()`, the shared spawn/stream/watchdog IPC loop used by both downloads and one-shot commands |
| `config.py` | Constants, config.json loading, logger setup (`silence_spotiflac_loggers` inlined) |
| `resolver.py` | Text parsing only (`parse_spotify_url`); search resolved in subprocess |
| `library.py` | Library paths (user folders, per-user output dir) |
| `track_utils.py` | Filename/path sanitization; `TrackMetadata` imported `TYPE_CHECKING` only |

### Subprocess (SpotiFLAC; spawned per job)
| Module | Role |
|--------|------|
| `download_job.py` | Entry point: reads one JSON spec from stdin, runs the job, emits JSON-lines |
| `downloader.py` | Download engine (`run_url`): resolution, partition, parallel download, rename, cover |
| `spotiflac_patch.py` | Import-time monkey-patches (console silencing, community-dead, MB no-op, provider tracking) |
| `m3u8.py` | Playlist m3u8 + cover generation (auto-build happens here, not in parent) |
| `flac_utils.py` | FLAC tag read/write, cover embed/dedupe (httpx/mutagen/pillow live here) |

## IPC protocol

**Job request** — parent writes one JSON line to the child's **stdin**:

```json
{"id": 12, "url": "https://open.spotify.com/...", "type": "link",
 "cfg": {"output_dir": "...", "quality": "LOSSLESS", ...},
 "skip_titles": ["x"], "want_m3u8": true}
```

`type` is `"link"` (URL, album/playlist/track) or `"search"` (`artist - song`).
Parent closes stdin after writing; the child reads one line, then runs.

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