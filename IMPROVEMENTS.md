# Improvements

## Telegram Bot + Docker + SQLite Queue

### Bot
- [ ] **`bot.py`** — Telegram bot via `python-telegram-bot`
  - [ ] `/start` — welcome + instructions
  - [ ] Link handler — validate Spotify URL, add to queue, reply "Queued (#N)"
  - [ ] `/status` — current download + queue position + history stats
  - [ ] Chat ID whitelist: only your user ID can interact; others silently ignored

### Queue
- [ ] **`queue_manager.py`** — SQLite persistence
  - [ ] `queue` table: id, url, title, status (queued/running/done/failed), created_at, started_at, completed_at
  - [ ] `history` table: same + ok/skipped/failed counts
  - [ ] Survives container restarts
  - [ ] Methods: `enqueue()`, `dequeue()`, `mark_running()`, `mark_done()`, `mark_failed()`, `get_status()`, `get_history()`

### Worker
- [ ] **`worker.py`** — sequential queue processor
  - [ ] Polls queue for next `queued` item
  - [ ] Calls core download function
  - [ ] Sends Telegram notification on completion (summary: X ok, Y skipped, Z failed)

### Refactor
- [ ] **`downloader.py`** — extract reusable core
  - [ ] Extract `async def run_playlist(url) -> dict` returning `{ok, skipped, failed}`
  - [ ] Keep standalone `main()` working for CLI use (no breaking changes)

### Docker
- [ ] **`Dockerfile`** — container image
  - [ ] `python:3.14-slim` base
  - [ ] Install SpotiFLAC + python-telegram-bot + aiofiles
  - [ ] Copy `spoty_loop/` into container
  - [ ] CMD: run `bot.py`
- [ ] **`docker-compose.yml`** — single service
  - [ ] Mount `~/Music:/home/espo/Music`
  - [ ] Mount `~/.spotiflac:/root/.spotiflac`
  - [ ] Env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`
- [ ] **`requirements.txt`** — add `python-telegram-bot`, `aiofiles`

---

## Existing (done)
- [x] Skip-race fix: per-track-ID `_in_progress` guard inside semaphore
- [x] Config reading from `~/.spotiflac/config.json`
- [x] `[N/M]` progress counter on outcome logs
- [x] Retry loop: retries until zero failures (no fast-fail)
- [x] Removed `track_file_exists` — relies on SpotiFLAC internal skip detection
- [x] MusicBrainz tag strip — patched tagger.py to avoid Navidrome misgrouping
- [x] Cover fix — `enrich_providers` excludes qobuz; `fix_covers.py` re-embeds Spotify covers
