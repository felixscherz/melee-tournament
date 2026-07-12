"""FastAPI server — team lobby, watch, bot generation, and WebSocket feeds."""

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from core.bot_generator import (
    GenerateError,
    assemble_prompt,
    generate_bot,
    get_generated_path,
)
from core.bot_validator import BotValidationError, validate_bot_code
from core.config import load_settings
from core.game_state import Phase, PlayerConfig, app_state
from core.roster import SELECTABLE_CHARACTERS, is_valid_character
from core.teams import TeamError, teams

log = logging.getLogger(__name__)

BOTS_DIR = Path(__file__).parent.parent / "core" / "bots"
UPLOAD_DIR = Path(__file__).parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

config = load_settings()

app = FastAPI(title="Smash Tournament")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

# Injected by main.py
_orchestrator = None

# Per-team locks so only one generation runs at a time per team.
_gen_locks: dict[int, asyncio.Lock] = {}

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
    """Twitch embed params shared by every page with a live player."""
    parents = ["localhost", "127.0.0.1"]
    frontend = (config.get("domains") or {}).get("frontend", "").strip()
    if frontend:
        parents.insert(0, frontend)
    return {
        "twitch_channel": config.get("streaming", {}).get("twitch_channel", "").strip(),
        "twitch_parents": parents,
    }


def _team_error_to_http(exc: TeamError) -> HTTPException:
    msg = str(exc)
    if msg == "invalid_team":
        return HTTPException(status_code=404, detail="Team not found")
    if msg == "captain_exists":
        return HTTPException(status_code=409, detail="captain_exists")
    if msg == "invalid_character":
        return HTTPException(status_code=400, detail="Unknown character")
    if msg == "invalid_name":
        return HTTPException(status_code=400, detail="Invalid team name")
    if msg == "empty_contribution":
        return HTTPException(status_code=400, detail="Contribution is empty")
    if msg == "contribution_too_long":
        return HTTPException(status_code=400, detail="Contribution too long")
    if msg == "contribution_not_found":
        return HTTPException(status_code=404, detail="Contribution not found")
    return HTTPException(status_code=400, detail=msg)


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


@app.get("/team/{team_id}", response_class=HTMLResponse)
async def team_page(request: Request, team_id: int):
    if team_id not in (1, 2, 3, 4):
        raise HTTPException(status_code=404, detail="Team not found")
    return templates.TemplateResponse(
        request,
        "team.html",
        {
            "team_id": team_id,
            "characters": SUPPORTED_CHARACTERS,
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
                    "team_name": p.team_name,
                    "character": SUPPORTED_CHARACTERS.get(p.character, p.character),
                }
                for p in app_state.players
            ],
        },
    )


# ------------------------------------------------------------------ #
#  Team API                                                            #
# ------------------------------------------------------------------ #


@app.get("/api/teams")
async def get_teams():
    return {"teams": teams.summary()}


@app.get("/api/team/{team_id}")
async def get_team(team_id: int, nonce: str = ""):
    try:
        return teams.team(team_id).to_dict(my_nonce=nonce)
    except TeamError as exc:
        raise _team_error_to_http(exc)


@app.post("/api/team/{team_id}/captain")
async def claim_captain(team_id: int, body: dict):
    try:
        teams.team(team_id)  # validate
        t = teams.claim_captain(
            team_id,
            nonce=body.get("nonce", ""),
            nickname=body.get("nickname", ""),
            force=bool(body.get("force", False)),
        )
        await teams.broadcast_summary()
        await teams.broadcast_team(team_id)
        return t.to_dict(my_nonce=body.get("nonce", ""))
    except TeamError as exc:
        raise _team_error_to_http(exc)


@app.post("/api/team/{team_id}/name")
async def set_team_name(team_id: int, body: dict):
    """Captain-only: rename the team."""
    try:
        t = teams.team(team_id)
        _require_captain(t, body.get("nonce", ""))
        teams.set_name(team_id, body.get("name", ""))
        await teams.broadcast_summary()
        await teams.broadcast_team(team_id)
        return teams.team(team_id).to_dict(my_nonce=body.get("nonce", ""))
    except TeamError as exc:
        raise _team_error_to_http(exc)


@app.post("/api/team/{team_id}/character")
async def set_character(team_id: int, body: dict):
    """Captain-only: pick the team's character."""
    try:
        t = teams.team(team_id)
        _require_captain(t, body.get("nonce", ""))
        teams.set_character(team_id, body.get("character", ""))
        await teams.broadcast_summary()
        await teams.broadcast_team(team_id)
        return teams.team(team_id).to_dict(my_nonce=body.get("nonce", ""))
    except TeamError as exc:
        raise _team_error_to_http(exc)


@app.post("/api/team/{team_id}/contribute")
async def add_contribution(team_id: int, body: dict):
    try:
        t = teams.add_contribution(
            team_id,
            author_nonce=body.get("nonce", ""),
            nickname=body.get("nickname", ""),
            text=body.get("text", ""),
        )
        await teams.broadcast_summary()
        await teams.broadcast_team(team_id)
        return t.to_dict(my_nonce=body.get("nonce", ""))
    except TeamError as exc:
        raise _team_error_to_http(exc)


@app.delete("/api/team/{team_id}/contribution/{contrib_id}")
async def remove_contribution(team_id: int, contrib_id: str, nonce: str = ""):
    try:
        t = teams.remove_contribution(team_id, contrib_id, nonce)
        await teams.broadcast_summary()
        await teams.broadcast_team(team_id)
        return t.to_dict(my_nonce=nonce)
    except TeamError as exc:
        raise _team_error_to_http(exc)


@app.post("/api/team/{team_id}/code")
async def set_code_override(team_id: int, body: dict):
    """Captain-only: set raw bot code that overrides the generated bot."""
    try:
        t = teams.team(team_id)
        _require_captain(t, body.get("nonce", ""))
        code = body.get("code", "") or ""
        # Validate immediately so the captain gets instant feedback.
        if code.strip():
            try:
                validate_bot_code(code)
            except BotValidationError as exc:
                return {"ok": False, "error": str(exc)}
        teams.set_code_override(team_id, code)
        await teams.broadcast_summary()
        await teams.broadcast_team(team_id)
        return {
            "ok": True,
            **teams.team(team_id).to_dict(my_nonce=body.get("nonce", "")),
        }
    except TeamError as exc:
        raise _team_error_to_http(exc)


@app.post("/api/team/{team_id}/prompt-preview")
async def preview_prompt(team_id: int, body: dict):
    """Assemble the team's contributions into a prompt and return it without
    generating. Lets the captain preview the merged prompt before committing."""
    try:
        t = teams.team(team_id)
        _require_captain(t, body.get("nonce", ""))
    except TeamError as exc:
        raise _team_error_to_http(exc)
    prompt = assemble_prompt(t.contributions)
    return {"prompt": prompt}


@app.post("/api/team/{team_id}/generate")
async def generate_team_bot(team_id: int, body: dict):
    """Assemble the team's contributions into one prompt and generate a bot.

    Captain-only. The assembled prompt (contributions + merge preamble) is
    sent to the bot-writer agent. The resulting bot file is recorded in
    generated/latest.json keyed by port (= team_id).
    """
    try:
        t = teams.team(team_id)
        _require_captain(t, body.get("nonce", ""))
    except TeamError as exc:
        raise _team_error_to_http(exc)

    contributions = t.contributions
    if not contributions:
        raise HTTPException(
            status_code=400,
            detail="No contributions yet — add some strategy ideas first.",
        )

    prompt = assemble_prompt(contributions)
    if not prompt:
        raise HTTPException(status_code=400, detail="Assembled prompt is empty")

    # Per-team lock: only one generation at a time.
    lock = _gen_locks.setdefault(team_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(
            status_code=409, detail="Generation already running for this team"
        )

    async with lock:
        try:
            result = await generate_bot(team_id, t.character, prompt)
            if result.get("ok"):
                teams.set_generated(team_id, result["version_id"])
                await teams.broadcast_summary()
                await teams.broadcast_team(team_id)
            return result
        except GenerateError as exc:
            return {"ok": False, "error": str(exc)}


@app.post("/api/team/{team_id}/ready")
async def set_ready(team_id: int, body: dict):
    """Captain-only: toggle the team's ready state."""
    try:
        t = teams.team(team_id)
        _require_captain(t, body.get("nonce", ""))
        teams.set_ready(team_id, bool(body.get("ready", False)))
        await teams.broadcast_summary()
        await teams.broadcast_team(team_id)
        return teams.team(team_id).to_dict(my_nonce=body.get("nonce", ""))
    except TeamError as exc:
        raise _team_error_to_http(exc)


@app.post("/api/teams/reset")
async def reset_teams():
    """Clear all team state for a fresh round (operator). Does not abort a
    running match — use /api/stop first."""
    if app_state.phase not in (Phase.IDLE, Phase.POSTGAME):
        raise HTTPException(
            status_code=409,
            detail="Stop the running match before resetting teams",
        )
    teams.reset_all()
    await teams.broadcast_summary()
    for n in (1, 2, 3, 4):
        await teams.broadcast_team(n)
    return {"ok": True}


# ------------------------------------------------------------------ #
#  Match API                                                           #
# ------------------------------------------------------------------ #


def _require_captain(team, nonce: str) -> None:
    if team.captain_nonce is None:
        raise HTTPException(status_code=409, detail="Team has no captain")
    if team.captain_nonce != nonce:
        raise HTTPException(status_code=403, detail="Not the captain")


@app.post("/api/validate")
async def validate_code(body: dict):
    """Static-check a bot code snippet without starting a match."""
    try:
        validate_bot_code(body.get("code", ""))
    except BotValidationError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def _resolve_bot_path(port: int, character: str, code: str | None) -> Path:
    """Return the bot file for a player (port = team_id).

    Priority:
      1. Captain's code override (validated, written to uploads/player{port}.py)
      2. Generated bot from generated/latest.json
      3. Character's default bot from core/bots/

    Raises HTTPException(400) if custom code fails validation.
    """
    if code and code.strip():
        try:
            validate_bot_code(code)
        except BotValidationError as exc:
            raise HTTPException(
                status_code=400, detail=f"Team {port} code rejected: {exc}"
            )
        dest = UPLOAD_DIR / f"player{port}.py"
        dest.write_text(code, encoding="utf-8")
        return dest

    generated = get_generated_path(port)
    if generated is not None:
        return generated

    return _default_bot_path(character)


@app.post("/api/start")
async def start_game():
    """Start a match from the current team state.

    All 4 teams must be READY and the game must be in IDLE or POSTGAME phase.
    Each team's character, code override, and generated bot are pulled from
    the TeamRegistry (not from the request body).
    """
    if app_state.phase not in (Phase.IDLE, Phase.POSTGAME):
        raise HTTPException(status_code=409, detail="A game is already running")
    if not teams.all_ready():
        raise HTTPException(
            status_code=409, detail="All 4 teams must be ready to start"
        )
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not ready")

    configs = []
    for team_id in (1, 2, 3, 4):
        t = teams.team(team_id)
        char = t.character.upper()
        if not is_valid_character(char):
            raise HTTPException(
                status_code=400, detail=f"Team {team_id} has no valid character"
            )
        bot_path = _resolve_bot_path(team_id, char, t.code_override)
        configs.append(
            PlayerConfig(
                port=team_id,
                name=t.captain_name or t.name,
                character=char,
                bot_path=bot_path,
                team_name=t.name,
            )
        )

    _orchestrator.queue_match(configs)
    return {"status": "starting"}


@app.post("/api/stop")
async def stop_game():
    if _orchestrator is not None:
        _orchestrator.abort_match()
    else:
        app_state.reset()
    return {"status": "stopped"}


@app.get("/api/state")
async def get_state():
    return app_state.to_dict()


# ------------------------------------------------------------------ #
#  WebSockets                                                          #
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


@app.websocket("/ws/teams")
async def teams_ws(websocket: WebSocket):
    """Landing page feed — pushes a 4-team summary on any team change."""
    await websocket.accept()
    teams.register_summary(websocket)
    # Send an initial snapshot immediately.
    try:
        await websocket.send_text(json.dumps({"teams": teams.summary()}))
    except WebSocketDisconnect:
        teams.unregister_summary(websocket)
        return
    try:
        while True:
            # Block until the client sends a message or disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        teams.unregister_summary(websocket)


@app.websocket("/ws/team/{team_id}")
async def team_ws(websocket: WebSocket, team_id: int):
    """Team page feed — pushes full team state on any change to that team.

    The client's nonce is passed as a ?nonce= query param so the server can
    include `you_are_captain` in each personalized message without leaking
    the captain's nonce to other clients.
    """
    if team_id not in (1, 2, 3, 4):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    nonce = websocket.query_params.get("nonce", "")
    teams.register_team(team_id, websocket, nonce)
    try:
        await websocket.send_text(
            json.dumps(teams.team(team_id).to_dict(my_nonce=nonce))
        )
    except WebSocketDisconnect:
        teams.unregister_team(team_id, websocket)
        return
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        teams.unregister_team(team_id, websocket)
