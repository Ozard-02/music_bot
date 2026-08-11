"""Neutralize SpotiFLAC's noisy/leaky internals (monkey-patches).

Kept in one module so the hacks are easy to find and review.  Importing this
module runs `install_console_silencing()`, `_patch_qobuz_lock()`,
`_patch_musicbrainz()` and `disable_progress_manager()` once — downloader.py
relies on that side-effect (regression-tested in tests/test_downloader.py).
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import sys
import threading

_CONSOLE_PRINTS = (
    "print_source_banner",
    "print_api_failure",
    "print_quality_fallback",
    "print_track_header",
    "print_summary",
    "print_official_source",
)


def install_console_silencing() -> None:
    """Permanently no-op SpotiFLAC's console output and interactive prompts.

    Run once at import time, before any SpotiFLAC module is used.  This must be
    permanent (never restored), for two reasons:

    * SpotiFLAC call sites use module-level copies (`from .core.console import
      print_summary`), so patching the console module's attributes alone does
      nothing — the copies are fixed at import time.  We patch the console
      module AND overwrite the copies in every already-imported module;
      modules imported later pick up the patched attributes automatically.
    * The old context-manager approach restored the originals in a `finally` —
      with one manager per download thread, the first thread to exit
      un-patched builtins.input while the others were still downloading (the
      "Incolla qui il grant" prompt leaked into the logs).  No restore, no
      race.
    """
    import SpotiFLAC.core.console as _console
    import SpotiFLAC.core.progress as _progress

    for _name in _CONSOLE_PRINTS:
        setattr(_console, _name, lambda *a, **kw: None)
    _progress.safe_tqdm_write = lambda *a, **kw: None
    builtins.input = lambda *a, **kw: ""

    _sources = {_name: _console for _name in _CONSOLE_PRINTS}
    _sources["safe_tqdm_write"] = _progress
    for _mod_name, _mod in list(sys.modules.items()):
        if not _mod_name.startswith("SpotiFLAC"):
            continue
        for _name, _src in _sources.items():
            if hasattr(_mod, _name):
                setattr(_mod, _name, getattr(_src, _name))


class _AsyncLockAdapter:
    """asyncio.Lock stand-in with no event-loop affinity.

    QobuzProvider stores an asyncio.Lock created in the first event loop that
    touched it; SpotiFLAC's own to_thread + asyncio.run patterns later await it
    from fresh loops, raising 'is bound to a different event loop' and failing
    the whole download (the main cause of the 100s timeouts in production).  A
    threading.Lock works across loops and satisfies the `async with` usage.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    async def __aenter__(self) -> "_AsyncLockAdapter":
        await asyncio.to_thread(self._lock.acquire)
        return self

    async def __aexit__(self, *exc) -> None:
        self._lock.release()


def _patch_qobuz_lock() -> None:
    try:
        from SpotiFLAC.providers import qobuz
    except ImportError:
        return
    if getattr(qobuz.QobuzProvider, "_spoty_loop_lock_patched", False):
        return
    qobuz.QobuzProvider._spoty_loop_lock_patched = True
    _orig_init = qobuz.QobuzProvider.__init__

    def _patched_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        self._creds_lock = _AsyncLockAdapter()

    qobuz.QobuzProvider.__init__ = _patched_init


def _patch_community_session() -> None:
    """Fail fast on an expired/invalid qobuz community session.

    ensure_community_session() (SpotiFLAC core/signed_session_desktop.py)
    triggers interactive verification (browser grant / solver / terminal
    input) whenever the stored session is dead or expired.  In this headless
    bot none of those can complete, so the verification thread holds
    community_session_mu and runs until COMMUNITY_VERIFY_TIMEOUT (300s) —
    per-track wait_for aborts at PER_TRACK_TIMEOUT (100s) but the leaked
    thread keeps running, stalling the asyncio.run teardown (the recurring
    'executor did not finish joining its threads within 300 seconds').  Each
    track burns 100s before failing.

    A valid session is still used as-is (returns fast, no verification); an
    invalid one raises immediately so the track fails in seconds and the
    other qobuz APIs (gdstudio/wjhe/squid/...) can win the parallel race.
    The only way to get a valid session headless is the desktop-export bridge
    (config.bridge_community_session), which is the supported flow.
    """
    try:
        from SpotiFLAC.core import signed_session_desktop as ssd
    except ImportError:
        return
    if getattr(ssd, "_spoty_loop_community_patched", False):
        return
    ssd._spoty_loop_community_patched = True
    _orig_ensure = ssd.ensure_community_session

    def _patched_ensure():
        record = ssd.load_community_session()
        if not ssd.community_session_valid(record):
            raise RuntimeError(
                "qobuz community session missing/expired — export one from the "
                "desktop SpotiFLAC and bridge it (community_sessions.json)",
            )
        return _orig_ensure()

    ssd.ensure_community_session = _patched_ensure

    # Import-copy gotcha: providers do `from ...signed_session_desktop import
    # ensure_community_session`, so overwrite the already-copied name too.
    for _mod_name, _mod in list(sys.modules.items()):
        if not _mod_name.startswith("SpotiFLAC"):
            continue
        if hasattr(_mod, "ensure_community_session"):
            setattr(_mod, "ensure_community_session", _patched_ensure)


def _patch_musicbrainz() -> None:
    """No-op SpotiFLAC's MusicBrainz lookup at download time.

    Every provider (qobuz, deezer, amazon, apple_music, tidal, gdstudio,
    pandora, youtube) looks up MusicBrainz by ISRC during download and embeds
    the result via extra_tags=mb_tags: MUSICBRAINZ_TRACKID/ALBUMID/ARTISTID/
    RELEASEGROUPID/ALBUMARTISTID plus barcode/label/country/sort/etc. The IDs
    are often inconsistent between tracks of the same album → Navidrome groups
    the album into multiple releases — exactly what /fixmetadata exists to
    strip. So fresh downloads keep reproducing the problem /fixmetadata fixes.

    No-op the whole lookup so downloads match /fixmetadata output:
    mb_result_to_tags always returns {} (covers every provider),
    AsyncMBFetch never spawns a thread, fetch_mb_metadata_async returns {}
    (skips the ~12s/track MB network call). Enrichment providers
    (apple/deezer/soundcloud) still supply genre/label/bpm/UPC.
    """
    try:
        from SpotiFLAC.core import musicbrainz as mb
    except ImportError:
        return
    if getattr(mb, "_spoty_loop_mb_patched", False):
        return
    mb._spoty_loop_mb_patched = True

    def _noop_tags(*a, **kw) -> dict:
        return {}

    import concurrent.futures

    _done = concurrent.futures.Future()
    _done.set_result(None)

    def _noop_init(self, isrc: str) -> None:
        self.isrc = isrc
        self.future = _done

    async def _noop_async(*a, **kw) -> dict:
        return {}

    mb.mb_result_to_tags = _noop_tags
    mb.AsyncMBFetch.__init__ = _noop_init
    mb.fetch_mb_metadata_async = _noop_async

    # Import-copy gotcha: providers do `from ...musicbrainz import ...`, so
    # overwrite the already-copied names too (same sweep as console silencing).
    for _mod_name, _mod in list(sys.modules.items()):
        if not _mod_name.startswith("SpotiFLAC"):
            continue
        for _name in ("mb_result_to_tags", "fetch_mb_metadata_async"):
            if hasattr(_mod, _name):
                setattr(_mod, _name, getattr(mb, _name))


def disable_progress_manager():
    """Neutralize SpotiFLAC's ProgressManager + console interception once.

    ProgressManager keeps class-level asyncio state (_event_queue, _worker_task)
    bound to the first event loop that touched it.  The bot runs each download
    in its own thread/loop (asyncio.to_thread + asyncio.run), so the shared
    queue ends up 'bound to a different event loop' on every subsequent job,
    flooding the log.  We never use its tqdm bars, so make it a no-op.

    SpotiFLAC's install_console_interception() (called once per track
    download) strips every StreamHandler — including ours — off the root
    logger and adds a TqdmLoggingHandler that is never removed.  Handlers
    pile up on root one per track, so every log line prints N times in
    SpotiFLAC's format and spoty_loop.log stops growing.  Neutralize it in
    both modules that reference it (core.progress and downloader).
    """
    try:
        from SpotiFLAC.core import progress
        from SpotiFLAC.core.progress import ProgressManager
        import SpotiFLAC.downloader as sf_downloader
    except ImportError:
        return
    ProgressManager._event_queue = None
    ProgressManager._worker_task = None
    ProgressManager._bars = {}
    ProgressManager._slot_map = {}
    ProgressManager._master_bar = None
    ProgressManager.enqueue_progress = lambda *a, **kw: None
    ProgressManager.start_worker = lambda *a, **kw: None
    ProgressManager.initialize_master_bar = lambda *a, **kw: None

    for _mod in (progress, sf_downloader):
        _mod.install_console_interception = lambda *a, **kw: None
        _mod.uninstall_console_interception = lambda *a, **kw: None


def reset_progress_manager():
    """Detach ProgressManager's class-level state before using SpotiFLAC in a new loop."""
    try:
        from SpotiFLAC.core.progress import ProgressManager
    except ImportError:
        return
    ProgressManager._event_queue = None
    ProgressManager._worker_task = None


def silence_spotiflac_loggers() -> None:
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("SpotiFLAC"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
    for noisy in ("httpx", "httpcore", "nodriver"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def install_all() -> None:
    """Apply every patch (idempotent).

    Runs at import time (below).  spotiflac_loader re-calls it after an idle
    unload so a re-imported SpotiFLAC stack is patched before use.
    """
    install_console_silencing()
    _patch_qobuz_lock()
    _patch_community_session()
    _patch_musicbrainz()
    disable_progress_manager()


install_all()
