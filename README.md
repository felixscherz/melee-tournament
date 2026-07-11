# Smash Tournament — Self-Hosted Melee Bot Platform

A self-hosted platform for running AI/scripted bots against each other in Super Smash Bros. Melee, streamed live to your team via WebRTC.

## Architecture Overview

```
[Dolphin + libmelee] ──► [melee_orchestrator.py]
                               │
              ┌────────────────┴────────────────┐
              │                                 │
        [Ollama/LLM]                  [Dynamic Script Loader]
              │                                 │
              └────────────────┬────────────────┘
                               │
                        [FastAPI backend]
                               │
                    ┌──────────┴──────────┐
                    │                     │
              [WebSocket]           [REST API]
                    │
              [OvenMediaEngine] ◄── [OBS Studio via RTMP]
                    │
              [WebRTC / OvenPlayer]
                    │
         [stream_forwarder.py shim]
                    │
        [WireGuard tunnel (on-demand)]
                    │
          [nginx + TLS on Hetzner VM]
                    │
           [Public Internet / Team]
```

> Production streaming runs over WireGuard (native UDP). See `DEPLOYMENT.md` for
> steady-state ops and `VPN-MIGRATION.md` for the design + networking gotchas.

---

## Prerequisites

### 1. Homebrew Dependencies

```bash
brew install python@3.12 git wireguard-tools
brew install --cask dolphin-emu obs
```

### 2. Python Dependencies

```bash
cd /path/to/smash-tournament
python3 -m venv .venv
source .venv/bin/activate
pip install libmelee fastapi uvicorn websockets toml ollama aiohttp aiofiles python-multipart
```

### 3. Docker (for OvenMediaEngine)

Install Docker Desktop for Mac from https://www.docker.com/products/docker-desktop/

Then start OvenMediaEngine:

```bash
./start-ome.sh   # preferred — sets OME_HOST_IP + loopback ports for you
```

Equivalent `docker run` (WebRTC ports bound to loopback for the forwarder shim):

```bash
docker run -d --name ome \
  -p 1935:1935 \
  -p 127.0.0.1:3355:3333 \
  -p 127.0.0.1:10000-10009:10000-10009/udp \
  -e OME_HOST_IP=127.0.0.1 \
  -v "$(pwd)/config/ome-Server.xml:/opt/ovenmediaengine/bin/origin_conf/Server.xml:ro" \
  airensoft/ovenmediaengine:latest
```

### 4. Melee ISO

You must legally own Super Smash Bros. Melee (NTSC v1.02). Place your ISO at:

```
~/smash-tournament/assets/melee.iso
```

> We cannot provide or link to ISOs. Dump your own disc using a Wii and CleanRip.

### 5. Dolphin Slippi Build

Download the Slippi Dolphin build (required for `libmelee` compatibility) from:
https://slippi.gg/downloads

Move `Dolphin.app` to `/Applications/` or note its path for `config/settings.toml`.

---

## Configuration

### `config/settings.toml`

Edit this file before running:

```toml
[dolphin]
path = "/Applications/Slippi Dolphin.app/Contents/MacOS/Slippi Dolphin"
iso = "/Users/you/smash-tournament/assets/melee.iso"
port = 51441

[ollama]
model = "llama3"
base_url = "http://localhost:11434"

[server]
host = "0.0.0.0"
port = 8080

[streaming]
mode = "production"    # "local" = ws://localhost:3355, "production" = wss://stream domain
rtmp_ingest = "rtmp://localhost:1935/app/stream"
webrtc_signal = "ws://localhost:3355/app/stream"
```

### `config/wireguard/wg0-smash.conf` (runs on your Mac, gitignored)

The on-demand WireGuard tunnel config. The private key stays on the Mac and is
never committed. Bring the tunnel + forwarder shim up/down with `./stream-vpn.sh
up|down`. See `DEPLOYMENT.md` (Mac-side setup) and `VPN-MIGRATION.md` (Phase 1).

### Hetzner VM (Ansible-managed)

The VM's WireGuard server, nginx upstreams, and TLS live in the separate `home`
Ansible repo (deploy tags `vpn` and `proxy`) — not in this repo.

---

## Running the Platform

### Step 1 — Start OvenMediaEngine

```bash
docker start ome
```

### Step 2 — Start OBS Studio

1. Open OBS, go to Settings → Stream.
2. Set service to **Custom**, server to `rtmp://localhost:1935/app/stream`.
3. Add a Window Capture source pointed at Dolphin.
4. Click **Start Streaming**.

### Step 3 — Bring up the WireGuard tunnel + forwarder shim

On your Mac (the VM side is already provisioned via the `home` Ansible repo):
```bash
./stream-vpn.sh up      # joins the VPN + starts stream_forwarder.py
```

### Step 4 — Start the game orchestrator + web server

```bash
source .venv/bin/activate
python main.py          # FastAPI on :8080 + launches Dolphin (do not run uvicorn separately)
```

### Step 5 — Share with your team

Give your team the URL: `https://smash.felixscherz.me/` (served while the tunnel
is up). Run `./stream-vpn.sh down` when you're done streaming.

---

## Bot Development

See `core/bot_template.py` for the standardized interface every bot must implement.

Bots are uploaded via the dashboard and hot-reloaded without restarting the game loop.

---

## Directory Structure

```
smash-tournament/
├── core/
│   ├── melee_orchestrator.py   # Main async game loop
│   ├── bot_loader.py           # Dynamic script hot-reloader
│   ├── llm_client.py           # Ollama/LLM interface
│   └── bot_template.py         # Template for user bots
├── frontend/
│   ├── app.py                  # FastAPI server
│   ├── static/
│   │   └── dashboard.js
│   └── templates/
│       └── index.html          # OvenPlayer dashboard
├── config/
│   ├── settings.toml           # Main config
│   ├── ome-Server.xml          # OvenMediaEngine config (TcpForce=false)
│   └── wireguard/              # wg0-smash.conf + keys (gitignored)
├── stream-vpn.sh               # bring the VPN + forwarder shim up/down
├── stream_forwarder.py         # native 0.0.0.0→loopback shim (gvproxy can't serve the VPN)
├── start-ome.sh                # start/recreate the OME container
├── docs/
│   └── setup.md                # Extended setup guide
├── uploads/                    # User-uploaded bot scripts
├── assets/                     # Place melee.iso here
└── README.md
```
