#!/usr/bin/env python3
"""Create SpotiFLAC signed sessions (one-off, browser machine only).

SpotiFLAC 5.x downloads through JS extensions that need a `zarz-v2` signed
session.  The bot itself has no Chromium, so the first session is created here
on a machine with a browser (or with `chrome-taste`/Chromium on PATH) and then
copied to the server:

    python scripts/seed_sessions.py --data-dir ~/.spotiflac
    scp ~/.spotiflac/signed_sessions/zarz-v2-*.json \
        truenas:/mnt/pool/VM/spotiflac/signed_sessions/

After that `session_refresh.py` (run by the bot every 2h) keeps them alive.

Options:
  --extensions deezer,qobuz-web,amazon   which extensions to seed
  --manual                               paste a grant instead of using the
                                         local browser (useful on headless hosts)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

DEFAULT_EXTENSIONS = "deezer,qobuz-web,amazon"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


async def _manual_grant(client) -> None:
    """Browser-independent fallback: open the challenge, paste the grant.

    SpotiFLAC 5.x has no `authenticate_with_manual_grant()` helper anymore
    (only the automatic Turnstile path and the low-level bootstrap/exchange
    pair), so the flow is reproduced here.
    """
    boot = await client.bootstrap()
    if boot is True:
        return  # session obtained directly
    if not client.pending_challenge_id:
        raise RuntimeError(f"bootstrap did not provide a challenge (got {boot!r})")
    challenge_url = client._build_challenge_url_with_callback(
        client.pending_challenge_id,
        "http://127.0.0.1:1/callback",
    )
    print(
        "\nOpen this URL, solve the Turnstile, then copy the `grant` value from\n"
        "DevTools → Network → the `verify` request → Preview (valid ~60s):\n\n"
        f"{challenge_url}\n",
        file=sys.stderr,
    )
    grant = input("Paste grant: ").strip()
    if not grant:
        raise RuntimeError("no grant provided")
    await client.exchange_grant(grant)


async def _seed_one(name: str, root: Path, manual: bool) -> tuple[str, bool, str]:
    manifest_path = root / "extensions" / name / "manifest.json"
    if not manifest_path.exists():
        return name, False, "extension not installed"
    config = _load(manifest_path).get("signedSession")
    if not config:
        return name, False, "manifest has no signedSession"

    from SpotiFLAC.core.signed_session_mobile import client_from_manifest

    client = client_from_manifest(config, data_dir=str(root / "signed_sessions"))
    try:
        if client.authenticated:
            return name, True, f"already active until {client.expires_at}"
        if manual:
            await _manual_grant(client)
        else:
            try:
                await client.authenticate_with_turnstile(timeout=90)
            except Exception as exc:
                print(
                    f"{name}: automatic Turnstile failed ({exc}); "
                    "retrying with manual grant",
                    file=sys.stderr,
                )
                await _manual_grant(client)
        if not client.authenticated:
            return name, False, "authentication finished but no session is active"
        return name, True, f"active until {client.expires_at}"
    except Exception as exc:  # noqa: BLE001 - report per extension
        return name, False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def main_async(args) -> int:
    root = Path(args.data_dir).expanduser()
    names = [item.strip() for item in args.extensions.split(",") if item.strip()]
    results = []
    for name in names:
        print(f"Seeding {name}…", file=sys.stderr)
        results.append(await _seed_one(name, root, args.manual))

    print()
    failed = 0
    for name, ok, detail in results:
        print(f"  {'OK ' if ok else 'FAIL'}  {name}: {detail}")
        if not ok:
            failed += 1
    if failed:
        print(
            f"\n{failed} session(s) failed. The bot can still use the others.\n"
            "Sessions live in: " + str(root / "signed_sessions"),
            file=sys.stderr,
        )
        return 1
    print(f"\nSessions written to: {root / 'signed_sessions'}")
    print("Copy the zarz-v2-*.json files to the server's volume.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="~/.spotiflac")
    parser.add_argument("--extensions", default=DEFAULT_EXTENSIONS)
    parser.add_argument("--manual", action="store_true")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
