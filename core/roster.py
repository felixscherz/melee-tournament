"""Shared, single source of truth for the selectable Melee roster.

Keys are `melee.Character` enum names (also what the API/lobby send over the
wire); values are human-friendly display names for the dropdown. Non-playable
entries (wireframes, Giga Bowser, Sandbag, Nana) are intentionally excluded.
"""

from pathlib import Path

import melee

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BOTS_DIR = _REPO_ROOT / "core" / "bots"

# enum name -> display name, in the usual CSS-ish ordering.
SELECTABLE_CHARACTERS: dict[str, str] = {
    "MARIO": "Mario",
    "DOC": "Dr. Mario",
    "LUIGI": "Luigi",
    "BOWSER": "Bowser",
    "PEACH": "Peach",
    "YOSHI": "Yoshi",
    "DK": "Donkey Kong",
    "CPTFALCON": "Captain Falcon",
    "GANONDORF": "Ganondorf",
    "FALCO": "Falco",
    "FOX": "Fox",
    "NESS": "Ness",
    "POPO": "Ice Climbers",
    "KIRBY": "Kirby",
    "SAMUS": "Samus",
    "ZELDA": "Zelda",
    "SHEIK": "Sheik",
    "LINK": "Link",
    "YLINK": "Young Link",
    "PICHU": "Pichu",
    "PIKACHU": "Pikachu",
    "JIGGLYPUFF": "Jigglypuff",
    "MEWTWO": "Mewtwo",
    "GAMEANDWATCH": "Mr. Game & Watch",
    "MARTH": "Marth",
    "ROY": "Roy",
}

# enum name -> melee.Character, built from the roster above.
CHARACTER_MAP: dict[str, melee.Character] = {
    name: melee.Character[name] for name in SELECTABLE_CHARACTERS
}


def is_valid_character(name: str) -> bool:
    return name in SELECTABLE_CHARACTERS


# Hand-tuned default bots. Characters without a specific entry fall through to
# generic.py — a position-only chase-and-attack bot that plays everyone the same
# simple way. Used both by the lobby (frontend/app.py) when no pasted/generated
# bot is provided, and by the orchestrator as the in-process fallback when a
# subprocess worker dies mid-match (see core/bot_process.py +
# IMPROVE_BOT_ISOLATION.md). These files are trusted code shipped with the
# repo, so running them in-process (no sandbox) is safe.
_DEFAULT_BOT_FILES = {
    "FOX": "fox.py",
    "MARTH": "marth.py",
    "CPTFALCON": "falcon.py",
    "FALCO": "falco.py",
}
_GENERIC_BOT_FILE = "generic.py"


def default_bot_path(character: str) -> Path:
    """Resolve the trusted in-process bot file for a character.

    Always returns a path; characters without a hand-tuned bot get generic.py.
    """
    filename = _DEFAULT_BOT_FILES.get(character.upper(), _GENERIC_BOT_FILE)
    return _BOTS_DIR / filename
