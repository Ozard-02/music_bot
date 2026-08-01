"""Tests for fix_original_filenames.py — SpotiFLAC `_`-path → original-symbols path rename.

Regression for the dead-code bug where the rename logic was nested under
`if fpath == target: continue` and could never run.
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.fix_original_filenames import main


def _cfg(tmp_path: Path) -> dict:
    return {
        "output_dir": str(tmp_path),
        "filename_format": "{artist} - {title}",
        "first_artist_only": True,
    }


def _make_flac(dir: Path, name: str) -> Path:
    f = dir / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"fake")
    return f


def _fake_flac(tags: dict):
    audio = MagicMock()

    def get(tag, default=None):
        return [tags[tag]] if tag in tags else default

    audio.get.side_effect = get
    return audio


def _setup(tmp_path: Path):
    """Create a `_`-sanitized FLAC whose tags carry original symbols."""
    f = _make_flac(
        tmp_path / "Artist" / "Album_ With Colon",
        "Artist - Title_ Part.flac",
    )
    audio = _fake_flac(
        {
            "ARTIST": "Artist",
            "ALBUMARTIST": "Artist",
            "ALBUM": "Album: With Colon",
            "TITLE": "Title: Part",
        }
    )
    return f, audio


class TestFixOriginalFilenames:
    def test_renames_to_original_symbols_path_and_prunes_dir(self, tmp_path, caplog):
        src, audio = _setup(tmp_path)
        target = tmp_path / "Artist" / "Album: With Colon" / "Artist - Title: Part.flac"

        with caplog.at_level(logging.INFO), patch(
            "scripts.fix_original_filenames.load_config", return_value=_cfg(tmp_path)
        ), patch("scripts.fix_original_filenames.FLAC", return_value=audio):
            main()

        assert not src.exists()
        assert target.exists()
        # empty `_`-sanitized dirs pruned up to the output root
        assert not (tmp_path / "Artist" / "Album_ With Colon").exists()
        assert "RENAMED" in caplog.text

    def test_dry_run_leaves_file_untouched(self, tmp_path, caplog):
        src, audio = _setup(tmp_path)

        with caplog.at_level(logging.INFO), patch(
            "scripts.fix_original_filenames.load_config", return_value=_cfg(tmp_path)
        ), patch("scripts.fix_original_filenames.FLAC", return_value=audio):
            main(dry_run=True)

        assert src.exists()
        assert "WOULD RENAME" in caplog.text
        assert "RENAMED" not in caplog.text

    def test_matching_path_is_skipped(self, tmp_path, caplog):
        f = _make_flac(tmp_path / "Artist" / "Album", "Artist - Title.flac")
        audio = _fake_flac(
            {
                "ARTIST": "Artist",
                "ALBUMARTIST": "Artist",
                "ALBUM": "Album",
                "TITLE": "Title",
            }
        )

        with caplog.at_level(logging.INFO), patch(
            "scripts.fix_original_filenames.load_config", return_value=_cfg(tmp_path)
        ), patch("scripts.fix_original_filenames.FLAC", return_value=audio):
            main()

        assert f.exists()
        assert "RENAMED" not in caplog.text
