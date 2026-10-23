"""Neutralize SpotiFLAC's noisy/leaky internals (monkey-patches).

Kept in one module so the hacks are easy to find and review.  Importing this
module runs `install_console_silencing()`, `_patch_qobuz_lock()`,
`_patch_community_dead()`, `_patch_musicbrainz()`, `_patch_track_provider()`,
`_patch_provider_matching()` and `disable_progress_manager()` once —
downloader.py relies on that side-effect (regression-tested in
tests/test_downloader.py).
"""

from __future__ import annotations

import asyncio
import builtins
import difflib
import logging
import re
import sys
import threading
import unicodedata

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


# ---------------------------------------------------------------------------
# Provider track matching
#
# SpotiFLAC 1.6.0 falls back to fuzzy text search when the ISRC is not in a
# provider catalog.  Both fallbacks are unsafe:
#   * QobuzProvider._search_by_text_async returns its best candidate even below
#     its own score threshold (and the album-search loop never re-checks it).
#   * QobuzProvider._search_by_text_with_duration_async picks the closest
#     duration with no title/artist check at all.
# For "Noi no" by Articolo 31 (ISRC ITA270200113, absent from Qobuz) that chain
# downloaded "CHI SE NE FREGA DI NOI" and then a random Vietnamese track whose
# duration was ~5s off — with Spotify tags on top, so the file looked correct.
# The guards below reject any candidate whose title/artist (and ISRC/duration,
# when available) do not plausibly match, letting SpotiFLAC fall through to the
# next provider (Deezer does an exact ISRC lookup and had the right track).
# ---------------------------------------------------------------------------

_match_logger = logging.getLogger("spoty_loop.patch")

_MATCH_TITLE_RATIO = 0.82
_MATCH_ARTIST_RATIO = 0.75
_MATCH_DURATION_TOLERANCE_S = 10
_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|&|/|;|\bfeat\.?\b|\bft\.?\b|\bwith\b)\s*",
    re.IGNORECASE,
)
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")


def _normalize_for_match(text) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace."""
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _title_matches(expected: str, candidate: str) -> bool:
    exp = _normalize_for_match(expected)
    cand = _normalize_for_match(candidate)
    if not exp or not cand:
        return False
    if exp == cand:
        return True
    # Containment catches "(Remastered)"/"(feat. X)" suffixes, but only for
    # long enough strings: "Noi" must not match "Noi no" just by prefix.
    if min(len(exp), len(cand)) >= 4 and (exp in cand or cand in exp):
        return True
    return difflib.SequenceMatcher(None, exp, cand).ratio() >= _MATCH_TITLE_RATIO


def _artist_matches(expected: str, candidate: str) -> bool:
    cand = _normalize_for_match(candidate)
    if not cand:
        return False
    for part in _ARTIST_SPLIT_RE.split(str(expected or "")):
        exp = _normalize_for_match(part)
        if not exp:
            continue
        if exp == cand or exp in cand or cand in exp:
            return True
        if difflib.SequenceMatcher(None, exp, cand).ratio() >= _MATCH_ARTIST_RATIO:
            return True
    return False


def _candidate_artist(track: dict) -> str:
    """Artist name from a provider track dict (qobuz/deezer shapes)."""
    for key in ("performer", "artist"):
        value = track.get(key)
        if isinstance(value, dict) and value.get("name"):
            return str(value["name"])
        if isinstance(value, str) and value:
            return value
    album = track.get("album")
    if isinstance(album, dict):
        artist = album.get("artist")
        if isinstance(artist, dict) and artist.get("name"):
            return str(artist["name"])
    return ""


def _normalize_isrc_for_match(value: str) -> str:
    text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return text if _ISRC_RE.match(text) else ""


def _track_matches(
    track: dict,
    title: str,
    artist: str,
    duration_s: int | None = None,
) -> tuple[bool, str]:
    """True when a provider candidate is plausibly the requested track."""
    candidate_title = str(track.get("title") or "")
    candidate_artist = _candidate_artist(track)
    if not _title_matches(title, candidate_title):
        return False, f"title {candidate_title!r} vs {title!r}"
    if artist and not _artist_matches(artist, candidate_artist):
        return False, f"artist {candidate_artist!r} vs {artist!r}"
    if duration_s:
        candidate_duration = track.get("duration") or 0
        if (
            isinstance(candidate_duration, (int, float))
            and candidate_duration > 0
            and abs(candidate_duration - duration_s) > _MATCH_DURATION_TOLERANCE_S
        ):
            return False, f"duration {candidate_duration}s vs {duration_s}s"
    return True, ""


def _isrc_matches(expected: str, candidate: str) -> bool:
    exp = _normalize_isrc_for_match(expected)
    cand = _normalize_isrc_for_match(candidate)
    if not exp or not cand:
        return True  # nothing to compare against (candidate may omit ISRC)
    return exp == cand


def _reject(
    provider: str,
    kind: str,
    track: dict,
    title: str,
    artist: str,
    reason: str,
) -> None:
    _match_logger.warning(
        "%s %s match rejected for %r - %r: candidate %r by %r (%ss) — %s",
        provider,
        kind,
        title,
        artist,
        track.get("title"),
        _candidate_artist(track),
        track.get("duration"),
        reason,
    )


def _patch_provider_matching() -> None:
    """Reject fuzzy-search candidates that are not the requested track."""
    try:
        from SpotiFLAC.providers import deezer, qobuz
    except ImportError:
        return

    if not getattr(qobuz.QobuzProvider, "_spoty_loop_match_patched", False):
        qobuz.QobuzProvider._spoty_loop_match_patched = True

        _orig_isrc = qobuz.QobuzProvider._search_by_isrc_async

        async def _isrc_guarded(self, isrc):
            track = await _orig_isrc(self, isrc)
            if track and not _isrc_matches(isrc, track.get("isrc") or ""):
                _reject(
                    "qobuz", "isrc", track, "", "",
                    f"asked {isrc}, got {track.get('isrc')}",
                )
                return None
            return track

        _orig_text = qobuz.QobuzProvider._search_by_text_async

        async def _text_guarded(self, title, artist):
            track = await _orig_text(self, title, artist)
            if not track:
                return None
            ok, reason = _track_matches(track, title, artist)
            if not ok:
                _reject("qobuz", "text", track, title, artist, reason)
                return None
            return track

        _orig_duration = qobuz.QobuzProvider._search_by_text_with_duration_async

        async def _duration_guarded(self, title, artist, target_duration_s):
            track = await _orig_duration(self, title, artist, target_duration_s)
            if not track:
                return None
            ok, reason = _track_matches(track, title, artist, target_duration_s)
            if not ok:
                _reject("qobuz", "duration", track, title, artist, reason)
                return None
            return track

        qobuz.QobuzProvider._search_by_isrc_async = _isrc_guarded
        qobuz.QobuzProvider._search_by_text_async = _text_guarded
        qobuz.QobuzProvider._search_by_text_with_duration_async = _duration_guarded

    if not getattr(deezer.DeezerProvider, "_spoty_loop_match_patched", False):
        deezer.DeezerProvider._spoty_loop_match_patched = True

        _orig_deezer_text = deezer.DeezerProvider._search_track_text_async

        async def _deezer_text_guarded(self, title, artist):
            track = await _orig_deezer_text(self, title, artist)
            if not track:
                return None
            ok, reason = _track_matches(track, title, artist)
            if not ok:
                _reject("deezer", "text", track, title, artist, reason)
                return None
            return track

        deezer.DeezerProvider._search_track_text_async = _deezer_text_guarded


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
_patch_provider_matching()
disable_progress_manager()
