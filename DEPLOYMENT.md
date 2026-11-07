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

- Uses a **pinned** ghcr tag (e.g. `:1.0.0` or `:sha-ee4b508`), not `latest`.
- Volumes point at `/mnt/pool/navidrome/Music` (shared with Navidrome) and
  `/mnt/pool/VM/spotiflac` (SpotiFLAC config/credentials).

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

Target: well under the old ~89MiB; parent ~15-20MiB. The subprocess only
exists while a job is running — it is never resident at idle.