# Code Structure

```
spoty_loop/
├── bot.py               # Telegram dispatch + handlers + main loop (front end)
├── api.py               # HTTP API for Laila: search/queue/cover (front end;
│                        #   standalone process or RUN_API thread inside bot.py)
├── spotify_credentials.py # Telegram-managed Spotify creds (front ends; stdlib)
├── telegram_client.py   # raw Bot API client on http.client keep-alive conns (front end)
├── worker.py            # job orchestration + stream_job IPC loop (spawn, JSON-lines, watchdog) (front end)
├── queue_manager.py     # SQLite queue (front ends; stdlib-only, WAL + busy timeout)
├── resolver.py          # input parsing + search resolution (front ends; no SpotiFLAC)
├── config.py            # constants, config.json, .env loader, logger (front ends)
├── library.py           # per-user library paths (front ends)
├── track_utils.py       # sanitize/path helpers (front ends; TrackMetadata TYPE_CHECKING)
│
├── download_job.py      # subprocess entry point: stdin spec → JSON-lines events
├── search_job.py        # subprocess entry point: stdin search spec → one JSON result
├── downloader.py        # download engine: run_url, partition, rename, cover (subprocess)
├── spotiflac_patch.py   # import-time SpotiFLAC monkey-patches + match guards (subprocess)
├── m3u8.py              # playlist m3u8 + cover (subprocess)
├── flac_utils.py        # FLAC tag/cover utilities (subprocess)
│
├── maintenance.py       # library maintenance: rescan/cover re-embed (subprocess; fix_covers dep)
├── scripts/             # CLI tools (fix_metadata, fix_covers, ...) (subprocess)
├── tests/               # pytest suite
│
├── Dockerfile
├── docker-compose.yml         # local dev (build .)
├── docker-compose.truenas.yml # TrueNAS (ghcr image)
├── requirements.txt           # subprocess deps (parent needs none)
├── requirements-dev.txt       # test deps (SpotiFLAC as in Dockerfile + pytest plugins)
├── config.default.json
├── .env.example
├── .gitignore
└── *.md (docs)
```

## Import rules (hard)

- **Front-end modules** (`bot.py`, `api.py` and their imports) may import:
  stdlib only, plus sibling front-end modules. Never `from SpotiFLAC import ...`,
  never httpx, never telegram.
- **Subprocess modules** (`download_job.py`, `search_job.py`, `downloader.py`,
  `flac_utils.py`, …) may import SpotiFLAC + its deps; they are never imported
  by a front end.
- `config.py` must NOT import `spotiflac_patch` — the silence loop is inlined
  so the front-end import graph never touches SpotiFLAC.

## Data flow

### Link message
```
bot.py ──handle_message──► resolver.parse_inputs(text)   # one item per line
   ──► queue_manager.enqueue(url, user)
   ──► worker wakes (asyncio.Event)
   ──► worker.spawn("download_job.py"), write JSON spec to stdin
   ──► child: run_url(...) → progress/failure/result JSON-lines on stdout
   ──► worker updates queue rows + bot edits Telegram message
```

### Search message
```
bot.py ──► queue_manager.enqueue(query, user)
   ──► subprocess job with type="search": resolves via SpotiFLAC, downloads
```

### Laila search tab (HTTP API)
```
Laila ──GET /api/search──► api.py ──cache?──► search_job.py (SpotiFLAC)
   ◄── tracks/albums/playlists + /api/cover proxy URLs ──┘
Laila ──POST /api/queue──► api.py ──► queue_manager.enqueue_unique("link", url, API_QUEUE_USER)
   ──► existing worker picks it up like a Telegram link
```

### /status
```
bot.py ──► queue_manager.stats(user) ──► sendMessage
```

### /mkplaylist, /fixmetadata and /synclyrics (one-shot subprocess commands)
```
bot.py ──► _run_command_job(spec) ──► worker.stream_job (type=m3u8|fix_metadata|sync_lyrics)
   ──► child: build_m3u8 / fix_library / sync_lyrics
   ──► progress / result JSON-lines on stdout ──► bot edits one Telegram message
```

### /rmplaylist (local, no subprocess)
```
bot.py ──► _rmplaylist(name) ──► reads <user_output>/name.m3u8 (sanitize)
   ──► deletes each listed FLAC + .lrc sidecar, prune_empty_parents
   ──► deletes .m3u8 + .jpg + temp/name_missing.txt
```

Downloads and one-shot commands share `worker.stream_job()`: one implementation
of spawn → stdin spec → stdout JSON-lines → stall/timeout/cancel watchdog.

## Subprocess job spec (stdin, one JSON line)
```json
{"id": 12, "type": "link|search", "url": "...", "cfg": {...},
 "skip_titles": [], "want_m3u8": false}
```

## Subprocess events (stdout, JSON-lines)
```
{"event":"progress","done":3,"total":12,"title":"...","provider":"qobuz"}
{"event":"failure","title":"...","error":"..."}
{"event":"result","result":{...}}   # final line
```
stderr carries logs. A crash (nonzero exit, no `result`) is a failed job.

## Job lifecycle (worker)
1. dequeue next item (asyncio.Event wakeup)
2. build JSON spec, `asyncio.create_subprocess_exec`
3. write spec to stdin, close stdin
4. stream stdout lines; update queue progress + Telegram message
5. watchdog: no line in `stall_timeout` → `proc.kill()`
6. on `result` (or crash): mark queue row, trim idle, backoff
7. `spotify_blocked` failure → pause the whole queue for `SPOTIFY_BLOCK_PAUSE`
</content>