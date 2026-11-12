"""Tests for config.load_config."""

import json
import logging

from config import (
    allowed_user_ids,
    file_log_path,
    first_allowed_user_id,
    load_config,
)


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


def test_allowed_user_ids_merges_list_and_legacy():
    env = {
        "TELEGRAM_ALLOWED_USER_IDS": "1, 2, not-a-number",
        "TELEGRAM_ALLOWED_USER_ID": "3",
    }
    assert allowed_user_ids(env) == {1, 2, 3}
    assert first_allowed_user_id(env) == 1
    assert allowed_user_ids({}) == set()
    assert first_allowed_user_id({}) is None


def test_file_log_path_finds_file_handler(tmp_path):
    logger = logging.getLogger("test.file_log_path")
    handler = logging.FileHandler(tmp_path / "test.log")
    logger.addHandler(handler)
    try:
        assert file_log_path(logger) == str(tmp_path / "test.log")
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert file_log_path(logging.getLogger("test.no_handlers")) is None
