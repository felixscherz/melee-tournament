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
            [frpc tunnel on Mac]
                    │
            [frps on Hetzner VM]
                    │
           [Public Internet / Team]
```

---

## Prerequisites

### 1. Homebrew Dependencies

```bash
brew install python@3.12 git frp
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
docker run -d --name ome \
  -p 1935:1935 \
  -p 3333:3333 \
  -p 3478:3478 \
  -p 10000-10009:10000-10009/udp \
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
rtmp_ingest = "rtmp://localhost:1935/app/stream"
webrtc_signal = "wss://YOUR_PUBLIC_IP:3333/app/stream"
```

### `config/frpc.toml` (runs on your Mac)

See `config/frpc.toml` — update `serverAddr` to your Hetzner VM IP.

### `config/frps.toml` (runs on your Hetzner VM)

See `config/frps.toml` — copy this file to your VM and run `frps -c frps.toml`.

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

### Step 3 — Start the frp tunnel

On your Mac:
```bash
frpc -c config/frpc.toml
```

On your Hetzner VM:
```bash
frps -c frps.toml
```

### Step 4 — Start the game orchestrator + web server

```bash
source .venv/bin/activate
python core/melee_orchestrator.py &
uvicorn frontend.app:app --host 0.0.0.0 --port 8080
```

### Step 5 — Share with your team

Give your team the URL: `http://YOUR_HETZNER_IP/`

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
│   ├── frpc.toml               # frp client (Mac)
│   └── frps.toml               # frp server (Hetzner)
├── docs/
│   └── setup.md                # Extended setup guide
├── uploads/                    # User-uploaded bot scripts
├── assets/                     # Place melee.iso here
└── README.md
```
