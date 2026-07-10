# libmelee v0.47.2 Reference

This skill contains authoritative notes on the libmelee API derived from the
installed package source at `.venv/lib/python3.13/site-packages/melee/` and the
official docs at https://libmelee.readthedocs.io/en/latest/.

Consult this before writing any code that touches menus, controllers, or game
state. Many of the APIs have subtle gotchas that cause silent breakage.

---

## Package name

```bash
pip install melee   # correct — NOT "libmelee"
```

---

## Console

```python
console = melee.Console(
    path="/Applications/Slippi Dolphin.app/Contents/MacOS/Slippi Dolphin",
    slippi_address="127.0.0.1",
    slippi_port=51441,
    blocking_input=False,
    polling_mode=False,   # step() blocks until next frame arrives
    fullscreen=False,
)
console.run(iso_path="assets/melee.iso")
console.connect()          # blocks until Dolphin is ready
gamestate = console.step() # first call — also flushes controller inputs
```

**`console.step()` flushes ALL controller inputs and returns the next
GameState.** This means:
- Call step() → receive frame N's state
- Set inputs on controllers for frame N
- Call step() again → those inputs are flushed to Dolphin, receive frame N+1

Do not call `controller.flush()` manually — step() handles it.

`step()` returns `None` if data is not yet available. Always guard:
```python
gs = console.step()
if gs is None:
    continue
```

### step() is the frame pacer — never add your own sleep

With `polling_mode=False`, `step()` blocks until Dolphin emits the next frame,
so the loop is already paced at 60fps. Adding a `sleep(1/60 - elapsed)` on top
creates a permanent stale-frame backlog: after any hiccup, buffered frames make
`step()` return instantly, the sleep then burns a full frame per buffered
frame, and the backlog never drains. Acting on stale gamestates makes
MenuHelper cursors overshoot and oscillate. In asyncio, call it via
`await asyncio.to_thread(console.step)` so the blocking read doesn't starve
the event loop. Full rules and the verified loop pattern: see the
**libmelee-game-loop** skill.

---

## Controller

```python
ctrl = melee.Controller(console=console, port=1, type=melee.ControllerType.STANDARD)
ctrl.connect()   # must call before any input
```

**Key input methods:**

| Method | Description |
|---|---|
| `tilt_analog(Button.BUTTON_MAIN, x, y)` | Main stick. x/y ∈ [0,1], 0.5=neutral |
| `tilt_analog(Button.BUTTON_C, x, y)` | C-stick. Same range |
| `press_button(Button.BUTTON_A)` | Hold a button |
| `release_button(Button.BUTTON_A)` | Release a button |
| `release_all()` | All buttons off, sticks centered at 0.5, shoulders 0 |
| `simple_press(x, y, button)` | Releases everything first, then applies |
| `press_shoulder(Button.BUTTON_L, amount)` | Analog shoulder 0–1 |

**`simple_press` releases all prior inputs before applying** — avoid using it
if you need to maintain simultaneous button presses.

Access the controller's previous frame state with `ctrl.prev` (a `ControllerState`
object). Used inside MenuHelper to alternate A/B presses without double-pressing.

---

## GameState

```python
gs.menu_state      # melee.Menu enum — current screen
gs.frame           # int, can be negative, resets to 0 at match start
gs.players         # dict[int, PlayerState] keyed by port (1-indexed)
gs.ready_to_start  # bool — "Ready to Start?" banner is showing on CSS
gs.menu_selection  # int — selected item index in current menu
gs.submenu         # melee.SubMenu — sub-menu within a screen
gs.distance        # float — Euclidean distance between P1 and P2
```

**`gs.players` only contains entries for ports whose controllers are
connected.** Always do `gs.players.get(port)` (not direct index access) when
the port might not be active.

### PlayerState — in-game

```python
p = gs.players[1]
p.position.x, p.position.y   # float, world coordinates
p.stock                       # int, remaining lives
p.percent                     # float, damage percentage
p.action                      # melee.Action enum — current animation
p.action_frame                # int, indexed from 1
p.facing                      # bool, True=facing right
p.on_ground                   # bool
p.jumps_left                  # int
p.hitstun_frames_left         # int
p.shield_strength             # float, max 60, breaks at 0
p.speed_air_x_self            # five separate speed components
```

### PlayerState — on CSS

```python
p.cursor.x, p.cursor.y       # float — CSS cursor world coordinates
p.coin_down                   # bool — coin/token placed on a character
p.character_selected          # melee.Character — currently highlighted char
p.character                   # melee.Character — same as above on CSS
p.cpu_level                   # int — 0 for human/bot, 1–9 for CPU
p.controller_status           # melee.ControllerStatus enum
p.is_holding_cpu_slider       # bool — cursor is dragging the CPU level bar
```

---

## MenuHelper

`MenuHelper` is a **stateful** class. Instantiate once per match and reuse:

```python
menu_helper = melee.MenuHelper()
```

**Never share a single MenuHelper instance across multiple controller ports.**
Each port needs its own `MenuHelper` because the instance tracks:
- `stage_selected` (bool)
- `frames_on_stage` (int)
- `frozen_stadium_selected` (bool)
- `inputs_live` and `name_tag_index` (for connect-code entry)

---

## Character Select Screen (CSS) — the hardest part

### Rules (from source + docs)

1. **Call `choose_character` every frame** for every connected controller.
   Missing a frame leaves the cursor mid-navigate.
2. **All cursors must stay above the character-level slider** (cursor_y ≥ 0)
   or the match will never start, even if one controller has pressed START.
3. **Only one controller should have `start=True`.**
4. **`choose_character` returns early** (releases all inputs) if
   `controller.port not in gamestate.players`. Ensure you only call it for
   ports that actually appear in the gamestate.

### How `choose_character` works internally

```
cursor position → calculate (target_x, target_y) on 9×3 character grid
if outside wiggleroom (1.5 units):
    tilt main stick full in the required direction, return
if inside wiggleroom and coin not down:
    alternate pressing A, return
if coin_down (character locked in):
    alternate release_all / press START (only when start=True and ready_to_start==0)
```

### CPU level flow (when `cpu_level > 0`)

When `cpu_level > 0`, `choose_character` has extra steps after the character
is selected:
1. Navigate cursor to the controller-type toggle (top row of the port's column)
   and press A to change from HUMAN → CPU.
2. Navigate cursor to the CPU slider at the bottom of the port's column.
3. Pick up the slider (`is_holding_cpu_slider` becomes True), drag it to the
   target level, drop it.

This is fragile — if `is_holding_cpu_slider` becomes True unexpectedly for a
human/bot controller (cpu_level=0), the code will still enter the CPU level
flow due to operator precedence:
```python
# BUG: last OR is evaluated separately, so is_holding_cpu_slider alone triggers the block
if use_cpu and correct_character and ... and (cpu_level != ai_state.cpu_level) \
        or ai_state.is_holding_cpu_slider:
```
This can cause erratic cursor movement if a cursor drifts into the slider area.

### Signature

```python
menu_helper.choose_character(
    character=melee.Character.FOX,
    gamestate=gs,
    controller=ctrl,
    cpu_level=0,     # 0 = human/bot; 1–9 = CPU
    costume=2,       # default is 2, not 0
    swag=False,
    start=False,     # True on exactly ONE controller only
)
```

### `gamestate.ready_to_start` semantics

`ready_to_start == 0` (False) means the "Ready to Start" banner is NOT yet
showing. `choose_character` presses START precisely when this is False and
`coin_down` is True. This is correct — pressing START causes the banner to
appear and then the match to begin.

---

## `menu_helper_simple`

Handles: `MAIN_MENU`, `PRESS_START`, `CHARACTER_SELECT`, `SLIPPI_ONLINE_CSS`,
`STAGE_SELECT`, `POSTGAME_SCORES`.

```python
menu_helper.menu_helper_simple(
    gamestate=gs,
    controller=ctrl,
    character_selected=melee.Character.FOX,
    stage_selected=melee.Stage.FINAL_DESTINATION,
    connect_code="",        # blank for VS mode
    cpu_level=0,
    costume=0,
    autostart=False,        # True to auto-start; set on ONE controller only
    swag=False,
    frozen_stadium=True,
)
```

**`menu_helper_simple` DOES drive CSS** — it calls `choose_character`
internally when `menu_state == CHARACTER_SELECT`. The CLAUDE.md note that it
"does not drive CSS" is wrong. However, it only drives the single `controller`
passed in, so you still need to call it (or `choose_character`) once per
connected controller port per frame.

---

## Stage Select

```python
menu_helper.choose_stage(
    stage=melee.Stage.FINAL_DESTINATION,
    gamestate=gs,
    controller=ctrl,
    character=melee.Character.FOX,   # required — used for Sheik handling
    frozen_stadium=True,
    autostart=False,                 # True on ONE controller only
)
```

Resets when `gamestate.frame == 0`. Does nothing on the first 20 frames
(`frame < 20`). Only one controller should have `autostart=True`.

---

## Menu enum values

```python
melee.Menu.MAIN_MENU          # 5
melee.Menu.PRESS_START        # 7
melee.Menu.CHARACTER_SELECT   # 0
melee.Menu.STAGE_SELECT       # 1
melee.Menu.IN_GAME            # 2
melee.Menu.SUDDEN_DEATH       # 3
melee.Menu.POSTGAME_SCORES    # 4
melee.Menu.SLIPPI_ONLINE_CSS  # 6
melee.Menu.UNKNOWN_MENU       # 255
```

---

## ControllerStatus enum

```python
melee.ControllerStatus.CONTROLLER_HUMAN     # 0
melee.ControllerStatus.CONTROLLER_CPU       # 1
melee.ControllerStatus.CONTROLLER_UNPLUGGED # 3
```

Use `change_controller_status` (static method on MenuHelper) to toggle a port
between HUMAN and CPU on the CSS. Requires the cursor to be on that port's
toggle button at the top of the CSS.

---

## Common pitfalls

| Symptom | Likely cause |
|---|---|
| Cursor overshoots target and oscillates on CSS | **Stale frames**: the game loop has a pacing sleep around `step()` or blocks the asyncio event loop, so inputs steer on old cursor positions. See the libmelee-game-loop skill. This was the actual root cause when it happened in this repo — check it before anything else |
| Cursor moves erratically on CSS (heads to slider/toggle rows) | `is_holding_cpu_slider` became True on a human-port (e.g. A held while entering CSS grabbed the slider); OR not calling `choose_character` for ALL connected ports |
| Match never starts | Cursor of any connected port is below y=0 (in the CPU-slider row) |
| `choose_character` does nothing | `controller.port not in gamestate.players` — port isn't in gamestate |
| Stage select never selects | `autostart=True` not set, or multiple controllers have it True |
| Costume wrong | Default costume index in `choose_character` is 2, not 0 |
| `menu_helper_simple` fails with ValueError | `connect_code` provided but no `user.json` configured |

---

## Canonical per-frame CSS pattern

```python
# At startup, create one MenuHelper per connected port
menu_helpers = {port: melee.MenuHelper() for port in [1, 2, 3, 4]}

# Each frame in CHARACTER_SELECT:
for port in connected_ports:  # only ports present in gamestate.players
    menu_helpers[port].choose_character(
        character=target_characters[port],
        gamestate=gs,
        controller=controllers[port],
        cpu_level=cpu_levels[port],   # 0 for bot, 1–9 for CPU
        costume=0,
        swag=False,
        start=(port == start_port),   # True on exactly one port
    )
```
