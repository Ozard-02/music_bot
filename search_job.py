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

import spotiflac_patch  # noqa: F401  (skip GitHub check, provider timeouts)
from config import silence_spotiflac_loggers

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("spoty_loop.search")

_BLOCK_MARKERS = ("406", "429", "rate limit", "too many requests", "blocked")


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def is_spotify_block(error: BaseException) -> bool:
    """True when the failure is Spotify rate-limiting us, not a bad query."""
    text = str(error).lower()
    return any(marker in text for marker in _BLOCK_MARKERS)


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


async def _search(query: str, limit: int) -> dict:
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

    try:
        result = asyncio.run(_search(query, limit))
    except BaseException as exc:  # noqa: BLE001 - report, never traceback to stdout
        log.warning("Search failed for %r: %s", query, exc)
        kind = "spotify_blocked" if is_spotify_block(exc) else "search_failed"
        _emit({"ok": False, "error": str(exc) or exc.__class__.__name__, "kind": kind})
        return 0

    _emit({"ok": True, **result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
