# PLAN

This repo is the stdlib-only parent + subprocess-per-download rewrite (merged
from the `music_bot_rewrite` scratch directory). Primary goals: **minimal idle
RAM and energy** (see AGENTS.md hard rules). Port proven modules from the old
single-process implementation's git history — don't reinvent.

## Phase 1 — Docs + scaffold (DONE)
- [x] MD files: README, AGENTS, ARCHITECTURE, STRUCTURE, METADATA, DEPLOYMENT, PLAN
- [x] Fresh git repo initialized on `main`
- [x] Skeleton: Dockerfile, docker-compose*, requirements, .env.example, .gitignore, config.default.json

## Phase 2 — Port parent (stdlib only)
- [x] `config.py` — drop `spotiflac_patch` import; inline the silence loop
- [x] `queue_manager.py` — port as-is (already stdlib)
- [x] `track_utils.py` — `TrackMetadata` via `TYPE_CHECKING` only
- [x] `library.py` — port as-is
- [x] `resolver.py` — keep `parse_spotify_url`/`format_help`; search resolution moved to subprocess (`resolve_search` lazy-imports SpotiFLAC)
- [x] `telegram_client.py` — raw getUpdates/sendMessage/editMessageText (new)

## Phase 3 — Bot + worker (parent)
- [x] `bot.py` — dispatch on raw Telegram client, allowlist, handlers
- [x] `worker.py` — spawn subprocess, JSON-lines stream, stall watchdog, backoff
- [x] flock in-flight guard (cross-process track locks)

## Phase 4 — Subprocess (SpotiFLAC side)
- [x] `download_job.py` — stdin spec -> stdout JSON-lines (new)
- [x] `downloader.py` — port run_url + cover/rename; add flock guard
- [x] `spotiflac_patch.py` — port as-is
- [x] `m3u8.py` — port; auto-build runs inside subprocess
- [x] `flac_utils.py` — port as-is

## Phase 5 — Tests
- [x] Port queue_manager/library/track_utils/resolver tests
- [x] telegram_client tests (mock HTTP)
- [x] download_job subprocess tests (spec -> events) — verified via IPC driver
- [x] worker tests (spawn, watchdog kill)
- [x] Full suite green: `261 passed` (parent import chain verified stdlib-clean; IPC + flock verified live)

## Phase 5b — One-shot commands (DONE)
- [x] `/mkplaylist <url> [name]` — subprocess command type `m3u8`, streams result event
- [x] `/fixmetadata [folder] [--lyrics]` — subprocess command type `fix_metadata`, streams per-folder progress + result
- [x] `bot._run_command_job` — spawn download_job.py, progress-edits a single Telegram message, stall watchdog
- [x] `maintenance.py` ported (fix_covers dependency; was silently broken in the rewrite)
- [x] Tests: command protocol verified live (progress→result events); bot handler tests green

## Phase 6 — Local Docker
- [x] Dockerfile (SpotiFLAC + pydoll lib, no browser)
- [x] `docker compose up -d --build`, run bot
- [x] Measure idle RSS — measured **~15MiB parent / ~31MiB container** (was ~252MiB)
- [x] End-to-end: queue a link, watch FLAC appear (13/13 ok)

## Phase 6b — RSS/KISS pass (DONE)
- [x] `telegram_client.py` on `http.client` with two keep-alive connections
      (dedicated long-poll + locked short-call slot) — parent 29.4 → 26.2MiB,
      zero per-call TLS handshakes (~1700/day eliminated)
- [x] `worker.stream_job()` shared IPC loop: downloads + one-shot commands use
      one spawn/stream/watchdog implementation; fixes child not killed on
      worker stall/timeout and on command-job cancellation
- [x] `MALLOC_ARENA_MAX=2` (Dockerfile + compose files)
- [x] `gc.freeze()` after startup in bot.main()
- [x] tests/test_stream_job.py: real-subprocess coverage of the IPC loop
- [x] Full suite green: 271 passed

## Phase 7 — TrueNAS
- [ ] `docker-compose.truenas.yml` (ghcr image, /mnt/TB2 volumes)
- [ ] Push image, deploy, verify

## Phase 8 — CI/CD
- [ ] GitHub Actions: pytest on push + build/push ghcr on main

## Deferred (explicit, not forgotten)
- `/fixmetadata` and `/mkplaylist` are subprocess-triggered commands (port the
  scripts behind a subprocess call); not part of the core download path.
- `maintenance.py`/scripts stay CLI tools runnable against the subprocess env.