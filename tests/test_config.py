"""Tests for config.load_config."""

import json
import logging

from config import load_config


def test_download_path_tilde_is_expanded(tmp_path, monkeypatch):
    """config.default.json ships "~/Music": it must not become a literal ~ dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".spotiflac"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"downloadPath": "~/Music"}))
    cfg = load_config(logging.getLogger("test"))
    assert cfg["output_dir"] == str(tmp_path / "Music")


def test_missing_config_defaults_to_home_music(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(logging.getLogger("test"))
    assert cfg["output_dir"] == str(tmp_path / "Music")
