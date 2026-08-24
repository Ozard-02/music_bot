import json
from unittest.mock import patch

import http.client
import pytest

from telegram_client import TelegramClient, TelegramError


class FakeConn:
    """Minimal HTTPSConnection stand-in; scripted via class attrs."""

    host = None
    timeout = None
    closed = False
    payload: bytes = b"{}"
    status = 200
    error: Exception | None = None  # raised once from getresponse, then cleared

    instances: list["FakeConn"] = []

    def __init__(self, host, timeout=None):
        self.host, self.timeout = host, timeout
        self.closed = False
        FakeConn.instances.append(self)

    def request(self, method, path, body=None, headers=None):
        self.path = path

    def getresponse(self):
        if FakeConn.error is not None:
            err, FakeConn.error = FakeConn.error, None
            raise err
        return self

    def read(self):
        return FakeConn.payload

    def close(self):
        self.closed = True


@pytest.fixture
def fake_conn():
    FakeConn.instances = []
    FakeConn.payload = b"{}"
    FakeConn.status = 200
    FakeConn.error = None
    with patch("telegram_client.http.client.HTTPSConnection", FakeConn):
        yield FakeConn


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


def test_call_sync_ok(fake_conn):
    fake_conn.payload = json.dumps({"ok": True, "result": ["r"]}).encode()
    assert TelegramClient("t")._call_sync("getUpdates", {}) == ["r"]
    assert "/bott/getUpdates" in fake_conn.instances[0].path


def test_call_sync_error_raises(fake_conn):
    fake_conn.payload = json.dumps({"ok": False, "description": "bot was blocked"}).encode()
    with pytest.raises(TelegramError, match="bot was blocked"):
        TelegramClient("t")._call_sync("sendMessage", {})


def test_http_error_status_raises(fake_conn):
    fake_conn.payload = b"Internal Server Error"
    fake_conn.status = 500
    with pytest.raises(TelegramError, match="HTTP 500"):
        TelegramClient("t")._call_sync("sendMessage", {})


def test_dead_socket_reconnects_once(fake_conn):
    # first attempt hits a socket the server closed (idle keep-alive);
    # the client must reconnect on a fresh conn and succeed
    fake_conn.error = ConnectionResetError("dead socket")
    fake_conn.payload = json.dumps({"ok": True, "result": {"message_id": 1}}).encode()
    assert TelegramClient("t")._call_sync("sendMessage", {}) == {"message_id": 1}
    assert len(fake_conn.instances) == 2
    assert fake_conn.instances[0].closed


def test_persistent_failure_raises_after_retry(fake_conn):
    class DeadConn(FakeConn):
        def getresponse(self):
            raise ConnectionResetError("still dead")

    with patch("telegram_client.http.client.HTTPSConnection", DeadConn):
        with pytest.raises(TelegramError):
            TelegramClient("t")._call_sync("sendMessage", {})
        assert len(DeadConn.instances) == 2  # exactly one retry