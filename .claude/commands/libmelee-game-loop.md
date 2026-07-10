# libmelee Game Loop — Frame Pacing Rules

Read this before writing or modifying any loop that calls `console.step()`.
These rules come from a real debugging session (July 2026) where violating
them caused erratic, oscillating CSS cursor movement that looked like a
libmelee bug but wasn't.

---

## Rule 1: `console.step()` IS the 60fps pacer — never add your own sleep

With `polling_mode=False`, `step()` blocks on a socket read until Dolphin
emits the next frame event. The loop is paced by Dolphin itself. The correct
loop body is just:

```python
while True:
    gs = console.step()
    if gs is None:
        continue
    handle_frame(gs)
```

**Why an extra `sleep(1/60 - elapsed)` is catastrophic, not just redundant:**

Dolphin pushes one frame event every ~16.7ms regardless of whether you read
them. Unread events queue in the socket buffer. The failure sequence:

1. Any hiccup delays one loop iteration (logging, GC, web traffic on the
   same event loop, a Rosetta stall). A frame gets buffered.
2. Next iteration, `step()` returns **instantly** (buffered frame), so
   `elapsed ≈ 0` and the pacing sleep waits a full 16ms.
3. During that sleep Dolphin produces exactly one more frame. You consume
   one buffered frame per frame produced — **the backlog never drains**.
4. Every hiccup adds another frame of permanent lag. All gamestates you act
   on are N frames stale, and N only grows.

**Symptom of stale frames:** menu cursors overshoot their targets and
oscillate. `MenuHelper.choose_character` is a bang-bang controller (full
stick deflection until within ±1.5 units, re-evaluated per frame) — it is
stable at true 60fps with fresh frames and unstable when steering on
positions from N frames ago. Bots similarly react late in-game.

**Do not "fix" this by slowing the cursor** (capping stick deflection,
wrapping the controller). That masks the symptom. Fix the loop.

## Rule 2: In asyncio, run `step()` in a worker thread

`step()` is a blocking socket read. Called directly in a coroutine it blocks
the entire event loop for ~16ms per frame, starving the web server — and web
server activity in turn delays the game loop, creating exactly the hiccups
Rule 1 describes. Use:

```python
gs = await asyncio.to_thread(console.step)
```

Sequencing stays safe: `step()` flushes controller inputs at its start, and
your frame handler runs strictly between `step()` calls, so there is no
concurrent controller access.

## Rule 3: On menu transitions, `release_all()` once on every controller

Mashing A/START through the main menu means A can still be held on the first
CSS frames. A held A near the bottom of the CSS grabs the CPU slider or the
HMN/CPU toggle, which triggers the `is_holding_cpu_slider` bug in
`choose_character` (see the `libmelee-css-debug` skill). Track the previous
`menu_state`; when it changes, release all controllers and skip that frame.

## Rule 4: Guard against the CPU-slider grab

Even with `cpu_level=0`, `choose_character` enters the CPU-slider flow when
`ai_state.is_holding_cpu_slider` is True (upstream operator-precedence bug).
Before calling the helper for a bot port:

```python
p_state = gs.players.get(port)
if p_state is not None and p_state.is_holding_cpu_slider:
    controllers[port].release_all()   # drop the slider — the only way out
    continue
```

---

## Canonical asyncio game loop

This is the verified pattern used in `core/melee_orchestrator.py`
(`_game_loop` / `_handle_frame`):

```python
async def _game_loop(self):
    while True:
        try:
            gs = await asyncio.to_thread(self.console.step)   # Rule 2
        except Exception as exc:
            log.error("console.step() error: %s", exc)
            await asyncio.sleep(0.1)
            continue
        if gs is None:
            await asyncio.sleep(0.001)
            continue
        self._latest_gs = gs
        await self._handle_frame(gs)
        # NO pacing sleep here — Rule 1

async def _handle_frame(self, gs):
    menu = gs.menu_state
    if menu != self._prev_menu:                               # Rule 3
        self._prev_menu = menu
        for ctrl in self._controllers.values():
            ctrl.release_all()
        return
    ...
```

Keep per-frame work well under 16ms. Anything slow (bot decisions, LLM
calls) belongs in a separate task that reads `self._latest_gs` and writes an
actions dict the frame handler applies — never inline in the frame path.

## Verifying loop health

- `console.processingtime` = seconds spent outside `step()` since the last
  call. If it regularly exceeds ~0.017, your frame handler is too slow.
- Behavioral check: queue a match and time CSS → IN_GAME. With a healthy
  loop, character select + stage select completes in a couple of seconds.
  Visible cursor overshoot/oscillation means stale frames — check for
  sleeps in the loop and blocking work on the event loop thread.
