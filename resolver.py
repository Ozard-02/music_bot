"""Parse user input and resolve name-based queries to Spotify URLs."""

import re

from SpotiFLAC import AsyncSpotiFLAC

SPOTIFY_URL_RE = re.compile(
    r"^https?://open\.spotify\.com/(track|album|playlist|artist)/[A-Za-z0-9_-]{22}(\?.*)?$"
)


def parse_input(text: str) -> tuple[str, str]:
    """Parse user input into (type, value).

    Types:
      'link'   — valid Spotify URL
      'search' — "X - Y" format for name-based lookup
      'invalid'— unrecognized format
    """
    text = text.strip()
    if SPOTIFY_URL_RE.match(text):
        return ("link", text)
    if " - " in text:
        return ("search", text)
    return ("invalid", text)


def format_help() -> str:
    return (
        "<b>What to send:</b>\n"
        "  \u2022 Spotify <b>link</b> \u2192 download it\n"
        "  \u2022 <b>Artist \u2014 Album</b> \u2192 download album\n"
        "  \u2022 <b>Album \u2014 Song</b> \u2192 download song\n"
        "  \u2022 <b>Artist \u2014 Song</b> \u2192 download song\n\n"
        "  Use a dash with spaces ( \u2014 ) between the two parts.\n\n"
        "<b>Commands:</b>\n"
        "  /help \u2014 this message\n"
        "  /status \u2014 queue status and recent items\n"
        "  /quality [value] \u2014 set your download quality (no arg lists options)\n"
        "  /purge \u2014 remove all queued items\n"
        "  /mkplaylist &lt;url&gt; [name] \u2014 build a .m3u8 from tracks on disk\n"
        "  /fixmetadata [album folder] \u2014 re-tag + fix covers &amp; lyrics (your library if omitted)"
    )


async def resolve_search(
    client: AsyncSpotiFLAC, query: str
) -> tuple[str, str, str]:
    """Resolve 'X - Y' search to a Spotify URL.

    Returns (url, display_name, type) where type is 'track' or 'album'.
    Raises ValueError if nothing matches.
    """
    parts = query.split(" - ", 1)
    a, b = parts[0].strip(), parts[1].strip()

    results = await client.search(f"{a} {b}", limit=20)
    tracks = results.get("tracks", [])
    albums = results.get("albums", [])

    best_track = best_track_match(tracks, a)
    best_album = _best_album_match(albums, a)

    if best_track and best_album:
        return _pick_between_track_and_album(best_track, best_album, b, a)
    if best_album:
        return (best_album["external_url"], best_album["name"], "album")
    if best_track:
        return (best_track.external_url, best_track.title, "track")
    raise ValueError(f"No results found for '{query}'")


def _pick_between_track_and_album(track, album, second_part: str, first_part: str) -> tuple[str, str, str]:
    """Both a track and an album matched — decide which the user meant."""
    track_album = getattr(track, "album", "") or ""
    if second_part.lower() == track_album.lower():
        return (track.external_url, track.title, "track")
    album_name = album.get("name", "") or ""
    if second_part.lower() in album_name.lower():
        return (album["external_url"], album["name"], "album")
    album_artist = album.get("artists", "") or ""
    if first_part.lower() in album_artist.lower():
        return (album["external_url"], album["name"], "album")
    return (track.external_url, track.title, "track")


def best_track_match(tracks: list, artist_hint: str = "", title_hint: str = ""):
    """Return the search result that best matches the hints, else the first.

    `artist_hint` matches against the track's artists (substring, case-
    insensitive); `title_hint` matches exactly against the title.      Falls back
    to `tracks[0]`, or None if there are no results.  Shared by the resolver
    and the maintenance scripts (fix_metadata and the archived
    backfill_urls/retag_missing).
    """
    artist = (artist_hint or "").strip().lower()
    title = (title_hint or "").strip().lower()
    for t in tracks:
        if artist and artist in (getattr(t, "artists", "") or "").lower():
            return t
        if title and title == (getattr(t, "title", "") or "").lower():
            return t
    return tracks[0] if tracks else None


def _best_album_match(albums: list, artist_hint: str):
    for alb in albums:
        if artist_hint.lower() in (alb.get("artists", "") or "").lower():
            return alb
    return albums[0] if albums else None
