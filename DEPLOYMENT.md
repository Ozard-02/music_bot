# Deployment

## Local Docker (dev)

```bash
cp .env.example .env          # token + allowed user IDs + API_TOKEN
docker compose up -d --build
docker compose logs -f music-bot
```

- Builds from `./Dockerfile` (SpotiFLAC 5.0.6 + Node.js + pydoll lib, no
  Chromium).
- `.:/app` bind-mount: edit code and `docker compose restart` to apply.
- Music + SpotiFLAC config live in the mounted volumes
  (`~/Music`, `~/.spotiflac`) — downloads survive container rebuilds.
- Downloads go through SpotiFLAC's JS extensions, which need Node.js (baked
  into the image) and a signed `zarz-v2` session. Sessions live in
  `~/.spotiflac/signed_sessions/`; the bot refreshes them every hour. The first
  session must be created on a machine with a browser:
  `python scripts/seed_sessions.py --data-dir ~/.spotiflac`, then copy the
  `zarz-v2-*.json` files to the server's volume (mode 0600). The bot itself
  never runs a browser.
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

- Uses a **pinned** ghcr tag (default `:1.2.2`; override with
  `SPOTY_LOOP_TAG`), not `latest`.
- Volumes point at `/mnt/<pool>/navidrome/Music` (shared with Navidrome) and
  `/mnt/<pool>/VM/spotiflac` (SpotiFLAC config/credentials/extensions/sessions).
  The reference deployment uses pool `TB2`; the compose file defaults to it
  via `TRUENAS_POOL`.
- After the first deploy, seed the sessions once (from a machine with a
  browser) and copy them to `/mnt/pool/VM/spotiflac/signed_sessions/`. The
  worker refreshes them every hour, so later image rebuilds need no manual
  session work as long as the volume is kept and the container is not stopped
  for longer than the session lifetime.

### First 5.x migration (one time)

The 5.x providers are JS extensions, so the volume needs both the current
extensions and a valid session:

```bash
# on the workstation, with the repo venv and a browser
.venv/bin/python scripts/seed_sessions.py --data-dir ~/.spotiflac

# copy the extension code + the two active session files to the NAS
# (the session files are 0600; use sudo/root on both sides)
sudo rsync -a ~/.spotiflac/extensions/ root@truenas:/mnt/TB2/VM/spotiflac/extensions/
sudo scp ~/.spotiflac/signed_sessions/zarz-v2-*.json \
  root@truenas:/mnt/TB2/VM/spotiflac/signed_sessions/
```

Only the active `zarz-v2-*.json` files matter; orphaned/expired ones are
harmless.  If the NAS IP is rejected by the gateway (unlikely: the refresh is
HMAC-signed with the session secret, not IP-bound), run the seed script against
a temporary container with Chromium on the NAS network — never add Chromium to
the bot image itself.

The bot pins the installed extensions (`sync_extensions=False`) because the
session file is keyed by the manifest's `appVersion`.  To update a provider,
copy a newer `extensions/` directory and re-seed if that `appVersion` changed;
do not rely on a runtime extension update.

After that, `session_refresh.py` keeps the sessions alive and a plain
`docker compose -f docker-compose.truenas.yml pull && up -d` is enough for
later image builds.
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
git tag v1.2.2
git push origin v1.2.2
# docker-compose.truenas.yml already defaults to :1.2.2
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

Current measurement (Python 3.14.10 base): **~36MiB** in the single container
with `RUN_API=1`, and ~40MiB on the host for the same code. The parent code is
unchanged stdlib-only; the SpotiFLAC 5.x deps live in the download subprocess
and are not resident at idle. The old ~89MiB single-process bot is still the
reference ceiling.
