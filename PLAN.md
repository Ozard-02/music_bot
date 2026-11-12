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

## Phase 6c — Provider matching guards (DONE)
- [x] `spotiflac_patch._patch_provider_matching()` — reject fuzzy Qobuz/Deezer
      candidates that don't match title/artist (and ISRC/duration when available)
- [x] Log rejected candidates at WARNING (`spoty_loop.patch`)
- [x] `resolver.best_track_match` scores the title too (same-artist wrong song fix)
- [x] Tests: real false candidates (Noi no/Articolo 31), resolver title hint
- [x] E2E: track `1ucxAsJ9oqhyVWBYQ7vzUA` → provider `deezer`, 297.6s, ISRC
      `ITA270200113` (Qobuz candidate correctly rejected)

## Phase 7 — Lenient Telegram input parsing (DONE)
- [x] `resolver.split_search()` — separator is any dash: en/em dashes always
      separate, a plain hyphen only when not glued to words (AC-DC intact);
      shared by `parse_input` and `resolve_search` (legacy ' - ' rows fine)
- [x] `parse_inputs()` — one item per line: paste several artists/tracks at
      once; links recognized inside prose; `bot._handle_text` queues each line
      and replies with one batch summary (single line keeps the old reply)
- [x] Link regex is extraction, not fullmatch: spotify: URIs, play.spotify.com,
      URLs wrapped in text, ?si= share fragments, intl-<lang> segments
- [x] Lenient name matching: token-based artist matching (Daft Punk feat.
      Pharrell, Simon & Garfunkel) + difflib similarity fallback; unresolved
      album names no longer fall back to albums[0]
- [x] Tests: 344 passed (split_search, parse_inputs, URIs, lenient matching,
      bot batch flow); docs synced

## Phase 7b — Spotify rate-limit handling (DONE)
- [x] 2026-11-07: TrueNAS got 406 from open.spotify.com for every link
      (metadata_error); cleared by itself — IP rate-limit, not a code bug.
      Embed-page fallback rejected (no ISRC → unsafe provider matching).
- [x] `downloader.is_spotify_block()` tags 406/429 from spotify.com as
      `spotify_blocked` (track path + job crash path)
- [x] Worker pauses the whole queue for `SPOTIFY_BLOCK_PAUSE` (30min) and
      requeues the item with at least that delay
- [x] `spotiflac_patch._skip_github_version_check()` — no api.github.com
      request per download subprocess
- [x] Real cause of the "keeps failing" after the block cleared: SpotiFLAC
      uses `timeout_s` for both the per-track budget and each provider's httpx
      timeout. A hung Qobuz stream ate the full 100s, Deezer never ran.
      `_patch_provider_timeout()` caps providers at `PROVIDER_IO_TIMEOUT` (30s);
      `PER_TRACK_TIMEOUT` raised 100 → 180s so fallbacks fit in the budget.
- [x] Tests: 393 passed

## Phase 7c — Lyrics maintenance (DONE)
- [x] `/synclyrics` (was: keep any existing lyrics) now upgrades embedded
      unsynced lyrics when the provider chain returns a timestamped version;
      plain replacements never overwrite existing lyrics. `--force` unchanged.
- [x] `flac_utils.has_lrc_timestamps()` shared by `read_lrc` and sync_lyrics
- [x] Tests: 396 passed

## Phase 8 — HTTP API for the Laila Spotify tab (DONE)

Approved exception to the parent's stdlib-only rule: `api.py` is a separate
stdlib-only process; SpotiFLAC stays in the short-lived `search_job.py`
subprocess. Shared queue DB via WAL.

- [x] `search_job.py` — stdin `{query, limit}` -> stdout one JSON object;
      `sync_extensions=False`, no GitHub check, block detection (406/429).
- [x] `api.py` — `health`, `search`, `queue`, `cover` endpoints; Bearer token;
      CORS allowlist; search cache + single-flight; canonical Spotify links.
- [x] Cover proxy: strict `*.scdn.co` / `*.spotifycdn.com` allowlist, no
      redirects, size cap, in-memory LRU.
- [x] `QueueManager` busy timeout for the second process.
- [x] `.env.example`, README, DEPLOYMENT, ARCHITECTURE, STRUCTURE updated.
- [x] `docker-compose.yml` + `docker-compose.truenas.yml`: second service.
- [x] Tests: auth, search cache, queue dedupe/invalid, cover allowlist, CORS;
      full suite green (435 passed).
- [x] Commit (2190d5d); push/deploy pending.

## Phase 8b — Telegram-managed Spotify credentials (DONE)

- [x] `spotify_credentials.py` — atomic 0600 JSON in `~/.spotiflac`, file values
      override `.env`, safe status (no token/secret).
- [x] Bot `/spotify` commands: status, token, client, market, test, clear;
      deletes the command message that contained a secret.
- [x] API re-reads the file on every search; client credentials refresh + one
      401 retry; anonymous stays the fallback.
- [x] `search_job.py` official Web API path (stdlib urllib) + normalization;
      anonymous path unchanged.
- [x] Docs/env/compose notes; tests 461 passed.
- [x] Commit; push/deploy pending (the TrueNAS image must include this build).

## Phase 8c — Single-container API for TrueNAS ix-app (DONE)

- [x] `RUN_API=1` starts the API in a daemon thread inside `bot.py`; default
      command stays `python bot.py`, so the TrueNAS `ix-app` needs only the env
      var (host networking + portal 8099 already configured).
- [x] `api.build_runtime` / `start_api_thread` extracted; separate `api.py`
      process still works for docker-compose.
- [x] `API_QUEUE_USER` honours the legacy `TELEGRAM_ALLOWED_USER_ID`.
- [x] Test: thread mode serves `/api/health`; legacy user mapping. 462 passed.
- [x] Release `v1.1.0` tag pushed; CI builds the image.
- [ ] Update the TrueNAS app image tag + `RUN_API=1`.

## Phase 9 — TrueNAS
- [x] `docker-compose.truenas.yml` (ghcr image, /mnt/pool volumes) — single
      service with `RUN_API=1`, port 8099.
- [ ] Deploy and verify on TrueNAS

## Phase 10 — CI/CD
- [x] GitHub Actions: pytest on push + build/push ghcr on main

## Phase 11 — KISS/RSS pass (DONE)

- [x] `spotify_credentials.py` on `http.client`: `urllib.request` no longer
      enters the parent; host bot idle 31.0 → 29.1 MiB.
- [x] API caches bounded: search 32 entries + expiry purge, covers 8 MiB.
- [x] `gc.freeze()` after the API thread and Bot construction.
- [x] Compose runs one service (`RUN_API=1`); standalone `python api.py` stays.
- [x] `QueueManager.reset_running()` is explicit and bot-only; an API restart
      can no longer requeue a live job.
- [x] `stream_job()` always kills/reaps the child, including `on_event`
      exceptions and broken stdin.
- [x] Worker idle poll capped at 30s (API-queued links start promptly).
- [x] `RUN_API` reuses the bot's `QueueManager`; unread HTTP bodies close the
      connection.
- [x] Architecture guard test: the parent never imports SpotiFLAC/httpx/
      telegram/mutagen/PIL/urllib.request.

## Deferred (explicit, not forgotten)
- `/fixmetadata` and `/mkplaylist` are subprocess-triggered commands (port the
  scripts behind a subprocess call); not part of the core download path.
- `maintenance.py`/scripts stay CLI tools runnable against the subprocess env.
