# SpotyLoop — Resilient Playlist Downloader

## Goal
Download Spotify playlists to FLAC using SpotiFLAC's anonymous providers (Tidal/Qobuz/Amazon), matching `~/Music/{Artist}/{Album}/{Artist} - {title}.flac`. Never deletes existing files. Retries until every track is downloaded.

## How it works

```
while True:
  copy desktop community session (avoids Cloudflare)
  suppress SpotiFLAC child loggers (spotiflac_patch.silence_spotiflac_loggers)
  dequeue next item from queue.db (worker.py)
  link → run_url() directly; "Artist - Album" → resolve_search() first
  run_url():
    dedup playlist by track.id (first kept)
    pre-check paths on disk → skip existing, skip given-up titles
    download all unique tracks with 2 parallel workers (100s timeout, 3 retries)
  retry/fail via decide_failure() (age >24h, retries ≥15, give-up, backoff)
```

Existing files are detected by the pre-check via `track_relative_path()` — no re-download.

## Usage

### Bare-metal
```bash
cd spoty_loop
python bot.py          # Telegram bot (downloads go through the queue)
```

### Docker
```bash
docker compose up -d
```

Requires `~/.spotiflac/` (created automatically on first run) and `~/Music/` on the host.
The `.env` file in the project root is passed via `env_file` to the container.

Auth: set `TELEGRAM_ALLOWED_USER_IDS` (comma-separated allowlist) or the legacy
single-user `TELEGRAM_ALLOWED_USER_ID`. Each allowed user gets their own library
folder `~/Music/{username}_Music/` and their own `/quality` preference.

### TrueNAS (production)
Same `docker-compose.yml` — just remove the `.:/app` volume mount so the baked-in code is used instead.
Create a dataset for `~/.spotiflac/` and set permissions before starting.

### CI/CD auto-build
On every `git push` to `main`, GitHub Actions builds the image and pushes it to
`ghcr.io/ozard-02/music_loop:latest`. No manual `docker build` or `docker push` needed.

**Auto-update on TrueNAS:** add a Watchtower sidecar container or check for updates
periodically in the TrueNAS Apps UI.

Press `Ctrl+C` to abort. Rerun to resume — existing files skipped.

## Provider priority
`SERVICES = ["qobuz", "deezer", "amazon"]` — Qobuz first. Tidal v1 API retired (410). Amazon last.

## Files
| File | Purpose |
|------|---------|
| `bot.py` | Telegram bot entry point |
| `downloader.py` | Core download engine |
| `maintenance.py` | Library maintenance (`rescan_library` cover re-embed) |
| `spotiflac_patch.py` | SpotiFLAC monkey-patches (progress manager, console) |
| `flac_utils.py` | Shared FLAC/tag/cover helpers |
| `queue_manager.py` | SQLite queue persistence |
| `worker.py` | Background queue processor |
| `resolver.py` | Input parsing + Spotify search |
| `m3u8.py` | M3U8 playlist generator |
| `track_utils.py` | Shared path utilities |
| `config.py` | Shared config, logging, session bridge |
| `config.default.json` | Reference config (keys used by downloader) |
| `.github/workflows/docker.yml` | CI/CD auto-build + push to GHCR |
| `Dockerfile` | Container image (python:3.14-slim + chromium) |
| `docker-compose.yml` | Single-service Docker Compose |
| `.dockerignore` | Excludes venv, caches, logs, secrets |
| `scripts/` | Maintenance CLIs: fix_metadata (also bot-called), fix_covers, fix_original_filenames, migrate_library; archived superseded one-offs in scripts/archive/ |
| `library.py` | Per-user library layout (user folders, quality set) |
| `spoty_loop.log` | Full log (all runs) |

Config is read from `~/.spotiflac/config.json` (created by the desktop app). If missing, a warning points to `config.default.json` and hardcoded defaults are used.

## Navidrome note
Qobuz metadata enrichment injects bogus `MUSICBRAINZ_ALBUMID` values that cause Navidrome to merge unrelated albums. Downloads now no-op the MusicBrainz lookup entirely (`spotiflac_patch._patch_musicbrainz`) and `SpotiFLAC/core/tagger.py` is patched to strip all `MUSICBRAINZ_*` tags before writing — fresh downloads never carry bogus MB IDs. Run `/fixmetadata` (or `scripts/fix_metadata.py`) on existing files if you see misgrouped albums.

## Cover art note
Qobuz enrichment returns wrong HD covers for some albums (e.g., Ditonellapiaga "Chimica"). `enrich_providers` in downloader.py excludes qobuz, uses `["apple", "deezer", "soundcloud"]` (priority order). Apple Music provides 3000×3000, Deezer and SoundCloud lower resolutions. The downloader overwrites covers with the 640×640 Spotify art after each track (`_fix_cover`); run `python scripts/fix_covers.py` on existing files with incorrect covers.
