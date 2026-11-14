"""Deezer metadata → SpotiFLAC TrackMetadata (subprocess only).

Deezer's public API gives us everything the provider chain needs: title,
artist, album, duration, ISRC, track/disc numbers, cover and release date.
The download still goes through SpotiFLAC's Qobuz/Deezer/Amazon providers;
only the metadata source changes for the Laila path.

This module imports httpx and SpotiFLAC's TrackMetadata, so it must never be
imported by a front end (bot.py, api.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx
from SpotiFLAC.core.models import TrackMetadata

DEEZER_API = "https://api.deezer.com"
DEFAULT_TIMEOUT = 15
DETAIL_CONCURRENCY = 2
DETAIL_MIN_INTERVAL = 0.2
USER_AGENT = "spoty_loop/1.0"
_DEEZER_RE = re.compile(
    r"^https?://(?:www\.)?deezer\.com/(?:[a-z]{2}/)?"
    r"(track|album|playlist)/([0-9]+)(?:[/?#].*)?$"
)

log = logging.getLogger("spoty_loop.deezer")


class DeezerMetadataError(Exception):
    """Deezer metadata could not be fetched or parsed."""


def is_deezer_url(value: str) -> bool:
    return bool(_DEEZER_RE.match((value or "").strip()))


def parse_deezer_url(value: str) -> dict | None:
    match = _DEEZER_RE.match((value or "").strip())
    if not match:
        return None
    return {"type": match.group(1), "id": match.group(2)}


def _cover(album: dict) -> str:
    for key in ("cover_xl", "cover_big", "cover_medium", "cover"):
        value = album.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
    return ""


def _artists(track: dict) -> str:
    contributors = track.get("contributors")
    if isinstance(contributors, list):
        names = [
            str(item.get("name"))
            for item in contributors
            if isinstance(item, dict) and item.get("name")
        ]
        # Keep first-seen order, drop duplicates (Deezer repeats the lead).
        unique = list(dict.fromkeys(names))
        if unique:
            return ", ".join(unique)
    artist = track.get("artist")
    if isinstance(artist, dict) and artist.get("name"):
        return str(artist["name"])
    return "Unknown"


def _album_artist(album: dict, track: dict) -> str:
    artist = album.get("artist")
    if isinstance(artist, dict) and artist.get("name"):
        return str(artist["name"])
    artist = track.get("artist")
    if isinstance(artist, dict) and artist.get("name"):
        return str(artist["name"])
    return "Unknown"


def _genre(album: dict) -> str:
    genres = album.get("genres")
    if isinstance(genres, list):
        for item in genres:
            if isinstance(item, dict) and item.get("name"):
                return str(item["name"])
    return ""


def _track_metadata(
    track: dict,
    *,
    album: dict | None = None,
    fallback_number: int = 0,
    total_tracks: int = 0,
    total_discs: int = 1,
) -> TrackMetadata:
    album_data = album if isinstance(album, dict) else {}
    if not album_data:
        album_data = track.get("album") if isinstance(track.get("album"), dict) else {}

    track_id = str(track.get("id") or "")
    album_id = str(album_data.get("id") or "")
    artist = track.get("artist") if isinstance(track.get("artist"), dict) else {}
    artist_id = str(artist.get("id") or "")
    title = str(track.get("title") or "Unknown")
    album_title = str(album_data.get("title") or "Unknown")

    try:
        duration_ms = max(0, int(track.get("duration") or 0)) * 1000
    except (TypeError, ValueError):
        duration_ms = 0
    try:
        track_number = int(track.get("track_position") or fallback_number or 0)
    except (TypeError, ValueError):
        track_number = fallback_number
    try:
        disc_number = int(track.get("disk_number") or 1)
    except (TypeError, ValueError):
        disc_number = 1
    try:
        bpm = int(track.get("bpm") or 0)
    except (TypeError, ValueError):
        bpm = 0

    return TrackMetadata(
        # Prefix keeps the flock filename safe and distinguishes Deezer from
        # Spotify IDs in logs and duplicate checks.
        id=f"deezer-{track_id}" if track_id else "",
        title=title,
        artists=_artists(track),
        album=album_title,
        album_artist=_album_artist(album_data, track),
        isrc=str(track.get("isrc") or ""),
        track_number=track_number,
        disc_number=disc_number,
        total_tracks=total_tracks or int(album_data.get("nb_tracks") or 0),
        total_discs=total_discs,
        duration_ms=duration_ms,
        release_date=str(track.get("release_date") or album_data.get("release_date") or ""),
        cover_url=_cover(album_data),
        external_url=f"https://www.deezer.com/track/{track_id}",
        album_id=album_id,
        album_url=f"https://www.deezer.com/album/{album_id}" if album_id else "",
        artist_id=artist_id,
        artist_url=f"https://www.deezer.com/artist/{artist_id}" if artist_id else "",
        composer="",
        genre=_genre(album_data),
        bpm=bpm,
        publisher=str(album_data.get("label") or ""),
        upc=str(album_data.get("upc") or ""),
        is_explicit=bool(track.get("explicit_lyrics")),
        artists_data=track.get("contributors") or [],
    )


def _quota_error(value: object) -> bool:
    text = str(value).lower()
    return "quota" in text or "429" in text or "too many requests" in text


async def _get(client: httpx.AsyncClient, path: str) -> dict:
    for attempt in (0, 1):
        try:
            response = await client.get(DEEZER_API + path)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001 - normalise every transport error
            if attempt == 0 and _quota_error(exc):
                await asyncio.sleep(2)
                continue
            raise DeezerMetadataError(
                f"Deezer metadata request failed: {exc}"
            ) from exc
        if isinstance(data, dict) and data.get("error"):
            error = data["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            if attempt == 0 and _quota_error(message):
                await asyncio.sleep(2)
                continue
            raise DeezerMetadataError(f"Deezer: {message or 'request failed'}")
        if not isinstance(data, dict):
            raise DeezerMetadataError("Deezer returned an invalid response")
        return data
    raise DeezerMetadataError("Deezer metadata request failed")


async def _throttled_get(
    client: httpx.AsyncClient,
    path: str,
    lock: asyncio.Lock,
    last: list[float],
) -> dict:
    # Deezer's public API enforces a per-IP quota. Detail calls for a 50-track
    # playlist can otherwise trip it in a couple of seconds.
    async with lock:
        wait = DETAIL_MIN_INTERVAL - (time.monotonic() - last[0])
        if wait > 0:
            await asyncio.sleep(wait)
        last[0] = time.monotonic()
    return await _get(client, path)


async def _details(
    client: httpx.AsyncClient,
    basics: list[dict],
) -> list[dict]:
    """Fetch full /track/{id} objects in bounded, throttled parallel batches.

    Album and playlist track lists omit ISRC, track_position and disk_number.
    If a detail call fails we keep the basic object so the download can still
    try the provider's text fallback.
    """
    semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
    lock = asyncio.Lock()
    last = [0.0]

    async def one(basic: dict) -> dict:
        track_id = basic.get("id")
        if not track_id:
            return basic
        async with semaphore:
            try:
                return await _throttled_get(
                    client, f"/track/{track_id}", lock, last
                )
            except DeezerMetadataError as exc:
                log.warning("Deezer detail failed for %s: %s", track_id, exc)
                return basic

    return list(await asyncio.gather(*(one(item) for item in basics)))


async def _album_tracks(client: httpx.AsyncClient, album_id: str) -> list[TrackMetadata]:
    album = await _get(client, f"/album/{album_id}")
    basics = [
        item
        for item in (album.get("tracks") or {}).get("data", [])
        if isinstance(item, dict) and item.get("id")
    ]
    details = await _details(client, basics)
    total = int(album.get("nb_tracks") or len(details) or 0)
    total_discs = max(
        (int(item.get("disk_number") or 1) for item in details), default=1
    )
    return [
        _track_metadata(
            item,
            album=album,
            fallback_number=index + 1,
            total_tracks=total,
            total_discs=total_discs,
        )
        for index, item in enumerate(details)
    ]


async def _playlist_tracks(
    client: httpx.AsyncClient, playlist_id: str
) -> list[TrackMetadata]:
    playlist = await _get(client, f"/playlist/{playlist_id}")
    basics = [
        item
        for item in (playlist.get("tracks") or {}).get("data", [])
        if isinstance(item, dict) and item.get("id")
    ]
    # The playlist payload already carries ISRC, title, artist, album and
    # cover for every track. Do not fan out into one /track/{id} per entry:
    # a 500-track playlist would cost hundreds of quota-limited requests.
    # Playlist order is the meaningful track order here; the album artist
    # falls back to the track artist for compilation entries.
    total = len(basics)
    return [
        _track_metadata(
            item,
            fallback_number=index + 1,
            total_tracks=total,
            total_discs=1,
        )
        for index, item in enumerate(basics)
    ]


async def _track(client: httpx.AsyncClient, track_id: str) -> TrackMetadata:
    data = await _get(client, f"/track/{track_id}")
    album_data = data.get("album") if isinstance(data.get("album"), dict) else {}
    album_id = album_data.get("id")
    album = album_data
    if album_id:
        try:
            album = await _get(client, f"/album/{album_id}")
        except DeezerMetadataError as exc:
            log.warning("Deezer album detail failed for %s: %s", album_id, exc)
    return _track_metadata(
        data,
        album=album,
        fallback_number=int(data.get("track_position") or 1),
        total_tracks=int((album or {}).get("nb_tracks") or 0),
        total_discs=1,
    )


async def fetch_tracks(url: str, timeout: int = DEFAULT_TIMEOUT) -> list[TrackMetadata]:
    parsed = parse_deezer_url(url)
    if not parsed:
        raise DeezerMetadataError("Not a Deezer track, album or playlist link")

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers=headers
    ) as client:
        if parsed["type"] == "track":
            return [await _track(client, parsed["id"])]
        if parsed["type"] == "album":
            return await _album_tracks(client, parsed["id"])
        return await _playlist_tracks(client, parsed["id"])
