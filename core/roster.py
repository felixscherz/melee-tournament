"""Shared, single source of truth for the selectable Melee roster.

Keys are `melee.Character` enum names (also what the API/lobby send over the
wire); values are human-friendly display names for the dropdown. Non-playable
entries (wireframes, Giga Bowser, Sandbag, Nana) are intentionally excluded.
"""
import melee

# enum name -> display name, in the usual CSS-ish ordering.
SELECTABLE_CHARACTERS: dict[str, str] = {
    "MARIO":        "Mario",
    "DOC":          "Dr. Mario",
    "LUIGI":        "Luigi",
    "BOWSER":       "Bowser",
    "PEACH":        "Peach",
    "YOSHI":        "Yoshi",
    "DK":           "Donkey Kong",
    "CPTFALCON":    "Captain Falcon",
    "GANONDORF":    "Ganondorf",
    "FALCO":        "Falco",
    "FOX":          "Fox",
    "NESS":         "Ness",
    "POPO":         "Ice Climbers",
    "KIRBY":        "Kirby",
    "SAMUS":        "Samus",
    "ZELDA":        "Zelda",
    "SHEIK":        "Sheik",
    "LINK":         "Link",
    "YLINK":        "Young Link",
    "PICHU":        "Pichu",
    "PIKACHU":      "Pikachu",
    "JIGGLYPUFF":   "Jigglypuff",
    "MEWTWO":       "Mewtwo",
    "GAMEANDWATCH": "Mr. Game & Watch",
    "MARTH":        "Marth",
    "ROY":          "Roy",
}

# enum name -> melee.Character, built from the roster above.
CHARACTER_MAP: dict[str, melee.Character] = {
    name: melee.Character[name] for name in SELECTABLE_CHARACTERS
}


def is_valid_character(name: str) -> bool:
    return name in SELECTABLE_CHARACTERS
