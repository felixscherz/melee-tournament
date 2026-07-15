---
name: frontend
description: How the Smash Tournament web frontend works - FastAPI server (frontend/app.py), Jinja2 templates, the team lobby, captain/nonce model, REST API, and the WebSocket feeds. Load this before adding a page, route, WebSocket field, or touching lobby/team/watch/admin UI.
---

## What the frontend is

A server-rendered dashboard for the team-based Melee bot tournament. No SPA, no
build step, no npm. It is **FastAPI + Jinja2**, and every page ships its own CSS
and JS inline inside the one template file. `frontend/static/` is mounted at
`/static` but is currently empty - do not expect a bundler or shared asset.

Stack:
- `frontend/app.py` - all routes (pages, REST, WebSockets) in one module.
- `frontend/templates/*.html` - one self-contained template per page.
- `core/teams.py` - `TeamRegistry` singleton: the lobby's source of truth.
- `core/game_state.py` - `AppState` singleton (`app_state`): live match phase +
  scores, written by the orchestrator, read by the frontend.

## How it is served

`frontend/app.py` defines `app = FastAPI(...)`. It is NOT run standalone.
`main.py` imports it, injects the orchestrator (`webapp._orchestrator = ...`),
and runs uvicorn in the same event loop so the 60fps game loop and the web
server share one loop. Always launch with `uv run main.py`, never
`uvicorn frontend.app:app` (the orchestrator would be missing and `/api/start`
would 503).

Config is read once at import via `core.config.load_settings()` into `config`.
`_twitch_context()` builds the `{twitch_channel, twitch_parents}` dict that
every page with a stream embed needs; reuse it, do not re-read config per route.

## Pages (HTML routes)

| Route | Template | Purpose |
|---|---|---|
| `GET /` | - | redirects to `/lobby` |
| `GET /lobby` | `lobby.html` | landing: active team cards + "add team" slots, ready bar, START/END/RESET, stream embed |
| `GET /team/{n}` | `team.html` | team workspace: captain claim, character, contributions, generate, code override, ready |
| `GET /watch` | `watch.html` | Twitch embed + live scoreboard (redirects to `/lobby` when IDLE) |
| `GET /admin` | `admin.html` | phase pill, active-team readiness, END MATCH / RESET TEAMS |

`{n}` is a team id / Dolphin port, validated as `n in (1,2,3,4)`. All 4 slots
always exist; whether a team is *active* is a separate flag (see Team model).

## REST API

All under `frontend/app.py`. JSON in, JSON out. Team-mutating routes are
captain-gated via `_require_captain(team, nonce)` (403/409 on mismatch).

Roster + match:
- `POST /api/start` - parameterless; builds `PlayerConfig` for every
  `teams.active_ids()`, requires `teams.all_ready()` (min 2 active, all ready),
  then `await _orchestrator.queue_match(configs)`.
- `POST /api/stop` - abort the running match.
- `POST /api/team/{n}/activate` / `deactivate` - add/remove a team from the
  roster. Guarded by `_require_lobby_open()` (IDLE/POSTGAME only). Min 2 / max 4.
- `POST /api/teams/reset` - clear captains/contributions/ready, keep the active
  set. Blocked while a match runs.
- `GET /api/state` - `{phase, scores, winner}` from `app_state.to_dict()`.

Team workspace (mostly captain-only):
- `GET /api/teams` - all-4-slot summary (each carries `active`).
- `GET /api/team/{n}?nonce=` - full team state, personalized (`you_are_captain`).
- `POST /api/team/{n}/captain` - claim/take over (`{nonce, nickname, force?}`).
- `POST /api/team/{n}/character | /name | /ready` - captain setters.
- `POST /api/team/{n}/contribute` (any teammate) / `DELETE .../contribution/{id}`.
- `POST /api/team/{n}/code` - captain code override; validated inline via
  `core.bot_validator.validate_bot_code`, returns `{ok, error?}`.
- `POST /api/team/{n}/prompt-preview` / `generate` - assemble contributions and
  (for generate) run the bot-writer agent under a per-team `asyncio.Lock`.
- `POST /api/validate` - static-check a pasted bot snippet.

`TeamError` from the registry is mapped to HTTP by `_team_error_to_http`
(e.g. `min_teams`/`max_teams` -> 409, `captain_exists` -> 409). Add new error
codes there when you add registry errors.

## WebSocket feeds (push-on-change)

Three feeds in `frontend/app.py`, all send JSON text:

| Feed | Who listens | Payload | Trigger |
|---|---|---|---|
| `WS /ws/teams` | lobby, admin | `{teams: [all 4 summaries]}` | `teams.broadcast_summary()` on any team change |
| `WS /ws/team/{n}?nonce=` | team page | full team state (personalized) | `teams.broadcast_team(n)` |
| `WS /ws/gamestate` | watch page | `app_state.to_dict()` | server loop pushes every 100ms (10Hz) |

Pattern for the team feeds: **mutate the registry, then broadcast.** Every
mutating REST handler calls `await teams.broadcast_summary()` and/or
`await teams.broadcast_team(n)` after the change so open pages update without
polling. The registry keeps the socket sets (`register_summary`/`register_team`)
and prunes dead sockets on send. `/ws/gamestate` is different: it is a
timer-driven 10Hz push of live state, not change-driven.

Phase is *also* polled: `lobby.html` and `admin.html` `fetch('/api/state')`
every 2s because phase transitions come from the orchestrator, not the registry,
so they are not on the team WS. Keep both: WS for team state, polling for phase.

## Team model (what the templates render)

A team's identity IS its Dolphin port (1-4). Every slot always exists in
`TeamRegistry`; each has an `active` flag. The active set is any subset of size
2-4 (non-contiguous is fine). Key registry methods the frontend leans on:
`active_ids()`, `activate(n)`/`deactivate(n)`, `all_ready()` (count-aware),
`summary()` (returns all 4 with `active`), `team(n).to_dict(my_nonce=...)`.

`summary()` returns all 4 so the lobby can draw active cards next to inactive
"ADD TEAM n" placeholders. In `lobby.html`: `allTeams` holds the raw list,
`activeTeams()` filters `active`, and add/remove controls only show while
`lobbyOpen()` (idle/postgame). START enables at `active.length >= 2 && every
ready`.

## Client identity and the captain model

There is no login. Identity is a random **nonce** stored in `localStorage`
(`smash_nonce`, minted by `getNonce()` in `team.html`); the nickname is
`smash_nick`. The nonce is sent in mutating request bodies and as the
`?nonce=` query param on `GET /api/team/{n}` and `WS /ws/team/{n}`.

The server never trusts the nonce as a secret - it only compares it to the
stored `captain_nonce` to decide `you_are_captain` and to gate captain actions.
Captain takeover is intentional and instant (`force: true`), with a UI confirm.
`to_dict(my_nonce=...)` computes `you_are_captain` per-recipient so a captain's
nonce is never broadcast to other clients.

## Styling and template conventions

- Each template is fully self-contained: `<style>` block at top, `<script>` at
  bottom, no shared CSS/JS files. Match the existing dark theme
  (`#0d0d0d` bg, `#e94560` accent, `Courier New`), the `header`/`nav` block, and
  the per-port color classes `.p1 .p2 .p3 .p4`.
- Always HTML-escape user text with the local `esc()` helper before inserting
  via `innerHTML`. Names, nicknames, and contributions are user-controlled.
- Jinja passes server context (e.g. `characters`, `team_id`, twitch vars);
  runtime data arrives over fetch/WebSocket and is rendered by JS.
- WebSocket clients auto-reconnect with `ws.onclose = () => setTimeout(connect, 2000)`.
  Keep that on any new feed.

## How to extend

**Add a page:** new `@app.get(...)` returning
`templates.TemplateResponse(request, "x.html", {...})`; copy an existing
template's header/nav/style scaffold; add the nav link in the other templates.

**Add a team mutation:** registry method on `TeamRegistry` (mutate + `self._save()`);
a captain-gated route in `app.py` that calls it then
`broadcast_summary()`/`broadcast_team(n)`; map any new `TeamError` in
`_team_error_to_http`; render it in `team.html`'s `render(s)` and, if it shows
on the landing page, in `lobby.html`'s card renderer + `to_summary()`.

**Add a live-score field:** add it to `PlayerScore`/`AppState.to_dict()` in
`core/game_state.py` (the orchestrator already writes scores), then read it in
`watch.html`'s `renderScores(data)`. It flows automatically over `/ws/gamestate`.

## Gotchas

- `queue_match` is async now - `await` it from `/api/start`.
- Roster changes (`activate`/`deactivate`) and `reset` are blocked outside
  IDLE/POSTGAME; the UI hides those controls during a match, but the server
  enforces it too (`_require_lobby_open`).
- Do not add a per-file `toml.load` - config comes from
  `core.config.load_settings()` only.
- The code-override textarea marks itself `dataset.touched` on input so an
  incoming WS snapshot does not clobber what the captain is typing. Preserve
  that guard if you touch that field.
