"""Optional Spotify credentials shared by the bot and the HTTP API.

Stdlib only. The bot writes `~/.spotiflac/spotify.json` (mode 0600) from
Telegram commands; the API re-reads it on the next search, so no restart is
needed. Values from the file win over `.env`; without either, search falls
back to SpotiFLAC's anonymous web token.
"""

from __future__ import annotations

import base64
import html
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
FIELDS = ("client_id", "client_secret", "access_token", "market")


class CredentialsError(Exception):
    """A safe, user-facing credential or network failure."""


def default_path() -> Path:
    raw = os.environ.get("SPOTIFY_CREDENTIALS_PATH", "").strip()
    return Path(raw) if raw else Path(os.path.expanduser("~/.spotiflac/spotify.json"))


def load_credentials(path: str | Path | None = None) -> dict:
    """Read the file; missing/malformed content is treated as empty."""
    target = Path(path) if path is not None else default_path()
    try:
        raw = json.loads(target.read_text())
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    data = {field: str(raw.get(field) or "").strip() for field in FIELDS}
    data["updated_at"] = str(raw.get("updated_at") or "").strip()
    return data


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    )
    try:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, path)
    finally:
        if not handle.closed:
            handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def save_credentials(path: str | Path | None, **updates: str) -> None:
    """Update fields atomically and mark the change time."""
    target = Path(path) if path is not None else default_path()
    data = load_credentials(target)
    for field, value in updates.items():
        if field not in FIELDS:
            raise ValueError(f"Unknown credential field: {field}")
        data[field] = str(value or "").strip()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write(target, {field: data[field] for field in FIELDS} | {"updated_at": data["updated_at"]})


def set_access_token(path: str | Path | None, token: str) -> None:
    """Use a static access token; clears client credentials (they would win)."""
    save_credentials(path, access_token=token, client_id="", client_secret="")


def set_client_credentials(path: str | Path | None, client_id: str, client_secret: str) -> None:
    """Use client credentials; clears a static access token."""
    save_credentials(
        path, client_id=client_id, client_secret=client_secret, access_token=""
    )


def set_market(path: str | Path | None, market: str) -> None:
    save_credentials(path, market=market.upper())


def clear_credentials(path: str | Path | None = None) -> bool:
    """Remove the file; returns True when something was deleted."""
    target = Path(path) if path is not None else default_path()
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CredentialsError("Cannot remove the credentials file") from exc


def effective_credentials(
    path: str | Path | None = None, env: dict | None = None
) -> dict:
    """File values override `.env`; returns the mode and its inputs."""
    source_env = env if env is not None else os.environ
    file_data = load_credentials(path)
    access_token = file_data["access_token"] or str(
        source_env.get("SPOTIFY_ACCESS_TOKEN", "")
    ).strip()
    client_id = file_data["client_id"] or str(
        source_env.get("SPOTIFY_CLIENT_ID", "")
    ).strip()
    client_secret = file_data["client_secret"] or str(
        source_env.get("SPOTIFY_CLIENT_SECRET", "")
    ).strip()
    market = file_data["market"] or str(source_env.get("SPOTIFY_MARKET", "")).strip()

    if access_token:
        mode = "token"
    elif client_id and client_secret:
        mode = "client"
    else:
        mode = "anonymous"

    from_file = any(file_data[field] for field in FIELDS)
    from_env = any(
        str(source_env.get(key, "")).strip()
        for key in ("SPOTIFY_ACCESS_TOKEN", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET")
    )
    source = "telegram" if from_file else "env" if from_env else "none"
    return {
        "mode": mode,
        "access_token": access_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "market": market,
        "source": source,
        "updated_at": file_data["updated_at"],
    }


def mask(value: str, keep: int = 4) -> str:
    if len(value) <= keep * 2:
        return "…"
    return f"{value[:keep]}…{value[-keep:]}"


def describe_credentials(path: str | Path | None = None) -> str:
    """HTML status message; never includes a token or secret."""
    data = effective_credentials(path)
    mode_label = {
        "anonymous": "anonymous (SpotiFLAC web token)",
        "token": "official API (access token)",
        "client": "official API (client credentials)",
    }[data["mode"]]
    source_label = {
        "telegram": "Telegram",
        "env": ".env",
        "none": "none",
    }[data["source"]]
    lines = [
        "🔑 <b>Spotify search credentials</b>",
        f"  Mode: <b>{mode_label}</b>",
        f"  Source: {source_label}",
    ]
    if data["access_token"]:
        lines.append(f"  Access token: set ({len(data['access_token'])} chars)")
    if data["client_id"]:
        lines.append(f"  Client ID: <code>{html.escape(mask(data['client_id']))}</code>")
    if data["client_secret"]:
        lines.append("  Client secret: set")
    if data["market"]:
        lines.append(f"  Market: <code>{html.escape(data['market'])}</code>")
    if data["updated_at"]:
        lines.append(f"  Updated: {html.escape(data['updated_at'])}")
    if data["mode"] == "anonymous":
        lines.append("  Falling back to the anonymous web token.")
    lines.append("")
    lines.append("Commands:")
    lines.append("  /spotify token &lt;token&gt; — use an access token")
    lines.append("  /spotify client &lt;id&gt; &lt;secret&gt; — use client credentials")
    lines.append("  /spotify market &lt;IT&gt; — optional official-API market")
    lines.append("  /spotify test — call the official API")
    lines.append("  /spotify clear — fall back to anonymous")
    return "\n".join(lines)


def fetch_client_credentials_token(
    client_id: str, client_secret: str, timeout: int = 10
) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = Request(
        TOKEN_URL,
        data=urlencode({"grant_type": "client_credentials"}).encode(),
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "spoty_loop/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise CredentialsError("Spotify rejected the client credentials") from exc
    except OSError as exc:
        raise CredentialsError("Cannot reach the Spotify token endpoint") from exc
    token = str(payload.get("access_token") or "")
    if not token:
        raise CredentialsError("Spotify returned no access token")
    return token


def probe_official(access_token: str, timeout: int = 15) -> None:
    """One tiny search; validates an access token without returning data."""
    params = urlencode({"q": "daft punk", "type": "track", "limit": 1})
    request = Request(
        f"{SEARCH_URL}?{params}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "spoty_loop/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            json.load(response)
    except HTTPError as exc:
        if exc.code == 401:
            raise CredentialsError("Spotify rejected the access token") from exc
        if exc.code == 429:
            raise CredentialsError("Spotify is rate-limiting the official API") from exc
        raise CredentialsError(f"Spotify API returned HTTP {exc.code}") from exc
    except OSError as exc:
        raise CredentialsError("Cannot reach the Spotify API") from exc


def test_credentials(path: str | Path | None = None) -> str:
    """Validate the effective mode; safe to call in a worker thread."""
    data = effective_credentials(path)
    if data["mode"] == "anonymous":
        return "Anonymous mode: no Spotify credentials to test."
    if data["mode"] == "token":
        probe_official(data["access_token"])
        return "Official Spotify API OK (access token)."
    token = fetch_client_credentials_token(data["client_id"], data["client_secret"])
    probe_official(token)
    return "Official Spotify API OK (client credentials)."
