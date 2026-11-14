"""Deezer metadata → TrackMetadata (no real network)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

import spotiflac_patch  # noqa: F401  (skip SpotiFLAC's GitHub version check)
from deezer_meta import (
    DeezerMetadataError,
    fetch_tracks,
    is_deezer_url,
    parse_deezer_url,
)


def _track(track_id: int = 3135553, **overrides) -> dict:
    data = {
        "id": track_id,
        "title": "One More Time",
        "duration": 320,
        "isrc": "GBDUW0000053",
        "track_position": 1,
        "disk_number": 1,
        "release_date": "2001-03-12",
        "explicit_lyrics": False,
        "bpm": 123,
        "artist": {"id": 27, "name": "Daft Punk"},
        "contributors": [{"name": "Daft Punk"}, {"name": "Romanthony"}],
        "album": {
            "id": 302127,
            "title": "Discovery",
            "cover_xl": "https://cdn-images.dzcdn.net/images/cover/abc/1000x1000.jpg",
            "release_date": "2001-03-12",
        },
    }
    data.update(overrides)
    return data


def _album() -> dict:
    return {
        "id": 302127,
        "title": "Discovery",
        "release_date": "2001-03-12",
        "nb_tracks": 2,
        "label": "Parlophone",
        "upc": "724384960624",
        "genres": [{"name": "Electronic"}],
        "artist": {"id": 27, "name": "Daft Punk"},
        "cover_xl": "https://cdn-images.dzcdn.net/images/cover/abc/1000x1000.jpg",
        "tracks": {
            "data": [
                {"id": 3135553, "title": "One More Time"},
                {"id": 3135554, "title": "Aerodynamic"},
            ]
        },
    }


def _install_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_parse_and_detect_deezer_urls():
    assert parse_deezer_url("https://www.deezer.com/track/3135553") == {
        "type": "track",
        "id": "3135553",
    }
    assert parse_deezer_url("https://deezer.com/it/album/302127?utm=x") == {
        "type": "album",
        "id": "302127",
    }
    assert parse_deezer_url("https://www.deezer.com/artist/27") is None
    assert is_deezer_url("https://www.deezer.com/playlist/908622995")
    assert not is_deezer_url("https://open.spotify.com/track/x")


@pytest.mark.asyncio
async def test_track_fetch_maps_album_artist_and_isrc(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/track/3135553":
            return httpx.Response(200, json=_track())
        if request.url.path == "/album/302127":
            return httpx.Response(200, json=_album())
        raise AssertionError(request.url)

    _install_transport(monkeypatch, handler)
    tracks = await fetch_tracks("https://www.deezer.com/track/3135553")

    assert len(tracks) == 1
    track = tracks[0]
    assert track.id == "deezer-3135553"
    assert track.title == "One More Time"
    assert track.artists == "Daft Punk, Romanthony"
    assert track.album == "Discovery"
    assert track.album_artist == "Daft Punk"
    assert track.isrc == "GBDUW0000053"
    assert track.track_number == 1
    assert track.disc_number == 1
    assert track.total_tracks == 2
    assert track.total_discs == 1
    assert track.release_date == "2001-03-12"
    assert track.genre == "Electronic"
    assert track.publisher == "Parlophone"
    assert track.upc == "724384960624"
    assert track.external_url == "https://www.deezer.com/track/3135553"
    assert track.album_url == "https://www.deezer.com/album/302127"
    assert track.artist_url == "https://www.deezer.com/artist/27"


@pytest.mark.asyncio
async def test_album_fetch_reads_each_track_for_isrc_and_positions(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/album/302127":
            return httpx.Response(200, json=_album())
        if request.url.path == "/track/3135553":
            return httpx.Response(200, json=_track())
        if request.url.path == "/track/3135554":
            return httpx.Response(
                200,
                json=_track(
                    3135554,
                    title="Aerodynamic",
                    track_position=2,
                    isrc="GBDUW0000054",
                ),
            )
        raise AssertionError(request.url)

    _install_transport(monkeypatch, handler)
    tracks = await fetch_tracks("https://www.deezer.com/album/302127")

    assert [track.title for track in tracks] == ["One More Time", "Aerodynamic"]
    assert [track.isrc for track in tracks] == [
        "GBDUW0000053",
        "GBDUW0000054",
    ]
    assert [track.track_number for track in tracks] == [1, 2]
    assert all(track.album_artist == "Daft Punk" for track in tracks)
    assert all(track.total_tracks == 2 for track in tracks)


@pytest.mark.asyncio
async def test_playlist_fetch_uses_playlist_order_and_isrc(monkeypatch):
    playlist = {
        "id": 908622995,
        "title": "French Touch",
        "nb_tracks": 2,
        "tracks": {
            "data": [
                {"id": 3135553, "title": "One More Time", "isrc": "GBDUW0000053"},
                {"id": 3135554, "title": "Aerodynamic", "isrc": "GBDUW0000054"},
            ]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/playlist/908622995":
            return httpx.Response(200, json=playlist)
        if request.url.path == "/track/3135553":
            return httpx.Response(200, json=_track())
        if request.url.path == "/track/3135554":
            return httpx.Response(
                200,
                json=_track(3135554, title="Aerodynamic", track_position=2),
            )
        raise AssertionError(request.url)

    _install_transport(monkeypatch, handler)
    tracks = await fetch_tracks("https://www.deezer.com/playlist/908622995")

    assert [track.title for track in tracks] == ["One More Time", "Aerodynamic"]
    assert [track.track_number for track in tracks] == [1, 2]
    assert tracks[0].total_tracks == 2


@pytest.mark.asyncio
async def test_detail_failure_falls_back_to_basic_track(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/album/302127":
            return httpx.Response(200, json=_album())
        if request.url.path == "/track/3135553":
            return httpx.Response(500, json={"error": "nope"})
        if request.url.path == "/track/3135554":
            return httpx.Response(500, json={"error": "nope"})
        raise AssertionError(request.url)

    _install_transport(monkeypatch, handler)
    tracks = await fetch_tracks("https://www.deezer.com/album/302127")

    assert len(tracks) == 2
    assert tracks[0].title == "One More Time"
    assert tracks[0].isrc == ""
    assert tracks[0].album_artist == "Daft Punk"


@pytest.mark.asyncio
async def test_quota_error_is_retried_once(monkeypatch):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, json={"error": {"message": "Quota limit exceeded"}})
        return httpx.Response(200, json=_track())

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    _install_transport(monkeypatch, handler)

    tracks = await fetch_tracks("https://www.deezer.com/track/3135553")
    assert tracks[0].isrc == "GBDUW0000053"
    assert calls["count"] >= 2


@pytest.mark.asyncio
async def test_invalid_url_raises():
    with pytest.raises(DeezerMetadataError):
        await fetch_tracks("https://open.spotify.com/track/abc")
