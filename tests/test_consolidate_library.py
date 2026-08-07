"""Tests for scripts/consolidate_library.py — consolidate scattered FLACs into
the canonical album_artist/album layout; remove only provable duplicates."""

import base64
import logging
from pathlib import Path

from scripts.consolidate_library import consolidate

log = logging.getLogger("test")


_MINIMAL_FLAC_B64 = (
    "ZkxhQwAAACIQABAAAABFAABFAPoA8AAAACh9IM/5764f4/t0wbC25gK2AwAAEgAAAAA"
    "AAAAAAAAAAAAAAAAAKAQAAFUgAAAAcmVmZXJlbmNlIGxpYkZMQUMgMS41LjAgMjAyNTAy"
    "MTEBAAAAKQAAAFVSTD1odHRwczovL29wZW4uc3BvdGlmeS5jb20vdHJhY2svYWJjMTIz"
    "gQAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAD/+GwIACcEZ0YCWAnWD+MN5rSrjGIm5zGFI0Bq/dcDtU2sERH4E1LXYH5qam2UV1nU"
    "5vxmcNEQq+yXf4DMr4g5OhCwVK8="
)


def _write_flac(path: Path, *, title, artist, album_artist, album, track_id) -> None:
    from mutagen.flac import FLAC

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(_MINIMAL_FLAC_B64))
    f = FLAC(str(path))
    f["TITLE"] = title
    f["ARTIST"] = artist
    f["ALBUMARTIST"] = album_artist
    f["ALBUM"] = album
    f["URL"] = f"https://open.spotify.com/track/{track_id}"
    f.save(str(path))


def _cfg(tmp_path):
    return {
        "output_dir": str(tmp_path),
        "filename_format": "{artist} - {title}",
        "first_artist_only": True,
    }


def test_moves_scattered_file_to_canonical_layout(tmp_path):
    cfg = _cfg(tmp_path)
    scattered = tmp_path / "The Cars" / "Soundtrack_ Live" / "The Cars - Drive On.flac"
    _write_flac(scattered, title="Drive On", artist="The Cars",
                album_artist="Various Artists", album="Soundtrack: Live", track_id="t1")

    counts = consolidate(cfg, dry_run=False)

    assert counts["moved"] == 1
    assert not scattered.exists()
    canonical = tmp_path / "Various Artists" / "Soundtrack: Live" / "The Cars - Drive On.flac"
    assert canonical.exists()


def test_sidecar_moves_with_flac(tmp_path):
    cfg = _cfg(tmp_path)
    scattered = tmp_path / "The Cars" / "Soundtrack_ Live" / "The Cars - Drive On.flac"
    _write_flac(scattered, title="Drive On", artist="The Cars",
                album_artist="Various Artists", album="Soundtrack: Live", track_id="t1")
    scattered.with_suffix(".lrc").write_text("[00:01.00]hello\n")

    consolidate(cfg, dry_run=False)

    canonical = tmp_path / "Various Artists" / "Soundtrack: Live" / "The Cars - Drive On.flac"
    assert canonical.exists()
    assert canonical.with_suffix(".lrc").read_text() == "[00:01.00]hello\n"
    assert not scattered.exists()


def test_removes_proven_duplicate_same_id(tmp_path):
    cfg = _cfg(tmp_path)
    canonical = tmp_path / "Various Artists" / "Soundtrack: Live" / "The Cars - Drive On.flac"
    duplicate = tmp_path / "The Cars" / "Soundtrack_ Live" / "The Cars - Drive On.flac"
    _write_flac(canonical, title="Drive On", artist="The Cars",
                album_artist="Various Artists", album="Soundtrack: Live", track_id="t1")
    _write_flac(duplicate, title="Drive On", artist="The Cars",
                album_artist="Various Artists", album="Soundtrack: Live", track_id="t1")

    counts = consolidate(cfg, dry_run=False)

    assert counts["deduped"] == 1
    assert canonical.exists()
    assert not duplicate.exists()


def test_different_id_keeps_both(tmp_path):
    cfg = _cfg(tmp_path)
    canonical = tmp_path / "Various Artists" / "Soundtrack: Live" / "The Cars - Drive On.flac"
    duplicate = tmp_path / "The Cars" / "Soundtrack_ Live" / "The Cars - Drive On.flac"
    _write_flac(canonical, title="Drive On", artist="The Cars",
                album_artist="Various Artists", album="Soundtrack: Live", track_id="t1")
    _write_flac(duplicate, title="Drive On", artist="The Cars",
                album_artist="Various Artists", album="Soundtrack: Live", track_id="t2")

    counts = consolidate(cfg, dry_run=False)

    assert counts["skipped"] == 1
    assert canonical.exists()
    assert duplicate.exists()


def test_unknown_id_keeps_both(tmp_path):
    cfg = _cfg(tmp_path)
    canonical = tmp_path / "Various Artists" / "Soundtrack: Live" / "The Cars - Drive On.flac"
    duplicate = tmp_path / "The Cars" / "Soundtrack_ Live" / "The Cars - Drive On.flac"
    _write_flac(canonical, title="Drive On", artist="The Cars",
                album_artist="Various Artists", album="Soundtrack: Live", track_id="t1")
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_bytes(base64.b64decode(_MINIMAL_FLAC_B64))  # no URL tag

    counts = consolidate(cfg, dry_run=False)

    assert counts["skipped"] == 1
    assert canonical.exists()
    assert duplicate.exists()


def test_dry_run_changes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    scattered = tmp_path / "The Cars" / "Soundtrack_ Live" / "The Cars - Drive On.flac"
    _write_flac(scattered, title="Drive On", artist="The Cars",
                album_artist="Various Artists", album="Soundtrack: Live", track_id="t1")
    before = scattered.read_bytes()

    counts = consolidate(cfg, dry_run=True)

    assert counts["moved"] == 1
    assert scattered.exists()
    assert scattered.read_bytes() == before


def test_already_canonical_untouched(tmp_path):
    cfg = _cfg(tmp_path)
    canonical = tmp_path / "Various Artists" / "Soundtrack: Live" / "The Cars - Drive On.flac"
    _write_flac(canonical, title="Drive On", artist="The Cars",
                album_artist="Various Artists", album="Soundtrack: Live", track_id="t1")

    counts = consolidate(cfg, dry_run=False)

    assert counts == {"moved": 0, "deduped": 0, "skipped": 0, "errors": 0}
    assert canonical.exists()


def test_never_leaves_user_folder(tmp_path):
    """Multi-user layout: a file already canonical inside {username}_Music
    must stay there — never move up into the shared root."""
    cfg = _cfg(tmp_path)
    inside = tmp_path / "Espo02_Music" / "Stromae" / "Cheese" / "Stromae - Dodo.flac"
    _write_flac(inside, title="Dodo", artist="Stromae",
                album_artist="Stromae", album="Cheese", track_id="t1")

    counts = consolidate(cfg, dry_run=False)

    assert counts == {"moved": 0, "deduped": 0, "skipped": 0, "errors": 0}
    assert inside.exists()
    assert not (tmp_path / "Stromae" / "Cheese" / "Stromae - Dodo.flac").exists()


def test_scattered_file_stays_inside_user_folder(tmp_path):
    cfg = _cfg(tmp_path)
    scattered = tmp_path / "Espo02_Music" / "The Cars" / "Soundtrack_ Live" / "The Cars - Drive On.flac"
    _write_flac(scattered, title="Drive On", artist="The Cars",
                album_artist="Various Artists", album="Soundtrack: Live", track_id="t1")

    counts = consolidate(cfg, dry_run=False)

    assert counts["moved"] == 1
    assert not scattered.exists()
    canonical = tmp_path / "Espo02_Music" / "Various Artists" / "Soundtrack: Live" / "The Cars - Drive On.flac"
    assert canonical.exists()
    assert not (tmp_path / "Various Artists" / "Soundtrack: Live" / "The Cars - Drive On.flac").exists()
