"""Maintenance cleanup of persisted tournament state, driven by the CLI.

All team-state operations go through the TeamRegistry singleton (not raw file
edits) so a server started in the same process right after cleanup sees
exactly the state that was written to disk.

Each function returns a list of human-readable action descriptions for the
CLI to print.
"""

from pathlib import Path

from core.bot_generator import LATEST_JSON
from core.teams import DEFAULT_TEAM_NAMES, GENERATED_DIR, TEAM_IDS, teams

REPO_ROOT = Path(__file__).parent.parent
UPLOADS_DIR = REPO_ROOT / "uploads"


def clear_code() -> list[str]:
    """Blank every team's code override and remove written upload files."""
    for n in TEAM_IDS:
        teams.set_code_override(n, "")
    removed = _unlink_all(UPLOADS_DIR.glob("player[1-4].py"))
    return [
        f"cleared code overrides for all teams, removed {removed} upload file(s)"
    ]


def clear_names() -> list[str]:
    for n in TEAM_IDS:
        teams.set_name(n, DEFAULT_TEAM_NAMES[n])
    return ["reset team names to defaults (TEAM 1..4)"]


def clear_bots() -> list[str]:
    """Delete generated bot files and clear the version index."""
    removed = _unlink_all(GENERATED_DIR.glob("p[1-4]_*.py"))
    if LATEST_JSON.exists():
        LATEST_JSON.write_text("{}", encoding="utf-8")
    for n in TEAM_IDS:
        teams.clear_generated(n)
    return [f"deleted {removed} generated bot file(s) and cleared the version index"]


def reset_teams() -> list[str]:
    """Same as the lobby's RESET TEAMS button: clears captains, contributions,
    ready flags, and code overrides; keeps names and the active set."""
    teams.reset_all()
    return [
        "reset team state (captains/contributions/ready/code cleared; "
        "names and active set kept)"
    ]


def factory_reset() -> list[str]:
    """Everything back to a fresh install: default team slots, no generated
    bots, no uploaded code."""
    actions = clear_bots() + clear_code()
    teams.wipe_all()
    actions.append(
        "wiped team state to factory defaults (names, captains, active set)"
    )
    return actions


def _unlink_all(paths) -> int:
    count = 0
    for p in paths:
        try:
            p.unlink()
            count += 1
        except OSError:
            pass
    return count
