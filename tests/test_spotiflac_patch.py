"""Tests for spotiflac_patch.py — permanent SpotiFLAC monkey-patches.

Importing spotiflac_patch runs _patch_musicbrainz() at import time, so the
MusicBrainz lookup is a no-op by the time any of these run.
"""

import pytest

import spotiflac_patch  # noqa: F401  (installs the patches on import)


_SAMPLE_MB_RES = {
    "mbid_track": "t1",
    "mbid_album": "a1",
    "mbid_artist": "ar1",
    "mbid_relgroup": "rg1",
    "mbid_albumartist": "aar1",
    "barcode": "00602537556647",
    "label": "Universal",
    "country": "IT",
    "status": "Official",
    "type": "Album",
    "genre": "Hip Hop",
    "bpm": 96,
    "original_date": "2020-01-10",
    "catalognumber": "0602435704596",
}


def test_mb_result_to_tags_always_empty():
    from SpotiFLAC.core.musicbrainz import mb_result_to_tags

    assert mb_result_to_tags(_SAMPLE_MB_RES) == {}
    assert mb_result_to_tags(None) == {}
    assert mb_result_to_tags({}) == {}


@pytest.mark.asyncio
async def test_fetch_mb_metadata_async_returns_empty():
    from SpotiFLAC.core.musicbrainz import fetch_mb_metadata_async

    assert await fetch_mb_metadata_async("USUM71500347") == {}


def test_async_mb_fetch_future_is_already_done():
    from SpotiFLAC.core.musicbrainz import AsyncMBFetch

    mb = AsyncMBFetch("USUM71500347")
    assert mb.future.done()
    assert mb.future.result(timeout=0) is None
    assert mb.result(timeout=0) is None


def test_patch_is_idempotent():
    spotiflac_patch._patch_musicbrainz()
    spotiflac_patch._patch_musicbrainz()
    from SpotiFLAC.core.musicbrainz import mb_result_to_tags

    assert mb_result_to_tags(_SAMPLE_MB_RES) == {}


def test_provider_import_copy_is_patched():
    from SpotiFLAC.core.musicbrainz import mb_result_to_tags

    import SpotiFLAC.providers.qobuz as qobuz

    assert qobuz.mb_result_to_tags(_SAMPLE_MB_RES) == {}
    assert qobuz.mb_result_to_tags is mb_result_to_tags
