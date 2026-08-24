# Code Structure

```
music_bot_rewrite/
├── bot.py               # Telegram dispatch + handlers + main loop (parent)
├── telegram_client.py   # raw Bot API client on http.client keep-alive conns (parent)
├── worker.py            # job orchestration + stream_job IPC loop (spawn, JSON-lines, watchdog) (parent)
├── queue_manager.py     # SQLite queue (parent; stdlib-only)
├── resolver.py          # text/URL parsing only (parent; no SpotiFLAC)
├── config.py            # constants, config.json, logger (parent; silence inlined)
├── library.py           # per-user library paths (parent)
├── track_utils.py       # sanitize/path helpers (parent; TrackMetadata TYPE_CHECKING)
│
├── download_job.py      # subprocess entry point: stdin spec → JSON-lines events
├── downloader.py        # download engine: run_url, partition, rename, cover (subprocess)
├── spotiflac_patch.py   # import-time SpotiFLAC monkey-patches (subprocess)
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
├── config.default.json
├── .env.example
├── .gitignore
└── *.md (docs)
```

## Import rules (hard)

- **Parent modules** may import: stdlib only, plus sibling parent modules.
  Never `from SpotiFLAC import ...`, never httpx, never telegram.
- **Subprocess modules** may import SpotiFLAC + its deps; they are never
  imported by the parent.
- `config.py` must NOT import `spotiflac_patch` — the 5-line silence loop is
  inlined so the parent's import graph never touches SpotiFLAC.

## Data flow

### Link message
```
bot.py ──handle_message──► resolver.parse_input(text)
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

### /status
```
bot.py ──► queue_manager.stats(user) ──► sendMessage
```

### /mkplaylist and /fixmetadata (one-shot subprocess commands)
```
bot.py ──► _run_command_job(spec) ──► worker.stream_job (type=m3u8|fix_metadata)
   ──► child: build_m3u8 / fix_library
   ──► progress / result JSON-lines on stdout ──► bot edits one Telegram message
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
</content>