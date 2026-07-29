# SpotyLoop — Resilient Playlist Downloader

## Goal
Download Spotify playlists to FLAC using SpotiFLAC's anonymous providers (Tidal/Qobuz/Amazon), matching `~/Music/{Artist}/{Album}/{Artist} - {title}.flac`. Never deletes existing files. Retries until every track is downloaded.

## How it works

```
while True:
  copy desktop community session (avoids Cloudflare)
  suppress SpotiFLAC child loggers
  health-check qobuz, tidal, amazon (every 5 min) until at least one UP
  while failed > 0:
    dedup playlist by track.id (first kept, duplicates → duplicates.log)
    download all unique tracks with 3 parallel workers (180s timeout, 3 retries)
    if zero failed → exit success
    if all failed → wait 5 min (server likely down)
    if some failed → wait 60s and retry only the failed ones
  if crash → restart outer loop after 30s
```

Existing files are detected by SpotiFLAC's internal `_file_exists()` — no re-download.

## Usage

### Bare-metal
```bash
cd spoty_loop
python bot.py          # Telegram bot
python downloader.py "https://open.spotify.com/playlist/..."   # CLI one-shot
```

### Docker
```bash
docker compose up -d
```

Requires `~/.spotiflac/` (created automatically on first run) and `~/Music/` on the host.
The `.env` file in the project root is passed via `env_file` to the container.

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
`SERVICES = ["qobuz", "tidal", "amazon"]` — Qobuz first. Tidal v1 API retired (410). Amazon last.

## Files
| File | Purpose |
|------|---------|
| `bot.py` | Telegram bot entry point |
| `downloader.py` | Core download engine |
| `queue_manager.py` | SQLite queue persistence |
| `worker.py` | Background queue processor |
| `resolver.py` | Input parsing + Spotify search |
| `m3u8.py` | M3U8 playlist generator |
| `track_utils.py` | Shared path utilities |
| `config.py` | Shared config, logging, session bridge |
| `config.default.json` | Reference config (keys used by downloader) |
| `Dockerfile` | Container image (python:3.14-slim + chromium) |
| `docker-compose.yml` | Single-service Docker Compose |
| `.dockerignore` | Excludes venv, caches, logs, secrets |
| `fix_mb_tags.py` | Remove MUSICBRAINZ_* tags from existing FLACs |
| `fix_covers.py` | Re-embed correct Spotify cover art into FLACs |
| `spoty_loop.log` | Full log (all runs) |
| `duplicates.log` | Track IDs that appeared >1× in the playlist |

Config is read from `~/.spotiflac/config.json` (created by the desktop app). If missing, a warning points to `config.default.json` and hardcoded defaults are used.

## Navidrome note
Qobuz metadata enrichment injects bogus `MUSICBRAINZ_ALBUMID` values that cause Navidrome to merge unrelated albums. `SpotiFLAC/core/tagger.py` is patched to strip all `MUSICBRAINZ_*` tags before writing. Run `fix_mb_tags.py` on existing files if you see misgrouped albums.

## Cover art note
Qobuz enrichment returns wrong HD covers for some albums (e.g., Ditonellapiaga "Chimica"). `enrich_providers` in downloader.py excludes qobuz, uses `["apple", "deezer", "tidal", "soundcloud"]` (priority order). Apple Music provides 3000×3000, Tidal 1280×1280, Deezer and SoundCloud lower resolutions. Run `fix_covers.py` on existing files with incorrect covers.
