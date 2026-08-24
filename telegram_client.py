"""Minimal stdlib-only Telegram Bot API client (long-poll).

http.client with two persistent keep-alive connections instead of a fresh
TCP+TLS handshake per call:
  - "_poll" — dedicated to the getUpdates long-poll loop (single-threaded by
    construction: only bot.run() calls it).
  - "_call" — shared by short calls (sendMessage/editMessageText) under a
    lock, so a worker notification never queues behind a 50s long-poll.

Telegram's server closes idle keep-alive sockets, so a dead connection is
retried once on a fresh socket. Polls are idempotent via offset and a rare
duplicate send is cosmetic — that trade is deliberate.
"""

import asyncio
import http.client
import json
import logging
import threading
from typing import cast

_API_HOST = "api.telegram.org"
_POLL_TIMEOUT = 65  # getUpdates long-poll (server holds ~50s) + slack
_CALL_TIMEOUT = 30
_BACKOFF_BASE = 5  # seconds; doubles per consecutive getUpdates failure
_BACKOFF_CAP = 60


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, logger: logging.Logger | None = None):
        self._token = token
        self._logger = logger or logging.getLogger("telegram_client")
        self._fails = 0  # consecutive getUpdates failures (drives backoff)
        self._conns: dict[str, http.client.HTTPConnection | None] = {"_poll": None, "_call": None}
        self._lock = threading.Lock()  # serializes the shared short-call conn

    def _request(self, slot: str, timeout: float, method: str, params: dict):
        """One POST on the slot's keep-alive conn; reconnect-and-retry once.
        Returns the API's `result` (dict or list, per method)."""
        body = json.dumps(params).encode()
        path = f"/bot{self._token}/{method}"
        err = None
        payload = None
        status = 0
        for _ in range(2):
            if self._conns[slot] is None:
                self._conns[slot] = http.client.HTTPSConnection(_API_HOST, timeout=timeout)
            try:
                conn = self._conns[slot]
                assert conn is not None
                conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                data = resp.read()
                status = resp.status
                payload = json.loads(data) if status == 200 else None  # error bodies aren't JSON
                break
            except (OSError, http.client.HTTPException) as e:
                conn = self._conns[slot]
                if conn is not None:
                    try:
                        conn.close()
                    except OSError:
                        pass
                self._conns[slot] = None
                err = e
        else:
            raise TelegramError(f"{method}: {err!r}")
        if status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
            desc = payload.get("description", f"HTTP {status}") if isinstance(payload, dict) else f"HTTP {status}: {data[:120].decode(errors='replace')}"
            raise TelegramError(f"{method}: {desc}")
        return payload["result"]

    def _call_sync(self, method: str, params: dict, timeout: float = _CALL_TIMEOUT) -> dict:
        """Blocking POST to the Bot API; raises TelegramError on failure."""
        if method == "getUpdates":
            return self._request("_poll", _POLL_TIMEOUT, method, params)
        with self._lock:
            return self._request("_call", timeout, method, params)

    async def _call(self, method: str, params: dict, timeout: float = _CALL_TIMEOUT) -> dict:
        return await asyncio.to_thread(self._call_sync, method, params, timeout)

    async def get_updates(self, offset: int | None = None) -> list:
        params = {"timeout": 50, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        try:
            result = await self._call("getUpdates", params)
            self._fails = 0
            return cast(list, result)
        except (OSError, TelegramError) as e:
            self._logger.warning("getUpdates failed: %s", e)
            # ponytail: backoff so outages retry once/min instead of hammering; cap 60s
            await asyncio.sleep(min(_BACKOFF_BASE * 2 ** self._fails, _BACKOFF_CAP))
            self._fails += 1
            return []

    async def send_message(self, chat_id: int, text: str) -> dict | None:
        """Send text with HTML parse mode; returns the message dict (for edits)."""
        try:
            return await self._call("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            })
        except (OSError, TelegramError) as e:
            self._logger.warning("sendMessage failed: %s", e)
            return None

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self._call("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
            })
        except (OSError, TelegramError) as e:
            self._logger.warning("editMessageText failed: %s", e)
