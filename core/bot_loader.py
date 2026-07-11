"""
Dynamic hot-reloader for user-uploaded bot scripts.

Uses importlib to load .py files from the uploads/ directory.
Validates each module against the BotBase interface before activating it.
"""

import importlib.util
import logging
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Optional

from core.bot_validator import BotValidationError, validate_bot_code

log = logging.getLogger(__name__)

REQUIRED_METHODS = ("act",)


class BotLoader:
    def __init__(self, upload_dir: Path):
        self.upload_dir = upload_dir
        self.upload_dir.mkdir(exist_ok=True)
        self._active_bot = None
        self._active_path: Optional[Path] = None
        self._active_mtime: float = 0.0

    def load(self, script_path: Path) -> bool:
        """Load and validate a bot script. Returns True on success.

        Static safety validation runs here, at the single point where code is
        actually executed — not just in the API layer. Any path that reaches
        `exec_module` (direct on-disk edits, mtime hot-reloads) is therefore
        forced through the same check, so the validator cannot be bypassed by
        writing to `uploads/` out of band.
        """
        try:
            try:
                source = script_path.read_text(encoding="utf-8")
            except OSError as exc:
                log.error("Could not read bot %s: %s", script_path.name, exc)
                return False
            try:
                validate_bot_code(source)
            except BotValidationError as exc:
                log.error("Rejected unsafe bot %s: %s", script_path.name, exc)
                return False
            mod = self._import(script_path)
            if not self._validate(mod):
                return False
            bot_instance = mod.Bot()
            self._active_bot = bot_instance
            self._active_path = script_path
            self._active_mtime = script_path.stat().st_mtime
            log.info("Loaded bot from %s", script_path.name)
            return True
        except Exception as exc:
            log.error("Failed to load bot %s: %s", script_path.name, exc)
            return False

    def get_active_bot(self):
        """Return the active bot instance, hot-reloading if the file changed."""
        if self._active_path is None:
            return None
        try:
            mtime = self._active_path.stat().st_mtime
            if mtime != self._active_mtime:
                log.info("Detected change in %s — hot-reloading", self._active_path.name)
                self.load(self._active_path)
        except FileNotFoundError:
            log.warning("Active bot file removed; deactivating")
            self._active_bot = None
            self._active_path = None
        return self._active_bot

    def deactivate(self):
        self._active_bot = None
        self._active_path = None
        self._active_mtime = 0.0
        log.info("Bot deactivated; falling back to LLM")

    def _import(self, path: Path) -> ModuleType:
        module_name = f"user_bot_{int(time.time())}"
        # Remove stale module if present to force a fresh import
        sys.modules.pop(module_name, None)
        spec = importlib.util.spec_from_file_location(module_name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod

    def _validate(self, mod: ModuleType) -> bool:
        if not hasattr(mod, "Bot"):
            log.error("Bot script missing top-level 'Bot' class")
            return False
        for method in REQUIRED_METHODS:
            if not callable(getattr(mod.Bot, method, None)):
                log.error("Bot class missing required method: %s", method)
                return False
        return True
