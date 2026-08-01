# MusicBot

**⚠️ Educational purpose only.** Downloads copyrighted music. Know your local laws. See [Disclaimer](#disclaimer).

![CI/CD](https://github.com/Ozard-02/music_bot/actions/workflows/docker.yml/badge.svg)

Telegram bot that turns Spotify links into FLAC files via SpotiFLAC (Qobuz/Deezer/Amazon lossless providers).

```
Telegram msg → SQLite queue → Worker → SpotiFLAC → FLAC on disk
```

## Features

- Send a Spotify link (track, album, playlist) → queued and downloaded automatically
- Search by name: `artist - song`
- Queue commands: `/status`, `/purge`
- Playlist builder: `/mkplaylist <url>` — generates m3u8 + cover art
- Configurable provider order and quality
- Exponential backoff retries
- Docker + CI/CD — auto-pushes to `ghcr.io`

## Commands

| Command | What it does |
|---------|-------------|
| `/start` | Welcome + help |
| `/status` | Queue stats + recent items |
| `/purge` | Clear all queued items |
| `/mkplaylist <url>` | Generate m3u8 playlist file |
| `/rescan` | Re-embed Spotify covers for the whole library |
| `/fixmetadata [folder]` | Re-tag all FLACs in a folder (whole library if omitted; fixes "same album split into several" in Navidrome) |

Metadata issues (bogus MusicBrainz tags, split albums, tagless files) are
explained in [METADATA.md](METADATA.md).

## Quick Start

```bash
cp .env.example .env   # fill in your token and user ID
python bot.py          # or: docker compose up -d
```

See [SETUP.md](SETUP.md) for full setup (bare-metal, Docker, TrueNAS).

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USER_ID` | Yes | — | Your Telegram user ID |
| `QUEUE_DB_PATH` | No | `./queue.db` | Queue database path |
| `CHROME_PATH` | No | system default | Chromium location |

Provider order, quality, and download options go in `~/.spotiflac/config.json`.

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| `ConnectTimeout` on replies | Container can't reach `api.telegram.org` — check DNS/firewall |
| Downloads time out at 100s | Slow network — increase `PER_TRACK_TIMEOUT` in `config.py` |
| `Node.js not found` | Extensions need Node.js ≥ 16 installed in the container |
| `Not a valid FLAC file` | Corrupt partial download — the retry will pick it up |
| Bot goes silent under load | Event loop blocked — ensure `run_url` runs in a thread executor |

## Disclaimer

**This project is for educational purposes only.** It demonstrates Telegram bot development, async task queues, SQLite persistence, Docker containerization, and third-party audio API integration.

Downloading copyrighted music may violate terms of service or applicable laws in your jurisdiction. The authors assume no liability for how you use this software.

MIT License — see [LICENSE](LICENSE).
