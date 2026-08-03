"""Tests for flac_utils.py — cover resolution (Apple HD else upgraded Spotify)."""

from unittest.mock import AsyncMock, patch

import pytest

from flac_utils import upgrade_apple_cover, upgrade_cover_url


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


def _track(title="MAREA", artist="Madame", isrc="", cover_url="https://i.scdn.co/image/ab67616d00001e02abcdef"):
    t = type("T", (), {
        "title": title,
        "first_artist": artist,
        "isrc": isrc,
        "cover_url": cover_url,
    })()
    return t


class TestResolveCoverData:
    @pytest.mark.asyncio
    async def test_apple_hd_wins_over_spotify(self):
        track = _track(isrc="ISRC0001")
        with patch(
            "SpotiFLAC.core.metadata_enrichment.enrich_metadata_async",
            new=AsyncMock(return_value=type("E", (), {"cover_url_hd": "https://x/100x100bb.jpg"})()),
        ) as enrich, patch("flac_utils._fetch_bytes", new=AsyncMock(
            side_effect=lambda url, timeout: b"APPLE3000" if "3000x3000" in url else b"SPOTIFY640"
        )) as fetch:
            from flac_utils import resolve_cover_data

            data = await resolve_cover_data(_track(isrc="ISRC0001"))

        enrich.assert_awaited_once_with("MAREA", "Madame", isrc="ISRC0001", providers=["apple"])
        assert data == b"APPLE3000"

    @pytest.mark.asyncio
    async def test_apple_missing_falls_back_to_upgraded_spotify(self):
        with patch(
            "SpotiFLAC.core.metadata_enrichment.enrich_metadata_async",
            new=AsyncMock(return_value=type("E", (), {"cover_url_hd": ""})()),
        ), patch("flac_utils._fetch_bytes", new=AsyncMock(
            side_effect=lambda url, timeout=10: b"SPOTIFY640"
        )) as fetch:
            from flac_utils import resolve_cover_data

            data = await resolve_cover_data(_track(isrc="ISRC0001"))

        assert data == b"SPOTIFY640"
        called = fetch.call_args[0][0]
        assert "b273" in called and "1e02" not in called

    @pytest.mark.asyncio
    async def test_none_when_everything_fails(self):
        with patch(
            "SpotiFLAC.core.metadata_enrichment.enrich_metadata_async",
            new=AsyncMock(return_value=type("E", (), {"cover_url_hd": ""})()),
        ), patch("flac_utils._fetch_bytes", new=AsyncMock(return_value=None)):
            from flac_utils import resolve_cover_data

            assert await resolve_cover_data(_track()) is None