"""Parse user input into (type, value) and resolve name searches.

The parent imports this module and only ever calls `parse_inputs`/`format_help`
— so SpotiFLAC is imported lazily inside `resolve_search`, which only runs in
the download subprocess."""

import difflib
import re
import unicodedata

# Lenient link extraction, not validation: users paste links inside prose
# ("check this <url>!"), share fragments (?si=…), spotify: URIs, the fallback
# host play.spotify.com and localized intl-<lang>/ segments.  This parent-side
# regex only locates a Spotify identifier; SpotiFLAC's own
# parse_spotify_url revalidates in the download subprocess.
SPOTIFY_URL_RE = re.compile(
    r"(?:https?://(?:open|play)\.spotify\.com/(?:intl-[A-Za-z-]+/)?"
    r"(?:track|album|playlist|artist)/[A-Za-z0-9_-]{22}\S*"
    r"|spotify:(?:track|album|playlist|artist):[A-Za-z0-9]{22})"
)

# Dashes that may act as the artist/track separator. En/em dashes always
# separate (they never occur inside real artist or song names); a plain
# hyphen separates only when it isn't glued to words on both sides
# ("AC-DC", "Me-I" stay intact).
SEPARATOR_DASH_RE = re.compile(r"[-–—]")


def _normalize_text(value: str) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: str) -> list[str]:
    return _normalize_text(value).split()


# Connector words that differ between how users type artist lists and how
# Spotify names them: 'Daft Punk feat. Pharrell' vs 'feat.' dropped from the
# candidate, 'Simon and Garfunkel' vs 'Simon & Garfunkel'.
_ARTIST_FILLERS = {"and", "vs", "feat", "ft", "with"}


def _similar(a: str, b: str, min_ratio: float = 0.8) -> bool:
    """Whitespace/punctuation-insensitive fuzzy equality (stdlib difflib)."""
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= min_ratio


def _artist_matches(hint: str, artists: str) -> bool:
    """Does the user's artist hint match the result's artist field?

    Lenient on purpose — artist lists are written many ways:
      • token match: 'Daft Punk feat. Pharrell' -> 'daft punk pharrell'
      • fuzzy full-string: 'juicewrld' -> 'juice wrld' (0.845)
    """
    hint_tokens = [t for t in _tokens(hint) if t not in _ARTIST_FILLERS]
    cand_tokens = [t for t in _tokens(artists) if t not in _ARTIST_FILLERS]
    if not hint_tokens or not cand_tokens:
        return False
    if all(t in cand_tokens for t in hint_tokens):
        return True
    return _similar(" ".join(hint_tokens), " ".join(cand_tokens))


def _title_matches(hint: str, candidate: str) -> bool:
    hint_n = _normalize_text(hint)
    cand_n = _normalize_text(candidate)
    if not hint_n or not cand_n:
        return False
    if hint_n == cand_n:
        return True
    if min(len(hint_n), len(cand_n)) >= 4 and (
        hint_n in cand_n or cand_n in hint_n
    ):
        return True
    return _similar(hint_n, cand_n)


def split_search(text: str) -> tuple[str, str] | None:
    """Split 'Artist - Track' at the first dash that acts as a separator.

    En/em dashes always separate; a plain hyphen separates only when a
    neighbour is not alphanumeric ('AC-DC', 'Me-I' stay intact). Returns
    None when no separator exists or a half would be empty.
    """
    for m in SEPARATOR_DASH_RE.finditer(text):
        before = text[m.start() - 1] if m.start() > 0 else ""
        after = text[m.end()] if m.end() < len(text) else ""
        if m.group() in "–—" or not (before.isalnum() and after.isalnum()):
            a, b = text[: m.start()].strip(), text[m.end() :].strip()
            if a and b:
                return (a, b)
    return None


def parse_inputs(text: str) -> list[tuple[str, str]]:
    """Parse every line of a message into ('link'|'search'|'invalid', value).

    Users paste several artists/tracks at once — one non-blank line is one
    item. A line with a Spotify link inside prose yields just the link.
    Blank lines are ignored.
    """
    items: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = SPOTIFY_URL_RE.search(line)
        if m:
            items.append(("link", m.group(0).rstrip(",.;:!)']")))
            continue
        parts = split_search(line)
        if parts:
            items.append(("search", f"{parts[0]} - {parts[1]}"))
            continue
        items.append(("invalid", line))
    return items


def parse_input(text: str) -> tuple[str, str]:
    """Parse a single (one-line) input into (type, value).

    Types:
      'link'   — valid Spotify URL or spotify: URI, possibly embedded in prose
      'search' — "X - Y" format for name-based lookup (any dash kind works)
      'invalid'- unrecognized format
    """
    items = parse_inputs(text)
    return items[0] if items else ("invalid", text.strip())


def format_help() -> str:
    return (
        "<b>What to send:</b>\n"
        "  \u2022 Spotify <b>link</b> \u2192 download it (a link inside text works too)\n"
        "  \u2022 <b>Artist \u2014 Album</b> \u2192 download album\n"
        "  \u2022 <b>Artist \u2014 Song</b> \u2192 download song\n"
        "  \u2022 <b>Album \u2014 Song</b> \u2192 download song\n"
        "  \u2022 Send <b>several lines</b> at once to queue all of them\n\n"
        "  Use any dash between the two parts ( hyphen, \u2013 en or \u2014 em dash \u2014\n"
        "  spaces optional).\n\n"
        "<b>Commands:</b>\n"
        "  /help \u2014 this message\n"
        "  /status \u2014 queue status and recent items\n"
        "  /quality [value] \u2014 set your download quality (no arg lists options)\n"
        "  /purge \u2014 remove all queued items\n"
        "  /retry &lt;id&gt; \u2014 re-queue one failed item (resets retries)\n"
        "  /mkplaylist &lt;url&gt; [name] \u2014 build a .m3u8 from tracks on disk (name defaults to Spotify title)\n"
        "  /mkplaylist &lt;name&gt; \u2014 create empty <code>&lt;name&gt;.m3u8</code> (no url)\n"
        "  /fixmetadata [album folder] \u2014 re-tag + fix covers (--lyrics also embeds lyrics; your library if omitted)\n"
        "  /synclyrics [album folder] [--force] \u2014 fetch missing/unsynced lyrics + .lrc sidecars (no re-tag/move)\n"
        "  /rmplaylist [name] \u2014 delete every track listed in &lt;name&gt;.m3u8 (default Trash) and prune empty folders"
    )


async def resolve_search(
    client, query: str
) -> tuple[str, str, str]:
    """Resolve 'X - Y' search to a Spotify URL.

    Returns (url, display_name, type) where type is 'track' or 'album'.
    Raises ValueError if nothing matches. `client` is an AsyncSpotiFLAC
    instance owned by the caller (the download subprocess)."""
    parts = split_search(query)
    # No separator (legacy/malformed row): search by the whole text as title.
    a, b = parts if parts else ("", query.strip())

    results = await client.search(" ".join(p for p in (a, b) if p), limit=20)
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
    match = _title_matches(second_part, album.get("name", ""))
    if second_part and match:
        return (album["external_url"], album["name"], "album")
    if track:
        return (track.external_url, track.title, "track")
    return (album["external_url"], album["name"], "album")


def best_track_match(tracks: list, artist_hint: str = "", title_hint: str = ""):
    """Return the search result that best matches the hints, else the first.

    Artist and title are both scored, accent/punctuation-insensitive: the
    artist match wins, but among tracks by the same artist the one whose title
    matches the query wins too. Note the artist can match with no title hint
    at all (and vice versa) — each scorer alone is enough to pick.
    """
    artist = _normalize_text(artist_hint)
    title = _normalize_text(title_hint)
    if not artist and not title:
        return tracks[0] if tracks else None

    best = None
    best_score = 0
    for t in tracks:
        score = 0
        if artist and _artist_matches(artist_hint, str(getattr(t, "artists", ""))):
            score += 2
        if title and _title_matches(title_hint, str(getattr(t, "title", ""))):
            score += 1
        if score > best_score:
            best, best_score = t, score
    return best if best is not None else (tracks[0] if tracks else None)


def _best_album_match(albums: list, artist_hint: str, title_hint: str = ""):
    """Pick the album matching the artist and, when given, the album name.

    Returns None when no album matched at all — falling back to albums[0]
    would download a random album when the search was really a track name
    that just has no track result.
    """
    best = None
    best_score = 0
    for alb in albums:
        name = _normalize_text(alb.get("name", ""))
        score = 0
        if artist_hint and _artist_matches(artist_hint, str(alb.get("artists", ""))):
            score += 2
        if title_hint and _title_matches(title_hint, name):
            score += 2
        if score > best_score:
            best, best_score = alb, score
    return best
