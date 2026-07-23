"""In-memory team state for the team-based lobby.

Four teams (one per Dolphin port). Each team has:
  - a captain (claimed via a client-side localStorage nonce; instant takeover
    with a confirm prompt on the UI side)
  - a character selection (captain-only)
  - a stack of prompt contributions from any teammate
  - an optional code override (captain-only; wins over the generated bot at
    match-start time)
  - a generated bot path (from the team's assembled prompt -> bot-writer)
  - a ready toggle (captain-only)

State is persisted to generated/teams.json so a page refresh doesn't wipe
contributions mid-session. A manual reset (POST /api/teams/reset) clears
everything for a fresh round.
"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import WebSocket

from core.roster import SELECTABLE_CHARACTERS

REPO_ROOT = Path(__file__).parent.parent
GENERATED_DIR = REPO_ROOT / "generated"
TEAMS_JSON = GENERATED_DIR / "teams.json"

TEAM_IDS = (1, 2, 3, 4)
TEAM_COLORS = {1: "p1", 2: "p2", 3: "p3", 4: "p4"}
DEFAULT_TEAM_NAMES = {1: "TEAM 1", 2: "TEAM 2", 3: "TEAM 3", 4: "TEAM 4"}
DEFAULT_CHARACTERS = {1: "FOX", 2: "MARTH", 3: "CPTFALCON", 4: "FALCO"}

# A team's identity IS its Dolphin port. All 4 slots always exist; each is
# either active (in play) or inactive. The active set can be any subset of
# {1,2,3,4} of size MIN_ACTIVE..4 (Melee supports non-contiguous plugged
# controllers, so keeping teams 1 and 4 while dropping 2 and 3 is fine). Fresh
# installs start with teams 1 and 2 active.
MIN_ACTIVE = 2
DEFAULT_ACTIVE = {1, 2}

MAX_CONTRIBUTION_CHARS = 1000
MAX_NICKNAME_CHARS = 24
MAX_TEAM_NAME_CHARS = 24


@dataclass
class Contribution:
    id: str
    author_nonce: str
    nickname: str
    text: str
    ts: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TeamState:
    team_id: int
    name: str
    color: str
    active: bool = True
    captain_nonce: Optional[str] = None
    captain_name: Optional[str] = None
    character: str = ""
    contributions: list[Contribution] = field(default_factory=list)
    code_override: str = ""
    generated_version: Optional[str] = None
    ready: bool = False
    # Transient: True while a bot generation is running for this team. Not
    # persisted, so a crash mid-generation can't leave a stale flag.
    generating: bool = False

    def to_dict(self, my_nonce: str = "") -> dict:
        return {
            "team_id": self.team_id,
            "name": self.name,
            "color": self.color,
            "active": self.active,
            "captain_name": self.captain_name,
            "has_captain": self.captain_nonce is not None,
            "you_are_captain": (
                self.captain_nonce is not None
                and my_nonce != ""
                and self.captain_nonce == my_nonce
            ),
            "character": self.character,
            "character_label": SELECTABLE_CHARACTERS.get(
                self.character, self.character
            ),
            "contributions": [c.to_dict() for c in self.contributions],
            "code_override": self.code_override,
            "has_code_override": bool(self.code_override.strip()),
            "generated_version": self.generated_version,
            "generating": self.generating,
            "ready": self.ready,
            "contrib_count": len(self.contributions),
        }

    def to_summary(self) -> dict:
        """Compact view for the landing page — no contribution bodies."""
        return {
            "team_id": self.team_id,
            "name": self.name,
            "color": self.color,
            "active": self.active,
            "captain_name": self.captain_name,
            "has_captain": self.captain_nonce is not None,
            "character": self.character,
            "character_label": SELECTABLE_CHARACTERS.get(
                self.character, self.character
            ),
            "contrib_count": len(self.contributions),
            "has_code_override": bool(self.code_override.strip()),
            "generated_version": self.generated_version,
            "generating": self.generating,
            "ready": self.ready,
        }


def _new_team(team_id: int, active: Optional[bool] = None) -> TeamState:
    return TeamState(
        team_id=team_id,
        name=DEFAULT_TEAM_NAMES[team_id],
        color=TEAM_COLORS[team_id],
        active=(team_id in DEFAULT_ACTIVE) if active is None else active,
        character=DEFAULT_CHARACTERS[team_id],
    )


def _gen_id() -> str:
    return f"c{int(time.time() * 1000)}{_short_nonce()}"


def _short_nonce() -> str:
    import secrets

    return secrets.token_hex(4)


class TeamError(Exception):
    """Raised on invalid team operations (caller maps to HTTP status)."""


class TeamRegistry:
    def __init__(self):
        self._teams: dict[int, TeamState] = {i: _new_team(i) for i in TEAM_IDS}
        # WebSocket clients for push-on-change updates.
        self._summary_clients: set[WebSocket] = set()
        self._team_clients: dict[int, dict[WebSocket, str]] = {i: {} for i in TEAM_IDS}
        self._load()

    # ---- read ----

    def active_ids(self) -> list[int]:
        return [i for i in TEAM_IDS if self._teams[i].active]

    def all_teams(self) -> list[TeamState]:
        """Active teams only (used to build a match)."""
        return [self._teams[i] for i in self.active_ids()]

    def summary(self) -> list[dict]:
        """All 4 slots (each carrying `active`) so the lobby can render active
        cards alongside inactive 'add team' placeholders."""
        return [self._teams[i].to_summary() for i in TEAM_IDS]

    def team(self, n: int) -> TeamState:
        self._require_valid(n)
        return self._teams[n]

    def all_ready(self) -> bool:
        active = self.active_ids()
        return len(active) >= MIN_ACTIVE and all(
            self._teams[i].ready for i in active
        )

    # ---- activation ----

    def activate(self, n: int) -> TeamState:
        self._require_valid(n)
        if not self._teams[n].active and len(self.active_ids()) >= len(TEAM_IDS):
            raise TeamError("max_teams")
        self._teams[n].active = True
        self._save()
        return self._teams[n]

    def deactivate(self, n: int) -> TeamState:
        self._require_valid(n)
        if self._teams[n].active and len(self.active_ids()) <= MIN_ACTIVE:
            raise TeamError("min_teams")
        self._teams[n].active = False
        self._save()
        return self._teams[n]

    # ---- captain ----

    def claim_captain(
        self, n: int, nonce: str, nickname: str, force: bool = False
    ) -> TeamState:
        self._require_valid(n)
        t = self._teams[n]
        nick = nickname.strip()[:MAX_NICKNAME_CHARS] or f"Captain {n}"
        if t.captain_nonce is None or t.captain_nonce == nonce:
            t.captain_nonce = nonce
            t.captain_name = nick
            self._save()
            return t
        # someone else is captain
        if force:
            t.captain_nonce = nonce
            t.captain_name = nick
            self._save()
            return t
        raise TeamError("captain_exists")

    # ---- character ----

    def set_character(self, n: int, character: str) -> TeamState:
        self._require_valid(n)
        char = character.strip().upper()
        if char not in SELECTABLE_CHARACTERS:
            raise TeamError("invalid_character")
        self._teams[n].character = char
        self._save()
        return self._teams[n]

    def set_name(self, n: int, name: str) -> TeamState:
        self._require_valid(n)
        nm = name.strip()[:MAX_TEAM_NAME_CHARS]
        if not nm:
            raise TeamError("invalid_name")
        self._teams[n].name = nm
        self._save()
        return self._teams[n]

    # ---- contributions ----

    def add_contribution(
        self, n: int, author_nonce: str, nickname: str, text: str
    ) -> TeamState:
        self._require_valid(n)
        nick = nickname.strip()[:MAX_NICKNAME_CHARS] or "Anon"
        body = text.strip()
        if not body:
            raise TeamError("empty_contribution")
        if len(body) > MAX_CONTRIBUTION_CHARS:
            raise TeamError("contribution_too_long")
        if not author_nonce:
            author_nonce = _short_nonce()
        self._teams[n].contributions.append(
            Contribution(
                id=_gen_id(),
                author_nonce=author_nonce,
                nickname=nick,
                text=body,
                ts=time.time(),
            )
        )
        self._save()
        return self._teams[n]

    def remove_contribution(
        self, n: int, contrib_id: str, author_nonce: str
    ) -> TeamState:
        self._require_valid(n)
        t = self._teams[n]
        is_captain = t.captain_nonce is not None and t.captain_nonce == author_nonce
        before = len(t.contributions)
        t.contributions = [
            c
            for c in t.contributions
            if not (
                c.id == contrib_id and (c.author_nonce == author_nonce or is_captain)
            )
        ]
        if len(t.contributions) == before:
            raise TeamError("contribution_not_found")
        self._save()
        return t

    # ---- code override ----

    def set_code_override(self, n: int, code: str) -> TeamState:
        self._require_valid(n)
        self._teams[n].code_override = code or ""
        self._save()
        return self._teams[n]

    # ---- generation result ----

    def set_generating(self, n: int, generating: bool) -> TeamState:
        """Transient in-progress flag (not persisted); caller broadcasts."""
        self._require_valid(n)
        self._teams[n].generating = bool(generating)
        return self._teams[n]

    def set_generated(self, n: int, version_id: str) -> TeamState:
        self._require_valid(n)
        t = self._teams[n]
        t.generated_version = version_id
        self._save()
        return t

    def clear_generated(self, n: int) -> TeamState:
        self._require_valid(n)
        self._teams[n].generated_version = None
        self._save()
        return self._teams[n]

    # ---- ready ----

    def set_ready(self, n: int, ready: bool) -> TeamState:
        self._require_valid(n)
        self._teams[n].ready = bool(ready)
        self._save()
        return self._teams[n]

    # ---- reset ----

    def wipe_all(self) -> None:
        """Factory reset: every slot back to defaults — names, characters,
        captains, contributions, code, and the active set. Unlike
        `reset_all`, custom team names and the active roster are NOT kept."""
        self._teams = {i: _new_team(i) for i in TEAM_IDS}
        self._save()

    def new_round(self) -> None:
        """Soft reset between matches: clear ready flags only, keeping
        captains, contributions, code overrides, and generated bots so teams
        can iterate round over round."""
        for i in TEAM_IDS:
            self._teams[i].ready = False
        self._save()

    def reset_all(self) -> None:
        for i in TEAM_IDS:
            old_name = self._teams[i].name
            old_active = self._teams[i].active
            # Preserve the active set (operator's chosen bracket size) across
            # resets — only captains/contributions/ready are cleared.
            self._teams[i] = _new_team(i, active=old_active)
            # Keep a custom team name across resets (cosmetic).
            if old_name and old_name != DEFAULT_TEAM_NAMES[i]:
                self._teams[i].name = old_name
        self._save()
        # Also clear the generated-bot index so teams don't pick up stale
        # bots from a previous round after resetting.
        from core.bot_generator import LATEST_JSON

        if LATEST_JSON.exists():
            try:
                LATEST_JSON.write_text("{}", encoding="utf-8")
            except OSError:
                pass

    # ---- WebSocket plumbing ----

    def register_summary(self, ws: WebSocket) -> None:
        self._summary_clients.add(ws)

    def unregister_summary(self, ws: WebSocket) -> None:
        self._summary_clients.discard(ws)

    def register_team(self, n: int, ws: WebSocket, nonce: str = "") -> None:
        self._require_valid(n)
        self._team_clients[n][ws] = nonce

    def unregister_team(self, n: int, ws: WebSocket) -> None:
        self._team_clients[n].pop(ws, None)

    async def broadcast_summary(self) -> None:
        await _safe_broadcast(self._summary_clients, {"teams": self.summary()})

    async def broadcast_team(self, n: int) -> None:
        self._require_valid(n)
        t = self._teams[n]
        clients = self._team_clients[n]
        dead = []
        for ws, nonce in list(clients.items()):
            try:
                await ws.send_text(json.dumps(t.to_dict(my_nonce=nonce)))
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.pop(ws, None)

    # ---- persistence ----

    def _save(self) -> None:
        GENERATED_DIR.mkdir(exist_ok=True)
        data = {}
        for i in TEAM_IDS:
            t = self._teams[i]
            data[str(i)] = {
                "name": t.name,
                "active": t.active,
                "captain_nonce": t.captain_nonce,
                "captain_name": t.captain_name,
                "character": t.character,
                "contributions": [asdict(c) for c in t.contributions],
                "code_override": t.code_override,
                "generated_version": t.generated_version,
                "ready": t.ready,
            }
        TEAMS_JSON.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load(self) -> None:
        if not TEAMS_JSON.exists():
            return
        try:
            data = json.loads(TEAMS_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for i in TEAM_IDS:
            entry = data.get(str(i))
            if not entry:
                continue
            t = self._teams[i]
            t.name = entry.get("name") or DEFAULT_TEAM_NAMES[i]
            t.active = entry.get("active", i in DEFAULT_ACTIVE)
            t.captain_nonce = entry.get("captain_nonce")
            t.captain_name = entry.get("captain_name")
            t.character = entry.get("character") or DEFAULT_CHARACTERS[i]
            t.contributions = [
                Contribution(**c) for c in entry.get("contributions", [])
            ]
            t.code_override = entry.get("code_override", "")
            t.generated_version = entry.get("generated_version")
            t.ready = entry.get("ready", False)

    def _require_valid(self, n: int) -> None:
        if n not in self._teams:
            raise TeamError("invalid_team")


async def _safe_broadcast(clients: set[WebSocket], payload) -> None:
    import asyncio

    dead = []
    msg = json.dumps(payload)
    for ws in clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


# Singleton — shared by FastAPI routes and WS handlers.
teams = TeamRegistry()
