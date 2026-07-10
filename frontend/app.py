"""
FastAPI web server — dashboard, bot upload, and WebSocket game state feed.
"""

import asyncio
import json
import logging
from pathlib import Path

import aiofiles
import toml
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.toml"
UPLOAD_DIR = Path(__file__).parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

config = toml.load(CONFIG_PATH)
app = FastAPI(title="Smash Tournament")

def _webrtc_url() -> str:
    """Derive WebRTC signaling URL from config mode."""
    mode = config["streaming"].get("mode", "local")
    if mode == "production":
        stream_domain = config["domains"]["stream"]
        return f"wss://{stream_domain}/app/stream"
    return config["streaming"]["webrtc_signal"]

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# WebSocket clients watching the game state feed
_ws_clients: list[WebSocket] = []

# Shared reference to the orchestrator (injected at startup)
_orchestrator = None


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    webrtc_url = _webrtc_url()
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "webrtc_url": webrtc_url},
    )


@app.post("/api/bot/upload")
async def upload_bot(file: UploadFile = File(...)):
    if not file.filename.endswith(".py"):
        raise HTTPException(status_code=400, detail="Only .py files are accepted")
    dest = UPLOAD_DIR / file.filename
    async with aiofiles.open(dest, "wb") as f:
        content = await file.read()
        await f.write(content)
    if _orchestrator is not None:
        success = _orchestrator.bot_loader.load(dest)
        if not success:
            raise HTTPException(status_code=422, detail="Bot script failed validation — check logs")
    return {"status": "loaded", "filename": file.filename}


@app.post("/api/bot/deactivate")
async def deactivate_bot():
    if _orchestrator is not None:
        _orchestrator.bot_loader.deactivate()
    return {"status": "deactivated"}


@app.post("/api/prompt")
async def submit_prompt(body: dict):
    """Accept a text prompt; the LLM client will use it on next decision cycle."""
    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    # Store prompt in the LLM client for next frame — simple override
    if _orchestrator is not None:
        _orchestrator.llm._override_prompt = prompt
    return {"status": "queued"}


@app.websocket("/ws/gamestate")
async def gamestate_ws(websocket: WebSocket):
    """Push live game state snapshots to connected browsers."""
    await websocket.accept()
    _ws_clients.append(websocket)
    try:
        while True:
            await asyncio.sleep(0.1)  # 10Hz state push is plenty for the UI
            if _orchestrator and _orchestrator._latest_gamestate:
                gs = _orchestrator._latest_gamestate
                p1 = gs.players.get(1)
                p2 = gs.players.get(2)
                payload = {
                    "frame": gs.frame,
                    "p1": {
                        "x": round(p1.position.x, 1) if p1 else None,
                        "y": round(p1.position.y, 1) if p1 else None,
                        "stock": p1.stock if p1 else None,
                        "percent": round(p1.percent, 0) if p1 else None,
                        "action": p1.action.name if p1 else None,
                    },
                    "p2": {
                        "x": round(p2.position.x, 1) if p2 else None,
                        "y": round(p2.position.y, 1) if p2 else None,
                        "stock": p2.stock if p2 else None,
                        "percent": round(p2.percent, 0) if p2 else None,
                        "action": p2.action.name if p2 else None,
                    },
                }
                try:
                    await websocket.send_text(json.dumps(payload))
                except WebSocketDisconnect:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.remove(websocket)
