"""Tests for path helpers in track_utils."""

from track_utils import prune_empty_parents


def test_prunes_empty_ancestors_inside_root(tmp_path):
    root = tmp_path / "root"
    flac = root / "Artist" / "Album" / "track.flac"
    flac.parent.mkdir(parents=True)
    flac.write_bytes(b"")
    flac.unlink()  # as after a failed download / moved file

    prune_empty_parents(flac, root)

    assert not (root / "Artist" / "Album").exists()
    assert not (root / "Artist").exists()
    assert root.exists()


def test_never_prunes_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside" / "sub" / "track.flac"
    outside.parent.mkdir(parents=True)

    prune_empty_parents(outside, root)

    # Even though the outside folders are empty, they must survive: the path
    # does not belong to root.
    assert (tmp_path / "outside" / "sub").exists()
    assert (tmp_path / "outside").exists()
