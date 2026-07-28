"""Shared configuration, logging, and setup utilities."""

import json
import logging
import os
from pathlib import Path


# Download engine
SERVICES = ["qobuz", "amazon"]
MAX_CONCURRENT = 3
PER_TRACK_TIMEOUT = 180
PER_TRACK_RETRIES = 3

# Queue
MAX_QUEUE_RETRIES = 50
MAX_DOWNLOAD_TIMEOUT = 7200  # 2h — kill stuck downloads


def load_config(logger: logging.Logger) -> dict:
    path = os.path.expanduser("~/.spotiflac/config.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        logger.warning("Config not found at %s, using defaults", path)
        cfg = {}
    except json.JSONDecodeError as e:
        logger.warning("Config parse error at %s: %s, using defaults", path, e)
        cfg = {}
    folder_template = cfg.get("folderTemplate", "{album_artist}/{album}")
    return {
        "output_dir": cfg.get("downloadPath", os.path.expanduser("~/Music")),
        "filename_format": cfg.get("filenameTemplate", "{artist} - {title}"),
        "use_artist_subfolders": "{album_artist}" in folder_template,
        "use_album_subfolders": "{album}" in folder_template,
        "first_artist_only": cfg.get("useFirstArtistOnly", True),
        "embed_lyrics": cfg.get("embedLyrics", True),
        "quality": cfg.get("tidalQuality", "LOSSLESS"),
    }


def setup_logger(log_path: str | Path | None = None) -> logging.Logger:
    if log_path is None:
        log_path = Path(__file__).parent / "spoty_loop.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
    )
    logger = logging.getLogger("spoty_loop")
    logger.info("Logging to %s", log_path)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("SpotiFLAC"):
            logging.getLogger(name).setLevel(logging.WARNING)

    return logger


def bridge_community_session(logger: logging.Logger):
    desktop = os.path.expanduser("~/.spotiflac/community_session.json")
    module_path = os.path.expanduser("~/.spotiflac/signed_sessions/community_sessions.json")
    try:
        with open(desktop) as f:
            data = json.load(f)
        if data.get("session_id"):
            os.makedirs(os.path.dirname(module_path), exist_ok=True)
            with open(module_path, "w") as f:
                json.dump(data, f, indent=2)
            logger.info("Community session bridged")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("No desktop session to bridge: %s", e)
