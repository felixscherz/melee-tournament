"""Frame snapshot serialization for the subprocess bot worker.

The in-process model gave bots the live `melee.GameState`. The sandbox model
instead passes a plain-dict snapshot of only the fields the bot interface
documents:

    gamestate.players[port].position.x / .y   (float)
    gamestate.players[port].stock             (int)
    gamestate.players[port].percent           (float)
    gamestate.players[port].action            (melee.Action name str)
    gamestate.players[port].character         (melee.Character name str)
    gamestate.players[port].facing            (bool)

This is exactly the surface mocked by core/test_bot.py, so every existing bot
(fox, marth, falcon, falco, generic, and prompt-generated bots) and the test
harness keep working unchanged - they just receive a types.SimpleNamespace
reconstruction instead of the live object.

Action dicts coming back from a worker are untrusted, so clamp_action
normalizes stick ranges and button keys before they are allowed to reach
libmelee via the orchestrator's _apply().
"""

from typing import Optional

REQUIRED_BUTTONS = (
    "BUTTON_A",
    "BUTTON_B",
    "BUTTON_X",
    "BUTTON_Y",
    "BUTTON_L",
    "BUTTON_R",
    "BUTTON_Z",
)


def frame_snapshot(gs, frame: int) -> dict:
    """Build a JSON-serializable snapshot of the live GameState for a worker.

    Only the documented bot-interface fields are extracted. Enums are stored
    by their `.name` string so the worker can reconstruct the same enum
    singleton via `melee.Action[name]` (identity comparisons still work).
    """
    players = {}
    for port, p in (getattr(gs, "players", None) or {}).items():
        players[str(port)] = {
            "position": {
                "x": float(p.position.x),
                "y": float(p.position.y),
            },
            "stock": int(p.stock),
            "percent": float(p.percent),
            "action": getattr(p.action, "name", None) or "",
            "character": getattr(p.character, "name", None) or "",
            "facing": bool(p.facing),
        }
    return {"frame": int(frame), "players": players}


def clamp_action(action) -> Optional[dict]:
    """Sanitize an action dict returned by an untrusted worker.

    Returns None for non-dict / None inputs. Stick values are clamped to
    [0.0, 1.0] with 0.5 (neutral) for missing/non-numeric. The buttons dict
    is normalized to exactly the seven required keys, each a bool (missing
    keys default to False = not pressed).
    """
    if action is None:
        return None
    if not isinstance(action, dict):
        return None
    return {
        "stick_x": _clamp_stick(action.get("stick_x", 0.5)),
        "stick_y": _clamp_stick(action.get("stick_y", 0.5)),
        "buttons": _normalize_buttons(action.get("buttons")),
    }


def _clamp_stick(value) -> float:
    # Reject bools (Python treats True/False as 0/1 otherwise).
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.5
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


def _normalize_buttons(buttons) -> dict:
    out = {b: False for b in REQUIRED_BUTTONS}
    if not isinstance(buttons, dict):
        return out
    for b in REQUIRED_BUTTONS:
        if b in buttons:
            out[b] = bool(buttons[b])
    return out
