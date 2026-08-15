# MusicBot (rewrite)

**⚠️ Educational purpose only.** Downloads copyrighted music. Know your local laws.

Telegram bot that turns Spotify links into FLAC files via SpotiFLAC
(Qobuz/Deezer/Amazon lossless providers).

```
Telegram msg → SQLite queue → worker → subprocess(download_job) → SpotiFLAC → FLAC on disk
```

## Why this rewrite

The previous single-process bot (python-telegram-bot + SpotiFLAC) idled at
~252MiB, ~89MiB after removing the browser stack. This version splits into a
**stdlib-only parent** (~15-20MiB idle) and a **short-lived subprocess per
download job** whose RSS is reclaimed on exit. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Features

- Send a Spotify link (track, album, playlist) or search `artist - song` → queued and downloaded
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
| `/mkplaylist <url>` | Generate m3u8 playlist file |
| `/fixmetadata [folder]` | Re-tag all FLACs in a folder (fixes split albums in Navidrome) |

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

*One of the two must be set.

Provider order, quality, and download options go in `~/.spotiflac/config.json`.

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