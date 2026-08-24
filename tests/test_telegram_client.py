import json
from unittest.mock import patch

import pytest

from telegram_client import TelegramClient, TelegramError


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


@pytest.fixture
def client():
    return TelegramClient("TOKEN", logger=None)


def _fake_sleep(into):
    async def fake_sleep(s):
        into.append(s)
    return fake_sleep


@pytest.mark.asyncio
async def test_get_updates_passes_offset(client):
    captured = {}

    async def fake_call(self, method, params, timeout=30):
        captured.update(params)
        return [{"update_id": 5, "message": {}}]

    with patch.object(TelegramClient, "_call", new=fake_call):
        updates = await client.get_updates(offset=99)
    assert updates == [{"update_id": 5, "message": {}}]
    assert captured["offset"] == 99
    assert captured["timeout"] == 50


@pytest.mark.asyncio
async def test_get_updates_no_offset(client):
    captured = {}

    async def fake_call(self, method, params, timeout=30):
        captured.update(params)
        return []

    with patch.object(TelegramClient, "_call", new=fake_call):
        await client.get_updates()
    assert "offset" not in captured


@pytest.mark.asyncio
async def test_get_updates_network_error_returns_empty(client):
    with patch.object(TelegramClient, "_call", side_effect=TimeoutError("boom")):
        with patch("telegram_client.asyncio.sleep", new=_fake_sleep([])):
            assert await client.get_updates() == []


@pytest.mark.asyncio
async def test_get_updates_backoff_and_reset(client):
    sleeps = []
    calls = {"n": 0}

    async def fake_call(self, method, params, timeout=30):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise TimeoutError("boom")
        return [{"update_id": 1, "message": {}}]

    with patch.object(TelegramClient, "_call", new=fake_call):
        with patch("telegram_client.asyncio.sleep", new=_fake_sleep(sleeps)):
            assert await client.get_updates() == []
            assert await client.get_updates() == []
            assert await client.get_updates() == [{"update_id": 1, "message": {}}]
            # backoff resets after success
            with patch.object(TelegramClient, "_call", side_effect=TimeoutError("boom")):
                with patch("telegram_client.asyncio.sleep", new=_fake_sleep(sleeps)):
                    assert await client.get_updates() == []
    assert sleeps == [5, 10, 5]


@pytest.mark.asyncio
async def test_send_message_uses_html(client):
    captured = {}

    async def fake_call(self, method, params, timeout=30):
        captured.update(params)
        return {"message_id": 7}

    with patch.object(TelegramClient, "_call", new=fake_call):
        res = await client.send_message(123, "hi <b>x</b>")
    assert res == {"message_id": 7}
    assert captured["chat_id"] == 123
    assert captured["parse_mode"] == "HTML"
    assert captured["text"] == "hi <b>x</b>"


@pytest.mark.asyncio
async def test_send_message_error_returns_none(client):
    with patch.object(TelegramClient, "_call", side_effect=TelegramError("bad request")):
        assert await client.send_message(1, "x") is None


def test_call_sync_ok():
    with patch("telegram_client.urllib.request.urlopen", return_value=FakeResp({"ok": True, "result": ["r"]})):
        assert TelegramClient("t")._call_sync("getUpdates", {}) == ["r"]


def test_call_sync_error_raises():
    with patch("telegram_client.urllib.request.urlopen", return_value=FakeResp({"ok": False, "description": "bot was blocked"})):
        with pytest.raises(TelegramError, match="bot was blocked"):
            TelegramClient("t")._call_sync("sendMessage", {})