"""Lazy SpotiFLAC loader + idle re-exec (keeps the bot's idle RSS lean).

The bot imports nothing SpotiFLAC-related at startup (config.py keeps
SpotiFLAC out of the import graph), so a mostly-idle container sits at the
lean baseline (~110MiB in-process, down from ~180MiB with the whole heavy
stack loaded).  The heavy stack (SpotiFLAC + pydoll + nodriver + mutagen +
PIL) loads on first use inside a `@wrap`-ped entry point.

CPython can't return a loaded module stack's RSS (modules aren't gc-tracked,
so module<->module reference cycles are never collected — verified: popping
all 386 modules + gc.collect + malloc_trim leaves RSS unchanged).  The only
reliable way back to the lean baseline is to re-exec the process once it has
been fully idle for a while: the worker's idle poll calls `should_reexec()`
at its 300s idle cap, and the refcount guard here makes sure it only fires
between ops, never mid-download.

Every SpotiFLAC-touching entry point is wrapped with `@wrap` (run_url,
worker._resolve, worker._auto_build_m3u8, m3u8.build_m3u8,
fix_metadata.fix_library, maintenance.rescan_library) — this both installs
the patches before use and keeps the refcount > 0 while an op is in flight.

Revert switch: SPOTIFLAC_EAGER_IMPORT=1 restores the old eager behavior
(SpotiFLAC loads at startup, no re-exec) — use()/should_reexec() no-op.
"""

from __future__ import annotations

import functools
import os
import time

REEXEC_AFTER = int(os.environ.get("SPOTIFLAC_REEXEC_AFTER", "600"))

_loaded = False
_was_loaded = False
_active = 0
_last_release = 0.0


def _load() -> None:
    global _loaded, _was_loaded
    import spotiflac_patch  # noqa: F401  (import-time side-effect installs patches)
    spotiflac_patch.install_all()
    _loaded = True
    _was_loaded = True


def use() -> None:
    """Enter a SpotiFLAC-using section (refcounted; loads the stack on first use)."""
    global _active
    _active += 1
    if os.environ.get("SPOTIFLAC_EAGER_IMPORT"):
        return
    if not _loaded:
        try:
            _load()
        except Exception:
            _active -= 1
            raise


def release() -> None:
    """Leave a SpotiFLAC-using section. Start the idle countdown."""
    global _active, _last_release
    if _active > 0:
        _active -= 1
    if _active == 0:
        _last_release = time.monotonic()


def wrap(fn):
    """Decorator: refcount a SpotiFLAC-using async entry point."""
    @functools.wraps(fn)
    async def _wrapped(*args, **kwargs):
        use()
        try:
            return await fn(*args, **kwargs)
        finally:
            release()
    return _wrapped


def should_reexec() -> bool:
    """True when the heavy stack has been loaded AND the process has been
    idle (no SpotiFLAC op in flight) for `REEXEC_AFTER` seconds — the worker
    then re-execs so RSS returns to the lean baseline."""
    if os.environ.get("SPOTIFLAC_EAGER_IMPORT"):
        return False  # revert switch: old eager behavior
    if not _was_loaded:
        return False  # stack never loaded — already lean, nothing to recover
    if _active > 0:
        return False  # op in flight — never re-exec mid-download
    if _last_release == 0:
        return False
    return time.monotonic() - _last_release >= REEXEC_AFTER
