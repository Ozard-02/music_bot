"""Hard rule: the parent import graph must stay stdlib-only."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_parent_does_not_import_spotiflac_or_telegram():
    code = """
import sys
import bot

forbidden_top = {"SpotiFLAC", "httpx", "telegram", "mutagen", "PIL"}
bad = sorted(
    m for m in sys.modules
    if m.split(".")[0] in forbidden_top or m.startswith("urllib.request")
)
if bad:
    raise SystemExit(f"parent imported: {bad}")
"""
    env = {
        **os.environ,
        "TELEGRAM_BOT_TOKEN": "test:token",
        "TELEGRAM_ALLOWED_USER_IDS": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
