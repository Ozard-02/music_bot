# SpotyLoop

**⚠️ Educational purpose only.** Downloads copyrighted music. Know your local laws. See [Disclaimer](#disclaimer).

![CI/CD](https://github.com/Ozard-02/music_loop/actions/workflows/docker.yml/badge.svg)

Telegram bot that queues Spotify downloads through SpotiFLAC (Qobuz/Deezer/Amazon lossless providers). Designed for bare-metal and TrueNAS Docker.

## Features

- Send Spotify track/album/playlist links → auto-downloaded to FLAC
- Search by name: `artist - song`
- Queue management: `/status`, `/purge`
- Playlist builder: `/mkplaylist <url>` generates an m3u8 with cover art
- Configurable provider order and quality
- Exponential backoff retries (survives network blips)
- Docker + CI/CD (auto-builds and pushes to `ghcr.io/ozard-02/music_loop`)

## How it works

```
Telegram msg → SQLite queue → Worker → SpotiFLAC (Qobuz→Deezer→Amazon) → FLAC on disk
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Help |
| `/status` | Queue status (queued/running/done/failed) |
| `/purge` | Clear all queued items |
| `/mkplaylist <url>` | Build m3u8 playlist with cover |

## Quick Start

```bash
# Bare-metal
python bot.py

# Docker
docker compose up -d
```

Requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_ID` in `.env`.
See [SETUP.md](SETUP.md) for full instructions.

## Configuration

| Env var | Required | Description |
|---------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USER_ID` | Yes | Your Telegram user ID |
| `QUEUE_DB_PATH` | No | Queue DB path (default: `./queue.db`) |
| `CHROME_PATH` | No | Chromium path for Qobuz (Docker only) |

Provider order, quality, and download options are set via `~/.spotiflac/config.json`.
See [SETUP.md](SETUP.md) for details.

## Deployment

- **Bare-metal**: `python bot.py` with `.env` and Chromium installed
- **Docker**: `docker compose up -d` mounts `~/.spotiflac` and `~/Music`
- **TrueNAS**: Same compose file, bake-in config via environment variables (no `.env`)

The Docker image auto-builds on every push to `main` and is available at:
`ghcr.io/ozard-02/music_loop:latest`

## Disclaimer

**This project is for educational purposes only.** It demonstrates:
- Telegram bot development with `python-telegram-bot`
- Asynchronous task queues with SQLite persistence
- Docker containerization for NAS deployment
- Integration with third-party audio APIs

Downloading copyrighted music may violate terms of service or applicable laws in your jurisdiction. The authors assume no liability for how you use this software.

MIT License — see [LICENSE](LICENSE).

---

*Built with SpotiFLAC, python-telegram-bot, and chromium.*
