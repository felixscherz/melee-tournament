---
name: melee-strategy
description: Generic Smash Melee gameplay knowledge for writing bots - how the game works, game states, universal tactical concepts, and the action enum. Character-agnostic. Load this when writing a bot that needs to play Melee.
---

## How Melee works

Super Smash Bros. Melee is a platform fighter. The goal is to knock opponents
off the stage until they run out of stocks (lives). Unlike traditional fighting
games, there is no health bar - instead, damage is measured in **percent**.
The higher a player's percent, the further they fly when hit.

### Core mechanics

- **Percent (damage):** Starts at 0%. Goes up as you get hit. Higher percent =
  more knockback from attacks. At very high percent (100%+), even weak hits
  can send a player flying off-screen.
- **Stocks:** Each player starts with 4 stocks (lives). Losing all stocks
  eliminates you. When you are hit off-screen (past the blast zone), you lose
  a stock and respawn.
- **Blast zones:** The invisible boundaries of the stage. If you fly past them
  (left, right, up, or down), you die. The stage itself is much smaller than
  the blast zone area.
- **Final Destination:** A flat platform with no platforms or hazards. The
  main stage is roughly from x=-65 to x=65. The edges are at about x=68.
  Below y=0 is off-stage. If you fall below about y=-50, you hit the bottom
  blast zone and die.

### Recovering

When you are knocked off-stage (position.y < 0, or |position.x| > 68), you
must get back. Most characters have an **up-B special** (press B + stick up)
that launches them upward. Some characters have side-B specials for horizontal
recovery. To recover:
1. Use your jump(s) first (press X or Y).
2. Then use up-B (hold stick up + press B) to gain height/distance.
3. Aim to land on the stage edge or grab the ledge.

If `position.y` is very negative (below -30), you are far below the stage and
need to recover immediately or you will die.

### Edge-guarding

When an opponent is off-stage trying to recover, you can try to prevent them
from getting back (edge-guarding). Common approaches:
- Stand near the edge and hit them as they approach.
- Jump off-stage and attack them in the air (risky - you must recover too).
- Grab the edge (press L/R or Z near the edge while airborne) to deny them
  the ledge.

## Game states

Melee gameplay cycles through several states. A good bot recognizes which
state it is in and acts accordingly:

### Neutral (both players safe, looking for an opening)
- Both players are on stage, not in hitstun, facing each other.
- Goal: find a way to start an advantage state (land a hit, grab, etc.).
- Common actions: space attacks, approach carefully, wait for opponent to
  make a mistake (whiff-punish).

### Advantage (you are pressuring the opponent)
- You just hit them or knocked them down. They are in hitstun, tumbling, or
  on the floor.
- Goal: extend the advantage - follow up with more hits, chase their
  tech/roll, or edge-guard if they went off-stage.
- Common actions: chase, attack again, cover their recovery options.

### Disadvantage (you are being pressured)
- You just got hit, you are tumbling, in hitstun, or knocked down.
- Goal: escape to neutral. Use DI (directional influence) to survive, tech
  to recover quickly, get up safely.
- Common actions: tech (press L/R before hitting a surface), DI (hold a
  direction while in hitstun to influence your trajectory), get up with
  an attack or a roll.

### Recovery (you are off-stage getting back)
- Your `position.y` is negative or `position.x` is past the stage edge.
- Goal: get back on stage without dying.
- Common actions: jump toward stage, use up-B, air-dodge (press L/R + a
  direction).

### Edge-guard (opponent is off-stage)
- The opponent's `position.y` is negative or their `position.x` is past
  the stage edge.
- Goal: prevent them from recovering.
- Common actions: attack them as they approach, grab the edge, intercept
  their recovery move.

## Universal tactical concepts

### Spacing
Attacking from the maximum range of your move so you can't be punished if
it misses. Good spacing means you hit your opponent but they can't hit you.
Use `position.x` to maintain the right distance.

### Whiff-punishing
Waiting for the opponent to attack, missing (whiffing), and then punishing
them during their recovery frames. This is hard to do with a simple bot
because you need to detect that the opponent just missed. One approach:
if the opponent's `action` is an attack action and they are not close
enough to hit you, approach and counterattack.

### Approach
Closing the distance to the opponent safely. Dashing (stick fully left/right)
is fast but leaves you vulnerable if you dash into an attack. Walking is
slower but lets you shield or attack immediately.

### Teching
When you hit a surface (floor, wall, ceiling) while in hitstun, you can press
L/R to "tech" - a quick recovery that lets you get up instantly instead of
bouncing. If you miss a tech (`TECH_MISS_DOWN`), you are stuck on the floor
and vulnerable. If you see an opponent in `LYING_GROUND_DOWN`, they missed a
tech and you can hit them or grab them.

### Tech-chasing
Predicting which direction an opponent will tech after hitting the floor, and
following them to punish. After a tech, opponents can go neutral, forward, or
backward. A simple bot can just chase to the opponent's current position.

### DI (Directional Influence)
When you are in hitstun (being launched), you can hold a direction on the
stick to slightly alter your launch trajectory. Holding up/side helps you
survive horizontal kills. Holding down can help you avoid being launched up.
This is done by returning the appropriate stick direction in your `act()`
return while your `action` is a damage/tumbling state.

### Shielding
Press L or R to raise your shield. The shield blocks attacks but shrinks over
time and can be broken. While shielding, you can:
- **Grab** (press Z or A): shield-grab, catches opponents who attack your
  shield carelessly.
- **Spot-dodge** (press down while shielding): dodge in place, avoids grabs.
- **Roll** (press left/right while shielding): roll away to escape pressure.

Shielding is risky if held too long (shield breaks = stunned for several
seconds). Use it to block a specific incoming attack, then release.

### Crouch-canceling
Holding down (stick_y = 0.0) while on the ground reduces knockback from
incoming attacks. Useful at low percent to tank a hit and counterattack.
At high percent it does not help much.

## Action enum reference

Use `melee.Action` enum values to check what state a player is in. Key states:

| Action | What it means | Strategic note |
|---|---|---|
| `STANDING` | Idle on ground | Neutral state, ready to act |
| `WALK_SLOW` / `DASHING` / `RUNNING` | Moving | Approaching or retreating |
| `TURNING` | Turning around | Briefly vulnerable |
| `JUMPING_FORWARD` / `JUMPING_BACKWARD` | Jumping | Can attack in the air |
| `FALLING` / `FALLING_AERIAL` | Airborne, falling | Vulnerable to juggles |
| `TUMBLING` | Knocked airborne | In hitstun, can DI |
| `SHIELD` | Shielding | Can grab, spot-dodge, or roll |
| `GRABBED` | Being held by opponent | Mash buttons to escape |
| `LYING_GROUND_DOWN` | Knocked down on floor | Vulnerable, must get up |
| `NEUTRAL_GETUP` / `GETUP_ATTACK` | Getting up from floor | Can be punished |
| `NEUTRAL_TECH` / `FORWARD_TECH` / `BACKWARD_TECH` | Quick recovery from impact | Hard to punish |
| `TECH_MISS_DOWN` | Failed tech | Stuck on floor, very vulnerable |
| `NAIR` / `FAIR` / `BAIR` / `UAIR` / `DAIR` | Aerial attacks | Neutral/forward/back/up/down air |
| `NEUTRAL_ATTACK_1` | Jab | Quick ground poke |
| `FSMASH_MID` | Forward smash | Strong horizontal kill move |
| `UPSMASH` | Up smash | Strong vertical kill move |
| `DOWNSMASH` | Down smash | Hits both sides |
| `DASH_ATTACK` | Dash attack | Attacking while running |
| `GROUND_ATTACK_UP` | Up tilt | Quick anti-air |
| `DEAD_DOWN` / `DEAD_LEFT` / `DEAD_RIGHT` / `DEAD_UP` | Dead | Lost a stock |
| `DEAD_FALL` | Falling off stage, dying | About to lose a stock |
| `ENTRY_START` / `ENTRY_END` | Spawning | Invincible briefly after respawn |

Check actions like:
```python
if me.action == melee.Action.SHIELD:
    # I am shielding - maybe shield-grab?
    ...
if opp.action == melee.Action.TUMBLING:
    # Opponent is in hitstun - chase and follow up!
    ...
if opp.action == melee.Action.LYING_GROUND_DOWN:
    # Opponent missed a tech - punish!
    ...
```

## 4-player free-for-all awareness

This platform runs 4-player matches, not 1v1. Key differences:

- There are 3 opponents, not 1. Don't tunnel-vision one player.
- Consider targeting the player with the highest percent (easiest to kill).
- Or target the nearest player (simplest approach logic).
- Be aware of your positioning relative to ALL players - you can be hit from
  behind while attacking someone else.
- Stage control is harder with 4 players. The center of the stage is more
  dangerous because attacks can come from any direction.
- Sometimes the best play is to let other players fight each other and pick
  off the winner.

## Frame budget

`act()` is called ~60 times per second. Your function must return quickly
(under ~16ms). Do not:
- Sleep or wait
- Do network or filesystem I/O
- Run expensive loops or computations
- Import modules inside act()

Do:
- Keep state in `__init__` (frame counters, cooldowns, etc.)
- Use simple arithmetic and comparisons
- Return a dict as fast as possible
