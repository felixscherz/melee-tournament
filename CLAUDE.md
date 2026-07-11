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

[OBS Studio] ──RTMP──► [OvenMediaEngine (Podman, loopback-published)]
                              │
                    [stream_forwarder.py]  (0.0.0.0 → 127.0.0.1 shim; gvproxy can't
                              │              serve the VPN interface — see below)
                              │
                    [WireGuard wg0]  Mac 10.0.0.20 ↔ 10.0.0.1 Hetzner VM
                              │        (on-demand: ./stream-vpn.sh up/down)
                              │
                      [nginx TLS termination on VM]
                      smash.felixscherz.me        → 10.0.0.20:8080  (FastAPI)
                      stream-smash.felixscherz.me → 10.0.0.20:3355  (OME signaling)
                      UDP 10000-10004 ─DNAT+MASQ─► 10.0.0.20        (OME media)
```

Full migration rationale + the two critical networking details (NAT symmetry,
the forwarder shim) are in `VPN-MIGRATION.md`. Steady-state ops in `DEPLOYMENT.md`.

---

## Installed Software (all already present — do not reinstall)

| Tool | Location | Notes |
|---|---|---|
| Slippi Dolphin | `/Applications/Slippi Dolphin.app` | Intel binary, runs under Rosetta 2 |
| Rosetta 2 | System | Already installed |
| Melee ISO | `assets/melee.iso` | NTSC v1.02 (GALE01 r2) |
| Python venv | `.venv/` | Python 3.13, activate before running anything |
| OvenMediaEngine | Podman container `ome` | RTMP 1935; WebRTC 3355 (signaling) + 10000-10009/UDP (media) published on `127.0.0.1` |
| OBS Studio | `/Applications/OBS.app` | Configured for 60fps, 6000 Kbps, no B-frames |
| wireguard-tools | `$(brew --prefix)/bin/wg` | `wg`/`wg-quick`; tunnel config `config/wireguard/wg0-smash.conf` (gitignored) |
| Podman | System | docker-compatible CLI; uses gvproxy for port forwarding (does not serve the VPN interface) |

---

## How to Start Everything (working local stack)

```bash
# 1. Start OvenMediaEngine
./start-ome.sh

# 2. Start the unified server (FastAPI + orchestrator; launches Dolphin automatically)
source .venv/bin/activate
python main.py

# 3. Open OBS and click Start Streaming
#    (OBS is already configured — just hit the button)

# 4. Open the lobby, pick 4 characters, start a match
open http://localhost:8080/lobby
```

---

## How to Run the Game Loop

Always activate the venv first:

```bash
source .venv/bin/activate
```

Run the unified launcher (starts FastAPI on :8080 and launches Dolphin once,
keeping it alive across matches so OBS captures a stable window):

```bash
python main.py
```

Dolphin boots the ISO and idles at the menus. Matches are queued via the
lobby UI or the API — always exactly 4 players:

```bash
curl -X POST http://localhost:8080/api/start -H 'Content-Type: application/json' -d '{
  "players": [
    {"port": 1, "name": "A", "character": "FOX"},
    {"port": 2, "name": "B", "character": "MARTH"},
    {"port": 3, "name": "C", "character": "FALCON"},
    {"port": 4, "name": "D", "character": "FALCO"}
  ]}'
```

The game loop picks the match up at CSS, locks in all four characters,
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
the game never starts. The orchestrator connects all 4 controllers at launch,
which is why `/api/start` requires exactly 4 players. Call `choose_character`
for every connected port every frame during
`menu_state == melee.Menu.CHARACTER_SELECT`.

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

Bots currently live at fixed paths in `core/bots/` (one per character) and
are hot-reloaded by `core/bot_loader.py` using `importlib` whenever the
file's mtime changes — no restart required. The `/api/bot/upload` route from
the target architecture is not implemented yet; edit the files in
`core/bots/` directly.

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
mode          = "local"        # "local" = ws://localhost:3355, "production" = wss://stream domain
webrtc_signal = "ws://localhost:3355/app/stream"
```

Switch to `mode = "production"` when the WireGuard tunnel + nginx are active
(`./stream-vpn.sh up`). The FastAPI server derives the OvenPlayer URL from this
at startup.

---

## OvenMediaEngine (WebRTC Streaming)

Container is named `ome`. Use the script to start it:

```bash
./start-ome.sh   # creates the container if needed, starts it, waits for ready
docker stop ome  # stop
docker logs ome  # check status
```

`start-ome.sh` handles both cases: if the container already exists it runs
`docker start ome`; if not it runs `docker run` with all required flags and
the bind-mounted config. No manual `docker cp` or `docker restart` needed.

The full `docker run` invocation (for reference):

```bash
docker run -d --name ome \
  -p 1935:1935 \
  -p 127.0.0.1:3355:3333 \
  -p 127.0.0.1:10000-10009:10000-10009/udp \
  -e OME_HOST_IP=127.0.0.1 \
  -v "$(pwd)/config/ome-Server.xml:/opt/ovenmediaengine/bin/origin_conf/Server.xml:ro" \
  airensoft/ovenmediaengine:latest
```

(`start-ome.sh` sets `OME_HOST_IP` to the public IP `78.46.220.137` in
`mode = "production"` so ICE candidates advertise a reachable address.)

**Why these flags matter:**

| Flag | Reason |
|---|---|
| `-e OME_HOST_IP=…` | Without this, OME advertises its container-internal IP in ICE candidates, unreachable from the browser (WebRTC error 5111). Local = `127.0.0.1`; production = the public IP. |
| `-p 127.0.0.1:3355:3333` | Signaling on host port `3355` (sidesteps the Obsidian `3333` collision), bound to **loopback** so `stream_forwarder.py` can own the VPN IP without a bind conflict. |
| `-p 127.0.0.1:10000-10009:…/udp` | WebRTC media, loopback-bound for the same reason. Production traffic arrives via the forwarder shim, not directly (gvproxy can't serve the VPN). |
| `config/ome-Server.xml` | Sets `TcpForce=false` so WebRTC uses direct UDP (lower latency, no TCP relay). |

**Stream URLs:**
- OBS → OME RTMP ingest: `rtmp://localhost:1935/app/stream`
- OvenPlayer (local): `ws://localhost:3355/app/stream`
- OvenPlayer (production, via tunnel): `wss://stream-smash.felixscherz.me/app/stream`

The SSL cert error in `docker logs ome` is harmless locally — port 3355 runs
plain WS without TLS (nginx on the VM terminates TLS for production).

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
  sources: [{ label: 'WebRTC', type: 'webrtc', file: 'ws://localhost:3355/app/stream' }],
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

## WireGuard Tunnel (public internet exposure)

Production streaming runs over a **WireGuard** tunnel to the Hetzner VM (replaced
the old frp tunnel). The Mac joins the `10.0.0.0/24` VPN **on-demand**, only while
streaming. Full setup + rationale in `VPN-MIGRATION.md`; steady-state ops in
`DEPLOYMENT.md`.

```bash
./stream-vpn.sh up       # join VPN + start the OME forwarder shim
./stream-vpn.sh status   # handshake + forwarder health
./stream-vpn.sh down     # leave VPN + stop forwarders
```

- Mac tunnel config: `config/wireguard/wg0-smash.conf` (gitignored; private key
  stays on the Mac). Mac = `10.0.0.20`, VM = `10.0.0.1`.
- VM side (WG server peer + media DNAT/MASQ, nginx upstreams, TLS) is
  **Ansible-managed in the `home` repo** — deploy tags `vpn` and `proxy`.
- Public routing: `smash` → `10.0.0.20:8080` (FastAPI); `stream-smash` →
  `10.0.0.20:3355` (OME signaling, via the shim); UDP `10000-10004` DNAT+MASQ →
  `10.0.0.20` (OME media, via the shim).

**The forwarder shim (`stream_forwarder.py`) is mandatory:** Podman/Docker's
gvproxy does not serve the WireGuard interface, so the VM cannot reach OME's
loopback-published ports directly. The shim binds `0.0.0.0` (which *does* receive
tunnel traffic) and relays to `127.0.0.1`. `stream-vpn.sh` starts/stops it with
the tunnel. (Note: `socat` does **not** work here — its listening socket doesn't
receive utun traffic on macOS; a plain Python socket does. Don't "simplify" it
back to socat.)

---

## Scaling to many viewers — Twitch relay

The WebRTC path fans out **per viewer from the Mac**, so it's capped by the Mac's
home upload bandwidth (~N × bitrate). Fine for a handful of viewers; ~30 viewers
× 6 Mbps ≈ 180 Mbps is far beyond a home uplink. For a crowd, relay to Twitch's
CDN — OME pushes **one** copy and Twitch fans out:

```bash
echo "<twitch-stream-key>" > config/twitch.key   # gitignored
./twitch-push.sh start    # relay app/stream -> Twitch (bypass_video H264 + aac_audio)
./twitch-push.sh status
./twitch-push.sh stop
```

Mechanics: OME's REST API (`127.0.0.1:8081`, enabled in `ome-Server.xml`
`<Managers>`, token `OME_API_TOKEN`, default `smash-ome-api`) drives
push-publishing via `POST .../apps/app:startPush`. The push uses the `bypass_stream`
profile's H264 passthrough + AAC (no re-encode) — exactly what Twitch wants.

**The `app` application must have `<Push />` in its `<Publishers>`** (it's enabled
in `ome-Server.xml`). Without it, OME logs "Push publisher is disabled" at startup
and `startPush` fails with a misleading `Could not find application` 404. Same
applies to `<File />` if you ever want the record API. The
low-latency WebRTC path keeps working alongside it: WebRTC for small groups,
Twitch for crowds (Twitch latency ~2-5s vs sub-second WebRTC).

## Web Dashboard

Served by `python main.py` (do not run uvicorn separately — the orchestrator
would be missing). `main.py` injects the `MeleeOrchestrator` instance into
`frontend.app._orchestrator` and runs uvicorn in the same event loop.

Routes (`frontend/app.py`):
- `GET /lobby` — pick 4 players/characters and start a match (`/` redirects here)
- `GET /watch` — OvenPlayer stream embed + live scores
- `POST /api/start` — queue a match; body: `{"players": [{port, name, character} × 4]}`
- `POST /api/stop` — reset app state
- `GET /api/state` — phase, scores, winner
- `WS /ws/gamestate` — 10Hz game state push (stocks, percent, action)

Characters map to fixed bot files in `core/bots/` (fox.py, marth.py,
falcon.py, falco.py), hot-reloaded by `core/bot_loader.py` on mtime change.

---

## What Is Not Yet Done

- [x] ~~Public tunnel~~ — WireGuard tunnel + nginx (Ansible) live; frp retired
- [ ] Ollama / Llama3 not installed (LLM decisions will always fall back to None)
- [ ] No test suite
