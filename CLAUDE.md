# Smash Tournament — Agent Playbook

This document is the authoritative guide for AI agents working in this repo.
Read it fully before touching any code.

---

## What This Project Is

A self-hosted platform running on Felix's MacBook that lets a team submit Python
bot scripts or LLM prompts to control Super Smash Bros. Melee characters via
`libmelee` and Slippi Dolphin. Games are streamed live to the team over WebRTC.

---

## Target Architecture

```
[Slippi Dolphin] ◄──── [melee_orchestrator.py]  (async 60fps loop)
                              │
               ┌──────────────┴──────────────┐
               │                             │
         [LLM / Ollama]           [BotLoader — importlib hot-reload]
               │                             │
               └──────────────┬──────────────┘
                              │
                       [FastAPI server]  :8080
                       /ws/gamestate — live push to browser
                       /api/bot/upload — hot-reload bot scripts
                       /api/prompt — override LLM prompt
                              │
                   ┌──────────┴──────────┐
                   │                     │
             [WebSocket]           [REST API]

[OBS Studio] ──RTMP──► [OvenMediaEngine (Docker)]
                              │
                         [WebRTC / OvenPlayer in browser]
                              │
                       [frpc on Mac] ──tunnel──► [frps on Hetzner VM]
                                                        │
                                              [nginx TLS termination]
                                              smash.felixscherz.me    → FastAPI
                                              stream-smash.felixscherz.me → OME WebRTC
```

---

## Installed Software (all already present — do not reinstall)

| Tool | Location | Notes |
|---|---|---|
| Slippi Dolphin | `/Applications/Slippi Dolphin.app` | Intel binary, runs under Rosetta 2 |
| Rosetta 2 | System | Already installed |
| Melee ISO | `assets/melee.iso` | NTSC v1.02 (GALE01 r2) |
| Python venv | `.venv/` | Python 3.13, activate before running anything |
| OvenMediaEngine | Docker container `ome` | Ports 1935/TCP, 3333/TCP, 3478/TCP, 10000-10009/UDP |
| OBS Studio | `/Applications/OBS.app` | Configured for 60fps, 6000 Kbps, no B-frames |
| frpc | `$(brew --prefix)/bin/frpc` | v0.69.1, frp client for Mac |
| Docker | System | v29.5, daemon already running |

---

## How to Start Everything (working local stack)

```bash
# 1. Start OvenMediaEngine
docker start ome

# 2. Start the game loop (launches Dolphin automatically)
source .venv/bin/activate
python3 -m core.melee_orchestrator &

# 3. Open OBS and click Start Streaming
#    (OBS is already configured — just hit the button)

# 4. Open the dashboard or test page in a browser
open /tmp/webrtc-test.html   # quick test
# or:
uvicorn frontend.app:app --host 0.0.0.0 --port 8080
```

---

## How to Run the Game Loop

Always activate the venv first:

```bash
source .venv/bin/activate
```

Run the orchestrator (launches Dolphin, navigates menus, starts match):

```bash
python3 -m core.melee_orchestrator
```

Dolphin will open a window. Within ~15 seconds it will:
1. Boot the ISO
2. Navigate to Character Select (CSS)
3. Lock in Fox (P1) vs Marth CPU level 3 (P2)
4. Navigate Stage Select → Final Destination
5. Start the match

To stop: `Ctrl+C` (or `kill <pid>`). Dolphin closes with the Python process.

MoltenVK (`[mvk-*]`) log spam is normal — it is Vulkan noise from the
Intel-on-ARM Rosetta path. Not errors. Filter with `grep -v mvk` if needed.

---

## libmelee Package

### Critical: the PyPI package is called `melee`, not `libmelee`

```bash
pip install melee          # correct
pip install libmelee       # wrong — does not exist on PyPI
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

# All-in-one helper (handles MAIN_MENU, STAGE_SELECT, POSTGAME_SCORES, PRESS_START)
# Does NOT drive CSS — handle that manually with choose_character above
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

### Known pitfall: CSS requires both controllers

If only P1's controller is driven through CSS, P2's cursor floats idle and
the game never starts. Always call `choose_character` for both ports every frame
during `menu_state == melee.Menu.CHARACTER_SELECT`.

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

Bots are uploaded via the dashboard (`/api/bot/upload`) and hot-reloaded by
`core/bot_loader.py` using `importlib` whenever the file's mtime changes —
no restart required.

---

## Configuration

All configuration lives in `config/settings.toml`. Key sections:

```toml
[dolphin]
path = "/Applications/Slippi Dolphin.app/Contents/MacOS/Slippi Dolphin"
iso  = "/Users/felixscherz/workspaces/personal/smash-tournament/assets/melee.iso"
port = 51441   # ENet port libmelee uses to talk to Dolphin

[domains]
frontend = "smash.felixscherz.me"
stream   = "stream-smash.felixscherz.me"

[streaming]
mode          = "local"        # "local" = ws://localhost:3333, "production" = wss://stream domain
webrtc_signal = "ws://localhost:3333/app/stream"
```

Switch to `mode = "production"` when the frp tunnel + nginx are active.
The FastAPI server derives the OvenPlayer URL from this at startup.

---

## OvenMediaEngine (WebRTC Streaming)

Container is named `ome`. Start/stop:

```bash
docker start ome
docker stop ome
docker logs ome   # check status
```

If you ever need to recreate the container (e.g. after `docker rm ome`), use
**exactly** this command — all flags are required for correct local operation:

```bash
docker run -d --name ome \
  -p 1935:1935 \
  -p 3333:3333 \
  -p 3478:3478 \
  -p 10000-10009:10000-10009/udp \
  -e OME_HOST_IP=127.0.0.1 \
  airensoft/ovenmediaengine:latest
```

After creating the container, push the tuned Server.xml into it:

```bash
docker cp config/ome-Server.xml ome:/opt/ovenmediaengine/bin/origin_conf/Server.xml
docker restart ome
```

**Why these flags matter:**

| Flag | Reason |
|---|---|
| `-e OME_HOST_IP=127.0.0.1` | Without this, OME advertises its Docker-internal IP in ICE candidates. The browser can't reach that IP on macOS — WebRTC fails with error 5111. |
| `-p 3478:3478` | TCP relay port. Required because OME default config has `TcpForce=true`. Missing this port = ICE timeout. |
| `config/ome-Server.xml` | Sets `TcpForce=false` so WebRTC uses direct UDP (lower latency). Must be applied after container creation. |

**Stream URLs:**
- OBS → OME RTMP ingest: `rtmp://localhost:1935/app/stream`
- OvenPlayer (local): `ws://localhost:3333/app/stream`
- OvenPlayer (production, via tunnel): `wss://stream-smash.felixscherz.me/app/stream`

The SSL cert error in `docker logs ome` is harmless locally — port 3333 runs
plain WS without TLS.

---

## OBS Studio Settings (working configuration)

OBS is already configured. If it ever needs to be set up again:

**Settings → Stream:**
- Service: `Custom`
- Server: `rtmp://localhost:1935/app`
- Stream Key: `stream`

**Settings → Output → Mode: Advanced → Streaming tab:**
- Encoder: `Apple VT H264 Hardware Encoder` (preferred on Apple Silicon)
- Rate Control: `CBR`
- Bitrate: `6000 Kbps`
- Keyframe Interval: `1` second
- Profile: `Baseline`
- **Use B-Frames: unchecked** ← critical. B-frames cause WebRTC stuttering.

**Settings → Video:**
- Base Resolution: `1920x1080` (or match Dolphin window)
- Output Resolution: `1280x720`
- Common FPS Values: **`60`** ← must match Melee's 60fps or every other frame drops

**Source:** Window Capture → `Slippi Dolphin`

---

## OvenPlayer Configuration (low-latency)

Use this config in the dashboard or any test page for lowest latency:

```js
OvenPlayer.create('player_id', {
  sources: [{ label: 'WebRTC', type: 'webrtc', file: 'ws://localhost:3333/app/stream' }],
  autoStart: true,
  mute: false,
  webrtcConfig: {
    timeoutMaxRetry: 4,
    connectionTimeout: 10000,
    playoutDelayHint: 0,   // request zero playout delay from browser
  },
});
```

---

## frp Tunnel (public internet exposure)

`frpc` is installed locally. Config at `config/frpc.toml`.

Before using, set:
- `serverAddr` = Hetzner VM public IP
- `auth.token` = shared secret (must match `frps.toml` on the VM)

Run on Mac:
```bash
frpc -c config/frpc.toml
```

The VM needs `frps` installed and `config/frps.toml` copied to it.
Deploy script: `./config/deploy-nginx.sh user@hetzner-ip`

Ports tunnelled:
- 80 → FastAPI (8080)
- 3333 TCP → OME WebRTC signaling
- 10000-10009 UDP → OME WebRTC media (must also be open in Hetzner firewall)

---

## Web Dashboard

```bash
source .venv/bin/activate
uvicorn frontend.app:app --host 0.0.0.0 --port 8080
```

Routes:
- `GET /` — dashboard with OvenPlayer embed
- `POST /api/bot/upload` — upload a `.py` bot file
- `POST /api/bot/deactivate` — fall back to LLM
- `POST /api/prompt` — send a text prompt to the LLM
- `WS /ws/gamestate` — 10Hz game state push (stocks, percent, action, position)

The orchestrator and web server are currently separate processes.
`frontend/app.py` has a `_orchestrator` global that must be injected at startup
to enable the WebSocket feed and bot API. This wiring is not yet done —
when connecting them, import `MeleeOrchestrator` in `frontend/app.py` and
assign the instance to `frontend.app._orchestrator` before `uvicorn` starts.

---

## What Is Not Yet Done

- [ ] frp tunnel not configured (needs Hetzner IP + auth token)
- [ ] nginx configs not deployed to Hetzner VM
- [ ] Orchestrator and FastAPI server not yet wired together into a single launcher
- [ ] Ollama / Llama3 not installed (LLM decisions will always fall back to None)
- [ ] No test suite
