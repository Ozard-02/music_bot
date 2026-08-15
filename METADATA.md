# Metadata & the "split album" problem

## The problem

In Navidrome (and other tag-based servers), one album sometimes shows up as
several releases with the **same album name**. Symptom: open an artist and see
`MADAME`, `MADAME`, `MADAME`.

Navidrome groups songs into an album by `(album name, album artist)` and uses
`MUSICBRAINZ_ALBUMID` to tell releases apart. Any file whose MusicBrainz album
ID doesn't match its siblings gets its own album entry.

## Root cause

The `MUSICBRAINZ_*` tags are added by **Qobuz metadata enrichment** during
download. They are often wrong — e.g. a track tagged with the MB ID of a
Various-Artists compilation instead of the artist's own album.

Related problems found in the same library:

- **Tagless files** — no tags at all (empty/`Lavf`-only Vorbis comment) shows
  up as "Unknown album".
- **Wrong enrichment match** — enrichment resolved to a different artist's
  release (wrong album artist, wrong label, wrong year).

## The fix: `/fixmetadata`

Re-tags files through the **SpotiFLAC metadata pipeline** — the same code that
tags new downloads — so tags stay consistent with what the downloader writes.

Since SpotiFLAC 1.5.9, fresh downloads no-op the MusicBrainz lookup at download
time (`spotiflac_patch._patch_musicbrainz`), so new tracks never get bogus
`MUSICBRAINZ_*` IDs in the first place. `/fixmetadata` is only needed for files
downloaded before that patch.

## In the rewrite

`/fixmetadata` and `/mkplaylist` run as **subprocess-triggered commands**: the
parent spawns `download_job.py` with a `cmd` mode (not a download) so the
SpotiFLAC import cost never lands in the parent. Same JSON-lines protocol; the
`result` event carries the command's outcome. CLI entry points in `scripts/`
remain for manual runs.