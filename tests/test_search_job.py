"""Tests for the search subprocess protocol helpers (no network)."""

from types import SimpleNamespace

from search_job import is_spotify_block, normalize


def test_normalize_tracks_and_collections():
    result = {
        "tracks": [
            SimpleNamespace(
                id="track-1",
                title="One More Time",
                artists="Daft Punk",
                album="Discovery",
                duration_ms=320357,
                external_url="https://open.spotify.com/track/track-1",
                cover_url="https://i.scdn.co/image/abc",
                is_explicit=True,
            )
        ],
        "albums": [
            {
                "id": "album-1",
                "name": "Discovery",
                "external_url": "https://open.spotify.com/album/album-1",
                "cover_url": "https://i.scdn.co/image/def",
                "artists": "Daft Punk",
                "release_date": "2001-03-12",
            }
        ],
        "playlists": [
            {
                "id": "playlist-1",
                "name": "French Touch",
                "external_url": "https://open.spotify.com/playlist/playlist-1",
                "cover_url": "",
                "owner": "espo",
            }
        ],
    }
    normalized = normalize(result)
    assert normalized["tracks"] == [
        {
            "id": "track-1",
            "title": "One More Time",
            "artists": "Daft Punk",
            "album": "Discovery",
            "durationMs": 320357,
            "url": "https://open.spotify.com/track/track-1",
            "cover": "https://i.scdn.co/image/abc",
            "explicit": True,
        }
    ]
    assert normalized["albums"][0]["releaseDate"] == "2001-03-12"
    assert normalized["albums"][0]["kind"] == "album"
    assert normalized["playlists"][0]["owner"] == "espo"
    assert normalized["playlists"][0]["kind"] == "playlist"


def test_normalize_tolerates_missing_fields():
    normalized = normalize({})
    assert normalized == {"tracks": [], "albums": [], "playlists": []}


def test_is_spotify_block():
    assert is_spotify_block(RuntimeError("HTTP 406 Not Acceptable"))
    assert is_spotify_block(RuntimeError("429 Too Many Requests"))
    assert is_spotify_block(RuntimeError("Spotify rate limit reached"))
    assert not is_spotify_block(RuntimeError("No results found"))
