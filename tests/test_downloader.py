"""Tests for downloader.py — download orchestration with pre-check."""

import asyncio
import logging
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from downloader import _rename_after_download, run_url
from SpotiFLAC.core.models import TrackMetadata
from track_utils import partition_tracks, spotiflac_track_relative_path, track_relative_path


def _touch(path: Path, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


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
    # SpotiFLAC's build_filename() reads these — real types, not MagicMocks
    t.isrc = ""
    t.release_date = ""
    t.year = ""
    t.disc_number = 0
    t.track_number = 0
    return t


# Minimal real FLAC (1 silent 0.05s frame) — mutagen can reopen and re-tag it.
_MINIMAL_FLAC_B64 = (
    "ZkxhQwAAACIQABAAAABFAABFAPoA8AAAACh9IM/5764f4/t0wbC25gK2AwAAEgAAAAA"
    "AAAAAAAAAAAAAAAAAKAQAAFUgAAAAcmVmZXJlbmNlIGxpYkZMQUMgMS41LjAgMjAyNTAy"
    "MTEBAAAAKQAAAFVSTD1odHRwczovL29wZW4uc3BvdGlmeS5jb20vdHJhY2svYWJjMTIz"
    "gQAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAD/+GwIACcEZ0YCWAnWD+MN5rSrjGIm5zGFI0Bq/dcDtU2sERH4E1LXYH5qam2UV1nU"
    "5vxmcNEQq+yXf4DMr4g5OhCwVK8="
)


def _write_flac(path: Path, track_id: str) -> None:
    """Write a minimal real FLAC carrying the Spotify track URL tag."""
    import base64

    from mutagen.flac import FLAC

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(_MINIMAL_FLAC_B64))
    f = FLAC(str(path))
    f["URL"] = f"https://open.spotify.com/track/{track_id}"
    f.save(str(path))


def _make_client(track_return=None, playlist_return=None,
                 download_return=None, cfg=None):
    client = AsyncMock()
    if track_return is not None:
        client.get_track_metadata = AsyncMock(return_value=track_return)
    else:
        client.get_track_metadata = AsyncMock()
    if playlist_return is not None:
        client.get_playlist = AsyncMock(return_value=playlist_return)

    # Map track id -> TrackMetadata so a "successful" download writes the file
    # at the real SpotiFLAC path (as SpotiFLAC does) — the reconcile check then
    # sees what production sees.
    registry = {}
    if track_return is not None:
        registry[track_return.id] = track_return
    if playlist_return:
        for t in playlist_return[1]:
            registry[t.id] = t

    async def _download_track(url):
        tid = url.rsplit("/", 1)[-1].split("?")[0]
        t = registry.get(tid)
        if download_return:
            # failed entries: TrackMetadata or (id, title, artists, err) tuples
            matching = [f for f in download_return if (f.id if hasattr(f, "id") else f[0]) == tid]
            if matching:
                return matching
        if t is not None and cfg is not None:
            p = Path(cfg["output_dir"]) / spotiflac_track_relative_path(t, cfg)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
        return []

    client.download_track = AsyncMock(side_effect=_download_track)
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
            client = _make_client(track_return=track, download_return=[], cfg=config)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.TRACK_URL, config, logger)

        assert result.to_dict() == {"ok": 1, "skipped": 0, "failed": 0, "failed_tracks": [], "gave_up_tracks": [], "providers": {}, "total": 1}
        client.download_track.assert_awaited_once_with(self.TRACK_URL)
        mock_cls.assert_called_once()
        assert mock_cls.call_args.kwargs["enrich_providers"] == [
            "apple", "deezer", "soundcloud",
        ]

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

        assert result.to_dict() == {"ok": 0, "skipped": 1, "failed": 0, "failed_tracks": [], "gave_up_tracks": [], "providers": {}, "total": 1}
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

        assert result.ok == 0
        assert result.failed == 1
        assert len(result.failed_tracks) == 1

    @pytest.mark.asyncio
    async def test_metadata_error_calls_failure_cb_with_two_args(self, config, logger):
        """Regression: downloader.py called failure_cb with 3 positional args
        ('', url, 'metadata_error') but the callback takes (title, err) — a
        transient Spotify 500/503 crashed the whole job with a TypeError
        instead of returning a retryable failure."""
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "track", "id": "abc123"}
            client = AsyncMock()
            client.get_track_metadata = AsyncMock(
                side_effect=RuntimeError("boom"),
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            calls = []
            result = await run_url(
                self.TRACK_URL, config, logger,
                failure_cb=lambda title, err: calls.append((title, err)),
            )

        assert result.failed == 1
        assert calls == [(self.TRACK_URL, "metadata_error")]

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

        assert result.to_dict() == {"ok": 0, "skipped": 2, "failed": 0, "failed_tracks": [], "gave_up_tracks": [], "providers": {}, "total": 2}
        client.download_track.assert_not_called()

    @pytest.mark.asyncio
    async def test_album_all_new(self, tmp_path, logger):
        t1 = _mock_track("t1", "Song A")
        t2 = _mock_track("t2", "Song B")
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
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(
                playlist_return=({"name": "Test Album", "type": "album"}, [t1, t2]),
                download_return=[],
                cfg=cfg,
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.ALBUM_URL, cfg, logger)

        assert result.to_dict() == {"ok": 2, "skipped": 0, "failed": 0, "failed_tracks": [], "gave_up_tracks": [], "providers": {}, "total": 2}
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
                cfg=cfg,
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.ALBUM_URL, cfg, logger)

        assert result.ok == 1
        assert result.skipped == 1
        assert result.failed == 0
        assert client.download_track.await_count == 1
        client.download_track.assert_any_call(t2.external_url)

    @pytest.mark.asyncio
    async def test_album_skips_given_up_titles(self, config, logger):
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
                cfg=config,
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.ALBUM_URL, config, logger, skip_titles={"Song B"})

        assert result.ok == 1
        assert result.skipped == 0
        assert result.failed == 0
        assert result.gave_up_tracks == [("t2", "Song B", "gave_up")]
        assert client.download_track.await_count == 1
        client.download_track.assert_any_call(t1.external_url)

    @pytest.mark.asyncio
    async def test_album_given_up_only(self, config, logger):
        t1 = _mock_track("t1", "Song A")
        t2 = _mock_track("t2", "Song B")
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(
                playlist_return=({"name": "Test Album", "type": "album"}, [t1, t2]),
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.ALBUM_URL, config, logger, skip_titles={"Song A", "Song B"})

        assert result.ok == 0
        assert result.skipped == 0
        assert result.failed == 0
        assert len(result.gave_up_tracks) == 2
        client.download_track.assert_not_called()

    @pytest.mark.asyncio
    async def test_album_partial_failure(self, config, logger):
        t1 = _mock_track("t1", "OK Song")
        t2 = _mock_track("t2", "Bad Song")
        failed = [TrackMetadata(id="t2", title="Bad Song", artists="Artist",
                                album="Album", album_artist="Artist")]
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(
                playlist_return=({"name": "Test Album", "type": "album"}, [t1, t2]),
                download_return=failed,
                cfg=config,
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.ALBUM_URL, config, logger)

        assert result.ok == 1
        assert result.failed == 1
        assert result.failed_tracks == [("t2", "Bad Song", "download_failed")]

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

        assert result.to_dict() == {"ok": 0, "skipped": 0, "failed": 1, "failed_tracks": [("abc123", "Test Track", "download_failed")], "gave_up_tracks": [], "providers": {}, "total": 1}

    @pytest.mark.asyncio
    async def test_failure_cb_called_for_failed_track(self, config, logger):
        t1 = _mock_track("t1", "OK Song")
        t2 = _mock_track("t2", "Bad Song")
        failed = [TrackMetadata(id="t2", title="Bad Song", artists="Artist",
                                album="Album", album_artist="Artist")]
        calls = []
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(
                playlist_return=({"name": "Test Album", "type": "album"}, [t1, t2]),
                download_return=failed,
                cfg=config,
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.ALBUM_URL, config, logger, failure_cb=lambda title, err: calls.append((title, err)))

        assert result.failed == 1
        assert result.failed_tracks == [("t2", "Bad Song", "download_failed")]
        assert calls == [("Bad Song", "download_failed")]

    @pytest.mark.asyncio
    async def test_failure_cb_tuple_shape_defensive(self, config, logger):
        """Older SpotiFLAC versions return (id, title, artists, error) tuples;
        that shape must still work (and carry the real error string)."""
        t1 = _mock_track("t1", "OK Song")
        t2 = _mock_track("t2", "Bad Song")
        failed = [("t2", "Bad Song", "Artist", "Qobuz 500")]
        calls = []
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(
                playlist_return=({"name": "Test Album", "type": "album"}, [t1, t2]),
                download_return=failed,
                cfg=config,
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.ALBUM_URL, config, logger, failure_cb=lambda title, err: calls.append((title, err)))

        assert result.failed == 1
        assert result.failed_tracks == [("t2", "Bad Song", "download_failed")]
        assert calls == [("Bad Song", "Qobuz 500")]

    @pytest.mark.asyncio
    async def test_failure_cb_called_on_exception(self, config, logger):
        track = _mock_track("abc123", "Test Track")
        calls = []
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "track", "id": "abc123"}
            client = _make_client(track_return=track)
            client.download_track = AsyncMock(side_effect=RuntimeError("Network error"))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.TRACK_URL, config, logger, failure_cb=lambda title, err: calls.append((title, err)))

        assert result.failed == 1
        assert len(calls) == 1
        assert calls[0][0] == "Test Track"

    @pytest.mark.asyncio
    async def test_failure_cb_not_called_on_success(self, config, logger):
        track = _mock_track("abc123", "Test Track")
        calls = []
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "track", "id": "abc123"}
            client = _make_client(track_return=track, download_return=[], cfg=config)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.TRACK_URL, config, logger, failure_cb=lambda title, err: calls.append((title, err)))

        assert result.ok == 1
        assert calls == []

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
                cfg=config,
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.ALBUM_URL, config, logger)

        assert result.ok == 2
        assert result.failed == 0
        assert client.download_track.await_count == 2
        client.download_track.assert_any_call(t1.external_url)
        client.download_track.assert_any_call(t2.external_url)

    @pytest.mark.asyncio
    async def test_success_but_no_file_reported_failed(self, config, logger):
        """Reconcile: a 'successful' download that leaves no file on disk is
        reported as failed (no silent ok), so the pre-check churn is visible."""
        track = _mock_track("abc123", "Test Track")
        calls = []
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "track", "id": "abc123"}
            client = _make_client(track_return=track)  # no cfg -> writes nothing
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.TRACK_URL, config, logger,
                                   failure_cb=lambda title, err: calls.append((title, err)))

        assert result.ok == 0
        assert result.failed == 1
        assert calls == [("Test Track", "no_file_after_download")]

    @pytest.mark.asyncio
    async def test_overlapping_parallel_jobs_download_each_track_once(self, tmp_path, logger):
        """Two jobs downloading the same track concurrently must produce one
        download: the second job skips the in-flight track, never double-writes."""
        from downloader import _download_tracks

        cfg = {
            "output_dir": str(tmp_path),
            "filename_format": "{artist} - {title}",
            "use_artist_subfolders": True,
            "use_album_subfolders": True,
            "first_artist_only": True,
            "embed_lyrics": True,
            "quality": "LOSSLESS",
            "services": ["qobuz"],
        }
        t = _mock_track("abc123", "Song A")
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def _slow_download(url):
            calls.append(url)
            started.set()
            await release.wait()
            p = Path(cfg["output_dir"]) / spotiflac_track_relative_path(t, cfg)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
            return []

        client = AsyncMock()
        client.get_track_metadata = AsyncMock(return_value=t)
        client.download_track = AsyncMock(side_effect=_slow_download)

        job1 = asyncio.create_task(_download_tracks(client, [t], cfg, logger))
        await started.wait()
        job2 = asyncio.create_task(_download_tracks(client, [t], cfg, logger))
        await asyncio.sleep(0.1)
        release.set()
        r1, r2 = await asyncio.gather(job1, job2)

        assert len(calls) == 1
        assert r1.ok + r1.skipped == 1 and r2.ok + r2.skipped == 1
        assert r1.failed == 0 and r2.failed == 0

    @pytest.mark.asyncio
    async def test_progress_cb_reports_per_track(self, config, logger):
        t1 = _mock_track("t1", "Song A")
        t2 = _mock_track("t2", "Song B")
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(
                playlist_return=({"name": "Test", "type": "album"}, [t1, t2]),
                download_return=[],
                cfg=config,
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            calls = []

            def progress_cb(done, total, title, provider=None):
                calls.append((done, total, title, provider))

            result = await run_url(self.ALBUM_URL, config, logger, progress_cb=progress_cb)

        assert result.ok == 2
        assert sorted(t for _, _, t, _ in calls) == ["Song A", "Song B"]
        assert all(total == 2 for _, total, _, _ in calls)
        # done-counts are 1 and 2; order is nondeterministic under concurrency
        assert sorted(d for d, _, _, _ in calls) == [1, 2]

    @pytest.mark.asyncio
    async def test_providers_reported_per_track(self, config, logger):
        """The provider that actually delivered each track is aggregated into
        the result and forwarded to progress_cb."""
        t1 = _mock_track("t1", "Song A")
        t2 = _mock_track("t2", "Song B")
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
            patch("downloader.pop_track_provider") as mock_pop,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(
                playlist_return=({"name": "Test", "type": "album"}, [t1, t2]),
                download_return=[],
                cfg=config,
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            mock_pop.side_effect = lambda track_id: {"t1": "deezer", "t2": "amazon"}.get(track_id)

            calls = []

            def progress_cb(done, total, title, provider=None):
                calls.append((title, provider))

            result = await run_url(self.ALBUM_URL, config, logger, progress_cb=progress_cb)

        assert result.ok == 2
        assert result.providers == {"deezer": 1, "amazon": 1}
        assert sorted(calls) == [("Song A", "deezer"), ("Song B", "amazon")]

    @pytest.mark.asyncio
    async def test_providers_empty_when_none_delivered(self, config, logger):
        """No provider recorded (e.g. everything already on disk) → empty dict,
        so the summary message stays unchanged."""
        t1 = _mock_track("t1", "Song A")
        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
        ):
            mock_parse.return_value = {"type": "album", "id": "alb123"}
            client = _make_client(
                playlist_return=({"name": "Test", "type": "album"}, [t1]),
                download_return=[],
                cfg=config,
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            # track already exists on disk → pre-check, no download
            rel = spotiflac_track_relative_path(t1, config)
            _touch(Path(config["output_dir"]) / rel)

            result = await run_url(self.ALBUM_URL, config, logger)

        assert result.skipped == 1
        assert result.providers == {}

    @pytest.mark.asyncio
    async def test_writes_lrc_sidecar_when_lyrics_embedded(self, tmp_path):
        """A track that already has line-synced LRC in its LYRICS tag gets a
        .lrc sidecar on download (Navidrome reads sidecar .lrc as synced)."""
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
        track = _mock_track("abc123", "Test Track")

        flac_path = tmp_path / "AlbumArtist" / "Album" / "Artist - Test Track.flac"
        lrc_path = flac_path.with_suffix(".lrc")

        async def _download(_url):
            # download_track creates the file on disk
            flac_path.parent.mkdir(parents=True, exist_ok=True)
            flac_path.write_bytes(b"")
            return []

        with (
            patch("SpotiFLAC.AsyncSpotiFLAC") as mock_cls,
            patch("SpotiFLAC.providers.spotify_metadata.parse_spotify_url") as mock_parse,
            patch("downloader.resolve_cover_data", new=AsyncMock(return_value=None)),
            patch("downloader.read_lrc", new=lambda p: "[01:23.45]hello\n[02:30.00]bye"),
        ):
            mock_parse.return_value = {"type": "track", "id": "abc123"}
            client = _make_client(track_return=track)
            client.download_track = AsyncMock(side_effect=_download)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock()

            result = await run_url(self.TRACK_URL, cfg, logger)

        assert result.ok == 1
        assert lrc_path.exists()
        assert "[01:23.45]hello" in lrc_path.read_text(encoding="utf-8")


class TestConsoleInterception:
    """SpotiFLAC's install_console_interception() pollutes the root logger:
    one TqdmLoggingHandler per track download, never removed.  The patch in
    spotiflac_patch.disable_progress_manager (triggered by importing
    downloader) must keep the root logger stable — regression for the 3x
    duplicate log lines seen in production."""

    def test_install_is_neutralized(self):
        import SpotiFLAC.core.progress as progress
        import SpotiFLAC.downloader as sf_downloader

        root = logging.getLogger()
        before = list(root.handlers)

        for _ in range(3):
            progress.install_console_interception()
            progress.uninstall_console_interception()
            sf_downloader.install_console_interception()
            sf_downloader.uninstall_console_interception()

        assert root.handlers == before

    def test_spoty_loop_still_logs_once_in_our_format(self):
        import SpotiFLAC.core.progress as progress
        import SpotiFLAC.downloader as sf_downloader

        for _ in range(3):
            progress.install_console_interception()
            sf_downloader.install_console_interception()

        logger = logging.getLogger("spoty_loop")
        logger.setLevel(logging.INFO)
        emitted = []

        class Capture(logging.Handler):
            def emit(self, record):
                emitted.append(self.format(record))

        cap = Capture()
        cap.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(cap)
        try:
            logger.info("Processing #42: test")
        finally:
            logger.removeHandler(cap)

        assert len(emitted) == 1
        assert emitted[0].endswith("[INFO] Processing #42: test")


class TestConsoleSilencing:
    """install_console_silencing's console no-ops must reach SpotiFLAC's module-level
    copies (`from .core.console import print_summary`), not just the console
    module attributes — otherwise the banners, timeout lines and the qobuz
    grant prompt leak into production logs (import-copy gotcha)."""

    def test_copied_names_in_downloader_are_noops(self, capsys):
        import SpotiFLAC.downloader as sf_downloader

        sf_downloader.print_summary(1, 1, [("t", "a", "err")], 0.5)
        sf_downloader.print_track_header(1, 2, "Song", "Artist", "Album")
        sf_downloader.safe_tqdm_write("  ⏱  Timeout reached for 'x' — skipping track.")
        sf_downloader.uninstall_console_interception()

        out, err = capsys.readouterr()
        assert out == "" and err == ""

    def test_provider_copies_are_noops(self, capsys):
        from SpotiFLAC.core import console
        from SpotiFLAC.providers import qobuz, tidal

        assert qobuz.print_api_failure is console.print_api_failure
        assert qobuz.print_source_banner is console.print_source_banner
        assert tidal.print_quality_fallback is console.print_quality_fallback
        qobuz.print_api_failure("qobuz", "", "bound to a different event loop")
        tidal.print_source_banner("tidal", "", "FLAC")

        out, err = capsys.readouterr()
        assert out == "" and err == ""

    def test_builtins_input_is_noop(self, capsys):
        import builtins

        prompt = "Incolla qui il grant (da DevTools ...): "
        assert builtins.input(prompt) == ""
        out, err = capsys.readouterr()
        assert out == "" and err == ""


class TestQobuzLockPatch:
    """QobuzProvider stores an asyncio.Lock bound to the first event loop;
    SpotiFLAC awaits it from fresh loops and dies with 'bound to a different
    event loop'.  _patch_qobuz_lock must replace it with a loop-agnostic lock."""

    def test_provider_uses_loop_agnostic_lock(self):
        from spotiflac_patch import _AsyncLockAdapter
        from SpotiFLAC.providers.qobuz import QobuzProvider

        provider = QobuzProvider()
        assert isinstance(provider._creds_lock, _AsyncLockAdapter)

    @pytest.mark.asyncio
    async def test_lock_works_across_two_event_loops(self):
        import threading
        from spotiflac_patch import _AsyncLockAdapter

        lock = _AsyncLockAdapter()

        async def _probe(l):
            async with l:
                pass

        results = []

        def _run_in_new_loop():
            asyncio.run(_probe(lock))
            results.append(True)

        await _probe(lock)

        threads = [threading.Thread(target=_run_in_new_loop) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results == [True, True]
        assert not any(t.is_alive() for t in threads)


class TestRenameAfterDownload:
    """_rename_after_download must only touch files the current download wrote:
    pre-existing files (naming drift) are never deleted or moved."""

    def _track(self):
        return _mock_track(
            "t1", "Song? <Nice>",
            album_artist="AC/DC", album="Greatest Hits: Vol 1", first_artist="M/A/R/R/S",
        )

    def _cfg(self, tmp_path):
        return {
            "output_dir": str(tmp_path),
            "filename_format": "{artist} - {title}",
            "use_artist_subfolders": True,
            "use_album_subfolders": True,
            "first_artist_only": True,
        }

    def _paths(self, tmp_path, t):
        cfg = self._cfg(tmp_path)
        return (
            Path(cfg["output_dir"]) / spotiflac_track_relative_path(t, cfg),
            Path(cfg["output_dir"]) / track_relative_path(t, cfg),
        )

    def test_fresh_duplicate_deleted_when_target_exists(self, tmp_path, logger):
        t = self._track()
        spoti, orig = self._paths(tmp_path, t)
        _write_flac(spoti, t.id)  # provably the same track (embedded Spotify ID)
        _write_flac(orig, t.id)   # canonical target proves the same track
        _rename_after_download(t, self._cfg(tmp_path), logger, started=time.time() - 1)
        assert orig.exists()
        assert not spoti.exists()

    def test_duplicate_with_mismatched_id_never_deleted(self, tmp_path, logger):
        """The unlink must never fire without proof the file is the same track:
        a different (or unknown) embedded ID keeps both files."""
        t = self._track()
        spoti, orig = self._paths(tmp_path, t)
        _write_flac(spoti, "other_track_id")
        _touch(orig)
        _rename_after_download(t, self._cfg(tmp_path), logger, started=time.time() - 1)
        assert spoti.exists()
        assert orig.exists()

    def test_duplicate_without_id_tag_never_deleted(self, tmp_path, logger):
        t = self._track()
        spoti, orig = self._paths(tmp_path, t)
        _touch(spoti)  # empty file: no tags, no proof
        _touch(orig)
        _rename_after_download(t, self._cfg(tmp_path), logger, started=time.time() - 1)
        assert spoti.exists()
        assert orig.exists()

    def test_duplicate_target_with_mismatched_id_never_deleted(self, tmp_path, logger):
        """The unlink needs BOTH files to prove the same track: a freshly
        written spoti file matching the ID must not delete a canonical file
        that embeds a different track ID."""
        t = self._track()
        spoti, orig = self._paths(tmp_path, t)
        _write_flac(spoti, t.id)
        _write_flac(orig, "other_track_id")
        _rename_after_download(t, self._cfg(tmp_path), logger, started=time.time() - 1)
        assert spoti.exists()
        assert orig.exists()

    def test_pre_existing_spoti_never_deleted(self, tmp_path, logger):
        t = self._track()
        spoti, orig = self._paths(tmp_path, t)
        _touch(spoti, mtime=time.time() - 3600)
        _touch(orig)
        _rename_after_download(t, self._cfg(tmp_path), logger, started=time.time())
        assert spoti.exists()
        assert orig.exists()

    def test_fresh_file_renamed_when_target_absent(self, tmp_path, logger):
        t = self._track()
        spoti, orig = self._paths(tmp_path, t)
        _touch(spoti)
        _rename_after_download(t, self._cfg(tmp_path), logger, started=time.time() - 1)
        assert orig.exists()
        assert not spoti.exists()

    def test_stale_file_left_alone_when_target_absent(self, tmp_path, logger):
        t = self._track()
        spoti, orig = self._paths(tmp_path, t)
        _touch(spoti, mtime=time.time() - 3600)
        _rename_after_download(t, self._cfg(tmp_path), logger, started=time.time())
        assert spoti.exists()
        assert not orig.exists()


class TestPartitionTracksNaming:
    """Pre-check counts a track as existing under either naming scheme, so
    tracks already on disk are never re-downloaded (or fed to the post-download
    hooks)."""

    def _track(self):
        return _mock_track(
            "t1", "Song? <Nice>",
            album_artist="AC/DC", album="Greatest Hits: Vol 1", first_artist="M/A/R/R/S",
        )

    def _cfg(self, tmp_path):
        return {
            "output_dir": str(tmp_path),
            "filename_format": "{artist} - {title}",
            "first_artist_only": True,
        }

    def test_sanitized_named_file_counts_as_existing(self, tmp_path):
        t = self._track()
        cfg = self._cfg(tmp_path)
        _touch(Path(cfg["output_dir"]) / spotiflac_track_relative_path(t, cfg))
        existing, given_up, missing = partition_tracks([t], cfg)
        assert existing == [t]
        assert missing == []

    def test_symbol_preserving_file_counts_as_existing(self, tmp_path):
        t = self._track()
        cfg = self._cfg(tmp_path)
        _touch(Path(cfg["output_dir"]) / track_relative_path(t, cfg))
        existing, given_up, missing = partition_tracks([t], cfg)
        assert existing == [t]
        assert missing == []

    def test_missing_when_absent(self, tmp_path):
        t = self._track()
        existing, given_up, missing = partition_tracks([t], self._cfg(tmp_path))
        assert existing == []
        assert missing == [t]
