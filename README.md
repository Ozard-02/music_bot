# MusicBot (rewrite)

**⚠️ Educational purpose only.** Downloads copyrighted music. Know your local laws.

Telegram bot that turns Spotify links into FLAC files via SpotiFLAC
(Qobuz/Deezer/Amazon lossless providers).

```
Telegram msg / Laila HTTP API → SQLite queue → worker → subprocess(download_job) → SpotiFLAC → FLAC on disk
```

## Why this rewrite

The previous single-process bot (python-telegram-bot + SpotiFLAC) idled at
~252MiB, ~89MiB after removing the browser stack. This version splits into a
**stdlib-only parent** and a **short-lived subprocess per download job** whose
RSS is reclaimed on exit. Measured idle: **~29MiB on the host**, **~22MiB for
the single container** (`docker stats`, bot + API). See
[ARCHITECTURE.md](ARCHITECTURE.md).

## Features

- Send a Spotify link (track, album, playlist) or search `artist - song` — any
  dash kind works, one item per line, multiple lines queue them all
- Queue commands: `/status`, `/quality`, `/purge`
- Playlist builder: `/mkplaylist <url>` (m3u8 + cover)
- Configurable provider order and quality
- Stall watchdog kills hung subprocesses (no leaked threads)

## Commands

| Command | What it does |
|---------|-------------|
| `/start` | Welcome + help |
| `/status` | Queue stats + recent items |
| `/quality [value]` | Set download quality |
| `/purge` | Clear all queued items |
| `/retry <id>` | Re-queue one failed item and reset its retries |
| `/mkplaylist <url> [name]` | Generate m3u8 from Spotify playlist (name defaults to Spotify title) |
| `/mkplaylist <name>` | Create empty `name.m3u8` (no url) |
| `/fixmetadata [folder]` | Re-tag all FLACs in a folder inside your library (fixes split albums in Navidrome) |
| `/synclyrics [folder] [--force]` | Fetch missing/unsynced lyrics + `.lrc` sidecars only (no re-tag, no moves) |
| `/rmplaylist [name]` | Delete every track in `name.m3u8` (default `Trash`) + prune empty folders |
| `/spotify` | Show Spotify search credential status |
| `/spotify token <token>` | Use a static Spotify access token (official API) |
| `/spotify client <id> <secret>` | Use client credentials (official API, auto-refresh) |
| `/spotify market <IT>` | Optional official-API market |
| `/spotify test` | Validate the configured official credentials |
| `/spotify clear` | Remove them and fall back to anonymous |

## Quick start (local Docker)

```bash
cp .env.example .env   # fill in token + user IDs
docker compose up -d --build
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | Yes* | — | Comma-separated Telegram user IDs (allowlist) |
| `TELEGRAM_ALLOWED_USER_ID` | Yes* | — | Legacy single-user fallback |
| `QUEUE_DB_PATH` | No | `./queue.db` | Queue database path |
| `SPOTIFY_CREDENTIALS_PATH` | No | `~/.spotiflac/spotify.json` | Credentials file written by `/spotify` |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | No | — | Official Spotify app credentials (fallback if the file is empty) |
| `SPOTIFY_ACCESS_TOKEN` | No | — | Static official token (fallback if the file is empty) |
| `SPOTIFY_MARKET` | No | — | Optional market for official search (e.g. `IT`) |
| `RUN_API` | No | off (on in compose) | Start the HTTP API inside the bot process/container (`1`) |
| `API_PORT` | No | `8099` | API listen port |
| `API_BIND` | No | `0.0.0.0` | API listen address |
| `API_TOKEN` | API only | — | Bearer token Laila must send |
| `API_CORS_ORIGINS` | No | — | Exact web origins allowed to call the API |
| `API_QUEUE_USER` | No | first allowed Telegram id | Whose folder/quality API downloads use |

*One of the two must be set.

### Spotify search credentials

Search works without any Spotify credentials: it uses SpotiFLAC's anonymous
web token. If Spotify starts rate-limiting it, add official credentials from
Telegram (no user login needed):

```text
/spotify client <client_id> <client_secret>   # recommended, auto-refreshed
/spotify token <access_token>                 # short-lived alternative
/spotify market IT                            # optional
/spotify test
/spotify clear                                # back to anonymous
```

- The bot writes `~/.spotiflac/spotify.json` (mode 0600) and deletes the
  command message that contained the secret, when Telegram allows it.
- The API re-reads the file on the next search: no restart needed.
- File values override `.env`; without either, anonymous is used.
- The token/secret never reaches Laila or the logs.
- Official credentials are only for search; downloads still use SpotiFLAC's
  provider chain.

Provider order, quality, and download options go in `~/.spotiflac/config.json`.

## HTTP API (Laila search tab)

`api.py` is an optional stdlib-only HTTP service on top of the same queue. It
powers a Spotify search tab in Laila: type a title, pick one result, queue it
for download. Searches run in a short-lived `search_job.py` subprocess, so the
API process never imports SpotiFLAC.

```bash
# .env: API_TOKEN=...  (required)
python api.py
```

The compose files run it inside the bot container: `RUN_API=1` starts the API
in a daemon thread, so one process serves Telegram and HTTP on `8099`. This is
also the single-container mode used by the TrueNAS `ix-app` (host network +
portal on 8099). The standalone `python api.py` process remains supported;
search still spawns `search_job.py` per cache miss in both modes.

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /api/health` | Bearer | Connectivity/token check |
| `GET /api/search?q=...&limit=20` | Bearer | Spotify tracks/albums/playlists |
| `POST /api/queue` | Bearer | Queue canonical Spotify links |
| `GET /api/cover?u=...` | none | Spotify CDN image proxy (allowlist) |

```bash
curl -H "Authorization: Bearer $API_TOKEN" \
  "http://localhost:8099/api/search?q=one%20more%20time"
```

- Search results are Spotify's own order (no best-match resolver): the caller
  picks. `queued`/`duplicates`/`invalid` come back from `/api/queue`.
- Links are canonicalized (`?si=...` removed) so the same track dedupes.
- The cover endpoint is open but only fetches `*.scdn.co` / `*.spotifycdn.com`
  images, with a size cap and no redirects, so `<img>` works without a token.
- Keep it on LAN/Tailscale. Do not expose this port to the public internet.
- Web clients need an exact origin in `API_CORS_ORIGINS`.

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — process split, IPC protocol, RSS budget
- [STRUCTURE.md](STRUCTURE.md) — module map, import rules, data flow
- [PLAN.md](PLAN.md) — phased roadmap
- [METADATA.md](METADATA.md) — the "split album" problem and /fixmetadata
- [DEPLOYMENT.md](DEPLOYMENT.md) — local Docker, TrueNAS, CI/CD

## Disclaimer

This project is for educational purposes only. Downloading copyrighted music
may violate terms of service or laws in your jurisdiction. The authors assume
no liability for how you use this software.
</content>
