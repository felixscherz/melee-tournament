"""Central settings loader.

All runtime config lives in ``config/settings.toml``. That file is per-machine
and gitignored; the committed ``config/settings.example.toml`` holds
localhost/placeholder defaults. On first checkout there is no ``settings.toml``,
so we transparently fall back to the example — the app boots on a fresh clone
with no setup, and a warning tells the user to make their own copy.
"""

import logging
from pathlib import Path

import toml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "settings.toml"
EXAMPLE_PATH = REPO_ROOT / "config" / "settings.example.toml"


def load_settings() -> dict:
    """Load settings.toml, falling back to settings.example.toml.

    Raises FileNotFoundError only if neither file exists.
    """
    if CONFIG_PATH.exists():
        return toml.load(CONFIG_PATH)
    if EXAMPLE_PATH.exists():
        log.warning(
            "config/settings.toml not found — using committed defaults from "
            "config/settings.example.toml. Copy it to config/settings.toml and "
            "edit it for your setup: cp config/settings.example.toml config/settings.toml"
        )
        return toml.load(EXAMPLE_PATH)
    raise FileNotFoundError(
        f"No settings file found. Expected {CONFIG_PATH} or {EXAMPLE_PATH}."
    )
