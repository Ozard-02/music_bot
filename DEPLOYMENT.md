# Deployment

## Local Docker (dev)

```bash
cp .env.example .env          # token + allowed user IDs + API_TOKEN
docker compose up -d --build
docker compose logs -f music-bot
```

- Builds from `./Dockerfile` (SpotiFLAC 1.6.0 + pydoll lib, no chromium).
- `.:/app` bind-mount: edit code and `docker compose restart` to apply.
- Music + SpotiFLAC config live in the mounted volumes
  (`~/Music`, `~/.spotiflac`) — downloads survive container rebuilds.
- One container: `music-bot` runs the Telegram bot and, with `RUN_API=1`
  (set by compose), the HTTP API on port `8099` in a daemon thread. The
  standalone `python api.py` service remains supported.
- The API refuses to start without `API_TOKEN`. Put it behind LAN/Tailscale;
  never publish the port to the internet.
- Spotify search credentials are optional and managed from Telegram:
  `/spotify client <id> <secret>` or `/spotify token <token>`. The bot writes
  `~/.spotiflac/spotify.json`; both services mount `~/.spotiflac`, and the API
  re-reads the file on the next search (no restart). Without it, search uses
  SpotiFLAC's anonymous token.

## TrueNAS

```bash
docker-compose -f docker-compose.truenas.yml up -d
```

- Uses a **pinned** ghcr tag (e.g. `:1.1.0` or `:sha-<commit>`), not `latest`.
- Volumes point at `/mnt/pool/navidrome/Music` (shared with Navidrome) and
  `/mnt/pool/VM/spotiflac` (SpotiFLAC config/credentials).
- One container with `RUN_API=1`: the API runs in a daemon thread inside the
  bot process, so it shares the same `queue.db` and mounts. The standalone
  `python api.py` service remains supported if you prefer two processes.
- **TrueNAS `ix-app` single-container:** keep one container and simply add
  `RUN_API=1` to its environment. The app already uses host networking and a
  portal on `8099`, so no port mapping or second app is needed.
- If the app has a memory limit, leave headroom for the search subprocess
  (SpotiFLAC); 512 MB is safer than 256 MB.

## CI/CD

GitHub Actions on `main`:
- `pytest` (host or container) on push
- build + push an immutable `:sha-<commit>` tag on every merge to `main`
- build + push semver tags (`1.2.3`, `1.2`) and move `latest` only when a
  `v*.*.*` Git tag is pushed

Release:

```bash
git tag v1.2.3
git push origin v1.2.3
# then pin docker-compose.truenas.yml to :1.2.3 and redeploy
```

The parent is stdlib-only, so the CI test job does not need the full SpotiFLAC
install for parent tests; subprocess tests need SpotiFLAC (installed via the
Dockerfile).

## Measuring idle RSS

```bash
docker compose up -d --build
sleep 30                                   # let the bot settle
docker stats --no-stream music-bot         # MEM USAGE
```

Measured after the KISS/RSS pass: **~29MiB host** for the parent and
**~22MiB for the single container** (`docker stats` with `RUN_API=1`). That is
well under the old ~89MiB; the subprocess only exists while a job is running,
so it is never resident at idle.
