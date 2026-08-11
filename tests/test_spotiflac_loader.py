"""Tests for spotiflac_loader — lazy load, refcount gating, idle re-exec."""

import time

import pytest

import spotiflac_loader


def _reset():
    spotiflac_loader._loaded = False
    spotiflac_loader._was_loaded = False
    spotiflac_loader._active = 0
    spotiflac_loader._last_release = 0.0


def test_loads_and_patches_on_first_use():
    _reset()
    try:
        spotiflac_loader.use()
        assert spotiflac_loader._loaded is True
        assert spotiflac_loader._was_loaded is True
        import SpotiFLAC.core.console as console
        assert console.print_summary(*[None] * 3) is None  # silenced by patch
    finally:
        spotiflac_loader.release()
        _reset()


def test_reexec_requires_loaded_then_idle_long_enough():
    _reset()
    assert spotiflac_loader.should_reexec() is False  # never loaded

    spotiflac_loader._was_loaded = True
    spotiflac_loader._active = 1
    assert spotiflac_loader.should_reexec() is False  # op in flight

    spotiflac_loader._active = 0
    assert spotiflac_loader.should_reexec() is False  # no idle baseline yet

    spotiflac_loader._last_release = time.monotonic() - spotiflac_loader.REEXEC_AFTER - 1
    assert spotiflac_loader.should_reexec() is True  # idle past the settle time
    _reset()


@pytest.mark.asyncio
async def test_wrap_refcounts_and_returns_value():
    _reset()

    @spotiflac_loader.wrap
    async def op():
        assert spotiflac_loader._active == 1
        return 42

    assert await op() == 42
    assert spotiflac_loader._active == 0
    assert spotiflac_loader._was_loaded is True  # wrapped op loaded the stack
    _reset()


def test_eager_import_switch_disables_reexec(monkeypatch):
    _reset()
    monkeypatch.setenv("SPOTIFLAC_EAGER_IMPORT", "1")
    assert spotiflac_loader.should_reexec() is False
    spotiflac_loader._was_loaded = True
    spotiflac_loader._active = 0
    spotiflac_loader._last_release = 1
    assert spotiflac_loader.should_reexec() is False
    _reset()
