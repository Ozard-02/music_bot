"""Tests for resolver.py — input parsing and search resolution."""

from unittest.mock import MagicMock, AsyncMock

import pytest

from resolver import (
    parse_input,
    parse_inputs,
    resolve_search,
    split_search,
    format_help,
)


class TestSplitSearch:
    def test_hyphen_spaced(self):
        assert split_search("Pink Floyd - Comfortably Numb") == (
            "Pink Floyd", "Comfortably Numb",
        )

    def test_en_em_dash_no_spaces(self):
        assert split_search("Beyoncé –Halo") == ("Beyoncé", "Halo")
        assert split_search("Beyoncé— Halo") == ("Beyoncé", "Halo")

    def test_internal_hyphen_kept_in_left(self):
        assert split_search("AC-DC - Thunderstruck") == ("AC-DC", "Thunderstruck")

    def test_hyphen_glued_both_sides_no_split(self):
        assert split_search("AC-DC") is None
        assert split_search("Me-I") is None

    def test_no_dash(self):
        assert split_search("just random text") is None

    def test_empty_half(self):
        assert split_search(" - Halo") is None

    def test_compat_with_legacy_stored_value(self):
        """Queue rows created long ago still contain plain ' - ' sentences."""
        assert split_search("Jovanotti - pieno di vita") == ("Jovanotti", "pieno di vita")


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

    def test_localized_web_link(self):
        """Web-player shares carry an intl-<lang> segment."""
        url = "https://open.spotify.com/intl-it/track/4cOdK2wGLETKBW3PvgPWqT?si=x"
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
        # search values are normalized to "artist - title" form
        assert parse_input("  Artist  -  Album  ") == ("search", "Artist - Album")

    def test_invalid_no_dash(self):
        assert parse_input("just random text") == ("invalid", "just random text")

    def test_invalid_empty(self):
        assert parse_input("") == ("invalid", "")

    def test_invalid_whitespace_only(self):
        assert parse_input("   ") == ("invalid", "")

    def test_search_en_dash(self):
        assert parse_input("Beyoncé – Halo") == ("search", "Beyoncé - Halo")

    def test_search_em_dash(self):
        assert parse_input("Beyoncé — Halo") == ("search", "Beyoncé - Halo")

    def test_search_dash_space_after_only(self):
        assert parse_input("Artist -Song") == ("search", "Artist - Song")

    def test_artist_with_internal_hyphen(self):
        """Dash glued to words on both sides never separates."""
        assert parse_input("AC-DC - Thunderstruck") == (
            "search", "AC-DC - Thunderstruck",
        )

    def test_em_dash_always_separates_even_inside_words(self):
        assert parse_input("AC–DC Thunderstruck") == ("search", "AC - DC Thunderstruck")

    def test_multidash_splits_at_first(self):
        assert parse_input("Eminem - Lose Yourself - From the Movie") == (
            "search",
            "Eminem - Lose Yourself - From the Movie",
        )

    def test_values_dedupe_across_dash_kinds(self):
        assert parse_input("Artist – Song") == parse_input("Artist - Song")


class TestSpotifyUri:
    def test_uri_scheme(self):
        value = "spotify:track:4cOdK2wGLETKBW3PvgPWqT"
        assert parse_input(value) == ("link", value)

    def test_uri_album(self):
        value = "spotify:album:1lNWxL4tFoELT0e1e7Kskl"
        assert parse_input(value)[0] == "link"

    def test_uri_bad_id_rejected(self):
        assert parse_input("spotify:track:shortid") == ("invalid", "spotify:track:shortid")

    def test_play_host(self):
        url = "https://play.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=x"
        assert parse_input(url) == ("link", url)

    def test_link_inside_prose(self):
        url = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=x"
        assert parse_input(f"check this out: {url} enjoy") == ("link", url)

    def test_link_trailing_punctuation_stripped(self):
        url = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=x"
        text, value = parse_input(f"so good {url},")
        assert value == url

    def test_link_and_text_line_still_one_item(self):
        items = parse_inputs("mamma https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT prova")
        assert items == [("link", "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")]


class TestParseInputs:
    def test_single_line_same_as_parse_input(self):
        assert parse_inputs("Radiohead - Creep") == [("search", "Radiohead - Creep")]

    def test_multiple_lines(self):
        items = parse_inputs("Radiohead - Creep\nPink Floyd - Wish You Were Here")
        assert items == [
            ("search", "Radiohead - Creep"),
            ("search", "Pink Floyd - Wish You Were Here"),
        ]

    def test_blank_lines_skipped(self):
        items = parse_inputs("\n\nArtist - Song\n\n")
        assert items == [("search", "Artist - Song")]

    def test_mixed_lines(self):
        url = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
        items = parse_inputs(f"{url}\nArtist - Song\nnonsense without dash")
        assert items == [
            ("link", url),
            ("search", "Artist - Song"),
            ("invalid", "nonsense without dash"),
        ]

    def test_links_on_separate_lines_each_queued(self):
        a = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
        b = "https://open.spotify.com/album/1lNWxL4tFoELT0e1e7Kskl"
        items = parse_inputs(f"{a}\n{b}")
        assert items == [("link", a), ("link", b)]

    def test_empty(self):
        assert parse_inputs("") == []
        assert parse_inputs("   \n \n") == []


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
    async def test_artist_song_query_prefers_track(
        self, client, mock_track, mock_album,
    ):
        mock_track.artists = "Test Artist"
        mock_album["artists"] = "Test Artist"
        mock_track.album = "Some Other Album"
        client.search.return_value = {"tracks": [mock_track], "albums": [mock_album]}
        url, display, kind = await resolve_search(client, "Test Artist - Test Song")
        assert kind == "track"
        assert url == mock_track.external_url

    @pytest.mark.asyncio
    async def test_album_name_query_prefers_album(
        self, client, mock_track, mock_album,
    ):
        mock_track.album = "Test Album"
        mock_album["name"] = "Test Album"
        client.search.return_value = {"tracks": [mock_track], "albums": [mock_album]}
        url, display, kind = await resolve_search(
            client, "Test Artist - Test Album"
        )
        assert kind == "album"
        assert url == mock_album["external_url"]

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
    async def test_album_name_hint_picks_correct_album(self, client, mock_track):
        wrong = {"external_url": "https://open.spotify.com/album/wrong",
                 "name": "Some Album", "artists": "Test Artist"}
        right = {"external_url": "https://open.spotify.com/album/right",
                 "name": "Test Album", "artists": "Test Artist"}
        client.search.return_value = {"tracks": [mock_track], "albums": [wrong, right]}
        url, display, kind = await resolve_search(client, "Test Artist - Test Album")
        assert kind == "album"
        assert url == right["external_url"]
        assert display == "Test Album"

    @pytest.mark.asyncio
    async def test_accentless_query_matches(self, client):
        wrong = MagicMock()
        wrong.artists = "Beyoncé"
        wrong.title = "Halo"
        wrong.external_url = "https://open.spotify.com/track/wrong"
        wrong.album = "X"
        right = MagicMock()
        right.artists = "Beyoncé"
        right.title = "Café"
        right.external_url = "https://open.spotify.com/track/right"
        right.album = "Y"
        client.search.return_value = {"tracks": [wrong, right], "albums": []}
        url, display, kind = await resolve_search(client, "Beyonce - Cafe")
        assert kind == "track"
        assert url == right.external_url

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
    async def test_title_hint_picks_same_artist_track(self, client):
        wrong = MagicMock()
        wrong.artists = "Articolo 31"
        wrong.title = "CHI SE NE FREGA DI NOI"
        wrong.external_url = "https://open.spotify.com/track/wrong"
        wrong.album = "PROTOMARANZA"
        right = MagicMock()
        right.artists = "Articolo 31"
        right.title = "Noi no"
        right.external_url = "https://open.spotify.com/track/right"
        right.album = "Domani smetto"

        client.search.return_value = {"tracks": [wrong, right], "albums": []}
        url, display, kind = await resolve_search(client, "Articolo 31 - Noi no")
        assert url == right.external_url
        assert display == "Noi no"
        assert kind == "track"

    @pytest.mark.asyncio
    async def test_search_query_parts(self, client, mock_track):
        client.search.return_value = {"tracks": [mock_track], "albums": []}
        await resolve_search(client, "  Test Artist  -  Test Song  ")
        called_query = client.search.call_args[0][0]
        assert called_query == "Test Artist Test Song"

    @pytest.mark.asyncio
    async def test_resolves_em_dash_query(self, client, mock_track):
        """parse_input normalizes to ' - ', but stored em-dash rows too."""
        client.search.return_value = {"tracks": [mock_track], "albums": []}
        url, display, kind = await resolve_search(client, "Test Artist – Test Song")
        assert kind == "track"
        assert display == "Test Song"

    @pytest.mark.asyncio
    async def test_no_separator_searches_by_title(self, client, mock_track):
        """No dash (legacy/malformed row): whole text used as title hint."""
        mock_track.title = "Some Song"
        client.search.return_value = {"tracks": [mock_track], "albums": []}
        url, display, kind = await resolve_search(client, "Some Song")
        assert kind == "track"

    @pytest.mark.asyncio
    async def test_unmatched_album_not_downloaded(self, client, mock_track):
        """Album exists but matches nothing and no track matched → error,
        never a random albums[0] download."""
        client.search.return_value = {
            "tracks": [],
            "albums": [{"external_url": "https://open.spotify.com/album/x",
                        "name": "Random Album", "artists": "Random Artist"}],
        }
        with pytest.raises(ValueError, match="No results found"):
            await resolve_search(client, "Test Artist - Test Song")

    @pytest.mark.asyncio
    async def test_album_name_partial_match_picks_album(
        self, client, mock_track, mock_album,
    ):
        """'Artist - Album' with the deluxe edition returned → the album."""
        mock_album["name"] = "Test Album (Deluxe Edition)"
        mock_track.artists = "Test Artist"
        mock_track.album = "Test Album"
        client.search.return_value = {"tracks": [mock_track], "albums": [mock_album]}
        url, display, kind = await resolve_search(
            client, "Test Artist - Test Album"
        )
        assert kind == "album"
        assert display == "Test Album (Deluxe Edition)"


class TestLenientMatching:
    """Artist lists are written many ways: /, &, 'feat.', reversed halves."""

    @pytest.fixture
    def client(self):
        return AsyncMock()

    @pytest.mark.asyncio
    async def test_ampersand_artists(self, client):
        right = MagicMock()
        right.artists = "Simon & Garfunkel"
        right.title = "The Boxer"
        right.external_url = "https://open.spotify.com/track/right"
        wrong = MagicMock()
        wrong.artists = "Full Moon Ensemble"
        wrong.title = "The Boxer"
        wrong.external_url = "https://open.spotify.com/track/wrong"
        client.search.return_value = {"tracks": [wrong, right], "albums": []}
        url, _, _ = await resolve_search(
            client, "Simon and Garfunkel - The Boxer"
        )
        assert url == right.external_url

    @pytest.mark.asyncio
    async def test_feat_token(self, client):
        right = MagicMock()
        right.artists = "Gerry Rafferty"
        right.title = "Baker Street"
        right.external_url = "https://open.spotify.com/track/right"
        wrong = MagicMock()
        wrong.artists = "Dave Hollis"
        wrong.title = "Baker Street"
        wrong.external_url = "https://open.spotify.com/track/wrong"
        client.search.return_value = {"tracks": [wrong, right], "albums": []}
        url, _, _ = await resolve_search(
            client, "Rafferty (feat. Gerry) - Baker Street"
        )
        assert url == right.external_url


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
