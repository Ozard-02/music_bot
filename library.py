"""Per-user library layout helpers.

Each allowed Telegram user gets their own subfolder of the shared output
root: `~/Music/{username}_Music/`. The worker resolves an item's `cfg` to
that folder via `user_cfg()`, so downloads, pre-checks and m3u8 files all
land in the right place.
"""

from pathlib import Path

from track_utils import sanitize

# SpotiFLAC's documented quality set (launcher --quality help). LOSSLESS is
# the default; LOW/HIGH are lossy and the typical choice for guest users.
QUALITY_CHOICES = ["DOLBY_ATMOS", "HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"]

DEFAULT_QUALITY = "LOSSLESS"

# Folder suffix for user libraries. Kept as a constant so migration scripts
# and the bot agree on the naming scheme.
FOLDER_SUFFIX = "_Music"


def user_folder_name(username: str | None, fallback: str = "user") -> str:
    """Build the folder name for a Telegram user, e.g. `espo_Music`."""
    return f"{sanitize(username, fallback=fallback)}{FOLDER_SUFFIX}"


def user_cfg(cfg: dict, folder: str) -> dict:
    """Return a copy of `cfg` with output_dir pointed at the user's folder."""
    return {**cfg, "output_dir": str(Path(cfg["output_dir"]) / folder)}
