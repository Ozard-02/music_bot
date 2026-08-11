"""Shared fixtures for spoty_loop tests."""

import logging
import shutil
from pathlib import Path

import pytest

from queue_manager import QueueManager


@pytest.fixture
def config():
    _path = Path("/tmp/test_music")
    if _path.exists():
        shutil.rmtree(_path)
    return {
        "output_dir": str(_path),
        "filename_format": "{artist} - {title}",
        "use_artist_subfolders": True,
        "use_album_subfolders": True,
        "first_artist_only": True,
        "embed_lyrics": True,
        "quality": "LOSSLESS",
        "services": ["qobuz", "deezer", "amazon"],
    }


@pytest.fixture
def logger():
    return logging.getLogger("test")


@pytest.fixture
def queue_manager(tmp_path: Path) -> QueueManager:
    db = tmp_path / "test_queue.db"
    return QueueManager(str(db))
