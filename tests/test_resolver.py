"""Tests for resolver.py — input parsing and search resolution."""

from unittest.mock import MagicMock, AsyncMock

import pytest

from resolver import parse_input, resolve_search, format_help


class TestParseInput:
    def test_spotify_track_link(self):
        url = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
        assert parse_input(url) == ("link", url)

    def test_spotify_album_link(self):
        url = "https://open.spotify.com/album/1lNWxL4tFoELT0e1e7Kskl"
        assert parse_input(url) == ("link", url)

    def test_spotify_playlist_link(self):
        url = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
        assert parse_input(url) == ("link", url)

    def test_spotify_artist_link(self):
        url = "https://open.spotify.com/artist/4Z8W4fKeB5YxbusRsdQVPb"
        assert parse_input(url) == ("link", url)

    def test_link_with_query_params(self):
        url = "https://open.spotify.com/track/abc123?si=deadbeef"
        assert parse_input(url)[0] == "invalid"

    def test_search_artist_album(self):
        assert parse_input("Radiohead - OK Computer") == (
            "search", "Radiohead - OK Computer",
        )

    def test_search_artist_song(self):
        assert parse_input("Pink Floyd - Comfortably Numb") == (
            "search", "Pink Floyd - Comfortably Numb",
        )

    def test_search_album_song(self):
        assert parse_input("The Wall - Another Brick in the Wall") == (
            "search", "The Wall - Another Brick in the Wall",
        )

    def test_search_leading_trailing_spaces(self):
        assert parse_input("  Artist  -  Album  ") == (
            "search", "Artist  -  Album",
        )

    def test_invalid_no_dash(self):
        assert parse_input("just random text") == ("invalid", "just random text")

    def test_invalid_empty(self):
        assert parse_input("") == ("invalid", "")

    def test_invalid_whitespace_only(self):
        assert parse_input("   ") == ("invalid", "")


class TestResolveSearch:
    @pytest.fixture
    def mock_track(self):
        t = MagicMock()
        t.external_url = "https://open.spotify.com/track/abc"
        t.title = "Test Song"
        t.artists = "Test Artist"
        t.album = "Test Album"
        return t

    @pytest.fixture
    def mock_album(self):
        return {
            "external_url": "https://open.spotify.com/album/xyz",
            "name": "Test Album",
            "artists": "Test Artist",
        }

    @pytest.fixture
    def client(self):
        return AsyncMock()

    @pytest.mark.asyncio
    async def test_track_match(self, client, mock_track):
        client.search.return_value = {"tracks": [mock_track], "albums": []}
        url, display, kind = await resolve_search(client, "Test Artist - Test Song")
        assert url == "https://open.spotify.com/track/abc"
        assert display == "Test Song"
        assert kind == "track"

    @pytest.mark.asyncio
    async def test_album_match(self, client, mock_album):
        client.search.return_value = {"tracks": [], "albums": [mock_album]}
        url, display, kind = await resolve_search(client, "Test Artist - Test Album")
        assert url == "https://open.spotify.com/album/xyz"
        assert display == "Test Album"
        assert kind == "album"

    @pytest.mark.asyncio
    async def test_album_wins_when_both_hit_and_b_not_track_album(
        self, client, mock_track, mock_album,
    ):
        mock_track.artists = "Test Artist"
        mock_album["artists"] = "Test Artist"
        mock_track.album = "Some Other Album"
        client.search.return_value = {"tracks": [mock_track], "albums": [mock_album]}
        url, display, kind = await resolve_search(client, "Test Artist - Test Song")
        assert kind == "album"

    @pytest.mark.asyncio
    async def test_track_wins_when_b_matches_track_album_name(
        self, client, mock_track, mock_album,
    ):
        mock_track.album = "Test Album"
        mock_album["name"] = "Test Album"
        client.search.return_value = {"tracks": [mock_track], "albums": [mock_album]}
        url, display, kind = await resolve_search(
            client, "Test Artist - Test Album"
        )
        assert kind == "track"

    @pytest.mark.asyncio
    async def test_artist_hint_filters_tracks(self, client, mock_track):
        wrong = MagicMock()
        wrong.artists = "Wrong Artist"
        wrong.title = "Other Song"
        wrong.external_url = "https://open.spotify.com/track/wrong"
        wrong.album = "Some Album"

        client.search.return_value = {"tracks": [wrong, mock_track], "albums": []}
        url, display, kind = await resolve_search(client, "Test Artist - Test Song")
        assert url == mock_track.external_url
        assert kind == "track"

    @pytest.mark.asyncio
    async def test_artist_hint_filters_albums(self, client, mock_album):
        wrong = {"external_url": "https://open.spotify.com/album/wrong",
                 "name": "Some Album", "artists": "Wrong Artist"}
        client.search.return_value = {"tracks": [], "albums": [wrong, mock_album]}
        url, display, kind = await resolve_search(
            client, "Test Artist - Test Album"
        )
        assert url == mock_album["external_url"]
        assert kind == "album"

    @pytest.mark.asyncio
    async def test_no_results_raises(self, client):
        client.search.return_value = {"tracks": [], "albums": []}
        with pytest.raises(ValueError, match="No results found"):
            await resolve_search(client, "Unknown - Nothing")

    @pytest.mark.asyncio
    async def test_uses_artist_as_fallback_when_no_album_match(
        self, client, mock_track, mock_album,
    ):
        mock_album["name"] = "Unrelated Album"
        mock_album["artists"] = "Some Other Artist"
        client.search.return_value = {"tracks": [mock_track], "albums": [mock_album]}
        url, display, kind = await resolve_search(client, "Test Artist - Test Song")
        assert kind == "track"

    @pytest.mark.asyncio
    async def test_track_when_b_equals_track_album(self, client, mock_track, mock_album):
        mock_track.album = "Test Album"
        mock_album["name"] = "Different Album"
        mock_album["artists"] = "Test Artist"
        client.search.return_value = {"tracks": [mock_track], "albums": [mock_album]}
        url, display, kind = await resolve_search(
            client, "Test Artist - Test Album"
        )
        assert kind == "track"

    @pytest.mark.asyncio
    async def test_search_query_parts(self, client, mock_track):
        client.search.return_value = {"tracks": [mock_track], "albums": []}
        await resolve_search(client, "  Test Artist  -  Test Song  ")
        called_query = client.search.call_args[0][0]
        assert called_query == "Test Artist Test Song"


class TestFormatHelp:
    def test_contains_keywords(self):
        text = format_help()
        assert "Spotify" in text
        assert "Artist" in text
        assert "link" in text

    def test_no_rescan_command(self):
        assert "/rescan" not in format_help()

    def test_returns_non_empty_string(self):
        assert len(format_help()) > 50
