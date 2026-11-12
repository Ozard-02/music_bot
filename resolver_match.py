"""Name-search matching for the download subprocess.

Kept out of `resolver.py` so the parent never imports `difflib`/`unicodedata`:
only `download_job.py` (and the maintenance scripts) need this module.
"""

import difflib
import re
import unicodedata

from resolver import split_search


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


async def resolve_search(client, query: str) -> tuple[str, str, str]:
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
