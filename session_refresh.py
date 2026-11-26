#!/usr/bin/env python3
"""Refresh SpotiFLAC's signed gateway sessions (subprocess, no browser).

SpotiFLAC 5.x downloads through JavaScript extensions that need a signed
`zarz-v2` session.  A session lasts ~5 hours and can be refreshed over HTTP
with its `session_secret`; no Turnstile/browser is involved.  This script is
run periodically by the worker so a bot that is idle for a few hours does not
wake up to an expired session.

It never creates a session: if one is missing or expired, it reports that and
exits non-zero.  Creating the first session needs a browser and is a one-off
operation (see `scripts/seed_sessions.py`).

Output is human-readable on stderr; exit codes:
  0  every required session is active (refreshed when due)
  1  at least one required session is missing/expired or refresh failed
  2  unexpected error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("spoty_loop.sessions")


def _data_dir(value: str | None) -> Path:
    raw = value or os.environ.get("SPOTIFLAC_DATA_DIR") or "~/.spotiflac"
    return Path(raw).expanduser()


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _refresh_due(client) -> bool:
    now = datetime.now(timezone.utc)
    refresh_at = _parse_time(getattr(client, "refresh_after", None))
    if refresh_at:
        return now >= refresh_at
    expires_at = _parse_time(getattr(client, "expires_at", None))
    if not expires_at:
        return False
    skew = float(getattr(client, "refresh_skew_seconds", 3600))
    return (expires_at - now).total_seconds() <= skew


async def _refresh_with_retry(client, attempts: int = 3) -> tuple[bool, str]:
    """Refresh until the expiry actually moves; 5.x swallows network errors."""
    before = _parse_time(getattr(client, "expires_at", None))
    last_error = ""
    for attempt in range(attempts):
        try:
            await client._refresh()
        except Exception as exc:  # noqa: BLE001 - report after the retries
            last_error = f"{type(exc).__name__}: {exc}"
        after = _parse_time(getattr(client, "expires_at", None))
        if client.authenticated and after and (before is None or after > before):
            return True, ""
        if not client.authenticated:
            last_error = "refresh produced an expired session"
        else:
            last_error = last_error or "refresh did not extend the expiry"
        if attempt + 1 < attempts:
            await asyncio.sleep(2 * (attempt + 1))
    return False, last_error


async def _refresh_one(name: str, manifest_path: Path, root: Path, force: bool) -> dict:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"name": name, "state": "invalid-manifest", "error": str(exc)}

    config = manifest.get("signedSession")
    if not config:
        return {"name": name, "state": "no-session", "error": "no signedSession block"}

    from SpotiFLAC.core.signed_session_mobile import client_from_manifest

    client = client_from_manifest(config, data_dir=str(root / "signed_sessions"))
    try:
        if not client.authenticated:
            return {
                "name": name,
                "state": "expired",
                "expires_at": getattr(client, "expires_at", None),
                "error": "session missing or expired",
            }
        if force or _refresh_due(client):
            log.info("%s: refreshing (expires %s)", name, client.expires_at)
            refreshed, error = await _refresh_with_retry(client)
            if not refreshed:
                return {
                    "name": name,
                    "state": "error",
                    "expires_at": getattr(client, "expires_at", None),
                    "error": error or "refresh failed",
                }
        return {
            "name": name,
            "state": "active",
            "expires_at": getattr(client, "expires_at", None),
            "refresh_after": getattr(client, "refresh_after", None),
        }
    except Exception as exc:  # noqa: BLE001 - report, never crash the keeper
        return {
            "name": name,
            "state": "error",
            "expires_at": getattr(client, "expires_at", None),
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def main_async(args) -> int:
    root = _data_dir(args.data_dir)
    ext_root = root / "extensions"
    selected = [item.strip() for item in (args.extensions or "").split(",") if item.strip()]
    if not selected:
        selected = sorted(
            p.parent.name for p in ext_root.glob("*/manifest.json")
        )
    required = {
        item.strip()
        for item in (args.required or ",".join(selected)).split(",")
        if item.strip()
    }

    results = []
    for name in selected:
        manifest_path = ext_root / name / "manifest.json"
        if not manifest_path.exists():
            results.append({"name": name, "state": "not-installed"})
            continue
        results.append(await _refresh_one(name, manifest_path, root, args.force))

    for result in results:
        state = result.get("state")
        line = f"{result['name']}: {state}"
        if result.get("expires_at"):
            line += f" (expires {result['expires_at']})"
        if result.get("error"):
            line += f" — {result['error']}"
        log.info(line)

    failed = [
        item for item in results
        if item["name"] in required and item.get("state") != "active"
    ]
    summary = {
        "ok": not failed,
        "required": sorted(required),
        "required_failed": [item["name"] for item in failed],
        "results": results,
    }
    # stdout is for the worker; the human-readable lines stay on stderr.
    print(json.dumps(summary), flush=True)
    if failed:
        log.warning(
            "Required sessions not active: %s",
            ", ".join(item["name"] for item in failed),
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", help="SpotiFLAC data dir (default ~/.spotiflac)")
    parser.add_argument(
        "--extensions",
        help="comma-separated extension ids (default: every installed one)",
    )
    parser.add_argument(
        "--required",
        help="comma-separated ids that must be active for exit 0 (default: all selected)",
    )
    parser.add_argument("--force", action="store_true", help="refresh even if not due")
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except Exception as exc:  # noqa: BLE001 - keeper only needs a status code
        log.error("session refresh crashed: %s: %s", type(exc).__name__, exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
