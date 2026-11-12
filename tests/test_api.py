"""Tests for the HTTP API used by Laila (no network, no SpotiFLAC)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from api import (
    ApiApp,
    CoverCache,
    SearchError,
    SearchRunner,
    SpotifyAuth,
    allowed_cover_url,
    canonical_spotify_url,
    create_server,
    make_search_provider,
)
from queue_manager import QueueManager
from spotify_credentials import set_access_token, set_client_credentials

TOKEN = "test-token"
ORIGIN = "http://localhost:8081"


class Client:
    _DEFAULT = object()

    def __init__(self, base: str, token: str = TOKEN):
        self.base = base
        self.token = token

    def request(self, method: str, path: str, body=None, token: str | None = None, headers=None, raw_body=None):
        data = None
        request_headers = dict(headers or {})
        if raw_body is not None:
            data = raw_body
            request_headers.setdefault("Content-Length", str(len(raw_body)))
        elif body is not None:
            data = json.dumps(body).encode()
            request_headers.setdefault("Content-Type", "application/json")
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)
            return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json(self, method: str, path: str, body=None, token=_DEFAULT, headers=None):
        auth = self.token if token is self._DEFAULT else token
        status, response_headers, raw = self.request(method, path, body, auth, headers)
        payload = json.loads(raw.decode()) if raw else None
        return status, response_headers, payload


@pytest.fixture
def api(tmp_path: Path):
    search_calls: list[tuple[str, int]] = []
    cover_calls: list[str] = []

    def fake_search(query: str, limit: int) -> dict:
        search_calls.append((query, limit))
        return {
            "tracks": [
                {
                    "id": "track-1",
                    "title": "One More Time",
                    "artists": "Daft Punk",
                    "album": "Discovery",
                    "durationMs": 320357,
                    "url": "https://open.spotify.com/track/track-1",
                    "cover": "https://i.scdn.co/image/abc123",
                    "explicit": False,
                },
                {
                    "id": "track-2",
                    "title": "Bad cover",
                    "artists": "Someone",
                    "album": "Elsewhere",
                    "durationMs": 1000,
                    "url": "https://open.spotify.com/track/track-2",
                    "cover": "https://evil.example/image.jpg",
                    "explicit": False,
                },
            ],
            "albums": [
                {
                    "id": "album-1",
                    "name": "Discovery",
                    "artists": "Daft Punk",
                    "releaseDate": "2001-03-12",
                    "url": "https://open.spotify.com/album/album-1",
                    "cover": "https://mosaic.scdn.co/640/abc",
                }
            ],
            "playlists": [
                {
                    "id": "playlist-1",
                    "name": "French Touch",
                    "owner": "espo",
                    "url": "https://open.spotify.com/playlist/playlist-1",
                    "cover": "https://pickasso.spotifycdn.com/image/abc/en",
                }
            ],
        }

    def fake_cover(url: str):
        cover_calls.append(url)
        return b"fake-image", "image/png"

    app = ApiApp(
        token=TOKEN,
        queue=QueueManager(str(tmp_path / "queue.db")),
        search=SearchRunner(fake_search, ttl=60),
        covers=CoverCache(fetcher=fake_cover),
        cors_origins=(ORIGIN,),
        queue_user=None,
        version="test",
    )
    server = create_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield {
            "client": Client(base),
            "app": app,
            "queue": app.queue,
            "search_calls": search_calls,
            "cover_calls": cover_calls,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_health_requires_token(api):
    client = api["client"]
    status, _, body = client.json("GET", "/api/health", token=None)
    assert status == 401
    assert body["error"] == "unauthorized"


def test_health_rejects_wrong_token(api):
    status, _, body = api["client"].json("GET", "/api/health", token="wrong")
    assert status == 401
    assert body["message"] == "Invalid bearer token"


def test_health_ok(api):
    status, _, body = api["client"].json("GET", "/api/health")
    assert status == 200
    assert body == {"ok": True, "service": "spoty_loop", "version": "test"}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_requires_token(api):
    status, _, _ = api["client"].json("GET", "/api/search?q=daft", token=None)
    assert status == 401


def test_search_shape_and_cover_proxy(api):
    status, _, body = api["client"].json(
        "GET",
        "/api/search?" + urllib.parse.urlencode({"q": "one more time", "limit": 2}),
    )
    assert status == 200
    assert body["query"] == "one more time"
    assert [track["id"] for track in body["tracks"]] == ["track-1", "track-2"]
    assert body["tracks"][0]["coverUrl"].startswith("/api/cover?u=")
    assert body["tracks"][1]["coverUrl"] is None
    assert "cover" not in body["tracks"][0]
    assert body["albums"][0]["coverUrl"].startswith("/api/cover?u=")
    assert body["playlists"][0]["coverUrl"].startswith("/api/cover?u=")
    assert api["search_calls"] == [("one more time", 2)]


def test_search_limit_is_clamped(api):
    api["client"].json("GET", "/api/search?q=x&limit=999")
    api["client"].json("GET", "/api/search?q=y&limit=0")
    assert api["search_calls"][0] == ("x", 50)
    assert api["search_calls"][1] == ("y", 1)


def test_search_cache_hits_do_not_rerun(api):
    api["client"].json("GET", "/api/search?" + urllib.parse.urlencode({"q": "Daft Punk"}))
    api["client"].json("GET", "/api/search?" + urllib.parse.urlencode({"q": "daft punk"}))
    assert len(api["search_calls"]) == 1


def test_search_empty_query(api):
    status, _, body = api["client"].json("GET", "/api/search?q=%20")
    assert status == 400
    assert body["error"] == "invalid_request"


def test_search_spotify_blocked(api):
    def blocked(query, limit):
        raise SearchError("spotify_blocked", 503, "Spotify is rate-limiting")

    api["app"].search = SearchRunner(blocked)
    status, headers, body = api["client"].json("GET", "/api/search?q=x")
    assert status == 503
    assert headers["Retry-After"] == "1800"
    assert body["error"] == "spotify_blocked"


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def test_enqueue_canonicalizes_and_dedupes(api):
    payload = {
        "urls": [
            "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV?si=abc",
            "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV",
        ]
    }
    status, _, body = api["client"].json("POST", "/api/queue", payload)
    assert status == 200
    assert body["queued"] == 1
    assert body["duplicates"] == 1
    assert body["invalid"] == 0
    assert body["items"][0]["url"] == "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV"
    item = api["queue"].get_item(body["items"][0]["id"])
    assert item["query"] == "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV"

    status, _, body = api["client"].json(
        "POST",
        "/api/queue",
        {"urls": ["https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV"]},
    )
    assert status == 200
    assert body["queued"] == 0
    assert body["duplicates"] == 1


def test_enqueue_rejects_invalid_links(api):
    payload = {
        "urls": [
            "not a link",
            "https://open.spotify.com/artist/0DiWol3AO6WpXZgp0goxAV",
            "spotify:track:0DiWol3AO6WpXZgp0goxAV",
        ]
    }
    status, _, body = api["client"].json("POST", "/api/queue", payload)
    assert status == 200
    assert body["queued"] == 1
    assert body["invalid"] == 2


def test_enqueue_requires_non_empty_list(api):
    status, _, body = api["client"].json("POST", "/api/queue", {"urls": []})
    assert status == 400
    assert body["error"] == "invalid_request"


def test_enqueue_rejects_too_many(api):
    urls = [f"spotify:track:{'a' * 22}" for _ in range(101)]
    status, _, body = api["client"].json("POST", "/api/queue", {"urls": urls})
    assert status == 400
    assert "at most" in body["message"]


def test_enqueue_uses_configured_user(api):
    api["app"].queue_user = 12345
    status, _, body = api["client"].json(
        "POST",
        "/api/queue",
        {"urls": ["spotify:track:0DiWol3AO6WpXZgp0goxAV"]},
    )
    assert status == 200
    item = api["queue"].get_item(body["items"][0]["id"])
    assert item["user"] == 12345


def test_enqueue_empty_body(api):
    status, _, raw = api["client"].request(
        "POST", "/api/queue", raw_body=b"", token=TOKEN
    )
    assert status == 400
    assert json.loads(raw)["error"] == "invalid_request"


def test_enqueue_invalid_json(api):
    status, _, raw = api["client"].request(
        "POST",
        "/api/queue",
        headers={"Content-Type": "application/json"},
        raw_body=b"{",
        token=TOKEN,
    )
    assert status == 400
    assert json.loads(raw)["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Covers and CORS
# ---------------------------------------------------------------------------


def test_cover_proxy(api):
    target = "https://i.scdn.co/image/abc123"
    status, headers, body = api["client"].request(
        "GET", "/api/cover?" + urllib.parse.urlencode({"u": target})
    )
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert body == b"fake-image"
    assert api["cover_calls"] == [target]


def test_cover_rejects_non_spotify_host(api):
    status, _, _ = api["client"].request(
        "GET",
        "/api/cover?"
        + urllib.parse.urlencode({"u": "https://evil.example/image/abc"}),
        token=None,
    )
    assert status == 404


def test_cover_allows_only_https(api):
    status, _, _ = api["client"].request(
        "GET",
        "/api/cover?" + urllib.parse.urlencode({"u": "http://i.scdn.co/image/abc"}),
        token=None,
    )
    assert status == 404


def test_cors_preflight_allowed(api):
    status, headers, _ = api["client"].request(
        "OPTIONS",
        "/api/search",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "Authorization" in headers["Access-Control-Allow-Headers"]


def test_cors_unknown_origin_gets_no_header(api):
    status, headers, _ = api["client"].request(
        "OPTIONS",
        "/api/search",
        headers={"Origin": "https://evil.example"},
    )
    assert status == 204
    assert "Access-Control-Allow-Origin" not in headers


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV?si=x", "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV"),
        ("spotify:album:0DiWol3AO6WpXZgp0goxAV", "https://open.spotify.com/album/0DiWol3AO6WpXZgp0goxAV"),
        ("https://open.spotify.com/artist/0DiWol3AO6WpXZgp0goxAV", None),
        ("https://open.spotify.com/track/short", None),
        ("not a link", None),
    ],
)
def test_canonical_spotify_url(value, expected):
    assert canonical_spotify_url(value) == expected


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("https://i.scdn.co/image/abc123", True),
        ("https://mosaic.scdn.co/640/abc", True),
        ("https://pickasso.spotifycdn.com/image/abc/en", True),
        ("https://image-cdn-ak.spotifycdn.com/image/abc", True),
        ("http://i.scdn.co/image/abc", False),
        ("https://evil.example/image/abc", False),
        ("https://i.scdn.co.evil.example/image/abc", False),
        ("https://user:pass@i.scdn.co/image/abc", False),
        ("https://i.scdn.co:8443/image/abc", False),
        ("https://i.scdn.co/../etc/passwd", False),
    ],
)
def test_allowed_cover_url(url, allowed):
    assert allowed_cover_url(url) is allowed


# ---------------------------------------------------------------------------
# Spotify auth (Telegram-managed file + env fallback)
# ---------------------------------------------------------------------------


@pytest.fixture
def spotify_env(monkeypatch, tmp_path):
    path = tmp_path / "spotify.json"
    for key in (
        "SPOTIFY_ACCESS_TOKEN",
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_MARKET",
    ):
        monkeypatch.delenv(key, raising=False)
    return path


def test_spotify_auth_anonymous(spotify_env):
    auth = SpotifyAuth(credentials_path=spotify_env)
    assert auth.mode == "anonymous"
    assert auth.source == "none"
    assert auth.search_spec() == ("anonymous", "")


def test_spotify_auth_block_token(spotify_env):
    set_access_token(spotify_env, "BQ" + "x" * 40)
    auth = SpotifyAuth(credentials_path=spotify_env)
    assert auth.mode == "token"
    assert auth.source == "telegram"
    mode, token = auth.search_spec()
    assert mode == "official"
    assert token.startswith("BQ")


def test_spotify_auth_client_credentials_cache_and_reload(spotify_env):
    set_client_credentials(spotify_env, "id", "secret")
    calls: list[tuple[str, str]] = []

    def fetch(client_id, client_secret):
        calls.append((client_id, client_secret))
        return "client-token"

    auth = SpotifyAuth(credentials_path=spotify_env, fetcher=fetch)
    assert auth.search_spec() == ("official", "client-token")
    assert auth.search_spec() == ("official", "client-token")
    assert calls == [("id", "secret")]

    set_client_credentials(spotify_env, "id", "new-secret")
    assert auth.search_spec() == ("official", "client-token")
    assert calls == [("id", "secret"), ("id", "new-secret")]


def test_spotify_auth_invalidate_refetches(spotify_env):
    set_client_credentials(spotify_env, "id", "secret")
    calls = []

    def fetch(client_id, client_secret):
        calls.append((client_id, client_secret))
        return f"token-{len(calls)}"

    auth = SpotifyAuth(credentials_path=spotify_env, fetcher=fetch)
    assert auth.search_spec()[1] == "token-1"
    auth.invalidate()
    assert auth.search_spec()[1] == "token-2"


def test_spotify_auth_env_fallback(spotify_env, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "env-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "env-secret")
    auth = SpotifyAuth(credentials_path=spotify_env)
    assert auth.mode == "client"
    assert auth.source == "env"


def test_search_provider_retries_once_on_auth_failure(spotify_env, monkeypatch):
    import api

    set_client_credentials(spotify_env, "id", "secret")
    auth = SpotifyAuth(credentials_path=spotify_env, fetcher=lambda i, s: "token")
    seen = []

    def fake_run(query, limit, **kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            raise SearchError("spotify_auth_failed", 502, "rejected")
        return {"tracks": [], "albums": [], "playlists": []}

    monkeypatch.setattr(api, "run_search_job", fake_run)
    provider = make_search_provider(auth, timeout=20)
    assert provider("x", 1) == {"tracks": [], "albums": [], "playlists": []}
    assert len(seen) == 2
    assert seen[0]["spotify_mode"] == "official"
    assert seen[0]["spotify_token"] == "token"
