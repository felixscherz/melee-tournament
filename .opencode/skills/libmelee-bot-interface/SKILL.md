---
name: libmelee-bot-interface
description: The Bot class contract, stick/button conventions, return dict shape, safety rules, and common pitfalls for writing a libmelee bot in this project. Load this before writing or debugging any bot code.
---

## Bot interface

A bot is a Python file with a top-level `Bot` class that has an `act` method:

```python
import melee


class Bot:
    def __init__(self):
        # Optional: one-time setup here (state variables, counters, etc.)
        pass

    def act(self, gamestate: melee.GameState, player_port: int) -> dict | None:
        # Called once per frame (~60fps) during a match.
        # Return a dict with controller inputs, or None to release everything.
        ...
```

The `act` method is called every frame. It must be fast - no sleeps, no I/O,
no network calls, no heavy computation. Just read gamestate and return inputs.

## Return dict shape

```python
{
    "stick_x": 0.5,   # 0.0 = left,  1.0 = right, 0.5 = neutral
    "stick_y": 0.5,   # 0.0 = down,  1.0 = up,    0.5 = neutral
    "buttons": {
        "BUTTON_A": False,
        "BUTTON_B": False,
        "BUTTON_X": False,
        "BUTTON_Y": False,
        "BUTTON_L": False,
        "BUTTON_R": False,
        "BUTTON_Z": False,
    },
}
```

### Stick conventions

- `stick_x` and `stick_y` must be floats in the range **0.0 to 1.0**.
- 0.5 is neutral (no input).
- 0.0 is full left/down, 1.0 is full right/up.
- Values outside [0.0, 1.0] will be rejected by the test harness.

### Button conventions

- All 7 button keys must be present in the `buttons` dict.
- Each value must be a `bool` (True = pressed, False = released).
- The 7 buttons are: `BUTTON_A`, `BUTTON_B`, `BUTTON_X`, `BUTTON_Y`,
  `BUTTON_L`, `BUTTON_R`, `BUTTON_Z`.
  - A: attack / grab context
  - B: special move (e.g. Fox's shine, Falco's laser, Fox's firefox)
  - X / Y: jump (both are jump buttons)
  - L / R: shield
  - Z: grab

### Returning None

Return `None` to release all inputs for that frame. This is the safe default
when you don't know what to do or when `me` is None.

## Reading gamestate

`gamestate.players` is a dict keyed by port number (1-4). Not all ports may
be active. Always use `.get()` and check for None:

```python
me = gamestate.players.get(player_port)
if me is None:
    return None
```

### Player fields you can read

| Field | Type | Description |
|---|---|---|
| `me.position.x` | float | Horizontal position. 0 = center of stage. Negative = left, positive = right. |
| `me.position.y` | float | Vertical position. 0 = stage floor. Positive = above stage. Negative = below stage (off-stage). |
| `me.stock` | int | Remaining stocks (lives). Starts at 4. |
| `me.percent` | float | Damage percent. Higher = more knockback when hit. |
| `me.action` | melee.Action | Current animation state (see below). |
| `me.character` | melee.Character | Which character this player is playing. |
| `me.facing` | bool | Which direction the player is facing. True = right, False = left. Use this to decide whether to turn around before attacking. |

### Finding opponents

Your bot can be on any port (1-4). Do NOT hardcode the opponent as port 2.
Iterate over all players and skip yourself:

```python
opponents = []
for port, player in gamestate.players.items():
    if port != player_port:
        opponents.append(player)
```

In a 4-player free-for-all, there are 3 opponents. Consider targeting the
nearest one, or the one with the highest percent, depending on your strategy.

### melee.Action values

Common action states you may want to check:

| Action name | Meaning |
|---|---|
| `STANDING` | Idle on ground |
| `WALK_SLOW`, `DASHING`, `RUNNING` | Moving on ground |
| `TURNING` | Turning around |
| `JUMPING_FORWARD`, `JUMPING_BACKWARD` | Jumping |
| `FALLING`, `FALLING_AERIAL` | Airborne, falling |
| `TUMBLING` | Knocked airborne, tumbling |
| `SHIELD` | Shielding |
| `GRABBED` | Being held by opponent |
| `LYING_GROUND_DOWN` | Knocked down on the floor |
| `NEUTRAL_GETUP`, `GETUP_ATTACK` | Getting up from the floor |
| `NEUTRAL_TECH`, `FORWARD_TECH`, `BACKWARD_TECH` | Teching (quick recovery on impact) |
| `TECH_MISS_DOWN` | Failed a tech (stays on floor longer) |
| `NAIR`, `FAIR`, `BAIR`, `UAIR`, `DAIR` | Aerial attacks (neutral/forward/back/up/down) |
| `NEUTRAL_ATTACK_1` | Jab |
| `FSMASH_MID` | Forward smash |
| `UPSMASH` | Up smash |
| `DOWNSMASH` | Down smash |
| `DASH_ATTACK` | Dash attack |
| `GROUND_ATTACK_UP` | Up tilt |
| `DEAD_DOWN`, `DEAD_LEFT`, `DEAD_RIGHT`, `DEAD_UP` | Dead (lost a stock) |
| `DEAD_FALL` | Falling off stage, about to die |
| `ENTRY_START`, `ENTRY_END` | Spawning onto the stage |

Check actions like: `if me.action == melee.Action.SHIELD:`

### melee.Character values

Character enum names match the roster: `FOX`, `FALCO`, `MARTH`, `CPTFALCON`,
`MARIO`, `LUIGI`, `PEACH`, `SHEIK`, `LINK`, `SAMUS`, `DK`, `BOWSER`, `YOSHI`,
`PIKACHU`, `JIGGLYPUFF`, `NESS`, `GANONDORF`, `ROY`, `DOC`, `YLINK`, `PICHU`,
`KIRBY`, `ZELDA`, `GAMEANDWATCH`, `POPO`.

## Import whitelist

Only these imports are allowed:

- `import melee` - the game API
- `import math` - math functions
- `import random` - random number generation

Any other import will be rejected by the validator. No `os`, `sys`, `socket`,
`subprocess`, `importlib`, or anything else.

## Banned builtins

These are rejected by the validator and must never appear in your code:

`eval`, `exec`, `compile`, `__import__`, `open`, `input`, `breakpoint`,
`globals`, `locals`, `vars`, `getattr`, `setattr`, `delattr`, `memoryview`,
`exit`, `quit`, `help`

No dunder attribute access (`__class__`, `__globals__`, `__subclasses__`, etc.).

## Common pitfalls

1. **Hardcoding the opponent port.** Your bot can be port 1, 2, 3, or 4. Always
   find opponents dynamically by iterating `gamestate.players` and skipping
   your own `player_port`.

2. **Not handling `me is None`.** If your port is not in `gamestate.players`
   (e.g. you lost your last stock), `me` will be None. Return None immediately.

3. **Stick values out of range.** `stick_x` and `stick_y` must be in [0.0, 1.0].
   0.5 is neutral. Do not use negative values or values above 1.0.

4. **Missing button keys.** All 7 buttons must be in the `buttons` dict, even
   if they are False. Missing keys will fail validation.

5. **Forgetting `__init__`.** If you need state (frame counters, cooldowns),
   define `__init__` and set them there. The `Bot` class is instantiated once
   at match start and `act` is called repeatedly.

6. **Using `me.position.x` as the only spatial info.** Also check `position.y`
   to detect being off-stage (negative y) or airborne (positive y).

7. **Not accounting for 4 players.** In a free-for-all, there are 3 opponents.
   Tunnel-visioning one can get you hit from behind.
