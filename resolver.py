"""Parse user input into (type, value).

The parent imports this module for `parse_inputs`/`format_help` only; search
matching lives in `resolver_match.py`, which only the download subprocess and
the maintenance scripts import."""

import re

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
        "  /rmplaylist [name] \u2014 delete every track listed in &lt;name&gt;.m3u8 (default Trash) and prune empty folders\n"
        "  /spotify [token|client|market|test|clear] \u2014 optional official Spotify search credentials"
    )

