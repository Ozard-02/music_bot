"""Shared configuration, logging, and setup utilities (parent, stdlib-only)."""

import html
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

os.environ.setdefault("TQDM_DISABLE", "1")  # suppress SpotiFLAC's tqdm progress bars

# Subprocess entry point (download jobs + one-shot maintenance commands).
# Lives in the same dir as the parent, so paths are stable inside Docker.
JOB_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "download_job.py")


def esc(value) -> str:
    """HTML-escape arbitrary text for Telegram parse_mode='HTML' messages."""
    return html.escape(str(value), quote=False)


# Download engine
DEFAULT_SERVICES = ["qobuz", "deezer", "amazon"]
MAX_CONCURRENT = 2
PER_TRACK_TIMEOUT = 100
PER_TRACK_RETRIES = 3

# Queue
MAX_PARALLEL_JOBS = 2
MAX_QUEUE_RETRIES = 15
MAX_TRACK_RETRIES = 10  # give up on a track after this many failed attempts
MAX_DOWNLOAD_TIMEOUT = 7200  # 2h — last-resort cap on a whole job (per-track timeouts are the primary limiter)
STALL_TIMEOUT = 1800         # 30min — a job that makes no per-track progress this long is stuck
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
        "stall_timeout": int(cfg.get("stallTimeoutSeconds", STALL_TIMEOUT)),
    }


def silence_spotiflac_loggers() -> None:
    """No-op SpotiFLAC's own loggers (inlined so the parent never imports SpotiFLAC).

    Runs in the parent too, where it's a harmless no-op (no SpotiFLAC loggers
    exist yet); the subprocess calls setup_logger after importing SpotiFLAC
    and this silences them there.
    """
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("SpotiFLAC"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
    for noisy in ("httpx", "httpcore", "nodriver"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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