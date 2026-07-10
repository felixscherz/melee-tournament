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

from core.bot_validator import BotValidationError, validate_bot_code
from core.game_state import Phase, PlayerConfig, app_state
from core.roster import SELECTABLE_CHARACTERS, is_valid_character

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.toml"
BOTS_DIR    = Path(__file__).parent.parent / "core" / "bots"
UPLOAD_DIR  = Path(__file__).parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

config = toml.load(CONFIG_PATH)

app = FastAPI(title="Smash Tournament")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# Injected by main.py
_orchestrator = None

# Remembers the last submitted lobby form (names, characters, custom code) so
# the lobby can repopulate it after a match ends and nobody loses what they
# typed. Survives match resets; only overwritten by the next /api/start.
_last_form: list[dict] | None = None

SUPPORTED_CHARACTERS = SELECTABLE_CHARACTERS

# Hand-tuned bots for a few characters; every other fighter falls back to the
# character-agnostic generic bot below.
GENERIC_BOT = BOTS_DIR / "generic.py"
BOT_FILES = {
    "FOX":       BOTS_DIR / "fox.py",
    "MARTH":     BOTS_DIR / "marth.py",
    "CPTFALCON": BOTS_DIR / "falcon.py",
    "FALCO":     BOTS_DIR / "falco.py",
}


def _default_bot_path(character: str) -> Path:
    return BOT_FILES.get(character, GENERIC_BOT)

def _webrtc_url() -> str:
    mode = config["streaming"].get("mode", "local")
    if mode == "production":
        return f"wss://{config['domains']['stream']}/app/stream"
    return config["streaming"]["webrtc_signal"]


# ------------------------------------------------------------------ #
#  Pages                                                               #
# ------------------------------------------------------------------ #

@app.get("/", response_class=RedirectResponse)
async def root():
    return RedirectResponse("/lobby")


@app.get("/lobby", response_class=HTMLResponse)
async def lobby(request: Request):
    return templates.TemplateResponse(request, "lobby.html", {
        "characters": SUPPORTED_CHARACTERS,
        "phase": app_state.phase.value,
    })


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    return templates.TemplateResponse(request, "admin.html", {})


@app.get("/watch", response_class=HTMLResponse)
async def watch(request: Request):
    if app_state.phase == Phase.IDLE:
        return RedirectResponse("/lobby")
    return templates.TemplateResponse(request, "watch.html", {
        "webrtc_url": _webrtc_url(),
        "players": [
            {"port": p.port, "name": p.name, "character": SUPPORTED_CHARACTERS.get(p.character, p.character)}
            for p in app_state.players
        ],
    })


# ------------------------------------------------------------------ #
#  API                                                                 #
# ------------------------------------------------------------------ #

class StartRequest(BaseModel):
    players: list[dict]   # [{port, name, character, code?}]


@app.post("/api/validate")
async def validate_code(body: dict):
    """Static-check a bot code snippet without starting a match."""
    try:
        validate_bot_code(body.get("code", ""))
    except BotValidationError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def _resolve_bot_path(port: int, character: str, code: str | None) -> Path:
    """Return the bot file for a player, writing/validating custom code first.

    If `code` is provided it is validated and persisted to uploads/player{port}.py
    (hot-reloaded by BotLoader). Otherwise the character's default bot is used.
    Raises HTTPException(400) if custom code fails validation.
    """
    if not code or not code.strip():
        return _default_bot_path(character)
    try:
        validate_bot_code(code)
    except BotValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Player {port} code rejected: {exc}")
    dest = UPLOAD_DIR / f"player{port}.py"
    dest.write_text(code, encoding="utf-8")
    return dest


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
        configs.append(PlayerConfig(
            port=port,
            name=p.get("name", f"Player {port}"),
            character=char,
            bot_path=bot_path,
        ))

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
    nobody loses the names/characters/bot code they entered for a prior match."""
    return {"players": _last_form or []}


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
