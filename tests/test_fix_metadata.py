"""Tests for fix_metadata.py — metadata re-tagging via the SpotiFLAC pipeline."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.fix_metadata import (
    _guess_metadata,
    _is_foreign,
    _majority,
    fix_album_folder,
    fix_library,
)


def _track(album="MADAME", title="MAREA", album_artist="Madame", artists="Madame", cover_url=""):
    from SpotiFLAC.core.models import TrackMetadata

    return TrackMetadata(
        id="trackid",
        title=title,
        artists=artists,
        album=album,
        album_artist=album_artist,
        track_number=2,
        total_tracks=18,
        cover_url=cover_url,
    )


class TestGuessMetadata:
    def test_artist_title_filename(self):
        assert _guess_metadata("Madame - BABY.flac") == ("Madame", "BABY")

    def test_no_dash_returns_none(self):
        assert _guess_metadata("track.flac") is None

    def test_empty_parts_returns_none(self):
        assert _guess_metadata("Madame - .flac") is None


class TestMajority:
    def test_picks_most_common(self):
        assert _majority(["MADAME", "MADAME", "OTHER"]) == "MADAME"

    def test_ignores_empties(self):
        assert _majority(["MADAME", "", ""]) == "MADAME"

    def test_all_empty(self):
        assert _majority(["", "", ""]) == ""


class TestIsForeign:
    def test_matches_folder_name(self):
        assert not _is_foreign(_track(album="MADAME"), Path("/x/MADAME"), "MADAME")

    def test_matches_artist_album_folder_name(self):
        folder = Path("/x/Madame - MADAME")
        assert not _is_foreign(_track(album="MADAME"), folder, "MADAME")

    def test_matches_majority_album(self):
        folder = Path("/x/Madame - MADAME")
        assert not _is_foreign(_track(album="MADAME"), folder, "MADAME")

    def test_different_album_is_foreign(self):
        folder = Path("/x/MADAME")
        assert _is_foreign(_track(album="LUNA"), folder, "MADAME")

    def test_different_folder_prefix_still_foreign(self):
        folder = Path("/x/SOME OTHER ALBUM")
        assert _is_foreign(_track(album="LUNA"), folder, "MADAME")


class TestFixAlbumFolder:
    def _make_folder(self, tmp_path: Path, names=None) -> Path:
        folder = tmp_path / "MADAME"
        folder.mkdir()
        for n in names or ["Madame - MAREA.flac", "Madame - CLITO.flac"]:
            (folder / n).write_bytes(b"fake")
        return folder

    def _patch_flac(self, urls=None):
        from contextlib import ExitStack, contextmanager

        urls = urls or {}

        def _fake_flac(path):
            audio = MagicMock()
            url = urls.get(Path(path).name)

            def get(tag, default=None):
                if tag in ("URL", "comment") and url:
                    return [url]
                return default

            audio.get.side_effect = get
            return audio

        @contextmanager
        def _stack():
            with ExitStack() as stack:
                stack.enter_context(patch("scripts.fix_metadata.FLAC", side_effect=_fake_flac))
                stack.enter_context(patch("flac_utils.FLAC", side_effect=_fake_flac))
                yield

        return _stack()

    @pytest.mark.asyncio
    async def test_url_track_retags_and_stays(self, tmp_path):
        folder = self._make_folder(tmp_path)
        mock_embed = AsyncMock()

        with self._patch_flac(urls={"Madame - MAREA.flac": "open.spotify.com/track/abc123", "Madame - CLITO.flac": "open.spotify.com/track/clito"}), patch(
            "SpotiFLAC.core.tagger.embed_metadata_async", mock_embed
        ), patch("SpotiFLAC.client.SpotifyMetadataClient") as client_cls:
            client = client_cls.return_value
            client.get_track_async = AsyncMock(return_value=_track(album="MADAME"))

            res = await fix_album_folder(folder, apply=True)

        assert res["fixed"] == 2
        assert res["moved"] == 0
        assert res["failed"] == 0
        assert mock_embed.await_count == 2
        _, kwargs = mock_embed.call_args
        opts = kwargs["opts"]
        assert opts.enrich is True
        assert opts.enrich_providers == ["apple", "deezer", "soundcloud"]
        assert opts.embed_lyrics is False  # default: no lyrics fetch (slow)
        # no file moved
        assert sorted(p.name for p in folder.iterdir()) == [
            "Madame - CLITO.flac", "Madame - MAREA.flac",
        ]

    @pytest.mark.asyncio
    async def test_lyrics_flag_embeds_lyrics(self, tmp_path):
        folder = self._make_folder(tmp_path)
        mock_embed = AsyncMock()

        with self._patch_flac(urls={"Madame - MAREA.flac": "open.spotify.com/track/abc123", "Madame - CLITO.flac": "open.spotify.com/track/clito"}), patch(
            "SpotiFLAC.core.tagger.embed_metadata_async", mock_embed
        ), patch("SpotiFLAC.client.SpotifyMetadataClient") as client_cls:
            client = client_cls.return_value
            client.get_track_async = AsyncMock(return_value=_track(album="MADAME"))

            res = await fix_album_folder(folder, apply=True, lyrics=True)

        assert res["fixed"] == 2
        _, kwargs = mock_embed.call_args
        assert kwargs["opts"].embed_lyrics is True

    @pytest.mark.asyncio
    async def test_existing_lyrics_preserved_when_not_fetching(self, tmp_path):
        folder = self._make_folder(tmp_path, names=["Madame - MAREA.flac"])
        files: dict = {}

        class FakeFLAC:
            def __init__(self, path):
                self._path = str(path)
                files.setdefault(self._path, {})
                self._tags = files[self._path]

            def get(self, tag, default=None):
                if tag in ("LYRICS", "UNSYNCEDLYRICS"):
                    val = self._tags.get(tag)
                    return [val] if val else default
                return default

            def __setitem__(self, key, value):
                self._tags[key] = value

            def save(self):
                pass

        target = folder / "Madame - MAREA.flac"
        files[str(target)] = {"LYRICS": "old lyric line 1"}

        async def _embed(filepath=None, metadata=None, opts=None, cover_data=None):
            # simulate SpotiFLAC wiping all tags then rewriting without lyrics
            files.get(str(filepath), {}).pop("LYRICS", None)

        with patch("scripts.fix_metadata.FLAC", side_effect=FakeFLAC), patch(
            "SpotiFLAC.core.tagger.embed_metadata_async", side_effect=_embed
        ), patch(
            "scripts.fix_metadata.resolve_cover_data", AsyncMock(return_value=None)
        ), patch("SpotiFLAC.client.SpotifyMetadataClient") as client_cls:
            client = client_cls.return_value
            client.search_tracks_async = AsyncMock(
                return_value=[_track(album="MADAME")]
            )

            res = await fix_album_folder(folder, apply=True)

        assert res["fixed"] == 1
        assert files[str(target)]["LYRICS"] == "old lyric line 1"

    @pytest.mark.asyncio
    async def test_tagless_file_uses_search(self, tmp_path):
        folder = self._make_folder(tmp_path)

        with self._patch_flac(), patch(
            "SpotiFLAC.core.tagger.embed_metadata_async", AsyncMock()
        ), patch("SpotiFLAC.client.SpotifyMetadataClient") as client_cls:
            client = client_cls.return_value
            client.search_tracks_async = AsyncMock(
                return_value=[_track(album="MADAME", title="Baby")]
            )

            res = await fix_album_folder(folder, apply=True)

        client.search_tracks_async.assert_called()
        assert res["fixed"] == 2
        assert res["skipped"] == 0

    @pytest.mark.asyncio
    async def test_foreign_file_moved_to_real_album_folder(self, tmp_path):
        folder = self._make_folder(
            tmp_path,
            names=["Madame - MAREA.flac", "Madame - LUNA.flac"],
        )
        urls = {
            "Madame - MAREA.flac": "open.spotify.com/track/marea",
            "Madame - LUNA.flac": "open.spotify.com/track/luna",
        }

        def _side_effect(sid):
            if sid == "luna":
                return _track(album="LUNA", title="LUNA (feat. Gaia)",
                              album_artist="Levante & Gaia")
            return _track(album="MADAME")

        with self._patch_flac(urls=urls), patch(
            "SpotiFLAC.core.tagger.embed_metadata_async", AsyncMock()
        ), patch("SpotiFLAC.client.SpotifyMetadataClient") as client_cls:
            client = client_cls.return_value
            client.get_track_async = AsyncMock(side_effect=_side_effect)

            res = await fix_album_folder(folder, apply=True)

        assert res["moved"] == 1
        luna_dir = tmp_path / "LUNA"
        assert luna_dir.is_dir()
        assert list(luna_dir.iterdir())[0].name.startswith("Madame - LUNA")
        remaining = [p.name for p in folder.iterdir()]
        assert not any(n.startswith("Madame - LUNA") for n in remaining)
        assert len(res["moved_files"]) == 1

    @pytest.mark.asyncio
    async def test_embeds_apple_hd_cover(self, tmp_path):
        """The embedded cover comes from resolve_cover_data (Apple HD,
        else upgraded Spotify) — not the raw Spotify 300x300 URL."""
        folder = self._make_folder(tmp_path, names=["Madame - MAREA.flac"])
        mock_embed = AsyncMock()

        with self._patch_flac(urls={"Madame - MAREA.flac": "open.spotify.com/track/abc123"}), patch(
            "SpotiFLAC.core.tagger.embed_metadata_async", mock_embed
        ), patch("SpotiFLAC.client.SpotifyMetadataClient") as client_cls, patch(
            "scripts.fix_metadata.resolve_cover_data", AsyncMock(return_value=b"COVERBYTES")
        ) as resolve:
            client = client_cls.return_value
            client.get_track_async = AsyncMock(return_value=_track(
                album="MADAME",
                cover_url="https://i.scdn.co/image/ab67616d00001e02abcdef",
            ))

            res = await fix_album_folder(folder, apply=True)

        assert res["fixed"] == 1
        resolve.assert_awaited_once()
        assert mock_embed.call_args.kwargs["cover_data"] == b"COVERBYTES"

    @pytest.mark.asyncio
    async def test_no_cover_skips_fetch_and_retags(self, tmp_path):
        """When resolve_cover_data yields nothing (no Apple match, no Spotify
        cover_url) the track still retags without any cover data."""
        folder = self._make_folder(tmp_path, names=["Madame - MAREA.flac"])
        mock_embed = AsyncMock()

        with self._patch_flac(urls={"Madame - MAREA.flac": "open.spotify.com/track/abc123"}), patch(
            "SpotiFLAC.core.tagger.embed_metadata_async", mock_embed
        ), patch("SpotiFLAC.client.SpotifyMetadataClient") as client_cls, patch(
            "scripts.fix_metadata.resolve_cover_data", AsyncMock(return_value=None)
        ) as resolve:
            client = client_cls.return_value
            client.get_track_async = AsyncMock(return_value=_track(album="MADAME"))

            res = await fix_album_folder(folder, apply=True)

        assert res["fixed"] == 1
        resolve.assert_awaited_once()
        assert mock_embed.call_args.kwargs["cover_data"] is None

    @pytest.mark.asyncio
    async def test_dry_run_does_not_write_or_move(self, tmp_path):
        folder = self._make_folder(tmp_path)
        mock_embed = AsyncMock()

        with self._patch_flac(urls={"Madame - MAREA.flac": "open.spotify.com/track/abc123", "Madame - CLITO.flac": "open.spotify.com/track/clito"}), patch(
            "SpotiFLAC.core.tagger.embed_metadata_async", mock_embed
        ), patch("SpotiFLAC.client.SpotifyMetadataClient") as client_cls:
            client = client_cls.return_value
            client.get_track_async = AsyncMock(return_value=_track(album="MADAME"))

            res = await fix_album_folder(folder, apply=False)

        assert res["fixed"] == 0
        assert res["would_fix"] == 2
        assert res["moved"] == 0
        mock_embed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_failure_does_not_abort_run(self, tmp_path):
        folder = self._make_folder(tmp_path)
        urls = {
            "Madame - MAREA.flac": "open.spotify.com/track/marea",
            "Madame - CLITO.flac": "open.spotify.com/track/clito",
        }

        def _side_effect(sid):
            if sid == "clito":
                raise RuntimeError("network down")
            return _track(album="MADAME")

        with self._patch_flac(urls=urls), patch(
            "SpotiFLAC.core.tagger.embed_metadata_async", AsyncMock()
        ), patch("SpotiFLAC.client.SpotifyMetadataClient") as client_cls:
            client = client_cls.return_value
            client.get_track_async = AsyncMock(side_effect=_side_effect)

            res = await fix_album_folder(folder, apply=True)

        assert res["fixed"] == 1
        assert res["failed"] == 1
        assert "Madame - CLITO.flac" in res["failed_files"]


class TestFixLibrary:
    @pytest.mark.asyncio
    async def test_walks_album_folders(self, tmp_path):
        lib = tmp_path / "Albums"
        (lib / "MADAME").mkdir(parents=True)
        (lib / "MADAME" / "a.flac").write_bytes(b"fake")
        (lib / "MADAME" / "b.flac").write_bytes(b"fake")
        (lib / "DISINCANTO").mkdir()
        (lib / "DISINCANTO" / "c.flac").write_bytes(b"fake")
        # non-music dirs are ignored
        (lib / "covers").mkdir()

        def _fake_flac(path):
            audio = MagicMock()
            name = Path(path).name
            sid = "disincanto" if name == "c.flac" else "madame"

            def get(tag, default=None):
                if tag in ("URL", "comment"):
                    return [f"open.spotify.com/track/{sid}"]
                return [""]

            audio.get.side_effect = get
            return audio

        def _side_effect(sid):
            album = "DISINCANTO" if sid == "disincanto" else "MADAME"
            return _track(album=album)

        with patch("scripts.fix_metadata.FLAC", side_effect=_fake_flac), patch(
            "flac_utils.FLAC", side_effect=_fake_flac
        ), patch(
            "SpotiFLAC.core.tagger.embed_metadata_async", AsyncMock()
        ), patch("SpotiFLAC.client.SpotifyMetadataClient") as client_cls:
            client = client_cls.return_value
            client.get_track_async = AsyncMock(side_effect=_side_effect)

            res = await fix_library(lib, apply=True)

        assert res["folders"] == 2
        assert res["total"] == 3
        assert res["fixed"] == 3
        assert res["moved"] == 0
