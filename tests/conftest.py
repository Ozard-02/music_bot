"""Shared fixtures for spoty_loop tests."""

import logging
from pathlib import Path

import pytest

from queue_manager import QueueManager


@pytest.fixture
def config():
    return {
        "output_dir": "/tmp/test_music",
        "filename_format": "{artist} - {title}",
        "use_artist_subfolders": True,
        "use_album_subfolders": True,
        "first_artist_only": True,
        "embed_lyrics": True,
        "quality": "LOSSLESS",
    }


@pytest.fixture
def logger():
    return logging.getLogger("test")


@pytest.fixture
def queue_manager(tmp_path: Path) -> QueueManager:
    db = tmp_path / "test_queue.db"
    return QueueManager(str(db))
