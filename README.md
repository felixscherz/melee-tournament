# Smash Tournament — Self-Hosted Melee Bot Platform

Run AI/scripted bots against each other in Super Smash Bros. Melee, streamed live
to your team via Twitch. Players submit a Python `Bot` class or a natural-language
prompt from a web lobby; the platform drives Slippi Dolphin through `libmelee` and
pushes live scores to the browser.

It runs entirely on your own machine (developed on an Apple Silicon Mac). You can
keep it on `localhost`, expose it on your LAN, or put it on the public internet
behind your own reverse proxy.

---

## Architecture Overview

```
[Dolphin + libmelee] ──► [melee_orchestrator.py]  (async 60fps loop)
                               │
              ┌────────────────┴────────────────┐
              │                                 │
   [sandboxed bot subprocess]        [trusted default bots]
        per port (user code)          core/bots/<char>.py
              │                                 │
              └────────────────┬────────────────┘
                               │
                        [FastAPI backend  :8080]
                        /lobby  /watch  /api/*  /ws/gamestate
                               │
                    ┌──────────┴──────────┐
                    │                     │
              [WebSocket]           [REST API]

[OBS Studio] ──RTMP──► [Twitch CDN]  (direct; fans out to viewers)
```

Bots run in a per-port subprocess sandbox (rlimits, scrubbed env, 10ms per-frame
deadline). Details in `CLAUDE.md` and `docs/IMPROVE_BOT_ISOLATION.md`. The video stream
goes straight from OBS to Twitch's CDN — the dashboard never carries video.

---

## Quickstart (localhost)

This is the fastest path. Everything runs on your Mac; nothing is exposed publicly.

### 1. Prerequisites

| What | Why | Get it |
|---|---|---|
| **Slippi Dolphin** | `libmelee` needs the Slippi fork, not mainline Dolphin | https://slippi.gg/downloads — move `Slippi Dolphin.app` to `/Applications/` |
| **Melee ISO** (NTSC v1.02 / GALE01 r2) | The game itself. You must legally own it | Dump your own disc ([guide](https://wiki.dolphin-emu.org/index.php?title=Ripping_Games)). We cannot provide one. Place at `assets/melee.iso` |
| **Python 3.12+** | Runs the server + orchestrator | `brew install python@3.12` |
| **OBS Studio** | Captures Dolphin and streams to Twitch | `brew install --cask obs` |
| **opencode** *(optional)* | Prompt-to-bot generation | See [Prompt-to-bot generation](#prompt-to-bot-generation-optional). Skip it and bots still work via pasted code and defaults |
| **Ollama** *(optional)* | In-game LLM decisions (currently unused; always falls back) | Skip it |

### 2. Install

```bash
git clone <this-repo> smash-tournament
cd smash-tournament
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

All settings live in **one file**: `config/settings.toml`. Create yours from the
template (the template is committed; your copy is gitignored so your values never
get committed):

```bash
cp config/settings.example.toml config/settings.toml
```

The defaults work for a local setup out of the box. Edit anything you like — see
the [Configuration reference](#configuration-reference) below. For a pure
localhost run you only really need to set `[streaming] twitch_channel` if you want
the embedded stream to show up.

> If `config/settings.toml` is missing, the app falls back to
> `config/settings.example.toml` and logs a warning, so a fresh clone still boots.

### 4. Run

```bash
source .venv/bin/activate
python main.py          # starts FastAPI on :8080 AND launches Dolphin
```

`main.py` is the single entry point — it runs the web server and the game
orchestrator in one event loop and launches Dolphin for you. **Do not run uvicorn
separately.**

Then open the lobby, pick 4 characters, and start a match:

```
http://localhost:8080/lobby
```

Watch page (stream embed + live scores): `http://localhost:8080/watch`.

To stop: `Ctrl+C`. Dolphin closes with the Python process.

---

## Configuration reference

Everything is in `config/settings.toml`. Full annotated template:
`config/settings.example.toml`.

| Section / key | What it does | Default |
|---|---|---|
| `[dolphin] path` | Path to the Slippi Dolphin **binary** (inside the `.app` bundle) | `/Applications/Slippi Dolphin.app/Contents/MacOS/Slippi Dolphin` |
| `[dolphin] iso` | Path to your Melee ISO (relative paths resolve from repo root) | `assets/melee.iso` |
| `[dolphin] port` | ENet port libmelee uses to talk to Dolphin | `51441` |
| `[server] host` | Bind address. `0.0.0.0` = LAN-visible, `127.0.0.1` = local only | `0.0.0.0` |
| `[server] port` | Dashboard port | `8080` |
| `[domains] frontend` | Public hostname serving the dashboard, if you expose it. Only used to whitelist the host for the Twitch embed. Leave empty for local-only | `""` |
| `[streaming] twitch_channel` | Your Twitch channel name (the part after `twitch.tv/`). Embeds the live stream. **Not** your stream key | `""` |
| `[opencode] model` | Model the bot-writer agent uses for prompt-to-bot. Empty = use the default in the agent file | `opencode/deepseek-v4-flash-free` |
| `[ollama] model` / `base_url` | Optional in-game LLM (currently unused) | `llama3` / `localhost:11434` |
| `[bots] deadline_ms` | Per-frame bot deadline before neutral input is applied | `10` |
| `[bots] max_misses` | Consecutive misses before a bot is killed and the default takes over | `3` |
| `[bots] scratch_dir` | cwd the sandboxed bot child runs in | `.bot_scratch` |

Two different Twitch values, don't mix them up:
- **`twitch_channel`** (this file) — public, embeds the player on `/watch` and `/lobby`.
- **Twitch stream key** — a secret you paste into **OBS**, never in this repo. See below.

---

## Streaming to Twitch (OBS)

OBS pushes video directly to Twitch's CDN (no local relay).

1. OBS → **Settings → Stream**: Service `Custom`, Server `rtmp://live.twitch.tv/app`,
   Stream Key = *your Twitch stream key* (Twitch dashboard → Settings → Stream).
2. OBS → **Settings → Output** (Advanced): `Apple VT H264 Hardware Encoder`, CBR,
   6000 Kbps, keyframe interval `1s`, Profile `Baseline`, **B-Frames unchecked**.
3. OBS → **Settings → Video**: Output `1280x720`, **60 FPS** (must match Melee).
4. Add a **Window Capture** source → `Slippi Dolphin`.
5. Click **Start Streaming**.

Set `[streaming] twitch_channel` in `settings.toml` to your channel so the embed
appears on the dashboard.

---

## Bot Development

Bots implement a single method. See `core/bot_template.py` for a working example.

```python
class Bot:
    def act(self, gamestate, player_port: int) -> dict | None:
        return {
            "stick_x": 0.5, "stick_y": 0.5,   # 0.0–1.0, 0.5 = neutral
            "buttons": {"BUTTON_A": False, "BUTTON_B": False, ...},
        }
```

Three ways to control a player, chosen in the lobby (priority:
pasted code > generated > default):

1. **Default AI** — leave both boxes blank; uses `core/bots/<char>.py`.
2. **Custom code** — paste a `Bot` class into the code box.
3. **Prompt** — type a natural-language prompt and click GENERATE (needs opencode).

Test a bot without Dolphin:

```bash
.venv/bin/python core/test_bot.py <path_to_bot.py>
```

Bots run in a per-port subprocess sandbox and hot-reload on file change — no
restart needed. Full model in `CLAUDE.md` → "How bots actually run".

### Prompt-to-bot generation (optional)

The GENERATE button shells out to the [`opencode`](https://opencode.ai) CLI with a
`bot-writer` agent (`.opencode/agents/bot-writer.md`). To enable it:

1. Install opencode (see its docs) and make sure `opencode` is on your `PATH`.
2. Authenticate a provider so opencode can reach a model (`opencode auth`).
3. Set `[opencode] model` in `settings.toml` to a model your install can reach
   (list them with `opencode models`). Leave it empty to use the agent's default.

If opencode isn't installed, the other two control methods still work.

---

## Going public (optional)

By default the dashboard is local/LAN only. To share it with a remote team you
need to put `http://<your-mac>:8080` behind a reverse proxy with TLS. **Bring your
own** — any of these work:

- A tunnel service (Cloudflare Tunnel, Tailscale Funnel, ngrok, …).
- A VPS running nginx/Caddy with TLS, reaching your Mac over a VPN
  (WireGuard/Tailscale).

Whatever you use, set `[domains] frontend` to the public hostname so the Twitch
embed is whitelisted for that host.

`docs/DEPLOYMENT.md` documents **one worked example** — the
author's on-demand WireGuard tunnel to a Hetzner VM with nginx (`stream-vpn.sh` +
an Ansible-managed VM). Treat it as a reference, not a required path; the VM side
lives in a separate private repo.

---

## Directory Structure

```
smash-tournament/
├── main.py                     # single entry point (server + orchestrator + Dolphin)
├── core/
│   ├── config.py               # loads settings.toml (falls back to the example)
│   ├── melee_orchestrator.py   # async 60fps game loop
│   ├── bot_process.py          # per-port subprocess sandbox (security boundary)
│   ├── bot_worker.py           # sandboxed worker entry point
│   ├── bot_generator.py        # prompt-to-bot via opencode
│   ├── bot_loader.py           # trusted in-process fallback bots
│   ├── bot_template.py         # example bot
│   ├── test_bot.py             # offline bot test harness
│   └── bots/                   # per-character default bots (fox, marth, …)
├── frontend/
│   ├── app.py                  # FastAPI routes
│   ├── static/ · templates/    # lobby / watch UI
├── config/
│   ├── settings.example.toml   # committed template — copy this
│   ├── settings.toml           # your copy (gitignored)
│   └── wireguard/              # optional VPN config (gitignored)
├── generated/                  # prompt-generated bots (gitignored)
├── uploads/                    # pasted bot scripts (gitignored)
├── assets/                     # place melee.iso here (gitignored)
├── stream-vpn.sh               # example public-exposure helper (author's setup)
└── CLAUDE.md                   # deep architecture / agent playbook
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Failed to connect to Dolphin` | Confirm the `[dolphin] path` is the binary inside the bundle and the ISO path is valid. Check port `51441` is free. |
| `Port 8080 is already in use` | `lsof -ti :8080 \| xargs kill -9`, or change `[server] port`. |
| Twitch player is blank | Set `[streaming] twitch_channel`, confirm OBS is actually streaming, and (if public) that `[domains] frontend` matches the host serving the page. |
| Bot file rejected | Needs a `Bot` class with `act(gamestate, port)`. Check server logs / run `core/test_bot.py`. |
| GENERATE fails | opencode not installed or not authenticated, or `[opencode] model` unreachable. See [Prompt-to-bot generation](#prompt-to-bot-generation-optional). |
| `[mvk-*]` log spam | Normal Vulkan noise from the Intel-on-ARM Rosetta path, not an error. `grep -v mvk` to hide. |

More detail lives in `docs/setup.md` (extended setup) and `CLAUDE.md`
(architecture and hard-won game-loop rules).
