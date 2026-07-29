![CI/CD](https://github.com/Ozard-02/music_loop/actions/workflows/docker.yml/badge.svg)
# SpotyLoop

Telegram bot that queues Spotify downloads through SpotiFLAC (Tidal/Qobuz/Amazon lossless providers).

## Disclaimer

**This project is for educational purposes only.** It demonstrates:
- Telegram bot development with `python-telegram-bot`
- Asynchronous task queues with SQLite persistence
- Docker containerization for NAS deployment
- Integration with third-party audio APIs

Downloading copyrighted music may violate terms of service or applicable laws in your jurisdiction. The authors assume no liability for how you use this software.

## License

MIT — see [LICENSE](LICENSE).

## Quick Start

```bash
# Bare-metal
python bot.py

# Docker
docker compose up -d
```

Requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_ID` in `.env`.
See [SETUP.md](SETUP.md) for details.

---

*Auto-built and pushed via GitHub Actions on every push to `main`.*
