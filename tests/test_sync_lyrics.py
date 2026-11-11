"""Tests for scripts/sync_lyrics.py — lyrics-only maintenance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.sync_lyrics import sync_lyrics


def _lib(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.flac").write_bytes(b"f")
    return music


@pytest.mark.asyncio
async def test_existing_lyrics_are_kept_and_sidecar_backfilled(tmp_path, monkeypatch):
    music = _lib(tmp_path)
    sidecars = []
    monkeypatch.setattr("scripts.sync_lyrics._read_lyrics", lambda p: "old lyrics")
    monkeypatch.setattr("scripts.sync_lyrics.read_lrc", lambda p: "[00:01.00]old")
    monkeypatch.setattr(
        "scripts.sync_lyrics.write_lrc_sidecar",
        lambda p, text: sidecars.append((p.name, text)),
    )

    res = await sync_lyrics(music, apply=True)

    assert res["kept"] == 0
    assert res["sidecars"] == 1
    assert res["fixed"] == 0
    assert sidecars == [("a.flac", "[00:01.00]old")]


@pytest.mark.asyncio
async def test_missing_lyrics_are_fetched_and_embedded(tmp_path, monkeypatch):
    music = _lib(tmp_path)
    writes = []
    sidecars = []
    track = SimpleNamespace(
        id="abc", title="Song", first_artist="Artist", album="Album",
        duration_ms=180000, isrc="",
    )

    class _Client:
        async def get_track_async(self, sid):
            return track

    monkeypatch.setattr("scripts.sync_lyrics._read_lyrics", lambda p: "")
    monkeypatch.setattr("scripts.sync_lyrics.get_spotify_id_from_file", lambda p: "abc")
    monkeypatch.setattr("scripts.sync_lyrics._write_lyrics", lambda p, text: writes.append((p.name, text)))
    monkeypatch.setattr("scripts.sync_lyrics.read_lrc", lambda p: "[00:01.00]new")
    monkeypatch.setattr(
        "scripts.sync_lyrics.write_lrc_sidecar",
        lambda p, text: sidecars.append((p.name, text)),
    )

    with (
        patch("SpotiFLAC.client.SpotifyMetadataClient", return_value=_Client()),
        patch(
            "SpotiFLAC.core.lyrics.fetch_lyrics_async",
            new=AsyncMock(return_value=("[00:01.00]new", "lrclib")),
        ),
    ):
        res = await sync_lyrics(music, apply=True)

    assert res["fixed"] == 1
    assert res["sidecars"] == 1
    assert writes == [("a.flac", "[00:01.00]new")]
    assert sidecars == [("a.flac", "[00:01.00]new")]


@pytest.mark.asyncio
async def test_unsynced_lyrics_are_upgraded_when_synced_version_found(tmp_path, monkeypatch):
    music = _lib(tmp_path)
    writes = []
    sidecars = []
    state = {"written": False}
    track = SimpleNamespace(
        id="abc", title="Song", first_artist="Artist", album="Album",
        duration_ms=180000, isrc="",
    )

    class _Client:
        async def get_track_async(self, sid):
            return track

    def _write(p, text):
        writes.append((p.name, text))
        state["written"] = True

    monkeypatch.setattr("scripts.sync_lyrics._read_lyrics", lambda p: "old plain lyrics")
    monkeypatch.setattr("scripts.sync_lyrics.get_spotify_id_from_file", lambda p: "abc")
    monkeypatch.setattr("scripts.sync_lyrics._write_lyrics", _write)
    monkeypatch.setattr(
        "scripts.sync_lyrics.read_lrc",
        lambda p: "[00:01.00]new" if state["written"] else None,
    )
    monkeypatch.setattr(
        "scripts.sync_lyrics.write_lrc_sidecar",
        lambda p, text: sidecars.append((p.name, text)),
    )

    with (
        patch("SpotiFLAC.client.SpotifyMetadataClient", return_value=_Client()),
        patch(
            "SpotiFLAC.core.lyrics.fetch_lyrics_async",
            new=AsyncMock(return_value=("[00:01.00]new", "lrclib")),
        ),
    ):
        res = await sync_lyrics(music, apply=True)

    assert res["fixed"] == 1
    assert res["kept"] == 0
    assert writes == [("a.flac", "[00:01.00]new")]
    assert sidecars == [("a.flac", "[00:01.00]new")]


@pytest.mark.asyncio
async def test_unsynced_lyrics_are_kept_when_provider_returns_plain(tmp_path, monkeypatch):
    music = _lib(tmp_path)
    writes = []
    track = SimpleNamespace(
        id="abc", title="Song", first_artist="Artist", album="Album",
        duration_ms=180000, isrc="",
    )

    class _Client:
        async def get_track_async(self, sid):
            return track

    monkeypatch.setattr("scripts.sync_lyrics._read_lyrics", lambda p: "old plain lyrics")
    monkeypatch.setattr("scripts.sync_lyrics.get_spotify_id_from_file", lambda p: "abc")
    monkeypatch.setattr("scripts.sync_lyrics._write_lyrics", lambda p, t: writes.append(t))
    monkeypatch.setattr("scripts.sync_lyrics.read_lrc", lambda p: None)

    with (
        patch("SpotiFLAC.client.SpotifyMetadataClient", return_value=_Client()),
        patch(
            "SpotiFLAC.core.lyrics.fetch_lyrics_async",
            new=AsyncMock(return_value=("plain new lyrics", "lrclib")),
        ),
    ):
        res = await sync_lyrics(music, apply=True)

    assert res["kept"] == 1
    assert res["fixed"] == 0
    assert writes == []


@pytest.mark.asyncio
async def test_unsynced_lyrics_are_kept_when_no_replacement_found(tmp_path, monkeypatch):
    music = _lib(tmp_path)
    track = SimpleNamespace(
        id="abc", title="Song", first_artist="Artist", album="Album",
        duration_ms=180000, isrc="",
    )

    class _Client:
        async def get_track_async(self, sid):
            return track

    monkeypatch.setattr("scripts.sync_lyrics._read_lyrics", lambda p: "old plain lyrics")
    monkeypatch.setattr("scripts.sync_lyrics.get_spotify_id_from_file", lambda p: "abc")
    monkeypatch.setattr("scripts.sync_lyrics.read_lrc", lambda p: None)

    with (
        patch("SpotiFLAC.client.SpotifyMetadataClient", return_value=_Client()),
        patch(
            "SpotiFLAC.core.lyrics.fetch_lyrics_async",
            new=AsyncMock(return_value=("", "")),
        ),
    ):
        res = await sync_lyrics(music, apply=True)

    assert res["kept"] == 1
    assert res["no_lyrics"] == 0
    assert res["fixed"] == 0


@pytest.mark.asyncio
async def test_no_lyrics_found_is_counted(tmp_path, monkeypatch):
    music = _lib(tmp_path)
    track = SimpleNamespace(
        id="abc", title="Song", first_artist="Artist", album="Album",
        duration_ms=180000, isrc="",
    )

    class _Client:
        async def get_track_async(self, sid):
            return track

    monkeypatch.setattr("scripts.sync_lyrics._read_lyrics", lambda p: "")
    monkeypatch.setattr("scripts.sync_lyrics.get_spotify_id_from_file", lambda p: "abc")

    with (
        patch("SpotiFLAC.client.SpotifyMetadataClient", return_value=_Client()),
        patch(
            "SpotiFLAC.core.lyrics.fetch_lyrics_async",
            new=AsyncMock(return_value=("", "")),
        ),
    ):
        res = await sync_lyrics(music, apply=True)

    assert res["no_lyrics"] == 1
    assert res["fixed"] == 0
