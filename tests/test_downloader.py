"""Tests for downloader.py — retry logic and download state."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import asyncio

from config import PER_TRACK_RETRIES
from downloader import (
    _download_track_with_retry,
    RunState,
    download_single_track,
)


@pytest.fixture
def track():
    t = MagicMock()
    t.id = "abc123"
    t.title = "Test Track"
    return t


@pytest.fixture
def client():
    c = AsyncMock()
    c.download_track = AsyncMock()
    return c


@pytest.fixture
def sem():
    return asyncio.Semaphore(1)


@pytest.fixture
def state():
    return RunState()


@pytest.fixture
def logger():
    import logging
    return logging.getLogger("test")


class TestDownloadTrackWithRetry:
    @pytest.mark.asyncio
    async def test_first_attempt_success(self, client, track, sem, state, logger):
        client.download_track.return_value = None
        await _download_track_with_retry(client, track, sem, state, logger)
        assert state.ok == 1
        assert state.failed == 0
        client.download_track.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retry_then_success(self, client, track, sem, state, logger):
        client.download_track.side_effect = [
            RuntimeError("First fail"),
            RuntimeError("Second fail"),
            None,
        ]
        with patch("downloader.asyncio.sleep", new_callable=AsyncMock):
            await _download_track_with_retry(client, track, sem, state, logger)
        assert state.ok == 1
        assert state.failed == 0
        assert client.download_track.await_count == 3

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self, client, track, sem, state, logger):
        client.download_track.side_effect = RuntimeError("Always fails")
        with patch("downloader.asyncio.sleep", new_callable=AsyncMock):
            await _download_track_with_retry(client, track, sem, state, logger)
        assert state.ok == 0
        assert state.failed == 1
        assert client.download_track.await_count == 1 + PER_TRACK_RETRIES

    @pytest.mark.asyncio
    async def test_in_progress_guard_skips_duplicate(self, client, track, sem, state, logger):
        state.in_progress.add(track.id)
        await _download_track_with_retry(client, track, sem, state, logger)
        assert state.skipped == 1
        assert state.ok == 0
        assert state.failed == 0
        client.download_track.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_in_progress_cleaned_after_success(self, client, track, sem, state, logger):
        client.download_track.return_value = None
        await _download_track_with_retry(client, track, sem, state, logger)
        assert track.id not in state.in_progress

    @pytest.mark.asyncio
    async def test_timeout_retried(self, client, track, sem, state, logger):
        client.download_track.side_effect = [
            asyncio.TimeoutError(),
            None,
        ]
        with patch("downloader.asyncio.sleep", new_callable=AsyncMock):
            await _download_track_with_retry(client, track, sem, state, logger)
        assert state.ok == 1
        assert state.failed == 0
        assert client.download_track.await_count == 2


    @pytest.mark.asyncio
    async def test_failed_track_recorded_in_state(self, client, track, sem, state, logger):
        client.download_track.side_effect = RuntimeError("Always fails")
        with patch("downloader.asyncio.sleep", new_callable=AsyncMock):
            await _download_track_with_retry(client, track, sem, state, logger)
        assert len(state.failed_tracks) == 1
        track_id, title, err = state.failed_tracks[0]
        assert track_id == "abc123"
        assert title == "Test Track"
        assert "Always fails" in err

    @pytest.mark.asyncio
    async def test_failed_track_not_recorded_on_success(
        self, client, track, sem, state, logger,
    ):
        client.download_track.return_value = None
        await _download_track_with_retry(client, track, sem, state, logger)
        assert state.failed_tracks == []


class TestRunState:
    def test_defaults(self):
        s = RunState()
        assert s.skipped == 0
        assert s.ok == 0
        assert s.failed == 0
        assert s.total == 0
        assert s.done == 0
        assert s.in_progress == set()
        assert s.failed_tracks == []
