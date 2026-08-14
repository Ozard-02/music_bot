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


def test_community_session_raises_fast_and_never_solves(monkeypatch):
    from SpotiFLAC.core import signed_session_desktop as ssd

    import SpotiFLAC.providers.qobuz as qobuz

    assert qobuz.ensure_community_session is ssd.ensure_community_session

    # The solver must never be invoked — the session is disabled outright.
    calls = {"n": 0}

    def _counting_solve(*a, **kw):
        calls["n"] += 1
        raise RuntimeError("should not be called")

    monkeypatch.setattr(ssd, "run_community_verification", _counting_solve)
    monkeypatch.setattr(ssd, "load_community_session", lambda: None)
    with pytest.raises(RuntimeError, match="community session disabled"):
        ssd.ensure_community_session()
    assert calls["n"] == 0


def test_community_endpoints_emptied():
    import SpotiFLAC.providers.qobuz as qobuz
    from SpotiFLAC.core.endpoints import get_community_url

    # Community API removed from qobuz's stream-fetch candidate pool and
    # get_community_url() (amazon gate) neutralized at the endpoint registry.
    assert qobuz._COMMUNITY_APIS == []
    assert get_community_url("amazon") == ""
    assert get_community_url("tidal") == ""


def test_track_provider_records_success_and_pops():
    from SpotiFLAC.core.models import DownloadResult
    import SpotiFLAC.downloader as sf_downloader
    from SpotiFLAC.core.models import TrackMetadata

    calls = {"n": 0}

    async def _fake_orig(metadata, *a, **kw):
        calls["n"] += 1
        return DownloadResult.ok("deezer", f"/tmp/{metadata.id}.flac")

    sf_downloader._spoty_loop_provider_patched = False
    sf_downloader.download_one_async = _fake_orig
    spotiflac_patch._patch_track_provider()

    import asyncio

    track = TrackMetadata(
        id="tricky", title="T", artists="A", album="Al", album_artist="A",
    )
    result = asyncio.run(sf_downloader.download_one_async(track, "/tmp", []))

    assert result.provider == "deezer"
    assert spotiflac_patch.pop_track_provider("tricky") == "deezer"
    assert spotiflac_patch.pop_track_provider("tricky") is None


def test_track_provider_ignores_failed_and_skipped():
    from SpotiFLAC.core.models import DownloadResult
    import SpotiFLAC.downloader as sf_downloader
    from SpotiFLAC.core.models import TrackMetadata

    async def _fake_orig(metadata, *a, **kw):
        if metadata.title == "fail":
            return DownloadResult.fail("none", "boom")
        return DownloadResult(
            success=True, provider="qobuz", file_path="/tmp/x.flac", skipped=True,
        )

    sf_downloader._spoty_loop_provider_patched = False
    sf_downloader.download_one_async = _fake_orig
    spotiflac_patch._patch_track_provider()

    import asyncio

    failed = TrackMetadata(id="f1", title="fail", artists="A", album="Al", album_artist="A")
    asyncio.run(sf_downloader.download_one_async(failed, "/tmp", []))
    skipped = TrackMetadata(id="s1", title="skip", artists="A", album="Al", album_artist="A")
    asyncio.run(sf_downloader.download_one_async(skipped, "/tmp", []))

    assert spotiflac_patch.pop_track_provider("f1") is None
    assert spotiflac_patch.pop_track_provider("s1") is None
