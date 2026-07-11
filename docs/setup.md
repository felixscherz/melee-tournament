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

Prefer `./start-ome.sh` — it picks `OME_HOST_IP` from the streaming mode and
publishes the WebRTC ports on loopback (for the forwarder shim). The equivalent
`docker run` is:

```bash
docker run -d --name ome \
  -p 1935:1935 \
  -p 127.0.0.1:3355:3333 \
  -p 127.0.0.1:10000-10009:10000-10009/udp \
  -e OME_HOST_IP=127.0.0.1 \
  -v "$(pwd)/config/ome-Server.xml:/opt/ovenmediaengine/bin/origin_conf/Server.xml:ro" \
  airensoft/ovenmediaengine:latest
```

WebRTC signaling (`3355`) and media (`10000-10009/udp`) bind loopback because
production traffic reaches OME via `stream_forwarder.py` over the VPN, not
directly (Podman/Docker gvproxy can't serve the WireGuard interface — see
`VPN-MIGRATION.md`). Verify the ingest is ready: `docker logs ome | grep Listening`

## 5. OBS Studio → OvenMediaEngine

1. OBS → Settings → Stream → Custom RTMP
2. Server: `rtmp://localhost:1935/app/stream`
3. Add Window Capture source, select Dolphin window.
4. Start Streaming.

## 6. Hetzner VM Setup (WireGuard)

The VM's WireGuard server, nginx, and TLS are provisioned from the `home` Ansible
repo (not this repo). See `DEPLOYMENT.md` for the tags (`vpn`, `proxy`) and the
Mac-side WireGuard setup. On the Mac:

```bash
brew install wireguard-tools
./stream-vpn.sh up      # joins the VPN + starts the OME forwarder shim
./stream-vpn.sh down    # leaves the VPN when done streaming
```

Open firewall ports (Hetzner Cloud console → Firewall rules):
```
# TCP: 22, 80, 443
# UDP: 51820 (WireGuard), 10000-10004 (WebRTC media)
```

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `Failed to connect to Dolphin` | Ensure Slippi Dolphin is open and the ISO is running. Check port 51441 is free. |
| OvenPlayer shows no stream | Confirm OBS is streaming and OME container is running. Check WebRTC URL in settings.toml. |
| Bot file rejected | Must have a `Bot` class with an `act(gamestate, port)` method. Check server logs. |
| VPN won't connect | `./stream-vpn.sh status` — check for a recent handshake; confirm the VM knows the Mac peer (Ansible `vpn` tag) and Hetzner firewall UDP 51820. |
| Stream connects but no video / ICE fails | Confirm the forwarder shim is alive (`./stream-vpn.sh status`) and OME publishes `3355`/`10000-10004` on loopback. In `chrome://webrtc-internals` the pair should be a UDP `host` pair, not `relay`. |
| LLM too slow / timing out | Use a smaller Ollama model (`ollama pull llama3:8b-instruct-q4_0`) or reduce prompt complexity. |
