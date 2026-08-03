# Metadata & the "split album" problem

## The problem

In Navidrome (and other tag-based servers), one album sometimes shows up as
several releases with the **same album name**. Symptom: open an artist and see
`MADAME`, `MADAME`, `MADAME`, `MADAME`.

Navidrome groups songs into an album by `(album name, album artist)` and uses
`MUSICBRAINZ_ALBUMID` to tell releases apart. So any file whose MusicBrainz
album ID doesn't match its siblings gets its own album entry.

## Root cause

The `MUSICBRAINZ_*` tags are added by **Qobuz metadata enrichment** during
download. They are often wrong:

- The enrichment maps a track to a **Various-Artists compilation** instead of
  the artist's own album (e.g. a track from *MADAME* tagged with the MB ID of
  *Hot Party Summer 2021*).
- Result: 3 tracks carrying bogus MB album IDs → 4 "MADAME" albums, all with
  the same name.

Related problems found in the same library:

- **Tagless files** — a track with no tags at all (empty/`Lavf`-only Vorbis
  comment) shows up as "Unknown album".
- **Wrong enrichment match** — a track whose Qobuz/Apple enrichment resolved to
  a different artist's release (wrong album artist, wrong label, wrong year).

## The fix: `/fixmetadata`

Re-tags files through the **SpotiFLAC metadata pipeline** — the same code that
tags new downloads — so tags stay consistent with what the downloader writes.

Since SpotiFLAC 1.5.9, fresh downloads also no-op the MusicBrainz lookup at
download time (`spotiflac_patch._patch_musicbrainz`), so new tracks never get
bogus `MUSICBRAINZ_*` IDs in the first place — `/fixmetadata` is only needed
for files downloaded before that patch.

For every FLAC in the target folder it:

1. Resolves the track's real identity: from the embedded `open.spotify.com`
   track URL, or by searching Spotify from the `Artist - Title` filename
   (used for tagless files).
2. Re-embeds metadata with `embed_metadata_async(..., enrich=True,
   enrich_providers=["apple", "deezer", "soundcloud"])`.
   - Apple Music is the **first** enrichment source, Deezer/SoundCloud are
     fallbacks.
   - `SpotiFLAC`'s FLAC tagger wipes the old Vorbis comment and **strips every
     `MUSICBRAINZ_*` tag** (patched in `SpotiFLAC/core/tagger.py`) — the
     bogus IDs that caused the split are gone by construction.
   - Album, album artist, title, artist(s), track number, year and cover come
     from Spotify (authoritative and consistent).
3. If the track's real album (per Spotify) doesn't match the folder it sits in,
   it is **moved to the folder of its real album** (never deleted). E.g. a
   single-track folder `OK/` whose song actually belongs to `DISINCANTO/`.

### Usage

CLI (reads tags, applies nothing by default):

```bash
python scripts/fix_metadata.py Albums/MADAME             # dry run — prints the plan
python scripts/fix_metadata.py Albums/MADAME --apply     # write tags
python scripts/fix_metadata.py /path/to/library --apply  # walk a whole library
```

Telegram bot (always applies):

```
/fixmetadata MADAME
```

The folder argument is resolved against the bot's output dir (`~/Music`, or
`/root/Music` inside Docker). Running it on the library root fixes every album
folder at once.

### After running

Navidrome does **not** detect tag changes by itself — trigger a full library
rescan (Settings → Scan) so the split albums collapse back into one.

### Safety

- `--dry-run` is the default; `--apply` is explicit.
- Nothing is ever deleted — misplaced files are *moved*, never removed.
- Idempotent: running it twice changes nothing the second time.
- A file that fails (network error, corrupt FLAC) is reported and the run
  continues; it never aborts the rest.
- Filenames are left untouched (except the move into the real album folder).

### Related one-off scripts

- `scripts/fix_covers.py` — re-embeds Spotify cover art into every FLAC with a
  Spotify track URL (same engine as `/fixmetadata`'s cover refresh).
- `scripts/archive/fix_mb_tags.py` — strips `MUSICBRAINZ_*` from every FLAC
  (the same cleanup, offline, but does not retag anything else); superseded by
  `/fixmetadata`.
- `scripts/archive/retag_missing.py` — retags a hardcoded list of tagless files
  (a manual predecessor of `fix_metadata.py`).

## Cover art

Two problems: (1) Qobuz enrichment returns wrong HD covers for some albums (e.g. Ditonellapiaga "Chimica"), and (2) enrichment covers shouldn't override the artist's real album art. Both are handled by `flac_utils.resolve_cover_data(track)` — the single cover-quality resolver used by downloads (`downloader._fix_cover`) and `/fixmetadata` (`scripts/fix_metadata.py`):

1. **Apple HD first** — enriches via Apple (`enrich_metadata_async(providers=["apple"])`); if an ISRC-matched Apple release exposes `cover_url_hd`, the URL is bumped to 3000×3000 (`upgrade_apple_cover`: `100x100`→`3000x3000`) and fetched. High-res, Apple-verified.
2. **Spotify fallback** — if Apple yields nothing, uses the track's Spotify `cover_url`, upgraded `1e02`→`b273` for 640×640 (`upgrade_cover_url`).
3. **Embed, never enrich-overwrite** — the chosen bytes are passed as `cover_data` into `SpotiFLAC`'s tagger (its `if not cover_data` guard means enrichment covers can never override it); `flac_utils.embed_cover()` writes a single JPEG front cover (dimensions read via `track_utils.get_jpeg_dimensions`) after replacing the file's pictures.

Bad-provider guard: downloads enrich with `["apple","deezer","soundcloud"]` (qobuz excluded — see [STRUCTURE.md#core-download-engine](STRUCTURE.md#core-download-engine)), so the qobuz wrong-cover source never feeds `enrich_providers`.

Cover management API (`resolve_cover_data`, `upgrade_apple_cover`, `upgrade_cover_url`, `fetch_cover`, `embed_cover`): [STRUCTURE.md#flac_utils](STRUCTURE.md#flac_utils). Re-embed on existing files: `python scripts/fix_covers.py` (wraps `maintenance.rescan_library()`).
