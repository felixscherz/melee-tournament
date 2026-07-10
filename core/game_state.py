"""Shared application state — written by the orchestrator, read by FastAPI."""
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class Phase(str, Enum):
    IDLE      = "idle"
    STARTING  = "starting"
    IN_GAME   = "in_game"
    POSTGAME  = "postgame"


@dataclass
class PlayerConfig:
    port: int
    name: str
    character: str   # melee.Character name, e.g. "FOX"
    bot_path: Path


@dataclass
class PlayerScore:
    port: int
    name: str
    character: str
    stock: int = 4
    percent: float = 0.0
    action: str = ""


@dataclass
class AppState:
    phase: Phase = Phase.IDLE
    players: list[PlayerConfig] = field(default_factory=list)
    scores: dict[int, PlayerScore] = field(default_factory=dict)  # keyed by port
    winner: Optional[str] = None

    def reset(self):
        self.phase = Phase.IDLE
        self.players = []
        self.scores = {}
        self.winner = None

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "scores": {
                p: {
                    "port": s.port,
                    "name": s.name,
                    "character": s.character,
                    "stock": s.stock,
                    "percent": s.percent,
                    "action": s.action,
                }
                for p, s in self.scores.items()
            },
            "winner": self.winner,
        }


# Singleton shared between orchestrator and FastAPI
app_state = AppState()
