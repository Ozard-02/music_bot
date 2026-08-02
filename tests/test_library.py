"""Tests for library.py (per-user layout) and scripts/migrate_library.py."""

from pathlib import Path

from library import FOLDER_SUFFIX, QUALITY_CHOICES, user_cfg, user_folder_name


class TestUserFolderName:
    def test_username_suffix(self):
        assert user_folder_name("espo") == "espo_Music"

    def test_spaces_sanitized(self):
        assert user_folder_name("Guns N' Roses") == "Guns N' Roses_Music"

    def test_empty_username_uses_fallback(self):
        assert user_folder_name(None, fallback="bob") == "bob_Music"
        assert user_folder_name("", fallback="bob") == "bob_Music"


class TestUserCfg:
    def test_reroutes_output_dir_keeps_other_keys(self):
        cfg = {"output_dir": "/music", "quality": "LOSSLESS"}
        out = user_cfg(cfg, "espo_Music")
        assert out["output_dir"] == "/music/espo_Music"
        assert out["quality"] == "LOSSLESS"
        assert out is not cfg

    def test_original_cfg_untouched(self):
        cfg = {"output_dir": "/music"}
        user_cfg(cfg, "espo_Music")
        assert cfg["output_dir"] == "/music"


class TestQualityChoices:
    def test_contains_spotiflac_set(self):
        assert QUALITY_CHOICES == ["DOLBY_ATMOS", "HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"]

    def test_suffix_constant(self):
        assert FOLDER_SUFFIX == "_Music"


class TestMigrateLibrary:
    def _root(self, tmp_path: Path) -> Path:
        root = tmp_path / "Music"
        root.mkdir()
        (root / "ArtistA").mkdir()
        (root / "ArtistA" / "a.flac").write_bytes(b"x")
        (root / "playlist.m3u8").write_text("#EXTM3U\n")
        (root / "espo_Music").mkdir()  # user folder — ignored
        (root / "shared_Music").mkdir()  # reserved — ignored
        return root

    def test_moves_root_entries_into_owner_folder(self, tmp_path, monkeypatch, caplog):
        import logging

        root = self._root(tmp_path)
        monkeypatch.setattr(
            "scripts.migrate_library.load_config",
            lambda _logger: {"output_dir": str(root)},
        )
        from scripts.migrate_library import main

        with caplog.at_level(logging.INFO):
            main("espo")

        assert (root / "espo_Music" / "ArtistA" / "a.flac").exists()
        assert (root / "espo_Music" / "playlist.m3u8").exists()
        assert not (root / "ArtistA").exists()
        assert not (root / "playlist.m3u8").exists()

    def test_dry_run_moves_nothing(self, tmp_path, monkeypatch, caplog):
        import logging

        root = self._root(tmp_path)
        monkeypatch.setattr(
            "scripts.migrate_library.load_config",
            lambda _logger: {"output_dir": str(root)},
        )
        from scripts.migrate_library import main

        with caplog.at_level(logging.INFO):
            main("espo", dry_run=True)

        assert (root / "ArtistA" / "a.flac").exists()
        assert (root / "playlist.m3u8").exists()
        assert "Would move" in caplog.text

    def test_nothing_to_migrate_when_only_user_folders(self, tmp_path, monkeypatch):
        root = tmp_path / "Music"
        root.mkdir()
        (root / "espo_Music").mkdir()
        monkeypatch.setattr(
            "scripts.migrate_library.load_config",
            lambda _logger: {"output_dir": str(root)},
        )
        from scripts.migrate_library import main

        main("espo")  # should not raise
        assert (root / "espo_Music").exists()
