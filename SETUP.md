# SpotyLoop — Resilient Playlist Downloader

## Goal
Download Spotify playlists to FLAC using SpotiFLAC's anonymous providers (Tidal/Qobuz/Amazon), matching `~/Music/{Artist}/{Album}/{Artist} - {title}.flac`. Never deletes existing files.

## How it works

```
loop forever:
  copy desktop community session (avoids Cloudflare)
  health-check amazon, tidal, qobuz (every 5 min)
  if any provider is UP:
    fetch playlist metadata
    for each track:
      skip if file exists on disk (exact match + directory scan)
      download with 3 parallel workers, 3 min timeout, 3 retries
    done → exit
  else:
    wait 5 min → retry
  if crash → restart loop after 30s
```

## Usage

```bash
cd /home/espo/spoty_loop
.venv/bin/python downloader.py "https://open.spotify.com/playlist/04T3Cj34SKqYqaGd90pAiX"
```

Press `Ctrl+C` to abort. Rerun to resume — existing files are skipped.

## Wait time
The script checks provider health every **300 seconds (5 minutes)**. If all providers are down, it loops forever until at least one responds. If a provider responds but the download fails mid-track, the track is retried up to 3 times (with exponential backoff), then the whole session restarts after 30s.

## Progress

- [x] Read existing config from `~/.spotiflac/config.json`
- [x] Confirm file structure: `~/Music/{Artist}/{Album}/{Artist} - {title}.flac`
- [x] Install SpotiFLAC in uv venv
- [x] Confirm imports work
- [x] Analyze SpotiFLAC path logic for skip check
- [x] Write `downloader.py`
- [x] Fix `first_artist` comma-inside-parentheses bug
- [x] Fix skip check with directory scan fallback
- [x] Bridge desktop community session (fixes Cloudflare prompt)
- [x] Test: existing files correctly detected (Billie Eilish, MACE, Ernia — all SKIP)
- [x] Test: non-existing files correctly detected (Nitro — not found, will download)
- [ ] Wait for Tidal/Qobuz APIs to come back online
- [ ] Run full playlist download
