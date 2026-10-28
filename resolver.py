"""Parse user input into (type, value) and resolve name searches.

The parent imports this module and only ever calls `parse_input`/`format_help`
— so SpotiFLAC is imported lazily inside `resolve_search`, which only runs in
the download subprocess."""

import re
import unicodedata

# Optional intl-<lang>/ segment: the Spotify web player shares localized
# links (open.spotify.com/intl-it/track/...); SpotiFLAC's parser accepts them.
SPOTIFY_URL_RE = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[A-Za-z-]+/)?(track|album|playlist|artist)/[A-Za-z0-9_-]{22}(\?.*)?$"
)


def _normalize_text(value: str) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_matches(hint: str, candidate: str) -> bool:
    hint_n = _normalize_text(hint)
    cand_n = _normalize_text(candidate)
    if not hint_n or not cand_n:
        return False
    if hint_n == cand_n:
        return True
    return min(len(hint_n), len(cand_n)) >= 4 and (
        hint_n in cand_n or cand_n in hint_n
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
        "  /mkplaylist &lt;url&gt; [name] \u2014 build a .m3u8 from tracks on disk (name defaults to Spotify title)\n"
        "  /mkplaylist &lt;name&gt; \u2014 create empty <code>&lt;name&gt;.m3u8</code> (no url)\n"
        "  /fixmetadata [album folder] \u2014 re-tag + fix covers (--lyrics also embeds lyrics; your library if omitted)\n"
        "  /rmplaylist [name] \u2014 delete every track listed in &lt;name&gt;.m3u8 (default Trash) and prune empty folders"
    )


async def resolve_search(
    client, query: str
) -> tuple[str, str, str]:
    """Resolve 'X - Y' search to a Spotify URL.

    Returns (url, display_name, type) where type is 'track' or 'album'.
    Raises ValueError if nothing matches. `client` is an AsyncSpotiFLAC
    instance owned by the caller (the download subprocess)."""
    parts = query.split(" - ", 1)
    a, b = parts[0].strip(), parts[1].strip()

    results = await client.search(f"{a} {b}", limit=20)
    tracks = results.get("tracks", [])
    albums = results.get("albums", [])

    best_track = best_track_match(tracks, a, b)
    best_album = _best_album_match(albums, a, b)

    if best_track and best_album:
        return _pick_between_track_and_album(best_track, best_album, b)
    if best_album:
        return (best_album["external_url"], best_album["name"], "album")
    if best_track:
        return (best_track.external_url, best_track.title, "track")
    raise ValueError(f"No results found for '{query}'")


def _pick_between_track_and_album(track, album, second_part: str) -> tuple[str, str, str]:
    """Both a track and an album matched — decide which the user meant.

    A second part equal to the album name means "Album" (help: Artist — Album
    downloads the album). Otherwise the track wins: a song query must never
    download a random album just because the artist matches.
    """
    album_name = _normalize_text(album.get("name", ""))
    if second_part and _normalize_text(second_part) == album_name:
        return (album["external_url"], album["name"], "album")
    if track:
        return (track.external_url, track.title, "track")
    return (album["external_url"], album["name"], "album")


def best_track_match(tracks: list, artist_hint: str = "", title_hint: str = ""):
    """Return the search result that best matches the hints, else the first.

    Artist and title are both scored, accent/punctuation-insensitive: the
    artist match wins, but among tracks by the same artist the one whose title
    matches the query wins too.
    """
    artist = _normalize_text(artist_hint)
    title = _normalize_text(title_hint)
    if not artist and not title:
        return tracks[0] if tracks else None

    best = None
    best_score = 0
    for t in tracks:
        score = 0
        if artist and artist in _normalize_text(getattr(t, "artists", "")):
            score += 2
        if title and _title_matches(title_hint, getattr(t, "title", "")):
            score += 1
        if score > best_score:
            best, best_score = t, score
    return best if best is not None else (tracks[0] if tracks else None)


def _best_album_match(albums: list, artist_hint: str, title_hint: str = ""):
    """Pick the album matching the artist and, when given, the album name."""
    artist = _normalize_text(artist_hint)
    title = _normalize_text(title_hint)
    best = None
    best_score = -1
    for alb in albums:
        name = _normalize_text(alb.get("name", ""))
        artists = _normalize_text(alb.get("artists", ""))
        score = 0
        if artist and (artist in artists or artist == name):
            score += 2
        if title and title == name:
            score += 2
        if score > best_score:
            best, best_score = alb, score
    return best if best is not None else (albums[0] if albums else None)