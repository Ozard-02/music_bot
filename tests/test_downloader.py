"""Tests for downloader.py — download orchestration with pre-check."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from downloader import run_url


def _mock_track(track_id: str, title: str, **kwargs):
    t = MagicMock()
    t.id = track_id
    t.title = title
    t.external_url = f"https://open.spotify.com/track/{track_id}"
    t.first_artist = kwargs.get("first_artist", "Artist")
    t.artists = kwargs.get("artists", "Artist")
    t.album_artist = kwargs.get("album_artist", "AlbumArtist")
    t.album = kwargs.get("album", "Album")
    t.duration_seconds = 0
    return t


def _make_client(track_return=None, playlist_return=None,
                 download_return=None):
    client = AsyncMock()
    if track_return is not None:
        client.get_track_metadata = AsyncMock(return_value=track_return)
    else:
        client.get_track_metadata = AsyncMock()
    if playlist_return is not None:
        client.get_playlist = AsyncMock(return_value=playlist_return)
    client.download_track = AsyncMock(return_value=download_return or [])
    return client


class TestRunUrl:
    TRACK_URL = "https://open.spotify.com/track/abc123"
    ALBUM_URL = "https://open.spotify.com/album/alb123"

    @pytest.mark.asyncio
    async def test_track_success(self, config, logger):
        track = _mock_track("abc123", "Test Track")
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "track", "id": "abc123"}
            client = _make_client(track_return=track, download_return=[])
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.TRACK_URL, config, logger)

        assert result == {"ok": 1, "skipped": 0, "failed": 0, "failed_tracks": [], "total": 1}
        client.download_track.assert_awaited_once_with(self.TRACK_URL)

    @pytest.mark.asyncio
    async def test_track_already_exists(self, tmp_path):
        logger = logging.getLogger("test")
        cfg = {
            "output_dir": str(tmp_path),
            "filename_format": "{artist} - {title}",
            "use_artist_subfolders": True,
            "use_album_subfolders": True,
            "first_artist_only": True,
            "embed_lyrics": True,
            "quality": "LOSSLESS",
            "services": ["qobuz", "deezer", "amazon"],
        }
        track = _mock_track("abc123", "Test Track", first_artist="Artist",
                            album_artist="AlbumArtist", album="Album")
        (tmp_path / "AlbumArtist" / "Album").mkdir(parents=True)
        (tmp_path / "AlbumArtist" / "Album" / "Artist - Test Track.flac").touch()

        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "track", "id": "abc123"}
            client = _make_client(track_return=track)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.TRACK_URL, cfg, logger)

        assert result == {"ok": 0, "skipped": 1, "failed": 0, "failed_tracks": [], "total": 1}
        client.download_track.assert_not_called()

    @pytest.mark.asyncio
    async def test_track_failure(self, config, logger):
        track = _mock_track("abc123", "Test Track")
        failed = [_mock_track("abc123", "Test Track")]
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "track", "id": "abc123"}
            client = _make_client(track_return=track, download_return=failed)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.TRACK_URL, config, logger)

        assert result["ok"] == 0
        assert result["failed"] == 1
        assert len(result["failed_tracks"]) == 1

    @pytest.mark.asyncio
    async def test_album_all_exist(self, tmp_path):
        logger = logging.getLogger("test")
        cfg = {
            "output_dir": str(tmp_path),
            "filename_format": "{artist} - {title}",
            "use_artist_subfolders": True,
            "use_album_subfolders": True,
            "first_artist_only": True,
            "embed_lyrics": True,
            "quality": "LOSSLESS",
            "services": ["qobuz", "deezer", "amazon"],
        }
        t1 = _mock_track("t1", "Song A", first_artist="ArtistA",
                          album_artist="AA", album="AA")
        t2 = _mock_track("t2", "Song B", first_artist="ArtistB",
                          album_artist="BB", album="BB")
        (tmp_path / "AA" / "AA").mkdir(parents=True)
        (tmp_path / "AA" / "AA" / "ArtistA - Song A.flac").touch()
        (tmp_path / "BB" / "BB").mkdir(parents=True)
        (tmp_path / "BB" / "BB" / "ArtistB - Song B.flac").touch()

        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(playlist_return=({"name": "Test Album", "type": "album"}, [t1, t2]))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.ALBUM_URL, cfg, logger)

        assert result == {"ok": 0, "skipped": 2, "failed": 0, "failed_tracks": [], "total": 2}
        client.download_track.assert_not_called()

    @pytest.mark.asyncio
    async def test_album_all_new(self, config, logger):
        t1 = _mock_track("t1", "Song A")
        t2 = _mock_track("t2", "Song B")
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(
                playlist_return=({"name": "Test Album", "type": "album"}, [t1, t2]),
                download_return=[],
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            cfg = {**config, "output_dir": "/tmp/does-not-exist-xyz"}
            result = await run_url(self.ALBUM_URL, cfg, logger)

        assert result == {"ok": 2, "skipped": 0, "failed": 0, "failed_tracks": [], "total": 2}
        assert client.download_track.await_count == 2
        client.download_track.assert_any_call(t1.external_url)
        client.download_track.assert_any_call(t2.external_url)

    @pytest.mark.asyncio
    async def test_album_partial_exist(self, tmp_path):
        logger = logging.getLogger("test")
        cfg = {
            "output_dir": str(tmp_path),
            "filename_format": "{artist} - {title}",
            "use_artist_subfolders": True,
            "use_album_subfolders": True,
            "first_artist_only": True,
            "embed_lyrics": True,
            "quality": "LOSSLESS",
            "services": ["qobuz", "deezer", "amazon"],
        }

        t1 = _mock_track("t1", "Song A", first_artist="ArtistA", album_artist="AA", album="AA")
        t2 = _mock_track("t2", "Song B", first_artist="ArtistB", album_artist="BB", album="BB")

        # create t1 on disk to simulate "already exists"
        (tmp_path / "AA" / "AA").mkdir(parents=True)
        (tmp_path / "AA" / "AA" / "ArtistA - Song A.flac").touch()

        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(
                playlist_return=({"name": "Test Album", "type": "album"}, [t1, t2]),
                download_return=[],
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.ALBUM_URL, cfg, logger)

        assert result["ok"] == 1
        assert result["skipped"] == 1
        assert result["failed"] == 0
        assert client.download_track.await_count == 1
        client.download_track.assert_any_call(t2.external_url)

    @pytest.mark.asyncio
    async def test_album_partial_failure(self, config, logger):
        t1 = _mock_track("t1", "OK Song")
        t2 = _mock_track("t2", "Bad Song")
        failed = [_mock_track("t2", "Bad Song")]
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(
                playlist_return=({"name": "Test Album", "type": "album"}, [t1, t2]),
            )
            client.download_track = AsyncMock(side_effect=[[], failed])
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.ALBUM_URL, config, logger)

        assert result["ok"] == 1
        assert result["failed"] == 1
        assert len(result["failed_tracks"]) == 1

    @pytest.mark.asyncio
    async def test_download_raises_exception(self, config, logger):
        track = _mock_track("abc123", "Test Track")
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "track", "id": "abc123"}
            client = _make_client(track_return=track)
            client.download_track = AsyncMock(side_effect=RuntimeError("Network error"))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.TRACK_URL, config, logger)

        assert result == {"ok": 0, "skipped": 0, "failed": 1, "failed_tracks": [("abc123", "Test Track", "download_failed")], "total": 1}

    @pytest.mark.asyncio
    async def test_album_dedup_counts_unique(self, config, logger):
        t1 = _mock_track("t1", "Song A")
        t2 = _mock_track("t2", "Song B")
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(
                playlist_return=({"name": "Test", "type": "album"}, [t1, t2, t1]),
                download_return=[],
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.ALBUM_URL, config, logger)

        assert result["ok"] == 2
        assert result["failed"] == 0
        assert client.download_track.await_count == 2
        client.download_track.assert_any_call(t1.external_url)
        client.download_track.assert_any_call(t2.external_url)
