"""Tests for playlists.py — local m3u8 create/empty helpers (stdlib only)."""

from pathlib import Path

from playlists import create_empty, empty_playlist


def test_create_empty_playlist(tmp_path: Path):
    result = create_empty(tmp_path, "My Playlist")

    path = tmp_path / "My Playlist.m3u8"
    assert result["file_name"] == "My Playlist"
    assert result["path"] == str(path)
    assert path.read_text(encoding="utf-8") == "#EXTM3U\n"


def test_create_empty_refuses_existing(tmp_path: Path):
    (tmp_path / "Trash.m3u8").write_text("#EXTM3U\n", encoding="utf-8")

    result = create_empty(tmp_path, "Trash")

    assert result["error"] == "exists"
    assert result["file_name"] == "Trash"


def test_create_empty_reports_write_error(tmp_path: Path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")

    result = create_empty(blocked, "Trash")

    assert result["error"] == "write_error"


def test_empty_playlist_deletes_flac_and_sidecar_keeps_other_files(tmp_path: Path):
    root = tmp_path / "lib"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    flac = album / "Artist - Song.flac"
    flac.write_bytes(b"audio")
    lrc = flac.with_suffix(".lrc")
    lrc.write_text("[00:01] lyric", encoding="utf-8")
    cover = album / "cover.jpg"
    cover.write_bytes(b"img")
    (root / "Trash.m3u8").write_text(
        "#EXTM3U\nArtist/Album/Artist - Song.flac\nArtist/Album/cover.jpg\n",
        encoding="utf-8",
    )

    result = empty_playlist(root, "Trash")

    assert result["deleted"] == 1
    assert result["not_found"] == 1
    assert not flac.exists()
    assert not lrc.exists()
    assert cover.exists()
    assert (root / "Trash.m3u8").read_text(encoding="utf-8") == "#EXTM3U\n"


def test_empty_playlist_prunes_empty_album_and_artist(tmp_path: Path):
    root = tmp_path / "lib"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "Artist - Song.flac").write_bytes(b"audio")
    (root / "Trash.m3u8").write_text(
        "#EXTM3U\nArtist/Album/Artist - Song.flac\n", encoding="utf-8"
    )

    result = empty_playlist(root, "Trash")

    assert result["deleted"] == 1
    assert result["pruned"] == 2
    assert not album.exists()
    assert not (root / "Artist").exists()
    assert root.is_dir()


def test_empty_playlist_blocks_traversal_and_absolute_paths(tmp_path: Path):
    root = tmp_path / "lib"
    root.mkdir()
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"keep")
    (root / "Trash.m3u8").write_text(
        "#EXTM3U\n../outside.flac\n/absolute.flac\n", encoding="utf-8"
    )

    result = empty_playlist(root, "Trash")

    assert outside.exists()
    assert result["deleted"] == 0
    assert result["not_found"] == 2


def test_empty_playlist_not_found_lists_available(tmp_path: Path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "A.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    (root / "B.m3u8").write_text("#EXTM3U\n", encoding="utf-8")

    result = empty_playlist(root, "Trash")

    assert result["error"] == "not_found"
    assert result["avail"] == ["A", "B"]


def test_empty_playlist_removes_missing_log_and_temp(tmp_path: Path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "Trash.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    temp = root / "temp"
    temp.mkdir()
    (temp / "Trash_missing.txt").write_text("missing", encoding="utf-8")

    result = empty_playlist(root, "Trash")

    assert result["deleted"] == 0
    assert not temp.exists()
