"""Tests for the search subprocess protocol helpers (no network)."""

from types import SimpleNamespace

from search_job import is_spotify_block, normalize, normalize_official


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


def test_normalize_official_payload():
    payload = {
        "tracks": {
            "items": [
                {
                    "id": "track-1",
                    "name": "One More Time",
                    "artists": [{"name": "Daft Punk"}],
                    "album": {
                        "name": "Discovery",
                        "images": [{"url": "https://i.scdn.co/image/abc"}],
                    },
                    "duration_ms": 320357,
                    "external_urls": {
                        "spotify": "https://open.spotify.com/track/track-1"
                    },
                    "explicit": False,
                },
                None,
            ]
        },
        "albums": {
            "items": [
                {
                    "id": "album-1",
                    "name": "Discovery",
                    "artists": [{"name": "Daft Punk"}],
                    "release_date": "2001-03-12",
                    "images": [{"url": "https://i.scdn.co/image/def"}],
                    "external_urls": {
                        "spotify": "https://open.spotify.com/album/album-1"
                    },
                }
            ]
        },
        "playlists": {
            "items": [
                {
                    "id": "playlist-1",
                    "name": "French Touch",
                    "owner": {"display_name": "espo"},
                    "images": [],
                    "external_urls": {
                        "spotify": "https://open.spotify.com/playlist/playlist-1"
                    },
                }
            ]
        },
    }

    normalized = normalize_official(payload)
    assert normalized["tracks"] == [
        {
            "id": "track-1",
            "title": "One More Time",
            "artists": "Daft Punk",
            "album": "Discovery",
            "durationMs": 320357,
            "url": "https://open.spotify.com/track/track-1",
            "cover": "https://i.scdn.co/image/abc",
            "explicit": False,
        }
    ]
    assert normalized["albums"][0]["releaseDate"] == "2001-03-12"
    assert normalized["albums"][0]["kind"] == "album"
    assert normalized["playlists"][0]["owner"] == "espo"
    assert normalized["playlists"][0]["cover"] == ""


def test_normalize_official_tolerates_missing_sections():
    assert normalize_official({}) == {"tracks": [], "albums": [], "playlists": []}
