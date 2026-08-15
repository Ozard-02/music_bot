# Deployment

## Local Docker (dev)

```bash
cp .env.example .env          # token + allowed user IDs
docker compose up -d --build
docker compose logs -f music-bot
```

- Builds from `./Dockerfile` (SpotiFLAC 1.6.0 + pydoll lib, no chromium).
- `.:/app` bind-mount: edit code and `docker compose restart` to apply.
- Music + SpotiFLAC config live in the mounted volumes
  (`~/Music`, `~/.spotiflac`) — downloads survive container rebuilds.

## TrueNAS

```bash
docker-compose -f docker-compose.truenas.yml up -d
```

- Uses the ghcr image (`ghcr.io/ozard-02/music_loop:latest`), not a local build.
- Volumes point at `/mnt/TB2/navidrome/Music` (shared with Navidrome) and
  `/mnt/TB2/VM/spotiflac` (SpotiFLAC config/credentials).

## CI/CD

GitHub Actions on `main`:
- `pytest` (host or container) on push
- build + push `ghcr.io/ozard-02/music_loop:latest` on merge to `main`

The parent is stdlib-only, so the CI test job does not need the full SpotiFLAC
install for parent tests; subprocess tests need SpotiFLAC (installed via the
Dockerfile).

## Measuring idle RSS

```bash
docker compose up -d --build
sleep 30                                   # let the bot settle
docker stats --no-stream music-bot         # MEM USAGE
```

Target: well under the old ~89MiB; parent ~15-20MiB. The subprocess only
exists while a job is running — it is never resident at idle.