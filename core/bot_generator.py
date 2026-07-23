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
from core.config import load_settings

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
GENERATED_DIR = REPO_ROOT / "generated"
LATEST_JSON = GENERATED_DIR / "latest.json"

GENERATE_TIMEOUT = 300  # 5 minutes, per opencode invocation
MAX_PROMPT_CHARS = 2000

# The Exxeta/OpenAI-compatible endpoint intermittently ends the agent's turn
# with an empty completion (typically right after the big skill payloads load).
# opencode treats a no-tool-call turn as "done" and exits having written
# nothing. When that happens we nudge the SAME session with "continue" instead
# of failing - the same thing a human does by hand. This many extra nudges.
MAX_CONTINUE_ATTEMPTS = 3


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


def assemble_prompt(contributions: list) -> str:
    """Assemble a team's contributions into one merged prompt for the bot-writer.

    Contributions are joined with author labels and a merge preamble that asks
    the agent to reconcile conflicting ideas. Returns the assembled string.
    """
    if not contributions:
        return ""
    parts = [
        "This bot's strategy was co-designed by a team. Each member contributed "
        "a strategy idea below. Combine them into a single coherent bot. If two "
        "ideas conflict, use your best judgement to pick the one that makes the "
        "bot play better, and blend the rest.\n",
    ]
    for c in contributions:
        parts.append(f"--- From {c.nickname} ---\n{c.text.strip()}\n")
    return "\n".join(parts).strip()


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
        f"Test it with uv run core/test_bot.py {rel} until it passes. "
        f"Print BOT_WRITTEN: {rel} when done."
    )


_BOT_WRITTEN_RE = re.compile(r"BOT_WRITTEN:\s*(.+)")
# `--print-logs` writes `message=created id=ses_...` to stderr on session start.
_SESSION_ID_RE = re.compile(r"message=created id=(ses_\w+)")


async def _run_opencode(cmd: list) -> str:
    """Run one opencode invocation to completion; return combined stdout+stderr.

    Raises GenerateError on a missing binary or a per-invocation timeout.
    """
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
    return stdout + stderr


async def generate_bot(port: int, character: str, prompt: str) -> dict:
    """Spawn opencode to generate a bot. Returns a result dict.

    On success: {"ok": True, "path": ..., "version_id": ...}
    On failure: {"ok": False, "error": ...}

    If the agent ends its turn early without producing the file (a known
    intermittent glitch of the OpenAI-compatible provider), the same session is
    resumed with "continue" up to MAX_CONTINUE_ATTEMPTS times before giving up.
    """
    if len(prompt) > MAX_PROMPT_CHARS:
        raise GenerateError(f"Prompt too long (max {MAX_PROMPT_CHARS} chars)")

    output_path = generate_versioned_path(port, character, prompt)
    rel = output_path.relative_to(REPO_ROOT)
    message = _build_agent_message(character, port, prompt, output_path)

    # `--print-logs` surfaces the session id (needed to resume on an early
    # stop). --agent and --model must be repeated on every invocation, incl.
    # the continue nudges: without --agent the resume falls back to the default
    # agent, and without --model it falls back to the agent-file default model.
    base = ["opencode", "run", "--print-logs", "--auto", "--agent", "bot-writer"]
    # Model comes from [opencode] model in settings.toml. If unset, opencode
    # falls back to the default declared in .opencode/agents/bot-writer.md.
    model = (load_settings().get("opencode") or {}).get("model", "").strip()
    if model:
        base += ["--model", model]

    log.info("Generating bot for port %d (%s) -> %s", port, character, rel)

    combined = ""
    session_id = None
    for attempt in range(MAX_CONTINUE_ATTEMPTS + 1):
        if attempt == 0:
            cmd = base + [message]
        else:
            if session_id is None:
                # Never learned the session id (start failed) - can't resume.
                break
            log.warning(
                "Bot-writer stopped early for port %d (attempt %d/%d); "
                "resuming session %s with 'continue'",
                port,
                attempt,
                MAX_CONTINUE_ATTEMPTS,
                session_id,
            )
            cmd = base + ["--session", session_id, "continue"]

        combined += await _run_opencode(cmd)

        if session_id is None:
            sid_match = _SESSION_ID_RE.search(combined)
            if sid_match:
                session_id = sid_match.group(1)

        # Check for a BOT_WRITTEN marker; the agent may have written to a
        # different path than we asked for.
        match = _BOT_WRITTEN_RE.search(combined)
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

        if output_path.exists():
            break

    if not output_path.exists():
        tail = combined[-500:]
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
