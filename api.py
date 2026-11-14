#!/usr/bin/env python3
"""HTTP API for the Laila download tab.

Stdlib only. It runs either as a standalone process or in a daemon thread
inside bot.py (RUN_API=1). It shares the SQLite queue (WAL). Laila searches
Deezer directly in this process; no SpotiFLAC import and no search subprocess.
Telegram keeps its own Spotify search path and is unaffected.

Endpoints (all but covers require `Authorization: Bearer $API_TOKEN`):

    GET  /api/health
    GET  /api/search?q=...&limit=20
    POST /api/queue        {"urls": ["https://www.deezer.com/track/..."]}
    GET  /api/cover?u=...  (Deezer/Spotify CDN allowlist, no auth)

The cover endpoint is intentionally open so `<img>`/expo-image can load it;
keep this service on LAN/Tailscale, never on the public internet.
"""

from __future__ import annotations

import hmac
import http.client
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from config import first_allowed_user_id, load_env, setup_logger
from deezer_search import DeezerError, search as deezer_search
from queue_manager import QueueManager
from spotify_credentials import (
    CredentialsError,
    effective_credentials,
    fetch_client_credentials_token,
)

ROOT = Path(__file__).parent.absolute()
SEARCH_SCRIPT = ROOT / "search_job.py"

MAX_BODY = 64 * 1024
MAX_QUEUE_URLS = 100
MAX_QUERY_LENGTH = 200
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024
COVER_TIMEOUT = 10
# Caches are bounded by memory, not by "nice to have": in RUN_API mode they
# live in the bot process, so 32 results and 8 MiB of covers are plenty.
SEARCH_CACHE_ENTRIES = 32
COVER_CACHE_ENTRIES = 32
COVER_CACHE_BYTES = 8 * 1024 * 1024

_HTTP_LINK_RE = re.compile(
    r"^https?://open\.spotify\.com/(track|album|playlist)/([A-Za-z0-9]{22})(?:[/?#].*)?$"
)
_SPOTIFY_URI_RE = re.compile(r"^spotify:(track|album|playlist):([A-Za-z0-9]{22})$")
_DEEZER_LINK_RE = re.compile(
    r"^https?://(?:www\.)?deezer\.com/(?:[a-z]{2}/)?"
    r"(track|album|playlist)/([0-9]+)(?:[/?#].*)?$"
)
_COVER_HOST_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:scdn\.co|spotifycdn\.com|dzcdn\.net)$"
)
_COVER_PATH_RE = re.compile(r"^/[A-Za-z0-9/_.%+-]{1,400}$")
_COVER_QUERY_RE = re.compile(r"^[A-Za-z0-9=&_.%+-]{0,200}$")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


class SearchError(Exception):
    def __init__(self, kind: str, status: int, message: str):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Search: cache + single-flight around the subprocess
# ---------------------------------------------------------------------------


class SpotifyAuth:
    """Optional Spotify credentials for the official search API.

    Reads the file written by the Telegram `/spotify` commands on every search,
    so changes apply without restarting the API. Without creds, search falls
    back to SpotiFLAC's anonymous web token. Tokens never reach Laila or logs.
    """

    def __init__(self, credentials_path=None, fetcher=None):
        self._path = credentials_path
        self._fetcher = fetcher or fetch_client_credentials_token
        self._token: str = ""
        self._expires_at = 0.0
        self._key: tuple[str, str] | None = None
        self._market = ""
        self._source = "none"
        self._lock = threading.Lock()

    def _read(self) -> dict:
        data = effective_credentials(self._path)
        self._market = data["market"]
        self._source = data["source"]
        return data

    @property
    def mode(self) -> str:
        return self._read()["mode"]

    @property
    def market(self) -> str:
        return self._market

    @property
    def source(self) -> str:
        return self._source

    def can_refresh(self) -> bool:
        return self.mode == "client"

    def search_spec(self) -> tuple[str, str]:
        """Return (search_job mode, token); anonymous has no token."""
        data = self._read()
        mode = data["mode"]
        if mode == "anonymous":
            return ("anonymous", "")
        if mode == "token":
            return ("official", data["access_token"])

        key = (data["client_id"], data["client_secret"])
        with self._lock:
            if key != self._key or not self._token or time.monotonic() >= self._expires_at:
                try:
                    self._token = self._fetcher(*key)
                except CredentialsError as exc:
                    raise SearchError(
                        "spotify_auth_failed", 502, str(exc)
                    ) from exc
                self._key = key
                self._expires_at = time.monotonic() + 3300
            return ("official", self._token)

    def invalidate(self) -> None:
        with self._lock:
            self._token = ""
            self._expires_at = 0.0
            self._key = None


def run_search_job(
    query: str,
    limit: int,
    timeout: int = 20,
    spotify_mode: str = "anonymous",
    spotify_token: str = "",
    spotify_market: str = "",
) -> dict:
    """Run search_job.py and return its parsed payload."""
    spec_payload = {
        "query": query,
        "limit": limit,
        "spotify_mode": spotify_mode,
        "spotify_market": spotify_market,
    }
    if spotify_token:
        spec_payload["spotify_token"] = spotify_token
    spec = json.dumps(spec_payload)
    try:
        proc = subprocess.run(
            [sys.executable, str(SEARCH_SCRIPT)],
            input=spec,
            text=True,
            capture_output=True,
            cwd=str(ROOT),
            timeout=timeout,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise SearchError("timeout", 504, "Spotify search timed out") from exc

    line = ""
    for candidate in reversed(proc.stdout.splitlines()):
        if candidate.strip():
            line = candidate
            break
    try:
        payload = json.loads(line)
    except (ValueError, TypeError) as exc:
        raise SearchError(
            "bad_response", 502, "Search subprocess did not return JSON"
        ) from exc

    if not payload.get("ok"):
        kind = str(payload.get("kind") or "search_failed")
        message = str(payload.get("error") or "Search failed")
        status = 503 if kind == "spotify_blocked" else 400 if kind == "invalid_request" else 502
        raise SearchError(kind, status, message)
    return payload


class SearchRunner:
    """TTL cache plus a lock so concurrent queries never fan out to Spotify."""

    def __init__(self, runner=run_search_job, ttl: int = 60, max_entries: int = SEARCH_CACHE_ENTRIES):
        self._runner = runner
        self._ttl = max(0, ttl)
        self._max_entries = max_entries
        self._cache: OrderedDict[tuple[str, int], tuple[float, dict]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._run_lock = threading.Lock()

    def search(self, query: str, limit: int) -> dict:
        key = (query.strip().lower(), limit)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                self._cache.move_to_end(key)
                return cached[1]

        # Serialize cache misses: one search at a time, but cache hits above
        # still return immediately.
        with self._run_lock:
            with self._cache_lock:
                cached = self._cache.get(key)
                if cached and cached[0] > time.monotonic():
                    return cached[1]
            result = self._runner(query, limit)
            with self._cache_lock:
                now = time.monotonic()
                # TTL only means "not fresh"; purge expired entries so they do
                # not stay resident until LRU eviction.
                expired = [
                    k for k, (expires, _) in self._cache.items()
                    if expires <= now
                ]
                for stale in expired:
                    del self._cache[stale]
                self._cache[key] = (now + self._ttl, result)
                self._cache.move_to_end(key)
                while len(self._cache) > self._max_entries:
                    self._cache.popitem(last=False)
        return result


def make_search_provider(auth: SpotifyAuth, timeout: int):
    """Build the runner used by SearchRunner, with one auth retry."""

    def run(query: str, limit: int) -> dict:
        for attempt in (0, 1):
            try:
                mode, token = auth.search_spec()
                return run_search_job(
                    query,
                    limit,
                    timeout=timeout,
                    spotify_mode=mode,
                    spotify_token=token,
                    spotify_market=auth.market,
                )
            except SearchError as exc:
                if (
                    exc.kind == "spotify_auth_failed"
                    and auth.can_refresh()
                    and attempt == 0
                ):
                    auth.invalidate()
                    continue
                raise
        raise SearchError("spotify_auth_failed", 502, "Spotify credentials failed")

    return run


def make_deezer_provider(timeout: int):
    """Build the Deezer runner used by the Laila tab.

    The legacy Spotify provider above stays available for tests and manual
    rollback, but the API defaults to Deezer: no credentials, no subprocess
    and no SpotiFLAC import in the parent.
    """

    def run(query: str, limit: int) -> dict:
        try:
            return deezer_search(query, limit, timeout=timeout)
        except DeezerError as exc:
            text = str(exc).lower()
            blocked = "quota" in text or "429" in text or "too many requests" in text
            kind = "deezer_blocked" if blocked else "deezer_unavailable"
            raise SearchError(kind, 503 if blocked else 502, str(exc)) from exc

    return run


# ---------------------------------------------------------------------------
# Cover proxy
# ---------------------------------------------------------------------------


def allowed_cover_url(url: str) -> bool:
    """Only Deezer/Spotify CDN image URLs, https, sane path/query."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    host = parsed.hostname or ""
    if not _COVER_HOST_RE.fullmatch(host):
        return False
    if parsed.port not in (None, 443):
        return False
    if not _COVER_PATH_RE.fullmatch(parsed.path):
        return False
    if ".." in parsed.path.split("/"):
        return False
    if parsed.query and not _COVER_QUERY_RE.fullmatch(parsed.query):
        return False
    if parsed.fragment:
        return False
    return True


def cover_path(url: str) -> str:
    """Relative proxy path for a validated Spotify cover URL."""
    return "/api/cover?u=" + quote(url, safe="")


def fetch_cover(url: str, timeout: int = COVER_TIMEOUT) -> tuple[bytes, str]:
    """Fetch a validated image without following redirects."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path
    if parsed.query:
        path += "?" + parsed.query
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        conn.request(
            "GET",
            path,
            headers={
                "User-Agent": "spoty_loop-api/1.0",
                "Accept": "image/*",
            },
        )
        response = conn.getresponse()
        if response.status != 200:
            raise ApiError(404, "cover_unavailable", "Cover not available")
        content_type = response.getheader("Content-Type", "")
        if not content_type.lower().startswith("image/"):
            raise ApiError(404, "cover_unavailable", "Cover is not an image")
        data = response.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ApiError(404, "cover_unavailable", "Cover is too large")
        if not data:
            raise ApiError(404, "cover_unavailable", "Cover is empty")
        return data, content_type
    except (OSError, http.client.HTTPException) as exc:
        raise ApiError(404, "cover_unavailable", "Cover fetch failed") from exc
    finally:
        conn.close()


class CoverCache:
    """Small in-memory LRU so scrolling does not refetch the same covers."""

    def __init__(
        self,
        fetcher=fetch_cover,
        max_entries: int = COVER_CACHE_ENTRIES,
        max_bytes: int = COVER_CACHE_BYTES,
    ):
        self._fetch = fetcher
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._bytes = 0

    def get(self, url: str) -> tuple[bytes, str]:
        if not allowed_cover_url(url):
            raise ApiError(404, "cover_unavailable", "Cover host is not allowed")
        with self._lock:
            item = self._items.get(url)
            if item is not None:
                self._items.move_to_end(url)
                return item
        data, content_type = self._fetch(url)
        with self._lock:
            self._items[url] = (data, content_type)
            self._bytes += len(data)
            while len(self._items) > self._max_entries or self._bytes > self._max_bytes:
                _, (old_data, _) = self._items.popitem(last=False)
                self._bytes -= len(old_data)
        return data, content_type


# ---------------------------------------------------------------------------
# Application logic
# ---------------------------------------------------------------------------


@dataclass
class ApiApp:
    token: str
    queue: QueueManager
    search: SearchRunner
    covers: CoverCache
    cors_origins: tuple[str, ...] = ()
    queue_user: int | None = None
    version: str = "dev"
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("spoty_loop.api"))

    # -- search ---------------------------------------------------------

    def search_results(self, query: str, limit: int) -> dict:
        raw = self.search.search(query, limit)
        return {
            "query": query,
            "tracks": [
                {
                    "id": item.get("id", ""),
                    "title": item.get("title", ""),
                    "artists": item.get("artists", ""),
                    "album": item.get("album", ""),
                    "durationMs": int(item.get("durationMs") or 0),
                    "url": item.get("url", ""),
                    "coverUrl": self._public_cover(item.get("cover", "")),
                    "explicit": bool(item.get("explicit")),
                    "isrc": item.get("isrc", ""),
                }
                for item in raw.get("tracks", [])
            ],
            "albums": [
                {
                    "id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "artists": item.get("artists", ""),
                    "releaseDate": item.get("releaseDate", ""),
                    "url": item.get("url", ""),
                    "coverUrl": self._public_cover(item.get("cover", "")),
                }
                for item in raw.get("albums", [])
            ],
            "playlists": [
                {
                    "id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "owner": item.get("owner", ""),
                    "url": item.get("url", ""),
                    "coverUrl": self._public_cover(item.get("cover", "")),
                }
                for item in raw.get("playlists", [])
            ],
        }

    def _public_cover(self, url) -> str | None:
        if not isinstance(url, str) or not url or not allowed_cover_url(url):
            return None
        return cover_path(url)

    # -- queue ----------------------------------------------------------

    def enqueue(self, urls: list) -> dict:
        if not isinstance(urls, list) or not urls:
            raise ApiError(400, "invalid_request", "urls must be a non-empty list")
        if len(urls) > MAX_QUEUE_URLS:
            raise ApiError(400, "invalid_request", f"at most {MAX_QUEUE_URLS} urls per request")
        if len(urls) > 0 and not all(isinstance(item, str) for item in urls):
            raise ApiError(400, "invalid_request", "every url must be a string")

        items = []
        queued = duplicates = invalid = 0
        seen: set[str] = set()
        for value in urls:
            original = value.strip()
            canonical = canonical_spotify_url(original) or canonical_deezer_url(original)
            if canonical is None:
                invalid += 1
                items.append({"url": original[:200], "status": "invalid"})
                continue
            if canonical in seen:
                duplicates += 1
                items.append({"url": canonical, "status": "duplicate"})
                continue
            seen.add(canonical)
            item_id, is_new = self.queue.enqueue_unique("link", canonical, self.queue_user)
            if is_new:
                queued += 1
                items.append({"url": canonical, "status": "queued", "id": item_id})
            else:
                duplicates += 1
                items.append({"url": canonical, "status": "duplicate", "id": item_id})

        return {
            "queued": queued,
            "duplicates": duplicates,
            "invalid": invalid,
            "items": items,
        }


def canonical_spotify_url(value: str) -> str | None:
    """Accept a clean Spotify link/URI and return a canonical https URL.

    Only track/album/playlist: an artist link would queue a discography.
    Query params (si=..., utm_...) are dropped so the same track dedupes.
    """
    if not value or len(value) > 500:
        return None
    match = _HTTP_LINK_RE.fullmatch(value)
    if match:
        kind, spotify_id = match.groups()
        return f"https://open.spotify.com/{kind}/{spotify_id}"
    match = _SPOTIFY_URI_RE.fullmatch(value)
    if match:
        kind, spotify_id = match.groups()
        return f"https://open.spotify.com/{kind}/{spotify_id}"
    return None


def canonical_deezer_url(value: str) -> str | None:
    """Accept a clean Deezer link and return a canonical https URL."""
    if not value or len(value) > 500:
        return None
    match = _DEEZER_LINK_RE.fullmatch(value.strip())
    if not match:
        return None
    kind, deezer_id = match.groups()
    return f"https://www.deezer.com/{kind}/{deezer_id}"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _parse_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_request", "limit must be an integer")
    return max(minimum, min(maximum, number))


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 20
    server_version = "spoty_loop-api/1.0"

    @property
    def app(self) -> ApiApp:
        return self.server.app  # type: ignore[attr-defined]

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        logging.getLogger("spoty_loop.api").debug(fmt, *args)

    def _dispatch(self, method: str) -> None:
        origin = self.headers.get("Origin")
        parsed = urlparse(self.path)
        try:
            if method == "OPTIONS":
                self._send_empty(204, origin)
                return

            if parsed.path == "/api/health":
                self._require_auth()
                if method != "GET":
                    raise ApiError(405, "method_not_allowed", "Use GET")
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "service": "spoty_loop",
                        "version": self.app.version,
                    },
                    origin,
                )
                return

            if parsed.path == "/api/search":
                self._require_auth()
                if method != "GET":
                    raise ApiError(405, "method_not_allowed", "Use GET")
                params = parse_qs(parsed.query, keep_blank_values=True)
                query = (params.get("q") or [""])[0].strip()
                if not query:
                    raise ApiError(400, "invalid_request", "q is required")
                if len(query) > MAX_QUERY_LENGTH:
                    raise ApiError(400, "invalid_request", "q is too long")
                limit = _parse_int((params.get("limit") or [None])[0], DEFAULT_LIMIT, 1, MAX_LIMIT)
                self._send_json(200, self.app.search_results(query, limit), origin)
                return

            if parsed.path == "/api/queue":
                self._require_auth()
                if method != "POST":
                    raise ApiError(405, "method_not_allowed", "Use POST")
                body = self._read_json()
                self._send_json(200, self.app.enqueue(body.get("urls")), origin)
                return

            if parsed.path == "/api/cover":
                if method != "GET":
                    raise ApiError(405, "method_not_allowed", "Use GET")
                params = parse_qs(parsed.query, keep_blank_values=True)
                target = (params.get("u") or [""])[0]
                data, content_type = self.app.covers.get(target)
                self._send_bytes(200, data, content_type, origin)
                return

            raise ApiError(404, "not_found", "Unknown endpoint")
        except ApiError as exc:
            self._send_json(
                exc.status,
                {"error": exc.code, "message": exc.message},
                origin,
                extra_headers=exc.headers,
            )
        except SearchError as exc:
            headers = (
                {"Retry-After": "1800"}
                if exc.kind in ("spotify_blocked", "deezer_blocked")
                else None
            )
            self._send_json(
                exc.status,
                {"error": exc.kind, "message": exc.message},
                origin,
                extra_headers=headers,
            )
        except Exception:  # noqa: BLE001 - never leak a traceback to the client
            self.app.logger.exception("Unhandled API error")
            self._send_json(
                500,
                {"error": "internal_error", "message": "Unexpected server error"},
                origin,
            )

    # -- helpers --------------------------------------------------------

    def _require_auth(self) -> None:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise ApiError(
                401,
                "unauthorized",
                "Missing bearer token",
                {"WWW-Authenticate": "Bearer"},
            )
        supplied = header[7:].strip()
        if not supplied or not hmac.compare_digest(supplied, self.app.token):
            raise ApiError(
                401,
                "unauthorized",
                "Invalid bearer token",
                {"WWW-Authenticate": "Bearer"},
            )

    def _read_json(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.close_connection = True
            raise ApiError(411, "length_required", "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError:
            self.close_connection = True
            raise ApiError(400, "invalid_request", "Invalid Content-Length")
        if length <= 0:
            self.close_connection = True
            raise ApiError(400, "invalid_request", "Empty body")
        if length > MAX_BODY:
            # The body is still on the socket: keeping the connection alive
            # would parse it as the next request.
            self.close_connection = True
            raise ApiError(413, "payload_too_large", "Body is too large")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "invalid_request", "Body must be JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_request", "Body must be a JSON object")
        return body

    def _cors_headers(self, origin: str | None) -> list[tuple[str, str]]:
        if not origin or origin not in self.app.cors_origins:
            return []
        return [
            ("Access-Control-Allow-Origin", origin),
            ("Vary", "Origin"),
            ("Access-Control-Allow-Headers", "Authorization, Content-Type"),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Max-Age", "600"),
        ]

    def _send_empty(self, status: int, origin: str | None) -> None:
        self.send_response(status)
        for key, value in self._cors_headers(origin):
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(
        self,
        status: int,
        payload: dict,
        origin: str | None,
        extra_headers: dict | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for key, value in self._cors_headers(origin):
            self.send_header(key, value)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, data: bytes, content_type: str, origin: str | None) -> None:
        self.send_response(status)
        for key, value in self._cors_headers(origin):
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: ApiApp):
        self.app = app
        super().__init__(address, ApiHandler)


def create_server(bind: str, port: int, app: ApiApp) -> ApiServer:
    return ApiServer((bind, port), app)


def _queue_user() -> int | None:
    raw = os.environ.get("API_QUEUE_USER", "").strip()
    if raw.isdigit():
        return int(raw)
    return first_allowed_user_id()


@dataclass
class ApiRuntime:
    app: ApiApp
    bind: str
    port: int
    queue_path: str
    search_provider: str = "deezer"


def build_runtime(
    logger: logging.Logger, queue: QueueManager | None = None
) -> ApiRuntime | None:
    """Build the API from the current environment; None when disabled/invalid.

    `queue` lets RUN_API mode reuse the bot's connection instead of opening a
    second one on the same SQLite file.
    """
    token = os.environ.get("API_TOKEN", "").strip()
    if not token:
        logger.error("API_TOKEN is required; refusing to start the HTTP API")
        return None

    bind = os.environ.get("API_BIND", "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = int(os.environ.get("API_PORT", "8099"))
    except ValueError:
        logger.error("API_PORT must be an integer")
        return None
    try:
        ttl = int(os.environ.get("SEARCH_CACHE_TTL", "60"))
    except ValueError:
        ttl = 60
    try:
        timeout = int(os.environ.get("SEARCH_TIMEOUT", "20"))
    except ValueError:
        timeout = 20

    origins = tuple(
        part.strip()
        for part in os.environ.get("API_CORS_ORIGINS", "").split(",")
        if part.strip()
    )
    queue_path = os.environ.get("QUEUE_DB_PATH", str(ROOT / "queue.db"))
    if queue is None:
        queue = QueueManager(queue_path)
    queue_path = queue.path
    app = ApiApp(
        token=token,
        queue=queue,
        search=SearchRunner(
            runner=make_deezer_provider(timeout),
            ttl=ttl,
        ),
        covers=CoverCache(),
        cors_origins=origins,
        queue_user=_queue_user(),
        version=os.environ.get("BUILD_COMMIT", "dev"),
        logger=logger,
    )
    return ApiRuntime(
        app=app,
        bind=bind,
        port=port,
        queue_path=queue_path,
        search_provider="deezer",
    )


def start_api_thread(
    logger: logging.Logger, queue: QueueManager | None = None
) -> ApiServer | None:
    """Start the API in a daemon thread (single-container / TrueNAS mode)."""
    runtime = build_runtime(logger, queue=queue)
    if runtime is None:
        return None
    server = create_server(runtime.bind, runtime.port, runtime.app)
    thread = threading.Thread(
        target=server.serve_forever,
        name="spoty-loop-api",
        daemon=True,
    )
    thread.start()
    logger.info(
        "API thread listening on %s:%s (queue=%s, user=%s, search=%s)",
        runtime.bind,
        runtime.port,
        runtime.queue_path,
        runtime.app.queue_user,
        runtime.search_provider,
    )
    return server


def main() -> int:
    load_env(ROOT / ".env")
    logger = setup_logger(ROOT / "api.log")
    runtime = build_runtime(logger)
    if runtime is None:
        return 1
    server = create_server(runtime.bind, runtime.port, runtime.app)
    logger.info(
        "API listening on %s:%s (queue=%s, user=%s, search=%s)",
        runtime.bind,
        runtime.port,
        runtime.queue_path,
        runtime.app.queue_user,
        runtime.search_provider,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
