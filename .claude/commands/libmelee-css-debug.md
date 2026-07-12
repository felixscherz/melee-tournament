# CSS Navigation — Debugging Erratic Cursor Movement

Use this when `choose_character` causes cursors to move in unexpected
directions or the match never starts. All observations are from reading the
installed `melee/menuhelper.py` directly (find it with
`uv run python -c "import melee, os; print(melee.__file__)"`, typically
`.venv/lib/python3.*/site-packages/melee/menuhelper.py`).

---

## Check FIRST: stale frames from a broken game loop

**This was the actual root cause of the erratic/overshooting cursors in this
repo (July 2026) — rule it out before suspecting the helper.**

`choose_character` is a bang-bang controller: full stick deflection until the
cursor is within ±1.5 units, re-evaluated per frame. It is stable at true
60fps but overshoots and oscillates when the gamestates it steers on are
stale. Stale frames happen when the loop around `console.step()`:

- adds its own pacing sleep (`sleep(1/60 - elapsed)`) — after any hiccup the
  socket backlog of frame events can never drain, so lag is permanent; or
- calls blocking `step()` directly on the asyncio event loop thread, so the
  web server and game loop starve each other and create the hiccups.

Fix: no pacing sleep (step() is the pacer), and
`await asyncio.to_thread(console.step)`. Full write-up: the
**libmelee-game-loop** skill. Do NOT slow the cursor down (deflection caps,
controller wrappers) — that masks the symptom.

---

## How `choose_character` reads position

```python
ai_state = gamestate.players[controller.port]
cursor_x, cursor_y = ai_state.cursor.x, ai_state.cursor.y
```

The cursor world position is read from the gamestate every frame. Navigation
is done by tilting the main analog stick to 0 or 1 (full deflection) in a
single axis until the cursor is within 1.5 units of the target.

---

## Known bug: `is_holding_cpu_slider` overrides `cpu_level`

From the source, the CPU-level adjustment block fires when:

```python
if (use_cpu and correct_character and (coin_down or cursor_y < 0)
        and (cpu_level != ai_state.cpu_level)) \
        or ai_state.is_holding_cpu_slider:   # <-- independent OR
```

The `or ai_state.is_holding_cpu_slider` is evaluated as a separate condition
due to Python operator precedence. This means: **even if `cpu_level=0`
(human/bot), if the gamestate reports `is_holding_cpu_slider=True`, the
function enters the CPU slider adjustment flow**, navigating the cursor to
target_y=-15.12 (the slider row) or target_y=-2.2 (the controller-type
toggle row). This directly causes erratic/unexpected cursor movement.

**How the cursor can accidentally end up holding the slider:**
- Cursor starts at or below y≈−15 on CSS load
- `choose_character` navigates upward, but A is pressed while passing near the slider
- The game interprets this as grabbing the CPU slider
- `is_holding_cpu_slider` becomes True in the gamestate
- Now the CPU flow takes over even though `cpu_level=0`

**Verified fixes (both implemented in `core/melee_orchestrator.py`):**

1. **Release held inputs on every menu transition.** A held from mashing
   through the main menu is how the cursor grabs the slider/toggle in the
   first place. Track the previous `menu_state`; when it changes, call
   `release_all()` on every controller and skip that frame.

2. **If a port reports `is_holding_cpu_slider`, release instead of calling
   the helper** — dropping the slider is the only way out of the buggy flow:
   ```python
   p_state = gs.players.get(port)
   if p_state is not None and p_state.is_holding_cpu_slider:
       controllers[port].release_all()
       continue
   ```

---

## Match never starts — checklist

1. **Every connected port must be above the slider (cursor_y > 0).** Run this
   debug print to check:
   ```python
   for port, p in gs.players.items():
       print(f"P{port}: cursor=({p.cursor.x:.1f},{p.cursor.y:.1f}) coin={p.coin_down} status={p.controller_status}")
   ```

2. **`coin_down` must be True** for the `start=True` controller before START
   is pressed. If the coin is never placed (cursor keeps moving past the
   character without stopping), the character coordinates may be wrong — check
   `melee.from_internal(character)` vs expected grid position.

3. **Only one controller may have `start=True`.** Multiple controllers trying
   to press START simultaneously can cause the ready banner to flicker.

4. **`gamestate.ready_to_start`** — START is only pressed by `choose_character`
   when this is `False`/`0`. If it stays True after the first press, the
   banner may be flickering; add a log to check its value each frame.

5. **Frame 0 reset** — `choose_stage` resets `stage_selected = False` when
   `gamestate.frame == 0`. If the stage select screen reports frame 0 due to
   a menu reset, this is fine. On CSS there is no such reset.

---

## Diagnosing cursor positions live

Add this to `_handle_frame` temporarily:

```python
if menu == melee.Menu.CHARACTER_SELECT:
    for port, p in gs.players.items():
        log.debug(
            "CSS P%d: cursor=(%.1f,%.1f) coin=%s char=%s slider=%s status=%s",
            port, p.cursor.x, p.cursor.y, p.coin_down,
            p.character, p.is_holding_cpu_slider, p.controller_status
        )
```

Run with `LOG_LEVEL=DEBUG` or change `basicConfig(level=logging.DEBUG)` in
`main.py`. Watch for any port where `is_holding_cpu_slider=True` or
`cursor_y < -10` persists — that's the erratic port.

---

## CSS coordinate reference

Character grid target coordinates (wiggleroom ±1.5):

| Row | Description | target_y |
|---|---|---|
| 0 (top) | row=2 in Melee internal | ≈15.5 |
| 1 (middle) | row=1 | ≈8.5 |
| 2 (bottom) | row=0 | ≈1.5 |

Special areas:
- Controller-type toggle (HUMAN/CPU): y≈−2.2
- CPU level slider: y≈−15.12
- "Above slider" safe zone: y > 0

Characters on the bottom row (row 2 internal = top of display) are shifted
one column right to account for the RANDOM slot at position (0,0).
