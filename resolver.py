"""Parse user input and resolve name-based queries to Spotify URLs."""

import re

from SpotiFLAC import AsyncSpotiFLAC

SPOTIFY_URL_RE = re.compile(
    r"^https?://open\.spotify\.com/(track|album|playlist|artist)/[A-Za-z0-9]{22}"
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
        "Send one of:\n\n"
        "- Spotify <b>link</b> \u2192 download it\n"
        "- <b>Artist \u2014 Album</b> \u2192 download album\n"
        "- <b>Album \u2014 Song</b> \u2192 download song\n"
        "- <b>Artist \u2014 Song</b> \u2192 download song\n\n"
        "Use a dash with spaces ( \u2014 ) between the two parts.\n\n"
        "<b>Commands:</b>\n"
        "/status \u2014 queue status and recent items\n"
        "/purge \u2014 remove all queued items\n"
        "/mkplaylist &lt;url&gt; [name] \u2014 build .m3u8 from playlist"
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

    results = await client.search(f"{a} {b}", limit=5)
    tracks = results.get("tracks", [])
    albums = results.get("albums", [])

    best_track = _best_track_match(tracks, a)
    best_album = _best_album_match(albums, a)

    if best_track and best_album:
        track_album = getattr(best_track, "album", "")
        if b.lower() == track_album.lower():
            return (best_track.external_url, best_track.title, "track")
        album_artist = best_album.get("artists", "")
        if a.lower() in album_artist.lower():
            return (best_album["external_url"], best_album["name"], "album")
        return (best_track.external_url, best_track.title, "track")
    elif best_track:
        return (best_track.external_url, best_track.title, "track")
    elif best_album:
        return (best_album["external_url"], best_album["name"], "album")
    else:
        raise ValueError(f"No results found for '{query}'")


def _best_track_match(tracks: list, artist_hint: str):
    for t in tracks:
        if artist_hint.lower() in (getattr(t, "artists", "") or "").lower():
            return t
    return tracks[0] if tracks else None


def _best_album_match(albums: list, artist_hint: str):
    for alb in albums:
        if artist_hint.lower() in (alb.get("artists", "") or "").lower():
            return alb
    return albums[0] if albums else None
