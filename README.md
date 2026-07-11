# Smash Tournament — Self-Hosted Melee Bot Platform

A self-hosted platform for running AI/scripted bots against each other in Super Smash Bros. Melee, streamed live to your team via Twitch.

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

[OBS Studio] ──RTMP──► [Twitch CDN]  (direct; fans out to viewers)

        [WireGuard tunnel (on-demand)]
                    │
          [nginx + TLS on Hetzner VM]
                    │
            [Public Internet / Team]
```

> The WireGuard tunnel only exposes the FastAPI dashboard (smash.felixscherz.me).
> The video stream goes directly from OBS to Twitch's CDN. See `DEPLOYMENT.md`
> for steady-state ops and `VPN-MIGRATION.md` for the tunnel design.

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

### 3. Melee ISO

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
# OBS streams directly to Twitch (no WebRTC/OME relay).
twitch_channel = "your-twitch-channel"
```

### `config/wireguard/wg0-smash.conf` (runs on your Mac, gitignored)

The on-demand WireGuard tunnel config (exposes the dashboard publicly). The
private key stays on the Mac and is never committed. Bring the tunnel up/down
with `./stream-vpn.sh up|down`. See `DEPLOYMENT.md` (Mac-side setup) and
`VPN-MIGRATION.md` (Phase 1).

### Hetzner VM (Ansible-managed)

The VM's WireGuard server, nginx upstreams, and TLS live in the separate `home`
Ansible repo (deploy tags `vpn` and `proxy`) — not in this repo.

---

## Running the Platform

### Step 1 — Start OBS Studio

1. Open OBS, go to Settings → Stream.
2. Set service to **Custom**, server to `rtmp://live.twitch.tv/app`.
3. Enter your Twitch stream key.
4. Add a Window Capture source pointed at Dolphin.
5. Click **Start Streaming**.

### Step 2 — Bring up the WireGuard tunnel (optional, for public dashboard)

On your Mac (the VM side is already provisioned via the `home` Ansible repo):
```bash
./stream-vpn.sh up      # joins the VPN (dashboard goes public)
```

### Step 3 — Start the game orchestrator + web server

```bash
source .venv/bin/activate
python main.py          # FastAPI on :8080 + launches Dolphin (do not run uvicorn separately)
```

### Step 4 — Share with your team

Give your team the URL: `https://smash.felixscherz.me/` (served while the tunnel
is up). Run `./stream-vpn.sh down` when you're done.

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
│       └── index.html          # Twitch embed dashboard (legacy)
├── config/
│   ├── settings.toml           # Main config
│   └── wireguard/              # wg0-smash.conf + keys (gitignored)
├── stream-vpn.sh               # bring the VPN up/down (dashboard exposure)
├── docs/
│   └── setup.md                # Extended setup guide
├── uploads/                    # User-uploaded bot scripts
├── assets/                     # Place melee.iso here
└── README.md
```
