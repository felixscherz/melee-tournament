# Smash Tournament — Agent Playbook

This document is the authoritative guide for AI agents working in this repo.
Read it fully before touching any code.

---

## What This Project Is

A self-hosted platform running on Felix's MacBook that lets a team submit Python
bot scripts or LLM prompts to control Super Smash Bros. Melee characters via
`libmelee` and Slippi Dolphin. Games are streamed live to the team via Twitch.

---

## Target Architecture

```
[Slippi Dolphin] ◄──── [melee_orchestrator.py]  (async 60fps loop)
                              │
               ┌──────────────┴──────────────┐
               │                             │
         [LLM / Ollama]    [sandboxed bot subprocess per port]
               │             (core/bot_process.BotWorker +
               │              core/bot_worker.py, JSON/stdio IPC,
               │              rlimits, scrubbed env, 10ms deadline)
               │                             │
               │   ┌─ worker dies? ──► [trusted in-process default bot
               │   │                     from core/bots/<char>.py, no sandbox]
               │   │
               └──────────────┬──────────────┘
                              │
                       [FastAPI server]  :8080
                       /lobby — team landing (join/add/remove teams, 2-4)
                       /team/{n} — team workspace (contributions, captain, generate)
                       /ws/gamestate — live push to browser
                       /ws/teams, /ws/team/{n} — team state push
                              │
                    ┌──────────┴──────────┐
                    │                     │
              [WebSocket]           [REST API]

[OBS Studio] ──RTMP──► [Twitch CDN]  (direct ingest; one upload, fans out to viewers)

[WireGuard wg0]  Mac 10.0.0.20 ↔ 10.0.0.1 Hetzner VM
                        │        (on-demand: ./stream-vpn.sh up/down)
                        │
                  [nginx TLS termination on VM]
                  smash.felixscherz.me → 10.0.0.20:8080  (FastAPI dashboard only)
```

The WireGuard tunnel is only used to expose the FastAPI dashboard publicly. The
video stream goes directly from OBS to Twitch's CDN, so the Mac uploads one copy
regardless of viewer count. Setup + steady-state ops in `docs/DEPLOYMENT.md`.

---

## Installed Software (all already present — do not reinstall)

| Tool | Location | Notes |
|---|---|---|
| Slippi Dolphin | `/Applications/Slippi Dolphin.app` | Intel binary, runs under Rosetta 2 |
| Rosetta 2 | System | Already installed |
| Melee ISO | `assets/melee.iso` | NTSC v1.02 (GALE01 r2) |
| uv | `$(command -v uv)` | Manages Python + deps. Virtualenv at `.venv/` (Python 3.13). Run everything via `uv run` — no manual activation. Deps declared in `pyproject.toml`, pinned in `uv.lock` |
| OBS Studio | `/Applications/OBS.app` | Configured for 60fps, 6000 Kbps, no B-frames; pushes directly to Twitch |
| wireguard-tools | `$(brew --prefix)/bin/wg` | `wg`/`wg-quick`; tunnel config `config/wireguard/wg0-smash.conf` (gitignored) |

---

## How to Start Everything (working local stack)

```bash
# 1. Start the unified server (FastAPI + orchestrator; launches Dolphin automatically)
#    First run (or after pulling): `uv sync` to install/update deps from uv.lock.
uv run main.py

# 2. Open OBS and click Start Streaming
#    (OBS is already configured to push directly to Twitch — just hit the button)

# 3. Open the lobby, pick 4 characters, start a match
open http://localhost:8080/lobby
```

---

## How to Run the Game Loop

Run everything through `uv run` (no venv activation needed — uv resolves the
interpreter and deps from `pyproject.toml` / `uv.lock`):

Run the unified launcher (starts FastAPI on :8080 and launches Dolphin once,
keeping it alive across matches so OBS captures a stable window):

```bash
uv run main.py
```

Dolphin boots the ISO and idles at the menus. Matches are queued via the
lobby UI or the API. The lobby is team-based: up to 4 team slots (one per
port), each with a captain, a character pick, a stack of prompt contributions,
and a ready toggle. A team's identity IS its port; each slot is **active** or
**inactive** and the active set can be any subset of `{1,2,3,4}` of size 2-4
(non-contiguous is fine — e.g. teams 1 and 4). Teams are added/removed from the
lobby (`POST /api/team/{n}/activate` / `/deactivate`). `/api/start` is
parameterless — it pulls every ACTIVE team's state from the `TeamRegistry`
(`core/teams.py`) and requires all active teams (minimum 2) to be ready:

```bash
curl -X POST http://localhost:8080/api/start
```

The game loop picks the match up at CSS, locks in each active team's character,
selects Final Destination, and starts. CSS → in-game takes a couple of
seconds; if cursors visibly overshoot or oscillate, see "Game loop rules"
below. Poll `GET /api/state` for phase/scores.

To stop: `Ctrl+C` (or `kill <pid>`). Dolphin closes with the Python process.

MoltenVK (`[mvk-*]`) log spam is normal — it is Vulkan noise from the
Intel-on-ARM Rosetta path. Not errors. Filter with `grep -v mvk` if needed.

### Game loop rules (hard-won — do not regress these)

The full write-up lives in the `libmelee-game-loop` skill. Summary:

1. **`console.step()` is the 60fps pacer** (`polling_mode=False` blocks until
   Dolphin's next frame). **Never add a pacing sleep around it.** An extra
   `sleep(1/60 - elapsed)` means that after any hiccup the socket backlog of
   frame events can never drain — every gamestate is permanently N frames
   stale, and `choose_character`'s bang-bang navigation then overshoots and
   oscillates. This exact bug caused erratic CSS cursors here; capping stick
   deflection to hide it was wrong. Use vanilla libmelee helpers.
2. **Call `step()` via `await asyncio.to_thread(...)`** — it is a blocking
   socket read and must not sit on the event loop thread next to uvicorn.
3. **On every menu transition, `release_all()` all controllers once** —
   entering CSS with A still held (from main-menu mashing) grabs the CPU
   slider.
4. **If a port reports `is_holding_cpu_slider`, `release_all()` instead of
   calling `choose_character`** — an upstream operator-precedence bug makes
   the helper chase the slider rows even with `cpu_level=0`.

---

## libmelee Package

### Critical: the PyPI package is called `melee`, not `libmelee`

```bash
uv add melee              # correct
uv add libmelee           # wrong — does not exist on PyPI
```

Current version: **0.47.2**

### Key API facts for melee 0.47

**`melee.Console`** — main entry point:

```python
console = melee.Console(
    path="/Applications/Slippi Dolphin.app/Contents/MacOS/Slippi Dolphin",
    slippi_address="127.0.0.1",
    slippi_port=51441,
    blocking_input=False,
    polling_mode=False,
    fullscreen=False,
)
console.run(iso_path="assets/melee.iso")  # launches Dolphin
console.connect()                          # blocks until Dolphin is ready
gamestate = console.step()                 # call every frame
```

**`melee.Controller`** — must connect before use:

```python
ctrl = melee.Controller(console=console, port=1, type=melee.ControllerType.STANDARD)
ctrl.connect()
ctrl.tilt_analog(melee.Button.BUTTON_MAIN, x, y)  # x/y: 0.0–1.0, 0.5=neutral
ctrl.press_button(melee.Button.BUTTON_A)
ctrl.release_button(melee.Button.BUTTON_A)
ctrl.release_all()
```

**`melee.MenuHelper`** — must be **instantiated**, not called statically:

```python
menu_helper = melee.MenuHelper()   # create once, reuse every frame

# CSS — call for BOTH controllers every frame or the match won't start.
# All controller cursors must be above the character level slider.
menu_helper.choose_character(
    character=melee.Character.FOX,
    gamestate=gamestate,
    controller=ctrl_p1,
    cpu_level=0,    # 0 = human/bot, 1-9 = CPU
    costume=0,
    swag=False,
    start=False,    # set True on exactly one controller to begin the match
)

# Stage select
menu_helper.choose_stage(
    stage=melee.Stage.FINAL_DESTINATION,
    gamestate=gamestate,
    controller=ctrl_p1,
    character=melee.Character.FOX,  # required in 0.47, absent in older docs
    autostart=True,
)

# All-in-one helper (handles MAIN_MENU, CHARACTER_SELECT, STAGE_SELECT,
# POSTGAME_SCORES, PRESS_START). It DOES drive CSS via choose_character
# internally — but only for the single controller passed in, so you still
# need one call per connected port per frame.
menu_helper.menu_helper_simple(
    gamestate=gamestate,
    controller=ctrl_p1,
    character_selected=melee.Character.FOX,
    stage_selected=melee.Stage.FINAL_DESTINATION,
    cpu_level=0,
    autostart=True,
)
```

**`melee.Menu` enum values:**

```
MAIN_MENU, CHARACTER_SELECT, STAGE_SELECT, IN_GAME,
POSTGAME_SCORES, PRESS_START, SUDDEN_DEATH, SLIPPI_ONLINE_CSS, UNKNOWN_MENU
```

**`gamestate.players`** — dict keyed by port number (1-indexed):

```python
p1 = gamestate.players.get(1)  # None if port not active
p1.position.x, p1.position.y
p1.stock
p1.percent
p1.action         # melee.Action enum — current animation state
p1.character      # melee.Character enum
```

### Known pitfall: CSS requires ALL connected controllers

If any connected port is not driven through CSS, its cursor floats idle and
the game never starts (Melee's `ready_to_start` only clears once every plugged
controller has locked in). The orchestrator plugs in exactly the active teams'
controllers (2-4), so every connected port maps to an active player and
`/api/start` requires all active teams ready. Call `choose_character` for every
connected port every frame during `menu_state == melee.Menu.CHARACTER_SELECT`.

**Variable team count → Dolphin relaunch.** Whether a port is plugged
(Dolphin's `SIDevice`) is read at boot, so changing the active set requires
relaunching Dolphin. `MeleeOrchestrator.queue_match` is async and calls
`_reconfigure(ports)` (stop the game loop, `console.stop()`, rebuild the
Console with STANDARD controllers for active ports and UNPLUGGED for the rest,
`run` + `connect`, restart the loop) whenever the queued match needs a
different port set than is currently plugged. Dolphin is therefore kept alive
across matches with the SAME active set, and only relaunched when it changes
(OBS re-acquires the "Slippi Dolphin" window automatically). `self._ports`
holds the current plugged set; there is no fixed 4-port assumption anymore.

---

## Bot Interface

User bots must define a `Bot` class with an `act` method:

```python
class Bot:
    def __init__(self): ...

    def act(self, gamestate: melee.GameState, player_port: int) -> dict | None:
        # return None to release all inputs
        return {
            "stick_x": 0.5,   # 0.0=left, 1.0=right, 0.5=neutral
            "stick_y": 0.5,   # 0.0=down, 1.0=up, 0.5=neutral
            "buttons": {
                "BUTTON_A": False,
                "BUTTON_B": False,
                "BUTTON_X": False,
                "BUTTON_Y": False,
                "BUTTON_L": False,
                "BUTTON_R": False,
                "BUTTON_Z": False,
            },
        }
```

See `core/bot_template.py` for a working example with a simple chase-and-attack logic.

> **The signature is unchanged from the bot author's perspective**, but bots no
> longer run in-process. See "How bots actually run (subprocess sandbox)"
> below for what happens between `act()` returning and the controller moving.

---

## How bots actually run (subprocess sandbox)

User-submitted bot code (pasted code, prompt-generated, or the
default-character bots when nobody pasted anything) runs in its own
**subprocess per port**, not in the server process. The orchestrator
(`core/melee_orchestrator.py`) spawns one `core/bot_process.BotWorker` per
port in `queue_match()`, tears them down on match end / abort, and talks to
each over JSON-on-stdio:

- The parent sends one JSON snapshot per frame to the worker's stdin:
  `{"frame": N, "port": 1, "players": {"1": {...}, "2": {...}, ...}}`
- The worker (`core/bot_worker.py`, a self-contained entry point that imports
  only stdlib + `melee`) reconstructs a `types.SimpleNamespace` view of the
  gamestate (same shape as the mocks in `core/test_bot.py`), calls
  `bot.act(gamestate, port)`, and writes a JSON response:
  `{"frame": N, "action": {...}, "error": false}`.

This is the security boundary. Before importing the bot module the worker
drops `RLIMIT_CPU`, `RLIMIT_FSIZE = 0`, `RLIMIT_NOFILE`, `RLIMIT_CORE`, and
`RLIMIT_AS` via `setrlimit`, and runs with a scrubbed environment (only
`PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ` are inherited) and
`cwd = .bot_scratch`. Twitch keys, WireGuard config, OLLAMA URLs etc. are
gone. RCE-via-reflection is over (separate address space), CPU/mem/file-write
DoS is bounded by rlimits, and the parent enforces a 10ms per-frame deadline
via `select.select([stdout_fd], [], [], deadline)` so a stuck bot can never
stall the 60fps loop. See `docs/IMPROVE_BOT_ISOLATION.md` for the full design
record and the remaining TODOs (network isolation is the big one).

Failure and lifecycle semantics an agent needs to know:

- **Per-frame deadline (10ms, tunable in `config/settings.toml [bots]`).** On
  miss: that port gets neutral input (`release_all`) for that frame.
- **After `max_misses` (3) consecutive deadline misses OR bot-exception
  responses**, the worker is killed and marked dead. The port then falls
  back to the trusted in-process default bot (`core/bots/<char>.py`, or
  `generic.py`) loaded via `BotLoader` for the rest of the match - that
  character keeps playing simple AI, not standing still.
- **Cold-start grace (`_COLD_START_BUDGET_S = 2s`):** the worker's first
  successful response is given a 2s budget (Python startup + `import melee`
  is ~130ms on this Mac) instead of 10ms, so cold respawn after a hot-reload
  doesn't immediately trip `max_misses`. In production the worker is spawned
  during CSS navigation and the first `act()` happens seconds later when
  `IN_GAME` starts, so the grace is mostly defensive.
- **Hot-reload is parent-side mtime + respawn.** Editing the bot file on
  disk makes the next `act()` call kill and respawn the child. The first
  post-respawn frame will likely miss (cold start); after that the new code
  is live. `_decision_loop` checks mtime once per frame per port.
- **Action clamping.** Every action coming back from a worker is type-checked
  and clamped (sticks to `[0.0, 1.0]`, buttons normalized to exactly the
  seven required keys) by `core/frame.clamp_action` before it reaches
  `_apply` and libmelee. A malformed return cannot reach the controller.

The static `core/bot_validator.py` stays as a fast pre-flight check at
`/api/start` (the frontend rejects obviously bad pasted code before even
writing it to disk) and inside `BotLoader.load()` (the in-process fallback
path). It is defense-in-depth; the runtime sandbox is the boundary.

**Bot author contract is unchanged.** Bots still `import melee`, define
`class Bot`, and implement `act(gamestate, port) -> dict | None`. The
`gamestate` argument is now a `types.SimpleNamespace` reconstruction (same
field surface as `core/test_bot.py`'s mocks) instead of the live
`melee.GameState`, but every existing bot (fox/marth/falcon/falco/generic
and any prompt-generated bot) works without changes - that's the whole
reason the snapshot is restricted to those fields.

### Three ways to control a player (per team)

Each team (one per port) controls its fighter through its team workspace at
`/team/{n}`. The captain is the one who commits the bot:

1. **Default AI** - no contributions, no code override, no generated bot. Uses
   the character's built-in bot from `core/bots/`.
2. **Generated from team prompt** - teammates add strategy contributions
   (short natural-language ideas). The captain clicks ASSEMBLE & GENERATE,
   which calls `assemble_prompt()` (`core/bot_generator.py`) to merge all
   contributions into one prompt with a merge preamble, then spawns
   `opencode run --auto --agent bot-writer`. The agent writes a versioned bot
   file to `generated/` and tests it with `core/test_bot.py` until it passes.
3. **Captain code override** - the captain pastes a Python `Bot` class into the
   team workspace's code override box. Validated by `core/bot_validator.py`
   and written to `uploads/player{port}.py` at match start.

Priority when starting a match: captain code override > generated bot > default bot.

### Team state (`core/teams.py`)

The `TeamRegistry` singleton holds 4 `TeamState` objects (one per port/team),
each with an `active` flag — the active subset (size 2-4) is the match roster.
`activate(n)`/`deactivate(n)` toggle it (`deactivate` guards the 2-team
minimum); `active_ids()` returns the current roster; `all_ready()` is
count-aware. Fresh installs default to teams 1 & 2 active. Each team has: a
captain (claimed via a client-side localStorage nonce; instant takeover with UI
confirm), a character pick, a stack of prompt contributions from any teammate,
an optional code override, a generated bot version, and a ready toggle. State
(including `active`) is persisted to `generated/teams.json` so a page refresh
doesn't lose anything mid-session. `summary()` returns all 4 slots (with
`active`) so the lobby can render active cards next to inactive "add team"
placeholders; `reset_all()` preserves the active set.
`POST /api/teams/reset` clears everything for a fresh round (operator).
WebSocket push-on-change: `/ws/teams` (landing page, 4-team summary) and
`/ws/team/{n}` (team workspace, full team state).

### Generated bots (`generated/`)

Prompt-generated bots are written to `generated/` with versioned filenames
(`p{port}_{char}_{timestamp}_{hash}.py`) - they are never overwritten. A
`generated/latest.json` index maps each port to its most recently generated
bot. The directory is gitignored.

The generation pipeline:
- `POST /api/team/{n}/generate` (`frontend/app.py`) assembles the team's
  contributions via `assemble_prompt()`, then `core/bot_generator.py` spawns
  `opencode run --auto --agent bot-writer` with the character, merged prompt,
  and target file path.
- The `bot-writer` agent (`.opencode/agents/bot-writer.md`) loads two skills
  (`.opencode/skills/libmelee-bot-interface/` and `melee-strategy/`), writes
  the bot, and iterates on `core/test_bot.py` until it passes.
- The agent's `edit` permission is scoped to `generated/**` only; its `bash`
  permission is scoped to the test harness command only.
- Model: from `[opencode] model` in `config/settings.toml`, passed to
  `opencode run --model ...` by `core/bot_generator.py`. If that key is empty,
  opencode uses the default declared in `.opencode/agents/bot-writer.md`
  (`opencode/deepseek-v4-flash-free`). settings.toml is the single source of
  truth — change the model there, not in the agent file.

### Test harness (`core/test_bot.py`)

Standalone script that imports a bot file and runs `act()` against 12 mock
gamestate scenarios (20 frames each) without Dolphin. Validates the return
dict shape, stick ranges, and button keys. Used by the bot-writer agent to
iterate. Can also be run manually:

```bash
uv run core/test_bot.py <path_to_bot.py>
```

Bots live at fixed paths in `core/bots/` (one per character) and are
hot-reloaded at match time via the subprocess sandbox
(`core/bot_process.BotWorker` watches the bot file's mtime and respawns the
child on change - no restart required). Generated bots in `generated/` use
the same mtime-respawn path. The in-process `core/bot_loader.py` is now
used only for the trusted default-bot fallback (see "How bots actually run"
above); it still hot-reloads on mtime change in that fallback path.

---

## Configuration

All configuration lives in `config/settings.toml`, loaded through
`core/config.load_settings()` (used by `main.py`, `frontend/app.py`, and
`core/bot_generator.py` - do not re-add per-file `toml.load` calls). That file
is **gitignored and per-machine**; the committed template is
`config/settings.example.toml`. If `settings.toml` is missing, `load_settings()`
falls back to the example and logs a warning, so a fresh clone still boots. When
adding a new setting, add it to the example (with a comment) and to the README's
Configuration reference table. Key sections:

```toml
[dolphin]
path = "/Applications/Slippi Dolphin.app/Contents/MacOS/Slippi Dolphin"
iso  = "assets/melee.iso"   # relative paths resolve from repo root
port = 51441                # ENet port libmelee uses to talk to Dolphin

[domains]
frontend = "smash.felixscherz.me"   # empty = local-only; only whitelists the Twitch embed host

[streaming]
# OBS streams directly to Twitch (no WebRTC/OME relay). Set the Twitch channel
# name (the part after twitch.tv/) so the embedded player works on /watch and
# /lobby.
twitch_channel = "v4in11111"

[opencode]
model = "opencode/deepseek-v4-flash-free"   # bot-writer model; empty = agent-file default
```

---

## OBS Studio Settings (working configuration)

OBS is already configured. If it ever needs to be set up again:

**Settings → Stream:**
- Service: `Custom`
- Server: `rtmp://live.twitch.tv/app`
- Stream Key: `<your Twitch stream key>`

**Settings → Output → Mode: Advanced → Streaming tab:**
- Encoder: `Apple VT H264 Hardware Encoder` (preferred on Apple Silicon)
- Rate Control: `CBR`
- Bitrate: `6000 Kbps`
- Keyframe Interval: `1` second
- Profile: `Baseline`
- **Use B-Frames: unchecked** ← critical for streaming quality.

**Settings → Video:**
- Base Resolution: `1920x1080` (or match Dolphin window)
- Output Resolution: `1280x720`
- Common FPS Values: **`60`** ← must match Melee's 60fps or every other frame drops

**Source:** Window Capture → `Slippi Dolphin`

---

## WireGuard Tunnel (public dashboard exposure)

The FastAPI dashboard is exposed to the internet over a **WireGuard** tunnel to
the Hetzner VM with nginx TLS termination. The Mac joins the `10.0.0.0/24` VPN
**on-demand**, only while the dashboard needs to be public. Full setup +
steady-state ops in `docs/DEPLOYMENT.md`.

```bash
./stream-vpn.sh up       # join VPN (dashboard goes public at smash.felixscherz.me)
./stream-vpn.sh status   # show handshake
./stream-vpn.sh down     # leave VPN
```

- Mac tunnel config: `config/wireguard/wg0-smash.conf` (gitignored; private key
  stays on the Mac). Mac = `10.0.0.20`, VM = `10.0.0.1`.
- VM side (WG server peer, nginx upstreams, TLS) is **Ansible-managed in the
  `home` repo** — deploy tags `vpn` and `proxy`.
- Public routing: `smash.felixscherz.me` → `10.0.0.20:8080` (FastAPI only).
  The video stream goes directly from OBS to Twitch, so the tunnel does not
  carry any video traffic.

## Web Dashboard

Served by `python main.py` (do not run uvicorn separately — the orchestrator
would be missing). `main.py` injects the `MeleeOrchestrator` instance into
`frontend.app._orchestrator` and runs uvicorn in the same event loop.

The full write-up of how the frontend is built (server structure, templates,
captain/nonce model, WebSocket feeds, how to add a page/route/field) lives in
the `frontend` skill (`.opencode/skills/frontend/SKILL.md`). Read it before
touching the dashboard.

Routes (`frontend/app.py`):
- `GET /lobby` — team landing page: active team cards + "add team" slots, live team status, START button (`/` redirects here)
- `GET /team/{n}` — team workspace: captain claim, character pick, contributions stack, assemble & generate, code override, ready toggle
- `GET /watch` — Twitch stream embed + live team scores
- `GET /admin` — phase pill, team readiness, end-match / reset-teams buttons
- `POST /api/start` — queue a match (parameterless; pulls every ACTIVE team's state from `TeamRegistry`; requires all active teams ready, min 2)
- `POST /api/stop` — abort the running match and return to IDLE
- `POST /api/team/{n}/activate` — add team n to the active roster (lobby only; max 4)
- `POST /api/team/{n}/deactivate` — remove team n from the roster (lobby only; min 2)
- `POST /api/teams/reset` — clear all team state for a fresh round, keeping the active set (operator)
- `GET /api/teams` — all-4-slot summary (each with `active`) for the landing page
- `GET /api/team/{n}` — full team state
- `POST /api/team/{n}/captain` — claim/take over captain (`{nonce, nickname, force?}`)
- `POST /api/team/{n}/character` — captain sets character
- `POST /api/team/{n}/name` — captain renames the team
- `POST /api/team/{n}/contribute` — add a prompt contribution (`{nonce, nickname, text}`)
- `DELETE /api/team/{n}/contribution/{id}` — remove a contribution (own or captain)
- `POST /api/team/{n}/code` — captain sets code override
- `POST /api/team/{n}/prompt-preview` — preview the assembled prompt without generating
- `POST /api/team/{n}/generate` — assemble + run bot-writer; returns version
- `POST /api/team/{n}/ready` — captain toggles ready
- `GET /api/state` — phase, scores (with team_name), winner
- `WS /ws/gamestate` — 10Hz game state push (stocks, percent, action, team_name)
- `WS /ws/teams` — push 4-team summary on any team change (landing page)
- `WS /ws/team/{n}` — push full team state on any change to that team (team workspace)

Characters map to fixed bot files in `core/bots/` (fox.py, marth.py,
falcon.py, falco.py) used as the trusted in-process fallback when a port's
subprocess worker dies or no pasted/generated bot is provided. Live bot
execution goes through the subprocess sandbox in `core/bot_process.py` /
`core/bot_worker.py` (see "How bots actually run" above).
Generated bots in `generated/` are resolved per-port via
`generated/latest.json`.

---

## What Is Not Yet Done

- [x] ~~Public tunnel~~ — WireGuard tunnel + nginx (Ansible) live; frp retired
- [x] ~~Prompt-to-bot generation~~ — opencode `bot-writer` agent + skills + test harness live
- [ ] Ollama / Llama3 not installed (LLM in-game decisions always fall back to None; prompt-to-bot uses opencode instead)
- [ ] No test suite for the core orchestrator/frontend (bot test harness exists at `core/test_bot.py`)
