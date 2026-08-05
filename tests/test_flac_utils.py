"""Tests for flac_utils.py — cover resolution (Spotify baseline, Apple/Deezer HD gated by similarity)."""

import io

import pytest
from PIL import Image
from unittest.mock import AsyncMock, patch

from flac_utils import _images_similar, upgrade_apple_cover, upgrade_cover_url


def test_upgrade_apple_cover_defaults_to_3000():
    url = "https://x/100x100bb.jpg"
    assert upgrade_apple_cover(url) == "https://x/3000x3000bb.jpg"


def test_upgrade_apple_cover_custom_size():
    url = "https://x/100x100bb.jpg"
    assert upgrade_apple_cover(url, "2000x2000") == "https://x/2000x2000bb.jpg"


def test_upgrade_cover_url_bumps_300_to_640():
    url = "https://i.scdn.co/image/ab67616d00001e02abcdef"
    assert "b273" in upgrade_cover_url(url)
    assert "1e02" not in upgrade_cover_url(url)


def _jpeg(color, size=600):
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "JPEG")
    return buf.getvalue()


_BASELINE = _jpeg((20, 80, 160))            # Spotify album art (640x640 stand-in)
_SAME_HD = _jpeg((20, 80, 160), size=3000)  # same artwork, higher res
_SAME_SMALL = _jpeg((20, 80, 160), size=300)  # same artwork, smaller
_OTHER = _jpeg((200, 30, 30), size=3000)    # different artwork (single-art)


class TestImagesSimilar:
    def test_same_artwork_detected(self):
        assert _images_similar(_BASELINE, _SAME_HD)

    def test_different_artwork_rejected(self):
        assert not _images_similar(_BASELINE, _OTHER)

    def test_garbage_rejected(self):
        assert not _images_similar(b"not a jpeg", _BASELINE)


def _track(title="MAREA", artist="Madame", isrc="", cover_url="https://i.scdn.co/image/ab67616d00001e02abcdef"):
    t = type("T", (), {
        "title": title,
        "first_artist": artist,
        "isrc": isrc,
        "cover_url": cover_url,
    })()
    return t


def _patch_sources(apple_url="", deezer_url=""):
    """Patch enrichment so Apple/Deezer return the given HD URLs."""
    def _enriched(url):
        return type("E", (), {"cover_url_hd": url})()

    apple = AsyncMock(return_value=_enriched(apple_url))
    deezer = AsyncMock(return_value=_enriched(deezer_url))
    return (
        patch("SpotiFLAC.core.metadata_enrichment.enrich_metadata_async", new=apple),
        patch("SpotiFLAC.core.metadata_enrichment._deezer_fetch_async", new=deezer),
    )


def _fetch_map(url_to_bytes):
    async def _fetch(url, timeout=10):
        for prefix, data in url_to_bytes:
            if prefix in url:
                return data
        return None
    return patch("flac_utils._fetch_bytes", new=AsyncMock(side_effect=_fetch))


class TestResolveCoverData:
    @pytest.mark.asyncio
    async def test_same_artwork_hd_candidate_upgrades_spotify(self):
        from flac_utils import resolve_cover_data

        p1, p2 = _patch_sources(apple_url="https://x/100x100bb.jpg")
        fetch = _fetch_map([("3000x3000", _SAME_HD), ("b273", _BASELINE)])
        with p1, p2, fetch:
            data = await resolve_cover_data(_track(isrc="ISRC0001"))

        assert data == _SAME_HD

    @pytest.mark.asyncio
    async def test_different_artwork_candidate_rejected_keeps_spotify(self):
        from flac_utils import resolve_cover_data

        p1, p2 = _patch_sources(apple_url="https://x/100x100bb.jpg")
        fetch = _fetch_map([("3000x3000", _OTHER), ("b273", _BASELINE)])
        with p1, p2, fetch:
            data = await resolve_cover_data(_track(isrc="ISRC0001"))

        assert data == _BASELINE

    @pytest.mark.asyncio
    async def test_smaller_same_artwork_candidate_rejected(self):
        """HD is only used when it beats the Spotify baseline in resolution."""
        from flac_utils import resolve_cover_data

        p1, p2 = _patch_sources(apple_url="https://x/100x100bb.jpg")
        fetch = _fetch_map([("3000x3000", _SAME_SMALL), ("b273", _BASELINE)])
        with p1, p2, fetch:
            data = await resolve_cover_data(_track(isrc="ISRC0001"))

        assert data == _BASELINE

    @pytest.mark.asyncio
    async def test_deezer_hd_used_when_apple_is_wrong_artwork(self):
        """Apple returns single-art (different image); Deezer's 1000x1000 album art wins."""
        from flac_utils import resolve_cover_data

        p1, p2 = _patch_sources(
            apple_url="https://x/100x100bb.jpg",
            deezer_url="https://y/cover_xl.jpg",
        )
        fetch = _fetch_map(
            [("3000x3000", _OTHER), ("cover_xl", _SAME_HD), ("b273", _BASELINE)]
        )
        with p1, p2, fetch:
            data = await resolve_cover_data(_track(isrc="ISRC0001"))

        assert data == _SAME_HD

    @pytest.mark.asyncio
    async def test_none_when_everything_fails(self):
        from flac_utils import resolve_cover_data

        p1, p2 = _patch_sources()
        with p1, p2, _fetch_map([]):
            assert await resolve_cover_data(_track()) is None


class _FakeAudio:
    """Minimal stand-in for mutagen's FLAC used by read_lrc/write paths."""

    def __init__(self, tags: dict[str, str]):
        self._tags = tags

    def get(self, tag, default=None):
        val = self._tags.get(tag)
        return [val] if val else default


class TestReadLrc:
    LRC = "[01:23.45]hello world\n[02:30.00]bye"
    PLAIN = "just some words\nno timestamps here"

    def test_returns_synced_from_lyrics_tag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "flac_utils.FLAC", lambda p: _FakeAudio({"LYRICS": self.LRC})
        )
        from flac_utils import read_lrc
        assert read_lrc(tmp_path / "x.flac") == self.LRC

    def test_falls_through_to_unsyncedlyrics(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "flac_utils.FLAC", lambda p: _FakeAudio({"UNSYNCEDLYRICS": self.LRC})
        )
        from flac_utils import read_lrc
        assert read_lrc(tmp_path / "x.flac") == self.LRC

    def test_returns_none_for_plain_lyrics(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "flac_utils.FLAC", lambda p: _FakeAudio({"LYRICS": self.PLAIN})
        )
        from flac_utils import read_lrc
        assert read_lrc(tmp_path / "x.flac") is None

    def test_returns_none_when_no_tag(self, tmp_path, monkeypatch):
        monkeypatch.setattr("flac_utils.FLAC", lambda p: _FakeAudio({}))
        from flac_utils import read_lrc
        assert read_lrc(tmp_path / "x.flac") is None

    def test_returns_none_on_unreadable_file(self, tmp_path, monkeypatch):
        def _raise(_p):
            raise KeyError("nope")
        monkeypatch.setattr("flac_utils.FLAC", _raise)
        from flac_utils import read_lrc
        assert read_lrc(tmp_path / "missing.flac") is None


class TestWriteLrcSidecar:
    def test_writes_sidecar(self, tmp_path):
        from flac_utils import write_lrc_sidecar
        fpath = tmp_path / "Artist - Title.flac"
        fpath.touch()
        write_lrc_sidecar(fpath, "[01:23.45]line one\n[02:30.00]line two")
        sidecar = tmp_path / "Artist - Title.lrc"
        assert sidecar.exists()
        text = sidecar.read_text(encoding="utf-8")
        assert "[01:23.45]line one" in text
        assert text.endswith("\n")

    def test_no_sidecar_for_empty(self, tmp_path):
        from flac_utils import write_lrc_sidecar
        fpath = tmp_path / "t.flac"
        fpath.touch()
        write_lrc_sidecar(fpath, "")
        assert not (tmp_path / "t.lrc").exists()