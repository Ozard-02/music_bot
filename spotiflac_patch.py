"""Neutralize SpotiFLAC's noisy/leaky internals (monkey-patches).

Kept in one module so the hacks are easy to find and review.  Importing this
module runs `install_console_silencing()`, `_patch_qobuz_lock()`,
`_patch_community_dead()`, `_patch_musicbrainz()`, `_patch_track_provider()`
and `disable_progress_manager()` once — downloader.py relies on that
side-effect (regression-tested in tests/test_downloader.py).
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


def _patch_community_dead() -> None:
    """Kill the community-session machinery so the browser never launches.

    ensure_community_session() (SpotiFLAC core/signed_session_desktop.py) only
    ever runs on the community branches of the providers:
      * qobuz  — `_COMMUNITY_APIS` (module-level list, already built when
        `_patch_qobuz_lock()` imports the provider) feeds `qbz-oss.spotbye.qzz.io`
        into the parallel stream-fetch pool (qobuz.py:1275).
      * amazon — `get_community_url("amazon")` gates both `_do_request_with_retry`
        (`is_community`, amazon.py:275) and the first download attempt
        (`_community_ep`, amazon.py:1392/1405).
      * tidal  — `_TIDAL_COMMUNITY_URL` built at import (tidal.py:78).
    All three call sites wrap `ensure_community_session()` in try/except and fall
    back to non-community mirrors (qobuz: anandserver.cfd stream; amazon: antra/
    mono/s_stream; tidal: legacy). Production evidence (PLAN #93, 2026-08-11→14):
    the solver never succeeds — browser fails to start or the Cloudflare bypass
    times out — while downloads keep succeeding via the fallback mirrors. So the
    community path is dead weight: each attempt spawns a chromium, burns ~10-60s,
    and holds RSS (PLAN #94).

    This patch makes `ensure_community_session()` raise immediately (fast-fail,
    no browser, no cooldown window) and empties the community endpoint lists so
    the branches are never even reached. No behaviour change for the non-community
    mirrors.
    """
    import SpotiFLAC.core.endpoints as endpoints
    import SpotiFLAC.core.signed_session_desktop as ssd

    def _dead_session(*a, **kw):
        raise RuntimeError("community session disabled (spoty_loop) — using non-community mirrors")

    ssd.ensure_community_session = _dead_session
    endpoints.get_community_url = lambda _provider: ""
    try:
        from SpotiFLAC.providers import qobuz
    except ImportError:
        qobuz = None
    if qobuz is not None:
        qobuz._COMMUNITY_APIS = []

    # Import-copy gotcha: providers do `from ...endpoints import get_community_url`
    # and `from ...signed_session_desktop import ensure_community_session`, so
    # overwrite the already-copied names too (same sweep as console silencing).
    for _mod_name, _mod in list(sys.modules.items()):
        if not _mod_name.startswith("SpotiFLAC"):
            continue
        for _name in ("ensure_community_session", "get_community_url"):
            if hasattr(_mod, _name):
                setattr(_mod, _name, _dead_session if _name == "ensure_community_session" else endpoints.get_community_url)


_track_providers: dict[str, str] = {}


def _patch_track_provider() -> None:
    """Record which provider actually delivered each track download.

    SpotiFLAC's download_one_async() (module-level in SpotiFLAC.downloader,
    called bare from the same module at downloader.py:499) returns a
    DownloadResult carrying `provider`. The public download_track() discards
    it, so we wrap download_one_async and stash track.id -> provider in
    `_track_providers`; downloader.py pops entries via pop_track_provider()
    after each successful track. Single-threaded asyncio writes, no lock.
    """
    try:
        import SpotiFLAC.downloader as sf_downloader
    except ImportError:
        return
    if getattr(sf_downloader, "_spoty_loop_provider_patched", False):
        return
    sf_downloader._spoty_loop_provider_patched = True
    _orig = sf_downloader.download_one_async

    async def _patched(metadata, *args, **kwargs):
        result = await _orig(metadata, *args, **kwargs)
        if result.success and not result.skipped:
            _track_providers[metadata.id] = result.provider
        return result

    sf_downloader.download_one_async = _patched


def pop_track_provider(track_id: str) -> str | None:
    """Pop the recorded provider for a track (None when not downloaded here)."""
    return _track_providers.pop(track_id, None)


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


install_console_silencing()
_patch_qobuz_lock()
_patch_community_dead()
_patch_musicbrainz()
_patch_track_provider()
disable_progress_manager()
