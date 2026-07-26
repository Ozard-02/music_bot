# Plan: SpotyLoop — Resilient Playlist Downloader

## Goal
Download Spotify playlists to FLAC matching `~/Music/{Artist}/{Album}/{Artist} - {title}.flac`.
Never delete existing files. Retry until servers are up. Parallel downloads.

## Done

1. **Environment** — uv venv, SpotiFLAC installed, nodriver patched (utf-8 header).
2. **Session bridge** — copy desktop `community_session.json` → module path (fixes Cloudflare).
3. **`_get_first_artist()`** — custom comma-split that respects parenthesis depth (fixes "Artificial Kid (Danno, Stabby)").
4. **`track_file_exists()`** — first exact path, then directory scan by title (fixes false-positive skips).
5. **`downloader.py`** — health-check loop (300s), 3-parallel downloads, 180s timeout, 3 retries, crash-restart.

## Remaining

6. **Run full playlist** — wait for Tidal/Qobuz APIs to come back online.
7. **Tune parallelism** — adjust `MAX_CONCURRENT` (3→? ) if rate-limited.
