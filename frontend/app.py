"""FastAPI server — lobby, watch, bot upload, and WebSocket game state."""

import asyncio
import json
import logging
from pathlib import Path

import toml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from core.bot_generator import GenerateError, generate_bot, get_generated_path
from core.bot_validator import BotValidationError, validate_bot_code
from core.game_state import Phase, PlayerConfig, app_state
from core.roster import SELECTABLE_CHARACTERS, is_valid_character

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.toml"
BOTS_DIR = Path(__file__).parent.parent / "core" / "bots"
UPLOAD_DIR = Path(__file__).parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

config = toml.load(CONFIG_PATH)

app = FastAPI(title="Smash Tournament")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

# Injected by main.py
_orchestrator = None

# Per-port locks so only one generation runs at a time per port.
_gen_locks: dict[int, asyncio.Lock] = {}

# Remembers the last submitted lobby form (names, characters, custom code,
# prompt) so the lobby can repopulate it after a match ends and nobody loses
# what they typed. Survives match resets; only overwritten by the next
# /api/start.
_last_form: list[dict] | None = None

SUPPORTED_CHARACTERS = SELECTABLE_CHARACTERS

# Hand-tuned bots for a few characters; every other fighter falls back to the
# character-agnostic generic bot below.
GENERIC_BOT = BOTS_DIR / "generic.py"
BOT_FILES = {
    "FOX": BOTS_DIR / "fox.py",
    "MARTH": BOTS_DIR / "marth.py",
    "CPTFALCON": BOTS_DIR / "falcon.py",
    "FALCO": BOTS_DIR / "falco.py",
}


def _default_bot_path(character: str) -> Path:
    return BOT_FILES.get(character, GENERIC_BOT)


def _twitch_context() -> dict:
    """Twitch embed params shared by every page with a live player.

    OBS streams directly to Twitch (no WebRTC/OME relay). Twitch's iframe
    requires the exact host(s) serving the page in `parent`.
    """
    return {
        "twitch_channel": config["streaming"].get("twitch_channel", "").strip(),
        "twitch_parents": [config["domains"]["frontend"], "localhost", "127.0.0.1"],
    }


# ------------------------------------------------------------------ #
#  Pages                                                               #
# ------------------------------------------------------------------ #


@app.get("/", response_class=RedirectResponse)
async def root():
    return RedirectResponse("/lobby")


@app.get("/lobby", response_class=HTMLResponse)
async def lobby(request: Request):
    return templates.TemplateResponse(
        request,
        "lobby.html",
        {
            "characters": SUPPORTED_CHARACTERS,
            "phase": app_state.phase.value,
            **_twitch_context(),
        },
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    return templates.TemplateResponse(request, "admin.html", {})


@app.get("/watch", response_class=HTMLResponse)
async def watch(request: Request):
    if app_state.phase == Phase.IDLE:
        return RedirectResponse("/lobby")
    return templates.TemplateResponse(
        request,
        "watch.html",
        {
            **_twitch_context(),
            "players": [
                {
                    "port": p.port,
                    "name": p.name,
                    "character": SUPPORTED_CHARACTERS.get(p.character, p.character),
                }
                for p in app_state.players
            ],
        },
    )


# ------------------------------------------------------------------ #
#  API                                                                 #
# ------------------------------------------------------------------ #


class StartRequest(BaseModel):
    players: list[dict]  # [{port, name, character, code?}]


@app.post("/api/validate")
async def validate_code(body: dict):
    """Static-check a bot code snippet without starting a match."""
    try:
        validate_bot_code(body.get("code", ""))
    except BotValidationError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


@app.post("/api/generate")
async def generate_bot_endpoint(body: dict):
    """Generate a bot from a natural-language prompt via the opencode agent.

    Request: {"port": 1, "character": "FOX", "prompt": "aggressive fox..."}
    Returns: {"ok": true, "path": "...", "version_id": "..."} on success.
    """
    port = int(body.get("port", 0))
    if port not in (1, 2, 3, 4):
        raise HTTPException(status_code=400, detail="port must be 1-4")

    character = body.get("character", "").upper()
    if not is_valid_character(character):
        raise HTTPException(status_code=400, detail=f"Unknown character: {character}")

    prompt = (body.get("prompt", "") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    # Per-port lock: only one generation at a time per port
    lock = _gen_locks.setdefault(port, asyncio.Lock())
    if lock.locked():
        raise HTTPException(
            status_code=409, detail="Generation already running for this port"
        )

    async with lock:
        try:
            result = await generate_bot(port, character, prompt)
            return result
        except GenerateError as exc:
            return {"ok": False, "error": str(exc)}


def _resolve_bot_path(port: int, character: str, code: str | None) -> Path:
    """Return the bot file for a player.

    Priority:
      1. Pasted code (validated, written to uploads/player{port}.py)
      2. Generated bot from generated/latest.json (if one exists for this port)
      3. Character's default bot from core/bots/

    Raises HTTPException(400) if custom code fails validation.
    """
    # 1. Explicit pasted code wins
    if code and code.strip():
        try:
            validate_bot_code(code)
        except BotValidationError as exc:
            raise HTTPException(
                status_code=400, detail=f"Player {port} code rejected: {exc}"
            )
        dest = UPLOAD_DIR / f"player{port}.py"
        dest.write_text(code, encoding="utf-8")
        return dest

    # 2. Generated bot (from prompt via opencode agent)
    generated = get_generated_path(port)
    if generated is not None:
        return generated

    # 3. Default bot
    return _default_bot_path(character)


@app.post("/api/start")
async def start_game(body: StartRequest):
    if app_state.phase not in (Phase.IDLE, Phase.POSTGAME):
        raise HTTPException(status_code=409, detail="A game is already running")
    if len(body.players) != 4:
        raise HTTPException(status_code=400, detail="Exactly 4 players required")

    configs = []
    for p in body.players:
        char = p.get("character", "").upper()
        if not is_valid_character(char):
            raise HTTPException(status_code=400, detail=f"Unknown character: {char}")
        port = int(p["port"])
        bot_path = _resolve_bot_path(port, char, p.get("code"))
        configs.append(
            PlayerConfig(
                port=port,
                name=p.get("name", f"Player {port}"),
                character=char,
                bot_path=bot_path,
            )
        )

    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not ready")

    # Remember exactly what was submitted so the lobby can restore it later.
    global _last_form
    _last_form = [
        {
            "port": int(p["port"]),
            "name": p.get("name", ""),
            "character": p.get("character", "").upper(),
            "code": p.get("code", "") or "",
            "prompt": p.get("prompt", "") or "",
        }
        for p in body.players
    ]

    _orchestrator.queue_match(configs)
    return {"status": "starting"}


@app.post("/api/stop")
async def stop_game():
    # Actually abort the running match (soft-reset Dolphin to the menus) and
    # return to IDLE. abort_match() also resets app_state; fall back to a bare
    # state reset if the orchestrator isn't wired up (e.g. tests).
    if _orchestrator is not None:
        _orchestrator.abort_match()
    else:
        app_state.reset()
    return {"status": "stopped"}


@app.get("/api/state")
async def get_state():
    return app_state.to_dict()


@app.get("/api/last-form")
async def get_last_form():
    """Return the last submitted lobby form so the UI can repopulate it and
    nobody loses the names/characters/bot code/prompt they entered for a prior
    match. Also includes generated bot info so the UI can restore the
    "Generated bot ready" status."""
    from core.bot_generator import read_latest

    latest = read_latest()
    players = []
    for p in _last_form or []:
        entry = {
            "port": p["port"],
            "name": p.get("name", ""),
            "character": p.get("character", ""),
            "code": p.get("code", ""),
            "prompt": p.get("prompt", ""),
        }
        gen = latest.get(str(p["port"]))
        if gen:
            entry["generated_version"] = gen.get("version_id", "")
        players.append(entry)
    return {"players": players}


# ------------------------------------------------------------------ #
#  WebSocket — live game state push                                    #
# ------------------------------------------------------------------ #


@app.websocket("/ws/gamestate")
async def gamestate_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await asyncio.sleep(0.1)
            try:
                await websocket.send_text(json.dumps(app_state.to_dict()))
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        pass
