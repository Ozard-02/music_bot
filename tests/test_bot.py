"""Tests for bot.py — Telegram bot handlers."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
os.environ["TELEGRAM_ALLOWED_USER_ID"] = "12345"

from bot import (
    start,
    help_cmd,
    status_cmd,
    purge_cmd,
    handle_message,
    _is_allowed,
    ALLOWED_USER_ID,
)


def _make_update(user_id: int = 12345, text: str = "") -> MagicMock:
    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    update.effective_user = user
    update.message = AsyncMock()
    update.message.text = text
    return update


def _make_context(qm=None, logger=None) -> MagicMock:
    import asyncio
    context = MagicMock()
    context.application.bot_data = {
        "queue_manager": qm or MagicMock(),
        "logger": logger or MagicMock(),
        "wake_event": asyncio.Event(),
    }
    return context


class TestIsAllowed:
    def test_allowed_user(self):
        upd = _make_update(user_id=12345)
        assert _is_allowed(upd) is True

    def test_denied_user(self):
        upd = _make_update(user_id=99999)
        assert _is_allowed(upd) is False

    def test_no_user(self):
        upd = MagicMock()
        upd.effective_user = None
        assert not _is_allowed(upd)


class TestStart:
    @pytest.mark.asyncio
    async def test_replies_with_help(self):
        update = _make_update()
        context = _make_context()
        await start(update, context)
        update.message.reply_html.assert_awaited_once()
        text = update.message.reply_html.call_args[0][0]
        assert "SpotiLoop" in text

    @pytest.mark.asyncio
    async def test_ignores_unauthorized(self):
        update = _make_update(user_id=99999)
        context = _make_context()
        await start(update, context)
        update.message.reply_html.assert_not_awaited()


class TestHelpCmd:
    @pytest.mark.asyncio
    async def test_replies_with_format_help(self):
        update = _make_update()
        context = _make_context()
        await help_cmd(update, context)
        update.message.reply_html.assert_awaited_once()
        text = update.message.reply_html.call_args[0][0]
        assert "Spotify" in text

    @pytest.mark.asyncio
    async def test_ignores_unauthorized(self):
        update = _make_update(user_id=99999)
        context = _make_context()
        await help_cmd(update, context)
        update.message.reply_html.assert_not_awaited()


class TestStatusCmd:
    @pytest.mark.asyncio
    async def test_replies_with_queue_summary(self):
        qm = MagicMock()
        qm.get_status.return_value = {
            "queued": 2, "running": 1, "done": 5, "failed": 0, "next_id": 3,
        }
        qm.get_history.return_value = [
            {"id": 8, "status": "done", "query": "Album A", "result_ok": 5,
             "result_skipped": 0, "result_failed": 0},
            {"id": 7, "status": "done", "query": "Album B", "result_ok": 3,
             "result_skipped": 1, "result_failed": 0},
        ]

        update = _make_update()
        context = _make_context(qm=qm)
        await status_cmd(update, context)
        update.message.reply_html.assert_awaited_once()
        text = update.message.reply_html.call_args[0][0]
        assert "Queue" in text
        assert "2" in text  # queued count
        assert "5" in text  # done count

    @pytest.mark.asyncio
    async def test_ignores_unauthorized(self):
        update = _make_update(user_id=99999)
        context = _make_context()
        await status_cmd(update, context)
        update.message.reply_html.assert_not_awaited()


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_enqueues_link(self):
        qm = MagicMock()
        qm.find_existing.return_value = None
        qm.enqueue.return_value = 42
        qm.get_status.return_value = {
            "queued": 3, "running": 1, "done": 0, "failed": 0,
        }

        update = _make_update(
            text="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        )
        context = _make_context(qm=qm)
        await handle_message(update, context)
        qm.find_existing.assert_called_once()
        qm.enqueue.assert_called_once()
        update.message.reply_html.assert_awaited_once()
        text = update.message.reply_html.call_args[0][0]
        assert "Queued" in text
        assert "42" in text

    @pytest.mark.asyncio
    async def test_enqueues_search(self):
        qm = MagicMock()
        qm.find_existing.return_value = None
        qm.enqueue.return_value = 7
        qm.get_status.return_value = {
            "queued": 1, "running": 0, "done": 0, "failed": 0,
        }

        update = _make_update(text="Artist - Album")
        context = _make_context(qm=qm)
        await handle_message(update, context)
        qm.find_existing.assert_called_once()
        qm.enqueue.assert_called_once()
        update.message.reply_html.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_shows_warning(self):
        qm = MagicMock()
        qm.find_existing.return_value = 99

        update = _make_update(text="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
        context = _make_context(qm=qm)
        await handle_message(update, context)
        qm.enqueue.assert_not_called()
        update.message.reply_html.assert_awaited_once()
        text = update.message.reply_html.call_args[0][0]
        assert "Already queued" in text
        assert "99" in text

    @pytest.mark.asyncio
    async def test_invalid_input_shows_help(self):
        update = _make_update(text="gibberish")
        context = _make_context()
        await handle_message(update, context)
        update.message.reply_html.assert_awaited_once()
        text = update.message.reply_html.call_args[0][0]
        assert "Didn't understand" in text

    @pytest.mark.asyncio
    async def test_ignores_unauthorized(self):
        update = _make_update(user_id=99999, text="Artist - Album")
        context = _make_context()
        await handle_message(update, context)
        update.message.reply_html.assert_not_awaited()


class TestPurgeCmd:
    @pytest.mark.asyncio
    async def test_purges_all_items(self):
        qm = MagicMock()
        qm.purge_all.return_value = 3
        update = _make_update(text="/purge")
        context = _make_context(qm=qm)
        await purge_cmd(update, context)
        qm.purge_all.assert_called_once()
        update.message.reply_html.assert_awaited_once()
        text = update.message.reply_html.call_args[0][0]
        assert "Purged" in text
        assert "3" in text
        assert "items" in text

    @pytest.mark.asyncio
    async def test_purge_singular(self):
        qm = MagicMock()
        qm.purge_all.return_value = 1
        update = _make_update()
        context = _make_context(qm=qm)
        await purge_cmd(update, context)
        text = update.message.reply_html.call_args[0][0]
        assert "item" in text
        assert "s" not in text.split("item")[0][-3:]  # singular

    @pytest.mark.asyncio
    async def test_ignores_unauthorized(self):
        update = _make_update(user_id=99999)
        context = _make_context()
        await purge_cmd(update, context)
        update.message.reply_html.assert_not_awaited()
