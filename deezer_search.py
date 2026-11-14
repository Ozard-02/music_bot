"""Fast Deezer search for the Laila tab (front end, stdlib only).

Deezer's public API needs no credentials. We query three endpoints in
parallel — tracks, albums and playlists — and normalise them to the shape
`api.py` already exposes to Laila. Keeping this in the parent means no
SpotiFLAC import and no short-lived subprocess for every search.
"""

from __future__ import annotations

import http.client
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from urllib.parse import urlencode

DEEZER_HOST = "api.deezer.com"
DEFAULT_TIMEOUT = 15
MAX_LIMIT = 50
USER_AGENT = "spoty_loop/1.0"


class DeezerError(Exception):
    """Deezer could not answer, or answered with an API error."""


def _get_json(path: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """One HTTPS GET against api.deezer.com, no redirects, no cookies."""
    for attempt in (0, 1):
        conn = http.client.HTTPSConnection(DEEZER_HOST, timeout=timeout)
        try:
            conn.request(
                "GET",
                path,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            response = conn.getresponse()
            raw = response.read()
            if response.status != 200:
                if attempt == 0 and response.status == 429:
                    time.sleep(1)
                    continue
                raise DeezerError(f"Deezer returned HTTP {response.status}")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise DeezerError("Deezer returned invalid JSON") from exc
            if isinstance(payload, dict) and payload.get("error"):
                error = payload["error"]
                message = (
                    error.get("message") if isinstance(error, dict) else str(error)
                )
                text = str(message or "").lower()
                if attempt == 0 and (
                    "quota" in text or "too many requests" in text
                ):
                    time.sleep(1)
                    continue
                raise DeezerError(f"Deezer: {message or 'request failed'}")
            if not isinstance(payload, dict):
                raise DeezerError("Deezer returned an invalid response")
            return payload
        except DeezerError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise DeezerError("Cannot reach Deezer") from exc
        finally:
            conn.close()
    raise DeezerError("Deezer request failed")


def _cover(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
    return ""


def _duration_ms(item: dict) -> int:
    try:
        return max(0, int(item.get("duration") or 0)) * 1000
    except (TypeError, ValueError):
        return 0


def _track(item: object) -> dict | None:
    if not isinstance(item, dict) or not item.get("id"):
        return None
    album = item.get("album") if isinstance(item.get("album"), dict) else {}
    artist = item.get("artist") if isinstance(item.get("artist"), dict) else {}
    return {
        "id": str(item["id"]),
        "title": str(item.get("title") or ""),
        "artists": str(artist.get("name") or ""),
        "album": str(album.get("title") or ""),
        "durationMs": _duration_ms(item),
        "url": f"https://www.deezer.com/track/{item['id']}",
        "cover": _cover(album, "cover_xl", "cover_big", "cover_medium"),
        "explicit": bool(item.get("explicit_lyrics")),
        "isrc": str(item.get("isrc") or ""),
    }


def _album(item: object) -> dict | None:
    if not isinstance(item, dict) or not item.get("id"):
        return None
    artist = item.get("artist") if isinstance(item.get("artist"), dict) else {}
    return {
        "id": str(item["id"]),
        "name": str(item.get("title") or ""),
        "artists": str(artist.get("name") or ""),
        "releaseDate": str(item.get("release_date") or ""),
        "url": f"https://www.deezer.com/album/{item['id']}",
        "cover": _cover(item, "cover_xl", "cover_big", "cover_medium"),
    }


def _playlist(item: object) -> dict | None:
    if not isinstance(item, dict) or not item.get("id"):
        return None
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    if not user and isinstance(item.get("creator"), dict):
        user = item["creator"]
    return {
        "id": str(item["id"]),
        "name": str(item.get("title") or ""),
        "owner": str(user.get("name") or ""),
        "releaseDate": str(item.get("creation_date") or ""),
        "url": f"https://www.deezer.com/playlist/{item['id']}",
        "cover": _cover(item, "picture_xl", "picture_big", "picture_medium"),
    }


def _items(payload: dict, mapper: Callable[[object], dict | None]) -> list[dict]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in (mapper(value) for value in data) if item is not None]


def search(
    query: str,
    limit: int = 20,
    timeout: int = DEFAULT_TIMEOUT,
    fetch: Callable[[str], dict] | None = None,
) -> dict:
    """Search tracks, albums and playlists on Deezer.

    `fetch` is injectable for tests; it receives one API path and returns the
    decoded JSON object. The default performs a real HTTPS request.
    """
    text = (query or "").strip()
    if not text:
        raise DeezerError("query is required")
    try:
        size = max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError) as exc:
        raise DeezerError("limit must be an integer") from exc

    getter = fetch or (lambda path: _get_json(path, timeout))
    common = urlencode({"q": text, "limit": size})
    paths = (
        f"/search?{common}",
        f"/search/album?{common}",
        f"/search/playlist?{common}",
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        tracks, albums, playlists = pool.map(getter, paths)

    return {
        "tracks": _items(tracks, _track),
        "albums": _items(albums, _album),
        "playlists": _items(playlists, _playlist),
    }
