"""Minimal stdlib-only Telegram Bot API client (long-poll).

Covers exactly what the bot uses: getUpdates, sendMessage, editMessageText —
HTML parse mode, text only. urllib + json, no third-party deps.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urlencode

_API = "https://api.telegram.org/bot{token}/{method}"
_GETUPDATES_TIMEOUT = 50  # long-poll seconds


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, logger: logging.Logger | None = None):
        self._token = token
        self._logger = logger or logging.getLogger("telegram_client")

    def _call_sync(self, method: str, params: dict, timeout: float = 30) -> dict:
        """Blocking POST to the Bot API; raises TelegramError on failure."""
        body = json.dumps(params).encode()
        req = urllib.request.Request(
            _API.format(token=self._token, method=method),
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
        if not payload.get("ok"):
            raise TelegramError(payload.get("description", "unknown error"))
        return payload["result"]

    async def _call(self, method: str, params: dict, timeout: float = 30) -> dict:
        return await asyncio.to_thread(self._call_sync, method, params, timeout)

    async def get_updates(self, offset: int | None = None) -> list[dict]:
        params = {"timeout": _GETUPDATES_TIMEOUT, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        try:
            return await self._call("getUpdates", params, timeout=_GETUPDATES_TIMEOUT + 15)
        except (urllib.error.URLError, TimeoutError, TelegramError) as e:
            self._logger.warning("getUpdates failed: %s", e)
            return []

    async def send_message(self, chat_id: int, text: str) -> dict | None:
        """Send text with HTML parse mode; returns the message dict (for edits)."""
        try:
            return await self._call("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            })
        except (urllib.error.URLError, TimeoutError, TelegramError) as e:
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
        except (urllib.error.URLError, TimeoutError, TelegramError) as e:
            self._logger.warning("editMessageText failed: %s", e)