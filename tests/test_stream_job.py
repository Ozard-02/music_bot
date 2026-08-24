"""Tests for worker.stream_job — the real subprocess IPC loop.

Drives a fake job script through spawn/spec/events/watchdog/kill paths.
"""

import asyncio
import json
import sys

import pytest

import worker as worker_mod
from worker import stream_job


FAKE_JOB = """\
import json, sys, time
spec = json.loads(sys.stdin.readline())
{body}
"""


def _install(tmp_path, monkeypatch, body):
    script = tmp_path / "fake_job.py"
    script.write_text(FAKE_JOB.format(body=body))
    monkeypatch.setattr(worker_mod, "JOB_SCRIPT", str(script))


async def _collect(spec, tmp_path, monkeypatch, body, stall_timeout=5.0):
    events = []

    async def on_event(event):
        events.append(event)

    _install(tmp_path, monkeypatch, body)
    result, reason = await stream_job(
        spec,
        logger=__import__("logging").getLogger("test"),
        on_event=on_event,
        stall_timeout=stall_timeout,
    )
    return result, reason, events


@pytest.mark.asyncio
async def test_happy_path_streams_events_and_result(tmp_path, monkeypatch):
    body = (
        "print(json.dumps({'event': 'progress', 'done': 1, 'total': 2, 'title': 'a'}), flush=True)\n"
        "print(json.dumps({'event': 'result', 'result': {'ok': 1, 'echo': spec['x']}}), flush=True)"
    )
    result, reason, events = await _collect({"x": 42}, tmp_path, monkeypatch, body)
    assert reason is None
    assert result == {"ok": 1, "echo": 42}  # spec round-trips via stdin
    assert [e["event"] for e in events] == ["progress"]


@pytest.mark.asyncio
async def test_bad_json_line_is_skipped(tmp_path, monkeypatch):
    body = (
        "print('garbage', flush=True)\n"
        "print(json.dumps({'event': 'result', 'result': {'ok': 2}}), flush=True)"
    )
    result, reason, _ = await _collect({}, tmp_path, monkeypatch, body)
    assert (result, reason) == ({"ok": 2}, None)


@pytest.mark.asyncio
async def test_stall_kills_child(tmp_path, monkeypatch):
    body = "time.sleep(30)"  # emits nothing — watchdog must fire
    result, reason, _ = await _collect({}, tmp_path, monkeypatch, body, stall_timeout=0.3)
    assert (result, reason) == (None, "stall")


@pytest.mark.asyncio
async def test_deadline_kills_child(tmp_path, monkeypatch):
    import time as _time
    body = (
        "print(json.dumps({'event': 'progress', 'done': 1}), flush=True)\n"
        "time.sleep(30)"
    )
    _install(tmp_path, monkeypatch, body)
    events = []

    async def on_event(event):
        events.append(event)

    result, reason = await stream_job(
        {},
        logger=__import__("logging").getLogger("test"),
        on_event=on_event,
        stall_timeout=30.0,
        deadline=_time.monotonic() + 0.3,
    )
    assert (result, reason) == (None, "timeout")


@pytest.mark.asyncio
async def test_exit_without_result_is_crash(tmp_path, monkeypatch):
    body = "pass"
    result, reason, _ = await _collect({}, tmp_path, monkeypatch, body)
    assert (result, reason) == (None, "crash")


@pytest.mark.asyncio
async def test_cancel_kills_child(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, "time.sleep(30)")

    async def on_event(event):
        pass

    task = asyncio.create_task(stream_job(
        {},
        logger=__import__("logging").getLogger("test"),
        on_event=on_event,
        stall_timeout=30.0,
    ))
    await asyncio.sleep(0.2)  # let it spawn and block
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
