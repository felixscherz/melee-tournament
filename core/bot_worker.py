"""Sandboxed worker process for untrusted bot code.

Spawned one-per-port by core.bot_process.BotWorker. Reads JSON frame snapshots
from stdin (one per line), calls the bot's act() method, and writes JSON
action responses to stdout.

This module is the security boundary. Before importing any bot code it:

- Drops POSIX rlimits (CPU, address space, file size, open files, core dumps).
  Caps a runaway `while True`, defeats `[0]*10**10` memory bombs, and forbids
  the bot from writing files anywhere on disk.
- Inherits a scrubbed environment (set by the parent at Popen time) and runs
  with cwd=scratch (also set by the parent), so the bot has no env or cwd
  foothold into the server process's state.
- Imports the bot module via importlib; the parent has already run the static
  AST validator (core/bot_validator.py) on this file, so we are a second
  layer, not the only one.

The worker is deliberately self-contained: it imports only the stdlib and
`melee` (for enum constants), so it does not need the project's PYTHONPATH and
has no dependency on `core.*` modules that user code could try to reach.

Network isolation is NOT enforced here yet. See IMPROVE_BOT_ISOLATION.md for
the layered plan (macOS sandbox profile or `--network none` container).

Protocol
--------
Each frame: parent writes one JSON object on a single line to our stdin:

    {"frame": 12345, "port": 1, "players": {"1": {...}, "2": {...}, ...}}

We respond with one JSON object on a single line to our stdout:

    {"frame": 12345, "action": {"stick_x": 0.5, "stick_y": 0.5,
                                "buttons": {"BUTTON_A": false, ...}}}

`action` may be null (the bot chose to release all inputs) or, on internal
error, we still respond with `{"frame": <frame>, "action": null}` so the
parent's frame-id matching can drain the stale line.

Usage
-----
    python -u core/bot_worker.py <bot_path>

(`-u` keeps stdin/stdout/stderr unbuffered; the parent also passes bufsize=0
and a scrubbed env at Popen time.)
"""

import json
import os
import resource
import sys
import traceback
import types
from importlib import util
from pathlib import Path

import melee  # worker needs melee for enum constants; safe to import

# ------------------------------------------------------------------ #
#  RLIMIT sandbox                                                     #
# ------------------------------------------------------------------ #

# 256 MB address space - enough for the heaviest legitimate bot, far below
# the server's working set, and well clear of `[0]*10**10` bombs.
_MAX_ADDRESS_SPACE_BYTES = 256 * 1024 * 1024
# 3 CPU-seconds backstop. A 60fps bot should use microseconds per frame; this
# catches a runaway loop only after it has burned 3s of CPU, which is plenty
# of slack for the per-frame deadline the parent enforces.
_MAX_CPU_SECONDS = 3
# Limit open file descriptors. The bot has no legitimate need for many fds.
_MAX_OPEN_FILES = 16

# Map int rlimit constants to readable names for diagnostic log lines. On
# macOS `resource.RLIMIT_*` are plain ints, not named enum values, so we
# cannot just call `.name` on them.
_RLIMIT_NAMES = {
    resource.RLIMIT_CPU: "RLIMIT_CPU",
    resource.RLIMIT_FSIZE: "RLIMIT_FSIZE",
    resource.RLIMIT_NOFILE: "RLIMIT_NOFILE",
    resource.RLIMIT_CORE: "RLIMIT_CORE",
    resource.RLIMIT_AS: "RLIMIT_AS",
}


def _apply_limits():
    """Drop rlimits before importing untrusted bot code. Best-effort: macOS
    sometimes rejects RLIMIT_AS (the inherited soft limit is unlimited and
    exceeds the proposed hard cap); failures are logged to stderr and
    skipped so the bot still runs - the per-frame parent deadline is the
    primary availability boundary, not rlimits.

    Note: the `resource.RLIMIT_*` constants are plain ints (not enum values)
    on macOS, so we map them to human-readable names ourselves for log lines.
    """

    def _name(which):
        return _RLIMIT_NAMES.get(which, str(which))

    def _set(which, soft_hard):
        try:
            resource.setrlimit(which, soft_hard)
        except (ValueError, OSError, OverflowError) as exc:
            sys.stderr.write(f"bot_worker: setrlimit {_name(which)} failed: {exc}\n")

    _set(resource.RLIMIT_CPU, (_MAX_CPU_SECONDS, _MAX_CPU_SECONDS))
    _set(resource.RLIMIT_FSIZE, (0, 0))
    _set(resource.RLIMIT_NOFILE, (_MAX_OPEN_FILES, _MAX_OPEN_FILES))
    _set(resource.RLIMIT_CORE, (0, 0))
    # RLIMIT_AS needs a two-step on macOS: the inherited soft limit is often
    # RLIM_INFINITY, which exceeds any new hard cap we try to set, so the
    # kernel rejects the single-call setrlimit with EINVAL. Lower the soft
    # first (preserving the current hard), then set hard = soft = cap.
    try:
        cur_soft, cur_hard = resource.getrlimit(resource.RLIMIT_AS)
        if cur_soft > _MAX_ADDRESS_SPACE_BYTES:
            resource.setrlimit(resource.RLIMIT_AS, (_MAX_ADDRESS_SPACE_BYTES, cur_hard))
        resource.setrlimit(
            resource.RLIMIT_AS,
            (_MAX_ADDRESS_SPACE_BYTES, _MAX_ADDRESS_SPACE_BYTES),
        )
    except (ValueError, OSError, OverflowError) as exc:
        sys.stderr.write(f"bot_worker: setrlimit RLIMIT_AS failed: {exc}\n")


# ------------------------------------------------------------------ #
#  Bot module import                                                  #
# ------------------------------------------------------------------ #


def _load_bot_module(path: Path):
    """Import the validated bot file and return an instantiated Bot.

    Raises on any failure (syntax error, missing Bot class, missing act,
    module-level exception). The caller (main) reports the failure to stderr
    and exits - the parent detects the dead child on its next act() call.
    """
    spec = util.spec_from_file_location("user_bot", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not create import spec for {path}")
    mod = util.module_from_spec(spec)
    sys.modules["user_bot"] = mod
    spec.loader.exec_module(mod)
    if not hasattr(mod, "Bot") or not callable(getattr(mod.Bot, "act", None)):
        raise RuntimeError("bot script missing top-level Bot class with act()")
    return mod.Bot()


# ------------------------------------------------------------------ #
#  Gamestate reconstruction (mirrors core/test_bot.py mocks)         #
# ------------------------------------------------------------------ #


def _reconstruct_gamestate(snap: dict):
    """Build a read-only SimpleNamespace view of the frame snapshot.

    The shape matches the documented bot interface (and what test_bot.py
    mocks): gamestate.players[port].{position.{x,y}, stock, percent, action,
    character, facing}. Enum names are rebuilt into the real enum singletons
    so `melee.Action.STANDING is gs.players[1].action` still holds.
    """
    players = {}
    for port_str, p in (snap.get("players") or {}).items():
        port = int(port_str)
        action = _enum_from_name(melee.Action, p.get("action"), melee.Action.STANDING)
        character = _enum_from_name(
            melee.Character, p.get("character"), melee.Character.FOX
        )
        players[port] = types.SimpleNamespace(
            port=port,
            position=types.SimpleNamespace(
                x=float(p["position"]["x"]),
                y=float(p["position"]["y"]),
            ),
            stock=int(p.get("stock", 0)),
            percent=float(p.get("percent", 0.0)),
            action=action,
            character=character,
            facing=bool(p.get("facing", True)),
        )
    return types.SimpleNamespace(players=players)


def _enum_from_name(enum_cls, name, fallback):
    if not name:
        return fallback
    try:
        return enum_cls[name]
    except KeyError:
        return fallback


# ------------------------------------------------------------------ #
#  Request/response loop                                              #
# ------------------------------------------------------------------ #


def _respond(frame, action, error: bool = False) -> bool:
    """Write one response line. Returns False if stdout is closed (parent gone).

    `error=True` distinguishes a bot that raised an exception (or whose
    gamestate failed to reconstruct) from a bot that legitimately returned
    None to release all inputs. The parent counts consecutive errors toward
    the same dead-worker threshold as deadline misses, so a perpetually
    crashing bot falls back to the default sooner rather than silently
    standing still forever.
    """
    try:
        sys.stdout.write(
            json.dumps({"frame": frame, "action": action, "error": error}) + "\n"
        )
        sys.stdout.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


def _run_loop(bot) -> int:
    """Read frames from stdin, dispatch to bot.act(), respond on stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            snap = json.loads(line)
        except json.JSONDecodeError:
            # Malformed input from parent - skip; we have no frame id to echo.
            continue
        frame = snap.get("frame")
        port = snap.get("port")
        action = None
        error = False
        if port is not None:
            try:
                gs = _reconstruct_gamestate(snap)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                gs = None
                error = True
            if gs is not None:
                try:
                    action = bot.act(gs, port)
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                    action = None
                    error = True
        if not _respond(frame, action, error):
            return 0  # parent closed the pipe - exit cleanly
    return 0


def main(argv) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: bot_worker <bot_path>\n")
        return 2
    bot_path = Path(argv[1])
    _apply_limits()
    try:
        bot = _load_bot_module(bot_path)
    except Exception:
        sys.stderr.write("FATAL: bot module failed to import\n")
        traceback.print_exc(file=sys.stderr)
        return 1
    return _run_loop(bot)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
