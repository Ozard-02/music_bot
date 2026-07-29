"""Tests for m3u8.py — M3U8 playlist generation."""

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from m3u8 import (
    build_m3u8_lines,
    load_config,
    write_m3u8,
)
from track_utils import (
    sanitize,
    spotiflac_sanitize,
    spotiflac_track_relative_path,
    track_relative_path,
)


def make_track(
    *,
    track_id="abc",
    title="Test Track",
    artists="Test Artist",
    first_artist="Test Artist",
    album="Test Album",
    album_artist="Test Artist",
    duration_seconds=180.0,
):
    t = MagicMock()
    t.id = track_id
    t.title = title
    t.artists = artists
    t.first_artist = first_artist
    t.album = album
    t.album_artist = album_artist
    t.duration_seconds = duration_seconds
    return t


class TestSanitize:
    def test_replaces_slash_only(self):
        assert sanitize('My:Playlist/1', fallback='playlist') == 'My:Playlist\u22151'

    def test_strips_whitespace(self):
        assert sanitize('  Test  ', fallback='playlist') == 'Test'

    def test_empty_fallback(self):
        assert sanitize('', fallback='playlist') == 'playlist'
        assert sanitize('   ', fallback='playlist') == 'playlist'

    def test_keeps_valid(self):
        assert sanitize('Summer Vibes 2024', fallback='playlist') == 'Summer Vibes 2024'

    def test_preserves_special_chars(self):
        assert sanitize('Song? <Nice>', fallback='unknown') == 'Song? <Nice>'


class TestSpotiflacSanitize:
    def test_replaces_special_chars(self):
        assert spotiflac_sanitize('My:Playlist/1', fallback='playlist') == 'My_Playlist_1'

    def test_strips_whitespace(self):
        assert spotiflac_sanitize('  Test  ', fallback='playlist') == 'Test'

    def test_empty_fallback(self):
        assert spotiflac_sanitize('', fallback='playlist') == 'playlist'
        assert spotiflac_sanitize('   ', fallback='playlist') == 'playlist'

    def test_keeps_valid(self):
        assert spotiflac_sanitize('Summer Vibes 2024', fallback='playlist') == 'Summer Vibes 2024'


class TestTrackRelativePath:
    def test_basic(self):
        cfg = {"first_artist_only": True, "filename_format": "{artist} - {title}"}
        t = make_track()
        assert track_relative_path(t, cfg) == "Test Artist/Test Album/Test Artist - Test Track.flac"

    def test_first_artist_only_off(self):
        cfg = {"first_artist_only": False, "filename_format": "{artist} - {title}"}
        t = make_track(artists="Artist A, Artist B", first_artist="Artist A")
        assert track_relative_path(t, cfg) == "Test Artist/Test Album/Artist A, Artist B - Test Track.flac"

    def test_custom_filename_template(self):
        cfg = {"first_artist_only": True, "filename_format": "{title} - {artist}"}
        t = make_track()
        assert track_relative_path(t, cfg) == "Test Artist/Test Album/Test Track - Test Artist.flac"

    def test_different_album_artist(self):
        cfg = {"first_artist_only": True, "filename_format": "{artist} - {title}"}
        t = make_track(album_artist="Various Artists")
        assert track_relative_path(t, cfg) == "Various Artists/Test Album/Test Artist - Test Track.flac"

    def test_sanitizes_unsafe_chars(self):
        cfg = {"first_artist_only": True, "filename_format": "{artist} - {title}"}
        t = make_track(
            album_artist="AC/DC",
            album="Greatest Hits: Vol 1",
            first_artist="M/A/R/R/S",
            title="Song? <Nice>",
        )
        assert track_relative_path(t, cfg) == "AC\u2215DC/Greatest Hits: Vol 1/M\u2215A\u2215R\u2215R\u2215S - Song? <Nice>.flac"

    def test_collapses_whitespace(self):
        cfg = {"first_artist_only": True, "filename_format": "{artist} - {title}"}
        t = make_track(
            album_artist="  The   Artist  ",
            album="  My   Album  ",
            first_artist="  Test  ",
            title="  Hello   World  ",
        )
        assert track_relative_path(t, cfg) == "The Artist/My Album/Test - Hello World.flac"


class TestSpotiflacTrackRelativePath:
    def test_basic(self):
        cfg = {"first_artist_only": True, "filename_format": "{artist} - {title}"}
        t = make_track()
        assert spotiflac_track_relative_path(t, cfg) == "Test Artist/Test Album/Test Artist - Test Track.flac"

    def test_first_artist_only_off(self):
        cfg = {"first_artist_only": False, "filename_format": "{artist} - {title}"}
        t = make_track(artists="Artist A, Artist B", first_artist="Artist A")
        assert spotiflac_track_relative_path(t, cfg) == "Test Artist/Test Album/Artist A, Artist B - Test Track.flac"

    def test_custom_filename_template(self):
        cfg = {"first_artist_only": True, "filename_format": "{title} - {artist}"}
        t = make_track()
        assert spotiflac_track_relative_path(t, cfg) == "Test Artist/Test Album/Test Track - Test Artist.flac"

    def test_different_album_artist(self):
        cfg = {"first_artist_only": True, "filename_format": "{artist} - {title}"}
        t = make_track(album_artist="Various Artists")
        assert spotiflac_track_relative_path(t, cfg) == "Various Artists/Test Album/Test Artist - Test Track.flac"

    def test_replaces_unsafe_chars(self):
        cfg = {"first_artist_only": True, "filename_format": "{artist} - {title}"}
        t = make_track(
            album_artist="AC/DC",
            album="Greatest Hits: Vol 1",
            first_artist="M/A/R/R/S",
            title="Song? <Nice>",
        )
        assert spotiflac_track_relative_path(t, cfg) == "AC_DC/Greatest Hits_ Vol 1/M_A_R_R_S - Song_ _Nice_.flac"

    def test_collapses_whitespace(self):
        cfg = {"first_artist_only": True, "filename_format": "{artist} - {title}"}
        t = make_track(
            album_artist="  The   Artist  ",
            album="  My   Album  ",
            first_artist="  Test  ",
            title="  Hello   World  ",
        )
        assert spotiflac_track_relative_path(t, cfg) == "The Artist/My Album/Test - Hello World.flac"


class TestBuildM3u8Lines:
    @patch("m3u8.Path.exists", return_value=True)
    def test_all_tracks_exist(self, mock_exists):
        cfg = {"output_dir": "/music", "first_artist_only": True, "filename_format": "{artist} - {title}"}
        tracks = [
            make_track(track_id="1", title="Song A", first_artist="Artist A", album="Album", album_artist="Artist A", duration_seconds=200),
            make_track(track_id="2", title="Song B", first_artist="Artist B", album="Album", album_artist="Artist B", duration_seconds=150),
        ]
        lines, count, missing = build_m3u8_lines(tracks, cfg)
        assert count == 2
        assert len(missing) == 0
        assert lines[0] == "#EXTM3U"
        assert "#EXTINF:200,Artist A - Song A" in lines
        assert "Artist A/Album/Artist A - Song A.flac" in lines
        assert "#EXTINF:150,Artist B - Song B" in lines
        assert "Artist B/Album/Artist B - Song B.flac" in lines

    @patch("m3u8.Path.exists", return_value=True)
    def test_dedup_by_track_id(self, mock_exists):
        cfg = {"output_dir": "/music", "first_artist_only": True, "filename_format": "{artist} - {title}"}
        tracks = [
            make_track(track_id="1", title="Song A", first_artist="Artist A", album="Album", album_artist="Artist A"),
            make_track(track_id="1", title="Song A", first_artist="Artist A", album="Album", album_artist="Artist A"),
            make_track(track_id="2", title="Song B", first_artist="Artist B", album="Album", album_artist="Artist B"),
        ]
        lines, count, missing = build_m3u8_lines(tracks, cfg)
        assert count == 2
        assert len(missing) == 0
        assert lines.count("Artist A/Album/Artist A - Song A.flac") == 1

    def test_some_tracks_exist(self, tmp_path):
        music = tmp_path / "music"
        cfg = {"output_dir": str(music), "first_artist_only": True, "filename_format": "{artist} - {title}"}
        tracks = [
            make_track(track_id="1", title="Song A", first_artist="Artist A", album="Album X", album_artist="Artist A"),
            make_track(track_id="2", title="Song B", first_artist="Artist B", album="Album Y", album_artist="Artist B"),
        ]
        # Only create Song A on disk
        track_a = music / "Artist A" / "Album X" / "Artist A - Song A.flac"
        track_a.parent.mkdir(parents=True)
        track_a.touch()

        lines, count, missing = build_m3u8_lines(tracks, cfg)
        assert count == 1
        assert len(missing) == 1
        assert missing[0][:2] == ("Artist B", "Song B")
        assert "Artist A/Album X/Artist A - Song A.flac" in lines
        assert "Artist B/Album Y/Artist B - Song B.flac" not in lines

    @patch("m3u8.Path.exists", return_value=False)
    def test_no_tracks_exist(self, mock_exists):
        cfg = {"output_dir": "/music", "first_artist_only": True, "filename_format": "{artist} - {title}"}
        tracks = [make_track(), make_track(track_id="2")]
        lines, count, missing = build_m3u8_lines(tracks, cfg)
        assert count == 0
        assert len(missing) == 2
        assert lines == ["#EXTM3U"]

    @patch("m3u8.Path.exists", return_value=True)
    def test_empty_track_list(self, mock_exists):
        cfg = {"output_dir": "/music", "first_artist_only": True, "filename_format": "{artist} - {title}"}
        lines, count, missing = build_m3u8_lines([], cfg)
        assert count == 0
        assert len(missing) == 0
        assert lines == ["#EXTM3U"]


class TestWriteM3u8:
    def test_writes_file(self, tmp_path):
        cfg = {"output_dir": str(tmp_path)}
        path = write_m3u8("My Playlist", ["#EXTM3U", "file.flac"], cfg)
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert content == "#EXTM3U\nfile.flac\n"

    def test_sanitizes_name(self, tmp_path):
        cfg = {"output_dir": str(tmp_path)}
        path = write_m3u8("Bad:Name/Test", ["#EXTM3U"], cfg)
        assert "Bad:Name\u2215Test.m3u8" in path.name

    def test_creates_parent_dirs(self, tmp_path):
        nested = tmp_path / "sub"
        cfg = {"output_dir": str(nested)}
        path = write_m3u8("test", ["#EXTM3U"], cfg)
        assert path.exists()


class TestLoadConfig:
    def test_returns_defaults_when_no_config(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            cfg = load_config(logging.getLogger("test"))
        assert cfg["output_dir"] == str(Path.home() / "Music")
        assert cfg["first_artist_only"] is True
        assert cfg["filename_format"] == "{artist} - {title}"

    def test_reads_config(self):
        data = json.dumps({
            "downloadPath": "/custom/music",
            "useFirstArtistOnly": False,
            "filenameTemplate": "{title} - {artist}",
            "folderTemplate": "{album_artist}/{album}",
        })
        with patch("builtins.open", mock_open(read_data=data)):
            cfg = load_config(logging.getLogger("test"))
        assert cfg["output_dir"] == "/custom/music"
        assert cfg["first_artist_only"] is False
        assert cfg["filename_format"] == "{title} - {artist}"

    def test_handles_bad_json(self):
        with patch("builtins.open", mock_open(read_data="not json")):
            cfg = load_config(logging.getLogger("test"))
        assert cfg["output_dir"] == str(Path.home() / "Music")


class TestBuildM3u8Async:
    @pytest.mark.asyncio
    async def test_not_a_playlist(self):
        with patch("m3u8.parse_spotify_url", return_value={"type": "track", "id": "abc"}):
            with pytest.raises(ValueError, match="Not a playlist URL"):
                from m3u8 import build_m3u8
                await build_m3u8("https://open.spotify.com/track/abc")

    @pytest.mark.asyncio
    async def test_happy_path(self, tmp_path):
        track = make_track(title="Song", first_artist="Artist", album="Album", album_artist="Artist", duration_seconds=180)
        # Create the file on disk so it's found
        music = tmp_path / "music"
        track_path = music / "Artist" / "Album" / "Artist - Song.flac"
        track_path.parent.mkdir(parents=True)
        track_path.touch()

        cfg = {"output_dir": str(music), "first_artist_only": True, "filename_format": "{artist} - {title}"}

        with (
            patch("m3u8.parse_spotify_url", return_value={"type": "playlist", "id": "pl"}),
            patch("m3u8.SpotifyMetadataClient") as mock_client_cls,
        ):
            mc = AsyncMock()
            mc.get_url_async.return_value = ("Test Playlist", [track], None, {"name": "Test Playlist"})
            mock_client_cls.return_value = mc

            from m3u8 import build_m3u8
            result = await build_m3u8("https://open.spotify.com/playlist/pl", cfg=cfg)

        assert result["playlist_name"] == "Test Playlist"
        assert result["total_tracks"] == 1
        assert result["exist_on_disk"] == 1
        assert result["missing_count"] == 0
        assert result["missing_log_path"] is None
        assert result["cover_path"] is None
        assert Path(result["path"]).exists()
        content = Path(result["path"]).read_text(encoding="utf-8")
        assert "#EXTM3U" in content
        assert "Artist/Album/Artist - Song.flac" in content

    @pytest.mark.asyncio
    async def test_uses_custom_name(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        cfg = {"output_dir": str(music), "first_artist_only": True, "filename_format": "{artist} - {title}"}

        with (
            patch("m3u8.parse_spotify_url", return_value={"type": "playlist", "id": "pl"}),
            patch("m3u8.SpotifyMetadataClient") as mock_client_cls,
        ):
            mc = AsyncMock()
            mc.get_url_async.return_value = ("Spotify Name", [], None, {"name": "Spotify Name"})
            mock_client_cls.return_value = mc

            from m3u8 import build_m3u8
            result = await build_m3u8("url", name="My Custom Name", cfg=cfg)

        assert result["playlist_name"] == "My Custom Name"
        assert "My Custom Name.m3u8" in result["path"]
