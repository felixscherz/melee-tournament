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

from core.game_state import Phase, PlayerConfig, app_state

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

SUPPORTED_CHARACTERS = {
    "FOX":    "Fox",
    "MARTH":  "Marth",
    "FALCON": "Captain Falcon",
    "FALCO":  "Falco",
}

BOT_FILES = {
    "FOX":    BOTS_DIR / "fox.py",
    "MARTH":  BOTS_DIR / "marth.py",
    "FALCON": BOTS_DIR / "falcon.py",
    "FALCO":  BOTS_DIR / "falco.py",
}

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
    players: list[dict]   # [{port, name, character}]


@app.post("/api/start")
async def start_game(body: StartRequest):
    if app_state.phase not in (Phase.IDLE, Phase.POSTGAME):
        raise HTTPException(status_code=409, detail="A game is already running")
    if len(body.players) != 4:
        raise HTTPException(status_code=400, detail="Exactly 4 players required")

    configs = []
    for p in body.players:
        char = p.get("character", "").upper()
        if char not in BOT_FILES:
            raise HTTPException(status_code=400, detail=f"Unknown character: {char}")
        configs.append(PlayerConfig(
            port=int(p["port"]),
            name=p.get("name", f"Player {p['port']}"),
            character=char,
            bot_path=BOT_FILES[char],
        ))

    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not ready")

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


@app.post("/api/restart")
async def restart_game():
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not ready")
    if not _orchestrator.restart_match():
        raise HTTPException(status_code=409, detail="No match to restart yet")
    return {"status": "restarting"}


@app.get("/api/state")
async def get_state():
    return app_state.to_dict()


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
