"""Neutralize SpotiFLAC's noisy/leaky internals (monkey-patches).

SpotiFLAC 5.x removed the native provider implementations: every download now
goes through a JavaScript extension.  The old provider-specific patches
(Qobuz lock, community session, provider timeouts, fuzzy-match guards) no
longer have a target and were deleted.

What remains is the subset that still matters for a headless bot:

* skip the GitHub "latest version" call at import time;
* silence console/tqdm output and interactive prompts;
* no-op MusicBrainz tags (Navidrome album grouping gets unstable IDs);
* keep the shared ProgressManager out of the way of our own event loops.

Importing this module applies everything once.  `download_job.py` imports it
before importing downloader/SpotiFLAC for exactly that side effect.
"""

from __future__ import annotations

import builtins
import logging
import sys

_CONSOLE_PRINTS = (
    "print_source_banner",
    "print_api_failure",
    "print_quality_fallback",
    "print_track_header",
    "print_summary",
    "print_official_source",
)

logger = logging.getLogger("spoty_loop.patch")


def _skip_github_version_check() -> None:
    """Stop SpotiFLAC from calling the GitHub API on every import.

    ``core/signed_session_desktop.py`` runs ``APP_VERSION = fetch_latest_version()``
    at import time: a blocking request to api.github.com in every download
    subprocess.  APP_VERSION only feeds the legacy community-session signature,
    which this bot does not use (the JS extensions use zarz-v2 sessions).
    """

    if "SpotiFLAC.core.signed_session_desktop" in sys.modules:
        return
    try:
        import requests
    except ImportError:  # pragma: no cover - SpotiFLAC always ships requests
        return

    class _NoRelease:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {}

    real_get = requests.get

    def _get(url, *a, **kw):
        if str(url).startswith("https://api.github.com/"):
            return _NoRelease()
        return real_get(url, *a, **kw)

    requests.get = _get
    try:
        import SpotiFLAC.core.signed_session_desktop  # noqa: F401
    finally:
        requests.get = real_get


def install_console_silencing() -> None:
    """Permanently no-op SpotiFLAC's console output and interactive prompts.

    SpotiFLAC call sites use module-level copies (``from .core.console import
    print_summary``), so patching the console module alone is not enough; the
    sweep below updates every already-imported SpotiFLAC module too.
    ``builtins.input`` is stubbed because the bot runs without a TTY and a
    stray prompt would block the download subprocess forever.
    """
    try:
        import SpotiFLAC.core.console as _console
        import SpotiFLAC.core.progress as _progress
    except ImportError:
        return

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


def _patch_musicbrainz() -> None:
    """No-op SpotiFLAC's MusicBrainz lookup at download time.

    The IDs are often inconsistent between tracks of the same album, which
    makes Navidrome split a release.  ``/fixmetadata`` already strips them, so
    fresh downloads should not re-introduce them.  Enrichment providers
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

    # Import-copy gotcha: modules do `from ...musicbrainz import ...`.
    for _mod_name, _mod in list(sys.modules.items()):
        if not _mod_name.startswith("SpotiFLAC"):
            continue
        for _name in ("mb_result_to_tags", "fetch_mb_metadata_async"):
            if hasattr(_mod, _name):
                setattr(_mod, _name, getattr(mb, _name))


def disable_progress_manager() -> None:
    """Neutralize SpotiFLAC's ProgressManager + console interception.

    The bot runs each download in its own subprocess/event loop; the shared
    class-level tqdm state is pointless there.  The console interception also
    strips every root StreamHandler (including ours) and adds a handler that
    is never removed, so log lines pile up job after job.
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


def reset_progress_manager() -> None:
    """Detach ProgressManager's class-level state before a new event loop."""
    try:
        from SpotiFLAC.core.progress import ProgressManager
    except ImportError:
        return
    ProgressManager._event_queue = None
    ProgressManager._worker_task = None


_skip_github_version_check()
install_console_silencing()
_patch_musicbrainz()
disable_progress_manager()
