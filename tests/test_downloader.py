"""Tests for downloader.py — download orchestration."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from downloader import run_url


@pytest.fixture
def config():
    return {
        "output_dir": "/tmp/test_music",
        "filename_format": "{artist} - {title}",
        "use_artist_subfolders": True,
        "use_album_subfolders": True,
        "first_artist_only": True,
        "embed_lyrics": True,
        "quality": "LOSSLESS",
    }


@pytest.fixture
def logger():
    import logging
    return logging.getLogger("test")


def _mock_client(download_return=None, get_playlist_return=None):
    client = AsyncMock()
    client.download_track = AsyncMock(return_value=download_return or [])
    if get_playlist_return:
        client.get_playlist = AsyncMock(return_value=get_playlist_return)
    return client


def _mock_track(track_id: str, title: str):
    t = MagicMock()
    t.id = track_id
    t.title = title
    return t


class TestDownloadOnce:
    @pytest.mark.asyncio
    async def test_track_success(self, config, logger):
        with patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls, \
             patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse:
            mock_parse.return_value = {"type": "track", "id": "abc123"}
            client = _mock_client()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url("https://spotify.com/track/abc123", config, logger)

            assert result == {"ok": 1, "skipped": 0, "failed": 0, "failed_tracks": []}
            client.download_track.assert_awaited_once_with("https://spotify.com/track/abc123")

    @pytest.mark.asyncio
    async def test_track_failure(self, config, logger):
        with patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls, \
             patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse:
            mock_parse.return_value = {"type": "track", "id": "abc123"}
            failed = [_mock_track("abc123", "Test Track")]
            client = _mock_client(download_return=failed)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url("https://spotify.com/track/abc123", config, logger)

            assert result["ok"] == 0
            assert result["failed"] == 1
            assert len(result["failed_tracks"]) == 1

    @pytest.mark.asyncio
    async def test_album_success(self, config, logger):
        with patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls, \
             patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse:
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            tracks = [_mock_track("t1", "Song A"), _mock_track("t2", "Song B")]
            client = _mock_client(
                download_return=[],
                get_playlist_return=({"name": "Test Album"}, tracks),
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url("https://spotify.com/album/alb123", config, logger)

            assert result == {"ok": 2, "skipped": 0, "failed": 0, "failed_tracks": []}
            client.get_playlist.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_album_partial_failure(self, config, logger):
        with patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls, \
             patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse:
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            tracks = [_mock_track("t1", "OK Song"), _mock_track("t2", "Bad Song")]
            failed = [_mock_track("t2", "Bad Song")]
            client = _mock_client(
                download_return=failed,
                get_playlist_return=({"name": "Test Album"}, tracks),
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url("https://spotify.com/album/alb123", config, logger)

            assert result["ok"] == 1
            assert result["failed"] == 1
            assert len(result["failed_tracks"]) == 1

    @pytest.mark.asyncio
    async def test_download_raises_exception(self, config, logger):
        with patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls, \
             patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse:
            mock_parse.return_value = {"type": "track", "id": "abc123"}
            client = AsyncMock()
            client.download_track = AsyncMock(side_effect=RuntimeError("Network error"))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url("https://spotify.com/track/abc123", config, logger)

            assert result == {"ok": 0, "skipped": 0, "failed": 1, "failed_tracks": []}

    @pytest.mark.asyncio
    async def test_album_dedup_counts_unique(self, config, logger):
        with patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls, \
             patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse:
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            tracks = [
                _mock_track("t1", "Song A"),
                _mock_track("t2", "Song B"),
                _mock_track("t1", "Song A"),  # duplicate
            ]
            client = _mock_client(
                download_return=[],
                get_playlist_return=({"name": "Test"}, tracks),
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url("https://spotify.com/album/alb123", config, logger)

            assert result["ok"] == 2  # unique count, not raw count
            assert result["failed"] == 0
