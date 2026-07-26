# Code Structure

## `downloader.py` — single file, no modules needed

```
main()
├─ _bridge_community_session()       # copy desktop session (avoids Cloudflare)
├─ health-check loop (every 300s)   # run_health_check + get_working_providers
│  └─ once providers UP (SERVICES=[qobuz, tidal, amazon]):
│     ├─ dedup by track.id           # first occurrence kept, rest → duplicates.log
│     ├─ fetch_playlist()            # SpotiFLAC resolves Spotify playlist → track list
│     └─ download loop               # asyncio.Semaphore(3) for concurrency
│        ├─ track_file_exists()      # exact path → directory scan fallback
│        ├─ _in_progress guard       # per-track-id, prevents parallel-dup races
│        ├─ _get_first_artist()      # parenthesis-aware first-artist extraction
│        └─ client.download_track()  # SpotiFLAC does the actual download
├─ SpotiFLAC child loggers → WARNING  # suppress internal noise
├─ on crash: wait 30s → restart
├─ stdout: progress bar per download
├─ spoty_loop.log: full log
└─ duplicates.log: repeated track IDs per run

Helpers (not in SpotiFLAC):
├─ track_file_exists(title, artist, album)
├─ _get_first_artist(artist_str)
└─ _bridge_community_session()
```

## External deps (via SpotiFLAC)
- `SpotiFLAC.providers.{tidal, qobuz, amazon}` — actual streaming providers
- `SpotiFLAC.core.health_check` — probes provider endpoints
- `SpotiFLAC.core.session_desktop` — community session management
