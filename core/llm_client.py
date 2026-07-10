"""
Ollama LLM interface for generating bot decisions from game state.
"""

import asyncio
import json
import logging
from typing import Optional

import aiohttp
import melee

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Super Smash Bros. Melee AI controller.
Given a game state summary, output a JSON action for player 1.
Output ONLY valid JSON in this exact format:
{
  "stick_x": <float 0.0-1.0, 0.5=neutral>,
  "stick_y": <float 0.0-1.0, 0.5=neutral>,
  "buttons": {
    "BUTTON_A": <bool>,
    "BUTTON_B": <bool>,
    "BUTTON_X": <bool>,
    "BUTTON_Y": <bool>,
    "BUTTON_L": <bool>,
    "BUTTON_R": <bool>,
    "BUTTON_Z": <bool>
  }
}"""


def _summarize(gamestate: melee.GameState) -> str:
    p1 = gamestate.players.get(1)
    p2 = gamestate.players.get(2)
    if p1 is None or p2 is None:
        return "Game in progress"
    return (
        f"P1: {p1.character.name} x={p1.position.x:.1f} y={p1.position.y:.1f} "
        f"stock={p1.stock} pct={p1.percent:.0f}% action={p1.action.name} | "
        f"P2: {p2.character.name} x={p2.position.x:.1f} y={p2.position.y:.1f} "
        f"stock={p2.stock} pct={p2.percent:.0f}% action={p2.action.name}"
    )


class LLMClient:
    def __init__(self, model: str, base_url: str):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def decide(self, gamestate: melee.GameState) -> Optional[dict]:
        summary = _summarize(gamestate)
        payload = {
            "model": self.model,
            "prompt": summary,
            "system": SYSTEM_PROMPT,
            "stream": False,
            "format": "json",
        }
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=0.5),
            ) as resp:
                data = await resp.json()
                return json.loads(data.get("response", "{}"))
        except Exception as exc:
            log.debug("LLM request failed: %s", exc)
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
