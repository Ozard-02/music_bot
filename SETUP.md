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

```bash
cd spoty_loop
./downloader.py "https://open.spotify.com/playlist/..."
```

Press `Ctrl+C` to abort. Rerun to resume — existing files skipped.

## Provider priority
`SERVICES = ["qobuz", "tidal", "amazon"]` — Qobuz first. Tidal v1 API retired (410). Amazon last.

## Files
| File | Purpose |
|------|---------|
| `downloader.py` | Main script |
| `config.default.json` | Reference config (keys used by downloader) |
| `fix_mb_tags.py` | Remove MUSICBRAINZ_* tags from existing FLACs |
| `fix_covers.py` | Re-embed correct Spotify cover art into FLACs |
| `spoty_loop.log` | Full log (all runs) |
| `duplicates.log` | Track IDs that appeared >1× in the playlist |

Config is read from `~/.spotiflac/config.json` (created by the desktop app). If missing, a warning points to `config.default.json` and hardcoded defaults are used.

## Navidrome note
Qobuz metadata enrichment injects bogus `MUSICBRAINZ_ALBUMID` values that cause Navidrome to merge unrelated albums. `SpotiFLAC/core/tagger.py` is patched to strip all `MUSICBRAINZ_*` tags before writing. Run `fix_mb_tags.py` on existing files if you see misgrouped albums.

## Cover art note
Qobuz enrichment returns wrong HD covers for some albums (e.g., Ditonellapiaga "Chimica"). `enrich_providers` in downloader.py excludes qobuz: `["deezer", "apple", "tidal", "soundcloud"]`. Run `fix_covers.py` on existing files with incorrect covers.
