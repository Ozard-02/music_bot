"""Tests for spotiflac_patch.py — the 5.x-compatible subset.

SpotiFLAC 5.x removed the native providers, so the old provider-matching,
community-session and Qobuz-lock patches have no target.  What remains:
MusicBrainz no-op, GitHub version check skip, console/input silencing (tested
in test_downloader.py) and the ProgressManager neutralization.
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


def test_github_version_check_skipped():
    """No api.github.com call at import; APP_VERSION is what the 404 gave."""
    import requests
    import SpotiFLAC.core.signed_session_desktop as ssd

    assert ssd.APP_VERSION == ""
    assert requests.get.__module__ == "requests.api"


def test_progress_manager_is_neutralized():
    from SpotiFLAC.core.progress import ProgressManager

    assert ProgressManager._event_queue is None
    assert ProgressManager._worker_task is None
    # Methods are replaced with no-ops by disable_progress_manager().
    ProgressManager.start_worker()
    assert ProgressManager._event_queue is None
