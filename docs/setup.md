# Extended Setup Guide

## 1. Dolphin (Slippi Build) — macOS

`libmelee` requires the **Slippi** fork of Dolphin, not the mainline build.

1. Go to https://slippi.gg/downloads and download **Slippi Dolphin** for macOS.
2. Move `Slippi Dolphin.app` to `/Applications/`.
3. First launch: right-click → Open (to bypass Gatekeeper).
4. In Dolphin: Options → Configuration → GameCube → Port 1 → set to **Standard Controller**.
5. Enable Dual Core and Idle Skipping OFF for deterministic frame timing.

## 2. Melee ISO

You must legally dump the ISO from your own disc.

- Wii + CleanRip: https://wiki.dolphin-emu.org/index.php?title=Ripping_Games
- Required version: **NTSC v1.02** (GALE01 r2)
- Place the ISO at: `assets/melee.iso`

## 3. Ollama (Local LLM)

```bash
brew install ollama
ollama serve &          # starts the local API server on port 11434
ollama pull llama3      # download the Llama 3 8B model (~5GB)
```

Verify: `curl http://localhost:11434/api/tags`

## 4. OvenMediaEngine via Docker

```bash
docker run -d --name ome \
  -p 1935:1935 \
  -p 3333:3333 \
  -p 10000-10009:10000-10009/udp \
  airensoft/ovenmediaengine:latest
```

Verify the ingest is ready: `docker logs ome | grep Listening`

## 5. OBS Studio → OvenMediaEngine

1. OBS → Settings → Stream → Custom RTMP
2. Server: `rtmp://localhost:1935/app/stream`
3. Add Window Capture source, select Dolphin window.
4. Start Streaming.

## 6. Hetzner VM Setup

```bash
# On your Hetzner VM (Ubuntu 22.04 recommended)
apt update && apt install -y frp
scp config/frps.toml user@YOUR_HETZNER_IP:/root/frps.toml

# Open firewall ports (Hetzner Cloud console → Firewall rules):
# TCP: 7000, 80, 3333, 1935
# UDP: 10000-10009

# Start frps
frps -c /root/frps.toml &
```

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `Failed to connect to Dolphin` | Ensure Slippi Dolphin is open and the ISO is running. Check port 51441 is free. |
| OvenPlayer shows no stream | Confirm OBS is streaming and OME container is running. Check WebRTC URL in settings.toml. |
| Bot file rejected | Must have a `Bot` class with an `act(gamestate, port)` method. Check server logs. |
| frp tunnel not connecting | Confirm `auth.token` matches in both frpc.toml and frps.toml. Check VM firewall port 7000. |
| LLM too slow / timing out | Use a smaller Ollama model (`ollama pull llama3:8b-instruct-q4_0`) or reduce prompt complexity. |
