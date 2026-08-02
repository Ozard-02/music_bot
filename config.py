"""Shared configuration, logging, and setup utilities."""

import html
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from spotiflac_patch import silence_spotiflac_loggers

os.environ.setdefault("TQDM_DISABLE", "1")  # suppress SpotiFLAC's tqdm progress bars


def esc(value) -> str:
    """HTML-escape arbitrary text for Telegram parse_mode='HTML' messages."""
    return html.escape(str(value), quote=False)


# Download engine
DEFAULT_SERVICES = ["qobuz", "deezer", "amazon"]
MAX_CONCURRENT = 2
PER_TRACK_TIMEOUT = 100
PER_TRACK_RETRIES = 3

# Queue
MAX_PARALLEL_JOBS = 3
MAX_QUEUE_RETRIES = 15
MAX_TRACK_RETRIES = 10  # give up on a track after this many failed attempts
MAX_DOWNLOAD_TIMEOUT = 28800  # 8h — last-resort cap on a whole job (per-track timeouts are the primary limiter)
MAX_QUEUE_AGE = 86400  # 24h — give up if item has been in queue this long
RETRY_BACKOFF_BASE = 5    # seconds, doubles each retry
MAX_RETRY_BACKOFF = 3600  # 1h cap
SCRIPT_MAX_CONCURRENT = 5  # default for utility scripts


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
    services = cfg.get("services", DEFAULT_SERVICES)
    if isinstance(services, str):
        services = [s.strip() for s in services.split(",")]
    return {
        "output_dir": cfg.get("downloadPath", os.path.expanduser("~/Music")),
        "filename_format": cfg.get("filenameTemplate", "{artist} - {title}"),
        "use_artist_subfolders": "{album_artist}" in folder_template,
        "use_album_subfolders": "{album}" in folder_template,
        "first_artist_only": cfg.get("useFirstArtistOnly", True),
        "embed_lyrics": cfg.get("embedLyrics", True),
        "quality": cfg.get("tidalQuality", "LOSSLESS"),
        "services": services,
        "max_download_timeout": int(cfg.get("maxDownloadTimeout", MAX_DOWNLOAD_TIMEOUT)),
    }


def setup_logger(log_path: str | Path | None = None) -> logging.Logger:
    if log_path is None:
        log_path = Path(__file__).parent / "spoty_loop.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3),
            logging.StreamHandler(),
        ],
    )
    logger = logging.getLogger("spoty_loop")
    logger.info("Logging to %s", log_path)

    silence_spotiflac_loggers()

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
