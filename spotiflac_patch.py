"""Neutralize SpotiFLAC's noisy/leaky internals (monkey-patches).

Kept in one module so the hacks are easy to find and review.  Importing this
module runs `disable_progress_manager()` once — downloader.py relies on that
side-effect (regression-tested in tests/test_downloader.py).
"""

from __future__ import annotations

import builtins
import logging
from contextlib import contextmanager


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


@contextmanager
def silence_spotiflac():
    """No-op SpotiFLAC's console prints and interactive prompts during a download."""
    import SpotiFLAC.core.console as _console
    import SpotiFLAC.core.progress as _progress

    _originals = {
        "print_source_banner": _console.print_source_banner,
        "print_api_failure": _console.print_api_failure,
        "print_quality_fallback": _console.print_quality_fallback,
        "print_track_header": _console.print_track_header,
        "print_summary": _console.print_summary,
        "print_official_source": _console.print_official_source,
        "safe_tqdm_write": _progress.safe_tqdm_write,
        "input": builtins.input,
    }

    _console.print_source_banner = lambda *a, **kw: None
    _console.print_api_failure = lambda *a, **kw: None
    _console.print_quality_fallback = lambda *a, **kw: None
    _console.print_track_header = lambda *a, **kw: None
    _console.print_summary = lambda *a, **kw: None
    _console.print_official_source = lambda *a, **kw: None
    _progress.safe_tqdm_write = lambda *a, **kw: None
    builtins.input = lambda *a, **kw: ""

    try:
        yield
    finally:
        _console.print_source_banner = _originals["print_source_banner"]
        _console.print_api_failure = _originals["print_api_failure"]
        _console.print_quality_fallback = _originals["print_quality_fallback"]
        _console.print_track_header = _originals["print_track_header"]
        _console.print_summary = _originals["print_summary"]
        _console.print_official_source = _originals["print_official_source"]
        _progress.safe_tqdm_write = _originals["safe_tqdm_write"]
        builtins.input = _originals["input"]


disable_progress_manager()
