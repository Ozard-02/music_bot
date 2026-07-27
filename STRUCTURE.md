# Code Structure

## `downloader.py` — single file

```
main()
├─ bridge_community_session(logger)  # copy desktop session (avoids Cloudflare)
├─ load_config(logger) → cfg        # read ~/.spotiflac/config.json
├─ suppress httpx + SpotiFLAC child loggers → WARNING
├─ outer loop (crash recovery):
│  └─ wait_for_providers(logger)    # health check every 300s until one UP
│     └─ inner loop (retry until failed==0):
│        ├─ state = RunState()      # dataclass: skipped/ok/failed/total/done/in_progress
│        ├─ AsyncSpotiFLAC(...)     # configured from cfg, enrich_providers excludes qobuz
│        ├─ download_playlist():
│        │  ├─ dedup by track.id → duplicates.log
│        │  ├─ Semaphore(3)
│        │  └─ download_track_with_retry():
│        │     ├─ in_progress guard per track ID
│        │     ├─ client.download_track() (180s timeout, 3 retries)
│        │     └─ [N/M] progress log per outcome
│        └─ if failed==0 → exit
│           all failed → 5min wait
│           some failed → 60s wait
│        on crash → 30s → restart outer loop
├─ spoty_loop.log: full log
└─ duplicates.log: repeated track IDs per run
```

## SpotiFLAC patch
`SpotiFLAC/core/tagger.py`: `_embed_flac` strips `MUSICBRAINZ_*` before writing Vorbis comments (avoids Navidrome album merge).

## Helper scripts
- `fix_mb_tags.py` — strip MUSICBRAINZ_* tags from all FLACs in ~/Music
- `fix_covers.py` — re-embed Spotify cover art into all FLACs with Spotify track IDs
- `IMPROVEMENTS.md` — planned Telegram bot + Docker + SQLite queue
