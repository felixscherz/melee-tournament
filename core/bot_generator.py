"""
Bot generator - spawns an opencode agent to write bot code from a prompt.

Manages the `generated/` directory of versioned bot files and the
`generated/latest.json` index that maps port -> most recently generated bot.
"""

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.bot_validator import BotValidationError, validate_bot_code

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
GENERATED_DIR = REPO_ROOT / "generated"
LATEST_JSON = GENERATED_DIR / "latest.json"

GENERATE_TIMEOUT = 300  # 5 minutes
MAX_PROMPT_CHARS = 2000


class GenerateError(Exception):
    """Raised when bot generation fails."""


def _ensure_dirs():
    GENERATED_DIR.mkdir(exist_ok=True)


def _version_id(prompt: str, ts: float) -> str:
    raw = f"{prompt}:{ts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def generate_versioned_path(port: int, character: str, prompt: str) -> Path:
    """Build a versioned file path for a new generated bot."""
    _ensure_dirs()
    ts = time.time()
    stamp = datetime.fromtimestamp(ts).strftime("%Y%m%d_%H%M%S")
    char_slug = character.lower()
    vid = _version_id(prompt, ts)
    return GENERATED_DIR / f"p{port}_{char_slug}_{stamp}_{vid}.py"


def read_latest() -> dict:
    """Read the latest.json index. Returns {} if missing or corrupt."""
    if not LATEST_JSON.exists():
        return {}
    try:
        return json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_latest(port: int, entry: dict):
    """Update the latest.json entry for a single port."""
    _ensure_dirs()
    data = read_latest()
    data[str(port)] = entry
    LATEST_JSON.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_generated_path(port: int) -> Optional[Path]:
    """Return the latest generated bot path for a port, or None."""
    data = read_latest()
    entry = data.get(str(port))
    if entry is None:
        return None
    p = Path(entry["path"])
    if not p.is_absolute():
        p = REPO_ROOT / p
    if p.exists():
        return p
    return None


def _build_agent_message(
    character: str, port: int, prompt: str, output_path: Path
) -> str:
    rel = (
        output_path.relative_to(REPO_ROOT) if output_path.is_absolute() else output_path
    )
    return (
        f"Character: {character}\n"
        f"Port: {port}\n"
        f"Prompt: {prompt}\n"
        f"Output file: {rel}\n\n"
        f"Load the libmelee-bot-interface and melee-strategy skills first. "
        f"Then write the bot to {rel}. "
        f"Test it with .venv/bin/python core/test_bot.py {rel} until it passes. "
        f"Print BOT_WRITTEN: {rel} when done."
    )


_BOT_WRITTEN_RE = re.compile(r"BOT_WRITTEN:\s*(.+)")


async def generate_bot(port: int, character: str, prompt: str) -> dict:
    """Spawn opencode to generate a bot. Returns a result dict.

    On success: {"ok": True, "path": ..., "version_id": ...}
    On failure: {"ok": False, "error": ...}
    """
    if len(prompt) > MAX_PROMPT_CHARS:
        raise GenerateError(f"Prompt too long (max {MAX_PROMPT_CHARS} chars)")

    output_path = generate_versioned_path(port, character, prompt)
    rel = output_path.relative_to(REPO_ROOT)
    message = _build_agent_message(character, port, prompt, output_path)

    cmd = ["opencode", "run", "--auto", "--agent", "bot-writer", message]

    log.info("Generating bot for port %d (%s) -> %s", port, character, rel)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(REPO_ROOT),
        )
    except FileNotFoundError:
        raise GenerateError("opencode binary not found in PATH")

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=GENERATE_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise GenerateError(f"Generation timed out after {GENERATE_TIMEOUT}s")

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    # Check for BOT_WRITTEN marker in the output
    match = _BOT_WRITTEN_RE.search(stdout + stderr)
    if match:
        marker_path = match.group(1).strip()
        marker_full = (
            REPO_ROOT / marker_path
            if not Path(marker_path).is_absolute()
            else Path(marker_path)
        )
        if marker_full != output_path:
            log.warning("Agent wrote to %s, expected %s", marker_full, output_path)
            output_path = marker_full

    if not output_path.exists():
        tail = (stdout + stderr)[-500:]
        raise GenerateError(f"Agent did not produce a file. Output tail:\n{tail}")

    # Validate the generated code
    code = output_path.read_text(encoding="utf-8")
    try:
        validate_bot_code(code)
    except BotValidationError as exc:
        raise GenerateError(f"Generated code failed validation: {exc}")

    vid = output_path.stem.split("_")[-1]
    entry = {
        "path": str(rel),
        "character": character,
        "prompt": prompt,
        "version_id": vid,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_latest(port, entry)

    log.info("Bot generated for port %d: %s (version %s)", port, rel, vid)
    return {"ok": True, "path": str(rel), "version_id": vid}
