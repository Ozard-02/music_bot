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


def test_community_session_attempts_solver_on_expired(monkeypatch):
    from SpotiFLAC.core import signed_session_desktop as ssd

    import SpotiFLAC.providers.qobuz as qobuz

    assert qobuz.ensure_community_session is ssd.ensure_community_session

    spotiflac_patch._community_solve_attempt_at = 0.0

    expired = ssd.CommunitySessionRecord(
        install_id="x",
        session_id="s",
        session_secret="k",
        expires_at="2020-01-01T00:00:00Z",
    )
    ssd.load_community_session = lambda: expired

    def _failing_solve(*a, **kw):
        raise RuntimeError("turnstile not solved")

    monkeypatch.setattr(ssd, "run_community_verification", _failing_solve)
    with pytest.raises(RuntimeError, match="turnstile not solved"):
        ssd.ensure_community_session()
    # A solve attempt was recorded; the next call is inside the cooldown window
    # and must NOT invoke the solver again — it raises fast instead.
    calls = {"n": 0}

    def _counting_solve(*a, **kw):
        calls["n"] += 1
        raise RuntimeError("turnstile not solved")

    monkeypatch.setattr(ssd, "run_community_verification", _counting_solve)
    with pytest.raises(RuntimeError, match="retrying in"):
        ssd.ensure_community_session()
    assert calls["n"] == 0


def test_community_session_solver_success_saves_record(monkeypatch):
    from SpotiFLAC.core import signed_session_desktop as ssd

    spotiflac_patch._community_solve_attempt_at = 0.0

    expired = ssd.CommunitySessionRecord(
        install_id="x",
        session_id="s",
        session_secret="k",
        expires_at="2020-01-01T00:00:00Z",
    )
    ssd.load_community_session = lambda: expired

    saved = {}

    def _fake_save(record):
        saved["session_id"] = record.session_id
        saved["session_secret"] = record.session_secret
        saved["expires_at"] = record.expires_at
        # A real solve writes the fresh record to disk, so the next
        # load_community_session() returns it (mimic the volume mount).
        ssd.load_community_session = lambda: record

    monkeypatch.setattr(ssd, "save_community_session", _fake_save)
    monkeypatch.setattr(ssd, "run_community_verification", lambda r: "grant123")
    monkeypatch.setattr(
        ssd,
        "exchange_community_grant",
        lambda r, g: ssd.CommunitySessionExchange(
            session_id="new_id",
            session_secret="new_secret",
            expires_at="2099-01-01T00:00:00Z",
        ),
    )

    result = ssd.ensure_community_session()
    assert result.session_id == "new_id"
    assert saved == {
        "session_id": "new_id",
        "session_secret": "new_secret",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    # A fresh valid session means the next call takes the fast path (no solve).
    calls = {"n": 0}

    def _counting_solve(*a, **kw):
        calls["n"] += 1
        raise RuntimeError("should not be called")

    monkeypatch.setattr(ssd, "run_community_verification", _counting_solve)
    assert ssd.ensure_community_session() is result
    assert calls["n"] == 0


def test_community_session_uses_valid_record_without_verification():
    from SpotiFLAC.core import signed_session_desktop as ssd

    valid = ssd.CommunitySessionRecord(
        install_id="x",
        session_id="s",
        session_secret="k",
        expires_at="2099-01-01T00:00:00Z",
    )
    ssd.load_community_session = lambda: valid
    assert ssd.ensure_community_session() is valid
