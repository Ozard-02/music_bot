"""Tests for bot.py — Telegram handlers (raw client, no PTB)."""

import asyncio
import os
from unittest.mock import AsyncMock

import pytest

os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "12345"
# neutralize the legacy var, whether it leaks from the shell or from ./env
os.environ["TELEGRAM_ALLOWED_USER_ID"] = ""

from bot import Bot, _parse_allowed_user_ids, ALLOWED_USER_IDS


@pytest.fixture
def bot(queue_manager, config, logger):
    return Bot("test:token", queue_manager, config, logger)


def _update(user_id: int = 12345, text: str = "") -> dict:
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": 12345},
            "from": {"id": user_id, "username": "espo", "first_name": "Espo"},
            "text": text,
        },
    }


def _assert_sent(bot, contains: str):
    assert bot._client.send_message.call_count >= 1
    texts = [c.args[1] for c in bot._client.send_message.call_args_list]
    assert any(contains in t for t in texts)


class TestAllowlist:
    def test_parse_allowed_user_ids(self):
        assert _parse_allowed_user_ids() == {12345}

    @pytest.mark.asyncio
    async def test_unknown_user_rejected(self, bot):
        bot._client.send_message = AsyncMock()
        await bot._handle_update(_update(user_id=999))
        bot._client.send_message.assert_not_awaited()


class TestHandlers:
    @pytest.mark.asyncio
    async def test_start(self, bot):
        bot._client.send_message = AsyncMock()
        await bot._handle_update(_update(text="/start"))
        bot._client.send_message.assert_awaited_once()
        assert "SpotiLoop Bot" in bot._client.send_message.call_args.args[1]

    @pytest.mark.asyncio
    async def test_help(self, bot):
        bot._client.send_message = AsyncMock()
        await bot._handle_update(_update(text="/help"))
        bot._client.send_message.assert_awaited_once()
        assert "Commands:" in bot._client.send_message.call_args.args[1]

    @pytest.mark.asyncio
    async def test_status_shows_stats(self, bot):
        bot._client.send_message = AsyncMock()
        await bot._handle_update(_update(text="/status"))
        bot._client.send_message.assert_awaited_once()
        assert "Queue Status" in bot._client.send_message.call_args.args[1]

    @pytest.mark.asyncio
    async def test_quality_no_arg_lists(self, bot):
        bot._client.send_message = AsyncMock()
        await bot._handle_update(_update(text="/quality"))
        bot._client.send_message.assert_awaited_once()
        assert "Quality" in bot._client.send_message.call_args.args[1]

    @pytest.mark.asyncio
    async def test_quality_set(self, bot):
        bot._client.send_message = AsyncMock()
        await bot._handle_update(_update(text="/quality HIGH"))
        bot._client.send_message.assert_awaited_once()
        assert "Quality set to" in bot._client.send_message.call_args.args[1]
        row = bot._qm.get_user(12345)
        assert row["quality"] == "HIGH"

    @pytest.mark.asyncio
    async def test_quality_invalid(self, bot):
        bot._client.send_message = AsyncMock()
        await bot._handle_update(_update(text="/quality BOGUS"))
        bot._client.send_message.assert_awaited_once()
        assert "Unknown quality" in bot._client.send_message.call_args.args[1]

    @pytest.mark.asyncio
    async def test_purge(self, bot):
        bot._client.send_message = AsyncMock()
        bot._qm.enqueue_unique("link", "https://open.spotify.com/track/abc")
        await bot._handle_update(_update(text="/purge"))
        bot._client.send_message.assert_awaited_once()
        assert "Purged" in bot._client.send_message.call_args.args[1]
        assert bot._qm.get_status()["queued"] == 0

    @pytest.mark.asyncio
    async def test_mkplaylist_usage_when_no_args(self, bot):
        bot._client.send_message = AsyncMock()
        bot._run_command_job = AsyncMock()
        await bot._handle_update(_update(text="/mkplaylist"))
        bot._run_command_job.assert_not_awaited()
        bot._client.send_message.assert_awaited_once()
        assert "Usage:" in bot._client.send_message.call_args.args[1]

    @pytest.mark.asyncio
    async def test_mkplaylist_builds(self, bot):
        bot._client.send_message = AsyncMock(return_value={"message_id": 7})
        bot._client.edit_message = AsyncMock()
        result = {
            "path": "/m/playlist.m3u8", "playlist_name": "Mix",
            "total_tracks": 5, "exist_on_disk": 4, "missing_count": 1,
            "missing_log_path": "/m/temp/Mix_missing.txt", "cover_path": "/m/Mix.jpg",
        }
        bot._run_command_job = AsyncMock(return_value=(result, 7))
        await bot._handle_update(_update(text="/mkplaylist https://open.spotify.com/playlist/abc"))
        bot._run_command_job.assert_awaited_once()
        spec = bot._run_command_job.call_args.args[1]
        assert spec["type"] == "m3u8"
        assert spec["url"] == "https://open.spotify.com/playlist/abc"
        edited = bot._client.edit_message.call_args.args[2]
        assert "Mix" in edited and "4/5 tracks" in edited and "Cover saved" in edited

    @pytest.mark.asyncio
    async def test_fixmetadata_whole_library(self, bot, tmp_path):
        bot._cfg["output_dir"] = str(tmp_path)
        bot._client.send_message = AsyncMock(return_value={"message_id": 7})
        bot._client.edit_message = AsyncMock()
        result = {"folders": 3, "fixed": 12, "moved": 1, "failed": 0,
                  "failed_files": [], "moved_files": ["/x/album/artist - title.flac"]}
        bot._run_command_job = AsyncMock(return_value=(result, 7))
        await bot._handle_update(_update(text="/fixmetadata"))
        spec = bot._run_command_job.call_args.args[1]
        assert spec["type"] == "fix_metadata"
        assert spec["folder"] == str(tmp_path / "espo_Music")
        assert spec["lyrics"] is False
        edited = bot._client.edit_message.call_args.args[2]
        assert "Fix metadata done" in edited and "Folders: 3" in edited and "Moved: 1" in edited

    @pytest.mark.asyncio
    async def test_fixmetadata_lyrics_flag(self, bot, tmp_path):
        bot._cfg["output_dir"] = str(tmp_path)
        bot._client.send_message = AsyncMock(return_value={"message_id": 7})
        bot._client.edit_message = AsyncMock()
        bot._run_command_job = AsyncMock(return_value=(
            {"folders": 1, "fixed": 2, "moved": 0, "failed": 0, "failed_files": [], "moved_files": []}, 7))
        await bot._handle_update(_update(text="/fixmetadata --lyrics"))
        assert bot._run_command_job.call_args.args[1]["lyrics"] is True

    @pytest.mark.asyncio
    async def test_fixmetadata_missing_folder(self, bot, tmp_path):
        bot._cfg["output_dir"] = str(tmp_path)
        bot._client.send_message = AsyncMock()
        bot._run_command_job = AsyncMock()
        await bot._handle_update(_update(text="/fixmetadata no/such/folder"))
        bot._run_command_job.assert_not_awaited()
        assert "Not a folder" in bot._client.send_message.call_args.args[1]


class TestEnqueue:
    @pytest.mark.asyncio
    async def test_valid_link_queued(self, bot):
        bot._client.send_message = AsyncMock()
        await bot._handle_update(
            _update(text="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
        )
        assert bot._qm.get_status()["queued"] == 1
        assert bot._qm.get_status()["running"] == 0

    @pytest.mark.asyncio
    async def test_duplicate_link_not_requeued(self, bot):
        bot._client.send_message = AsyncMock()
        await bot._handle_update(
            _update(text="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
        )
        await bot._handle_update(
            _update(text="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
        )
        assert bot._qm.get_status()["queued"] == 1
        _assert_sent(bot, "Already queued")

    @pytest.mark.asyncio
    async def test_search_queued(self, bot):
        bot._client.send_message = AsyncMock()
        await bot._handle_update(_update(text="Artist - Album"))
        assert bot._qm.get_status()["queued"] == 1

    @pytest.mark.asyncio
    async def test_invalid_text_shows_help(self, bot):
        bot._client.send_message = AsyncMock()
        await bot._handle_update(_update(text="random text"))
        bot._client.send_message.assert_awaited_once()
        assert "Didn't understand" in bot._client.send_message.call_args.args[1]
        assert bot._qm.get_status()["queued"] == 0