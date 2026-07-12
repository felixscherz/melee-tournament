# Extended Setup Guide

This supplements the [README](../README.md) quickstart with per-component detail.
All runtime config lives in `config/settings.toml` (copy it from
`config/settings.example.toml`); see the README's Configuration reference.

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

## 3. opencode (Prompt-to-Bot) — optional

The lobby's GENERATE button turns a natural-language prompt into a bot by shelling
out to the [`opencode`](https://opencode.ai) CLI with the `bot-writer` agent. Skip
this if you only use pasted code and default bots.

1. Install opencode per its docs and confirm it's on your `PATH`: `which opencode`.
2. Authenticate a provider so opencode can reach a model: `opencode auth`.
3. Pick a model and set it in `config/settings.toml`:
   ```toml
   [opencode]
   model = "opencode/deepseek-v4-flash-free"   # or any id from `opencode models`
   ```
   Leave `model = ""` to use the default declared in
   `.opencode/agents/bot-writer.md`. The value in `settings.toml` is passed to
   `opencode run --model ...`, so it's the single source of truth.

## 4. Ollama (Local LLM) — optional, currently unused

In-game LLM decisions are not wired up (they always fall back), so you can skip
this entirely. If you want to experiment:

```bash
brew install ollama
ollama serve &          # starts the local API server on port 11434
ollama pull llama3      # download the Llama 3 8B model (~5GB)
```

Verify: `curl http://localhost:11434/api/tags`

## 5. OBS Studio -> Twitch

1. OBS -> Settings -> Stream -> Custom RTMP
2. Server: `rtmp://live.twitch.tv/app`
3. Stream key: your Twitch stream key
4. Add Window Capture source, select Dolphin window.
5. Start Streaming.

OBS pushes directly to Twitch's CDN. No local relay (OME) is involved.

## 6. Going public — one example (WireGuard + Hetzner)

**Optional and setup-specific.** The dashboard is local/LAN only by default; to
expose it remotely, put `http://<your-mac>:8080` behind any reverse proxy with TLS
(Cloudflare Tunnel, Tailscale Funnel, a VPS with nginx/Caddy over a VPN, …). Set
`[domains] frontend` to the public hostname so the Twitch embed is whitelisted.

Below is the **author's** worked example: an on-demand WireGuard tunnel to a
Hetzner VM whose WireGuard server, nginx, and TLS are provisioned from a separate
private `home` Ansible repo (not this repo). See `DEPLOYMENT.md` for the tags
(`vpn`, `proxy`) and full Mac-side WireGuard setup. On the Mac:

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

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `Failed to connect to Dolphin` | Ensure Slippi Dolphin is open and the ISO is running. Check port 51441 is free. |
| Twitch stream not loading | Confirm OBS is streaming. Check the `twitch_channel` in settings.toml matches your channel. |
| Bot file rejected | Must have a `Bot` class with an `act(gamestate, port)` method. Check server logs. |
| VPN won't connect | `./stream-vpn.sh status` - check for a recent handshake; confirm the VM knows the Mac peer (Ansible `vpn` tag) and Hetzner firewall UDP 51820. |
| LLM too slow / timing out | Use a smaller Ollama model (`ollama pull llama3:8b-instruct-q4_0`) or reduce prompt complexity. |