# Extended Setup Guide

## 1. Dolphin (Slippi Build) - macOS

`libmelee` requires the **Slippi** fork of Dolphin, not the mainline build.

1. Go to https://slippi.gg/downloads and download **Slippi Dolphin** for macOS.
2. Move `Slippi Dolphin.app` to `/Applications/`.
3. First launch: right-click -> Open (to bypass Gatekeeper).
4. In Dolphin: Options -> Configuration -> GameCube -> Port 1 -> set to **Standard Controller**.
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

## 4. OBS Studio -> Twitch

1. OBS -> Settings -> Stream -> Custom RTMP
2. Server: `rtmp://live.twitch.tv/app`
3. Stream key: your Twitch stream key
4. Add Window Capture source, select Dolphin window.
5. Start Streaming.

OBS pushes directly to Twitch's CDN. No local relay (OME) is involved.

## 5. Hetzner VM Setup (WireGuard)

The VM's WireGuard server, nginx, and TLS are provisioned from the `home` Ansible
repo (not this repo). See `DEPLOYMENT.md` for the tags (`vpn`, `proxy`) and the
Mac-side WireGuard setup. On the Mac:

```bash
brew install wireguard-tools
./stream-vpn.sh up      # joins the VPN (dashboard goes public)
./stream-vpn.sh down    # leaves the VPN when done
```

Open firewall ports (Hetzner Cloud console -> Firewall rules):
```
# TCP: 22, 80, 443
# UDP: 51820 (WireGuard)
```

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `Failed to connect to Dolphin` | Ensure Slippi Dolphin is open and the ISO is running. Check port 51441 is free. |
| Twitch stream not loading | Confirm OBS is streaming. Check the `twitch_channel` in settings.toml matches your channel. |
| Bot file rejected | Must have a `Bot` class with an `act(gamestate, port)` method. Check server logs. |
| VPN won't connect | `./stream-vpn.sh status` - check for a recent handshake; confirm the VM knows the Mac peer (Ansible `vpn` tag) and Hetzner firewall UDP 51820. |
| LLM too slow / timing out | Use a smaller Ollama model (`ollama pull llama3:8b-instruct-q4_0`) or reduce prompt complexity. |