"""Tests for download_job.py — the subprocess side of the JSON-lines protocol."""

import json
import logging

import pytest

import download_job
from downloader import DownloadResult


@pytest.mark.asyncio
async def test_progress_events_reach_stdout(monkeypatch, capsys):
    """Each per-track progress callback must emit a protocol line.

    The parent's /status display and its stall watchdog both live on these
    lines: a job that never emits progress looks hung after stall_timeout.
    """
    async def fake_run_url(url, cfg, logger, skip_titles, progress_cb, failure_cb):
        progress_cb(1, 2, "First", "qobuz")
        progress_cb(2, 2, "Second", "deezer")
        return DownloadResult()

    monkeypatch.setattr(download_job, "run_url", fake_run_url)
    await download_job._job(
        {"type": "link", "url": "https://open.spotify.com/album/x", "cfg": {}},
        logging.getLogger("test"),
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    progress = [e for e in events if e["event"] == "progress"]
    assert [(e["done"], e["total"], e["title"]) for e in progress] == [
        (1, 2, "First"), (2, 2, "Second"),
    ]
