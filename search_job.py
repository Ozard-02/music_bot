#!/usr/bin/env python3
"""One-shot Spotify search subprocess for the HTTP API.

Reads one JSON spec from stdin (`{"query": ..., "limit": ...}`) and writes one
JSON object to stdout. SpotiFLAC is imported here, never in `api.py`: the API
process stays stdlib-only and its RSS stays flat. stdout is protocol only;
logs go to stderr and are captured by the API.

SpotiFLAC's `search_async` swallows network errors and returns empty lists, so
we wrap `web_client.query` to keep the last exception. Without that a Spotify
406/429 would look like "no results" to the UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import silence_spotiflac_loggers

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("spoty_loop.search")

_BLOCK_MARKERS = ("406", "429", "rate limit", "too many requests", "blocked")
_OFFICIAL_SEARCH_URL = "https://api.spotify.com/v1/search"


class OfficialAuthError(Exception):
    """The configured Spotify credentials were rejected."""


class OfficialRateLimited(Exception):
    """The official API is rate-limiting us."""


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def is_spotify_block(error: BaseException) -> bool:
    """True when the failure is Spotify rate-limiting us, not a bad query."""
    text = str(error).lower()
    return any(marker in text for marker in _BLOCK_MARKERS)


class SpotifyApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _image_url(item: dict) -> str:
    images = item.get("images") or []
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            return str(first.get("url") or "")
    return ""


def _artists(item: dict) -> str:
    artists = item.get("artists") or []
    if not isinstance(artists, list):
        return ""
    return ", ".join(
        str(artist.get("name") or "")
        for artist in artists
        if isinstance(artist, dict) and artist.get("name")
    )


def normalize_official(payload: dict) -> dict:
    """Convert the official Web API search payload into the API's shape."""
    tracks = []
    for item in ((payload.get("tracks") or {}).get("items") or []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        album = item.get("album") or {}
        tracks.append(
            {
                "id": str(item.get("id")),
                "title": str(item.get("name") or ""),
                "artists": _artists(item),
                "album": str(album.get("name") or ""),
                "durationMs": int(item.get("duration_ms") or 0),
                "url": str((item.get("external_urls") or {}).get("spotify") or ""),
                "cover": _image_url(album),
                "explicit": bool(item.get("explicit")),
            }
        )

    albums = []
    for item in ((payload.get("albums") or {}).get("items") or []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        albums.append(
            {
                "id": str(item.get("id")),
                "name": str(item.get("name") or ""),
                "url": str((item.get("external_urls") or {}).get("spotify") or ""),
                "cover": _image_url(item),
                "artists": _artists(item),
                "owner": "",
                "releaseDate": str(item.get("release_date") or ""),
                "kind": "album",
            }
        )

    playlists = []
    for item in ((payload.get("playlists") or {}).get("items") or []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        owner = item.get("owner") or {}
        playlists.append(
            {
                "id": str(item.get("id")),
                "name": str(item.get("name") or ""),
                "url": str((item.get("external_urls") or {}).get("spotify") or ""),
                "cover": _image_url(item),
                "artists": "",
                "owner": str(owner.get("display_name") or ""),
                "releaseDate": "",
                "kind": "playlist",
            }
        )

    return {"tracks": tracks, "albums": albums, "playlists": playlists}


def official_search(query: str, limit: int, token: str, market: str = "") -> dict:
    """Search through the official Web API with a client-credentials/user token."""
    params = {"q": query, "type": "track,album,playlist", "limit": limit}
    if market:
        params["market"] = market
    request = Request(
        f"{_OFFICIAL_SEARCH_URL}?{urlencode(params)}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "spoty_loop/1.0",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 401:
            raise OfficialAuthError("Spotify rejected the credentials") from exc
        if exc.code == 429:
            raise OfficialRateLimited("Spotify official API rate limit") from exc
        raise SpotifyApiError(exc.code, f"Spotify API returned HTTP {exc.code}") from exc
    except OSError as exc:
        raise SpotifyApiError(502, "Cannot reach the Spotify API") from exc
    return normalize_official(payload)


def normalize(result: dict) -> dict:
    """Convert SpotiFLAC dataclasses/dicts into the API's stable JSON shape."""
    tracks = []
    for track in result.get("tracks", []):
        tracks.append(
            {
                "id": getattr(track, "id", "") or "",
                "title": getattr(track, "title", "") or "",
                "artists": getattr(track, "artists", "") or "",
                "album": getattr(track, "album", "") or "",
                "durationMs": int(getattr(track, "duration_ms", 0) or 0),
                "url": getattr(track, "external_url", "") or "",
                "cover": getattr(track, "cover_url", "") or "",
                "explicit": bool(getattr(track, "is_explicit", False)),
            }
        )

    def simple(items: list, kind: str) -> list[dict]:
        out = []
        for item in items:
            out.append(
                {
                    "id": item.get("id", "") or "",
                    "name": item.get("name", "") or "",
                    "url": item.get("external_url", "") or "",
                    "cover": item.get("cover_url", "") or "",
                    "artists": item.get("artists", "") or "",
                    "owner": item.get("owner", "") or "",
                    "releaseDate": item.get("release_date", "") or "",
                    "kind": kind,
                }
            )
        return out

    return {
        "tracks": tracks,
        "albums": simple(result.get("albums", []), "album"),
        "playlists": simple(result.get("playlists", []), "playlist"),
    }


async def _anonymous_search(query: str, limit: int) -> dict:
    import spotiflac_patch  # noqa: F401  (skip GitHub check, provider timeouts)
    from SpotiFLAC import AsyncSpotiFLAC

    # Keep httpx/SpotiFLAC noise out of the API's stderr capture.
    silence_spotiflac_loggers()
    errors: list[BaseException] = []

    with tempfile.TemporaryDirectory(prefix="spoty-loop-search-") as tmp:
        # sync_extensions=False keeps this light: extensions are for download
        # providers, not for the GraphQL search.
        async with AsyncSpotiFLAC(
            output_dir=tmp,
            sync_extensions=False,
            timeout_s=10,
        ) as client:
            metadata = client._get_metadata_client()
            original_query = metadata.web_client.query

            def query_with_error_capture(payload):
                try:
                    return original_query(payload)
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    errors.append(exc)
                    raise

            metadata.web_client.query = query_with_error_capture
            result = await client.search(query, limit=limit)

    if errors and not any(result.get(key) for key in ("tracks", "albums", "playlists")):
        raise errors[-1]
    return normalize(result)


def main() -> int:
    try:
        spec = json.load(sys.stdin)
        query = str(spec.get("query", "")).strip()
        limit = max(1, min(50, int(spec.get("limit", 20))))
        if not query:
            raise ValueError("query is required")
    except Exception as exc:  # noqa: BLE001 - protocol wants a JSON error
        _emit({"ok": False, "error": str(exc), "kind": "invalid_request"})
        return 0

    mode = str(spec.get("spotify_mode") or "anonymous")
    token = str(spec.get("spotify_token") or "")
    market = str(spec.get("spotify_market") or "")

    try:
        if mode == "official":
            if not token:
                raise OfficialAuthError("No Spotify access token")
            result = official_search(query, limit, token, market)
        else:
            result = asyncio.run(_anonymous_search(query, limit))
    except OfficialAuthError as exc:
        _emit({"ok": False, "error": str(exc), "kind": "spotify_auth_failed"})
        return 0
    except OfficialRateLimited as exc:
        _emit({"ok": False, "error": str(exc), "kind": "spotify_blocked"})
        return 0
    except SpotifyApiError as exc:
        _emit({"ok": False, "error": str(exc), "kind": "search_failed"})
        return 0
    except BaseException as exc:  # noqa: BLE001 - report, never traceback to stdout
        log.warning("Search failed for %r: %s", query, exc)
        kind = "spotify_blocked" if is_spotify_block(exc) else "search_failed"
        _emit({"ok": False, "error": str(exc) or exc.__class__.__name__, "kind": kind})
        return 0

    _emit({"ok": True, **result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
