"""Deezer search normalisation (no network)."""

from __future__ import annotations

import pytest

from deezer_search import DeezerError, search


def _track_item(track_id: str = "3135553") -> dict:
    return {
        "id": int(track_id),
        "title": "One More Time",
        "duration": 320,
        "isrc": "GBDUW0000053",
        "explicit_lyrics": False,
        "artist": {"name": "Daft Punk"},
        "album": {
            "id": 302127,
            "title": "Discovery",
            "cover_medium": "https://cdn-images.dzcdn.net/images/cover/abc/250x250.jpg",
            "cover_big": "https://cdn-images.dzcdn.net/images/cover/abc/500x500.jpg",
            "cover_xl": "https://cdn-images.dzcdn.net/images/cover/abc/1000x1000.jpg",
        },
    }


def _album_item() -> dict:
    return {
        "id": 302127,
        "title": "Discovery",
        "release_date": "2001-03-12",
        "artist": {"name": "Daft Punk"},
        "cover_medium": "https://cdn-images.dzcdn.net/images/cover/abc/250x250.jpg",
        "cover_big": "https://cdn-images.dzcdn.net/images/cover/abc/500x500.jpg",
        "cover_xl": "https://cdn-images.dzcdn.net/images/cover/abc/1000x1000.jpg",
    }


def _playlist_item() -> dict:
    return {
        "id": 908622995,
        "title": "French Touch",
        "creation_date": "2020-01-01",
        "user": {"name": "espo"},
        "picture_medium": "https://cdn-images.dzcdn.net/images/cover/def/250x250.jpg",
        "picture_big": "https://cdn-images.dzcdn.net/images/cover/def/500x500.jpg",
        "picture_xl": "https://cdn-images.dzcdn.net/images/cover/def/1000x1000.jpg",
    }


def _result(path: str) -> dict:
    if path.startswith("/search?"):
        return {"data": [_track_item()]}
    if path.startswith("/search/album"):
        return {"data": [_album_item()]}
    if path.startswith("/search/playlist"):
        return {"data": [_playlist_item()]}
    raise AssertionError(path)


def test_search_normalises_all_three_result_types():
    seen: list[str] = []

    def fake(path: str) -> dict:
        seen.append(path)
        return _result(path)

    result = search("one more time", 7, fetch=fake)

    assert len(seen) == 3
    assert result["tracks"] == [
        {
            "id": "3135553",
            "title": "One More Time",
            "artists": "Daft Punk",
            "album": "Discovery",
            "durationMs": 320000,
            "url": "https://www.deezer.com/track/3135553",
            "cover": "https://cdn-images.dzcdn.net/images/cover/abc/250x250.jpg",
            "explicit": False,
            "isrc": "GBDUW0000053",
        }
    ]
    assert result["albums"] == [
        {
            "id": "302127",
            "name": "Discovery",
            "artists": "Daft Punk",
            "releaseDate": "2001-03-12",
            "url": "https://www.deezer.com/album/302127",
            "cover": "https://cdn-images.dzcdn.net/images/cover/abc/250x250.jpg",
        }
    ]
    assert result["playlists"] == [
        {
            "id": "908622995",
            "name": "French Touch",
            "owner": "espo",
            "releaseDate": "2020-01-01",
            "url": "https://www.deezer.com/playlist/908622995",
            "cover": "https://cdn-images.dzcdn.net/images/cover/def/250x250.jpg",
        }
    ]
    assert all("limit=7" in path for path in seen)


def test_search_clamps_limit_and_drops_malformed_items():
    seen: list[str] = []

    def fake(path: str) -> dict:
        seen.append(path)
        if path.startswith("/search?"):
            return {"data": [_track_item(), {}, {"id": None}, "nope"]}
        return {"data": []}

    result = search("x", 999, fetch=fake)

    assert all("limit=50" in path for path in seen)
    assert len(result["tracks"]) == 1
    assert result["albums"] == []
    assert result["playlists"] == []


def test_search_returns_partial_results_when_one_endpoint_fails():
    def fake(path: str) -> dict:
        if path.startswith("/search/album"):
            raise DeezerError("album endpoint down")
        return _result(path)

    result = search("one more time", 3, fetch=fake)

    assert len(result["tracks"]) == 1
    assert result["albums"] == []
    assert len(result["playlists"]) == 1


def test_search_raises_when_every_endpoint_fails():
    def fake(path: str) -> dict:
        raise DeezerError("Deezer down")

    with pytest.raises(DeezerError):
        search("one more time", 3, fetch=fake)


def test_search_rejects_empty_query():
    with pytest.raises(DeezerError):
        search("   ", 10)


def test_search_rejects_bad_limit():
    with pytest.raises(DeezerError):
        search("x", "many")  # type: ignore[arg-type]
