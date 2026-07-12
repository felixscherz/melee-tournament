#!/usr/bin/env python3
"""
Standalone test harness for generated bot scripts.

Runs a bot's `act()` method against a set of canned gamestate scenarios
without needing Dolphin or a live match. Used by the bot-writer agent to
iterate on its code until it passes.

Usage:
    uv run core/test_bot.py <path_to_bot.py>

Exit code 0 = all scenarios passed.
Exit code 1 = one or more scenarios failed (details on stderr).

The mock gamestate objects expose only the fields that bots in this project
are documented to read:
    gamestate.players[port].position.x / .y   (float)
    gamestate.players[port].stock             (int)
    gamestate.players[port].percent           (float)
    gamestate.players[port].action            (melee.Action enum)
    gamestate.players[port].character         (melee.Character enum)
    gamestate.players[port].facing            (bool - True=facing right)

If a bot reads fields beyond these, it may work in the real game but the
harness won't simulate them - keep to the documented interface.
"""

import importlib.util
import sys
import traceback
import types
from pathlib import Path

import melee

# ------------------------------------------------------------------ #
#  Mock gamestate construction                                        #
# ------------------------------------------------------------------ #

REQUIRED_BUTTONS = {
    "BUTTON_A",
    "BUTTON_B",
    "BUTTON_X",
    "BUTTON_Y",
    "BUTTON_L",
    "BUTTON_R",
    "BUTTON_Z",
}


def _player(
    port,
    character,
    x,
    y,
    stock=4,
    percent=0.0,
    action=melee.Action.STANDING,
    facing=True,
):
    return types.SimpleNamespace(
        port=port,
        character=character,
        position=types.SimpleNamespace(x=x, y=y),
        stock=stock,
        percent=percent,
        action=action,
        facing=facing,
    )


def _gamestate(players_dict):
    return types.SimpleNamespace(players=players_dict)


FOX = melee.Character.FOX
FALCO = melee.Character.FALCO
MARTH = melee.Character.MARTH
FALCON = melee.Character.CPTFALCON


def _scenarios():
    """Return a list of (name, gamestate, bot_port) tuples to test against."""
    return [
        (
            "neutral center 1v1",
            _gamestate(
                {
                    1: _player(1, FOX, 0.0, 0.0),
                    2: _player(2, FALCO, 20.0, 0.0),
                }
            ),
            1,
        ),
        (
            "close range 1v1",
            _gamestate(
                {
                    1: _player(1, FOX, 0.0, 0.0),
                    2: _player(2, MARTH, 8.0, 0.0),
                }
            ),
            1,
        ),
        (
            "far range 1v1",
            _gamestate(
                {
                    1: _player(1, FALCON, -10.0, 0.0),
                    2: _player(2, FOX, 80.0, 0.0),
                }
            ),
            1,
        ),
        (
            "off-stage recovering",
            _gamestate(
                {
                    1: _player(1, FOX, 90.0, -30.0, action=melee.Action.FALLING_AERIAL),
                    2: _player(2, FALCO, 0.0, 0.0),
                }
            ),
            1,
        ),
        (
            "opponent off-stage (edgeguard)",
            _gamestate(
                {
                    1: _player(1, MARTH, 60.0, 0.0),
                    2: _player(2, FOX, 85.0, -25.0, action=melee.Action.FALLING_AERIAL),
                }
            ),
            1,
        ),
        (
            "high percent",
            _gamestate(
                {
                    1: _player(1, FOX, 0.0, 0.0, percent=120.0),
                    2: _player(2, FALCO, 15.0, 0.0, percent=0.0),
                }
            ),
            1,
        ),
        (
            "four-player free-for-all",
            _gamestate(
                {
                    1: _player(1, FOX, 0.0, 0.0),
                    2: _player(2, FALCO, 30.0, 0.0),
                    3: _player(3, MARTH, -40.0, 0.0),
                    4: _player(4, FALCON, 50.0, 5.0),
                }
            ),
            1,
        ),
        (
            "opponent airborne",
            _gamestate(
                {
                    1: _player(1, FALCON, 0.0, 0.0),
                    2: _player(2, FOX, 10.0, 40.0, action=melee.Action.FALLING_AERIAL),
                }
            ),
            1,
        ),
        (
            "bot being attacked (tumbling)",
            _gamestate(
                {
                    1: _player(
                        1, FOX, 0.0, 10.0, percent=45.0, action=melee.Action.TUMBLING
                    ),
                    2: _player(2, MARTH, 20.0, 0.0),
                }
            ),
            1,
        ),
        (
            "bot is port 2",
            _gamestate(
                {
                    1: _player(1, FALCO, -20.0, 0.0),
                    2: _player(2, FOX, 0.0, 0.0),
                }
            ),
            2,
        ),
        (
            "facing wrong way",
            _gamestate(
                {
                    1: _player(1, FOX, 0.0, 0.0, facing=False),
                    2: _player(2, MARTH, 25.0, 0.0),
                }
            ),
            1,
        ),
        (
            "bot is port 4 in FFA",
            _gamestate(
                {
                    1: _player(1, FOX, 0.0, 0.0),
                    2: _player(2, FALCO, 30.0, 0.0),
                    3: _player(3, MARTH, -40.0, 0.0),
                    4: _player(4, FALCON, 50.0, 5.0),
                }
            ),
            4,
        ),
        (
            "me is None (port not in gamestate)",
            _gamestate(
                {
                    2: _player(2, FALCO, 0.0, 0.0),
                }
            ),
            1,
        ),
    ]


# ------------------------------------------------------------------ #
#  Validation                                                         #
# ------------------------------------------------------------------ #


def _validate_action(result, scenario_name, frame, bot_path):
    """Validate a single act() return value. Returns error string or None."""
    if result is None:
        return None  # None is valid - means release all inputs

    if not isinstance(result, dict):
        return (
            f'scenario "{scenario_name}" frame {frame}: '
            f"act() returned {type(result).__name__}, expected dict or None"
        )

    # Check stick_x
    sx = result.get("stick_x")
    if sx is None:
        return f'scenario "{scenario_name}" frame {frame}: missing "stick_x" key'
    if not isinstance(sx, (int, float)):
        return (
            f'scenario "{scenario_name}" frame {frame}: '
            f'"stick_x" is {type(sx).__name__}, expected float'
        )
    if not (0.0 <= sx <= 1.0):
        return (
            f'scenario "{scenario_name}" frame {frame}: '
            f'"stick_x" is {sx}, must be in [0.0, 1.0]'
        )

    # Check stick_y
    sy = result.get("stick_y")
    if sy is None:
        return f'scenario "{scenario_name}" frame {frame}: missing "stick_y" key'
    if not isinstance(sy, (int, float)):
        return (
            f'scenario "{scenario_name}" frame {frame}: '
            f'"stick_y" is {type(sy).__name__}, expected float'
        )
    if not (0.0 <= sy <= 1.0):
        return (
            f'scenario "{scenario_name}" frame {frame}: '
            f'"stick_y" is {sy}, must be in [0.0, 1.0]'
        )

    # Check buttons
    buttons = result.get("buttons")
    if buttons is None:
        return f'scenario "{scenario_name}" frame {frame}: missing "buttons" key'
    if not isinstance(buttons, dict):
        return (
            f'scenario "{scenario_name}" frame {frame}: '
            f'"buttons" is {type(buttons).__name__}, expected dict'
        )

    # All 7 button keys must be present and be bools
    for btn in REQUIRED_BUTTONS:
        if btn not in buttons:
            return (
                f'scenario "{scenario_name}" frame {frame}: '
                f'buttons dict missing "{btn}"'
            )
        val = buttons[btn]
        if not isinstance(val, bool):
            return (
                f'scenario "{scenario_name}" frame {frame}: '
                f'button "{btn}" is {type(val).__name__}, expected bool'
            )

    return None


# ------------------------------------------------------------------ #
#  Main test runner                                                   #
# ------------------------------------------------------------------ #

FRAMES_PER_SCENARIO = 20


def _load_bot(bot_path):
    """Import the bot module from a file path. Returns the Bot class."""
    spec = importlib.util.spec_from_file_location("test_bot_module", bot_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {bot_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_bot_module"] = mod
    spec.loader.exec_module(mod)

    if not hasattr(mod, "Bot"):
        raise RuntimeError("Bot script does not define a top-level `Bot` class")
    if not callable(getattr(mod.Bot, "act", None)):
        raise RuntimeError("Bot class does not have a callable `act` method")
    return mod.Bot


def run_tests(bot_path):
    """Run all scenarios against the bot. Returns (passed, failures)."""
    failures = []
    passed = 0

    try:
        BotClass = _load_bot(bot_path)
    except Exception as exc:
        tb = traceback.format_exc()
        return 0, [f"Failed to load bot:\n{tb}"]

    # Instantiate the bot - __init__ must not crash
    try:
        bot = BotClass()
    except Exception as exc:
        tb = traceback.format_exc()
        return 0, [f"Bot.__init__() raised:\n{tb}"]

    for scenario_name, gs, bot_port in _scenarios():
        for frame in range(FRAMES_PER_SCENARIO):
            try:
                result = bot.act(gs, bot_port)
            except Exception:
                tb = traceback.format_exc()
                failures.append(
                    f'scenario "{scenario_name}" frame {frame}: '
                    f"act() raised an exception:\n{tb}"
                )
                break  # stop this scenario on first crash
            else:
                err = _validate_action(result, scenario_name, frame, bot_path)
                if err:
                    failures.append(err)
                    break  # stop this scenario on first validation error
        else:
            # All frames in this scenario passed
            passed += 1

    return passed, failures


def main():
    if len(sys.argv) != 2:
        print("Usage: uv run core/test_bot.py <path_to_bot.py>", file=sys.stderr)
        sys.exit(1)

    bot_path = Path(sys.argv[1])
    if not bot_path.exists():
        print(f"Error: file not found: {bot_path}", file=sys.stderr)
        sys.exit(1)

    total = len(_scenarios())
    passed, failures = run_tests(bot_path)

    if failures:
        print(
            f"FAIL: {len(failures)} error(s), {passed}/{total} scenarios passed:",
            file=sys.stderr,
        )
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        print(
            f"PASS: {passed}/{total} scenarios, {passed * FRAMES_PER_SCENARIO} frames, no errors"
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
