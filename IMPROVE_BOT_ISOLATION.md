# Improving Bot Isolation

Status: proposal. Not yet implemented. The current defense is `core/bot_validator.py`
(static AST checks) enforced at `core/bot_loader.py` (the single execution
choke point). This document describes the stronger, runtime isolation we should
build next.

---

## Why the static validator is not enough

User bots run **in-process** in the game loop. `BotLoader._import` calls
`spec.loader.exec_module(mod)`, so uploaded code executes with the full
privileges of the server process: the same filesystem access, the same network,
the same ability to read the WireGuard config, or the Melee
ISO, and the same ability to kill the match.

The AST validator (`core/bot_validator.py`) is a **denylist**, and a denylist of
a Turing-complete language is a losing game:

- It cannot enumerate every reflection path. We already closed one
  (`gi_frame.f_builtins["__import__"]`); the general class of
  "reach a frame, reach builtins, subscript your way out" keeps producing new
  variants (coroutine frames, traceback frames, C-level helpers exposed by
  future stdlib versions).
- It cannot reason about **runtime behavior at all**. It is a static check, so
  it is blind to:
  - **Infinite loops / CPU exhaustion** — `while True: pass` inside `act()`
    stalls the 60fps loop thread. Per the game-loop rules, `act()` runs every
    frame; one slow bot degrades or freezes every match.
  - **Memory bombs** — `[0] * 10**10` OOMs the whole server.
  - **Blocking calls** — a bot that blocks (even accidentally) hangs the loop.

Static validation should stay as cheap, fail-fast defense-in-depth. But the
**security and availability boundary must be a runtime sandbox**, because that
is the only thing that can bound what code *does*, not just what it *says*.

---

## Recommended approach: run each bot in a locked-down subprocess

Move bot execution out of the server process entirely. Each bot runs in its own
child process with dropped privileges and hard resource limits. The parent
(orchestrator) speaks to it over a pipe: it sends a serialized `GameState`
snapshot each frame and receives an action dict (or a timeout/crash signal)
back. Nothing the bot does can touch the parent's memory, and if it misbehaves
the parent simply kills it and falls back to the LLM / neutral input.

```
[orchestrator, 60fps loop]                 [bot worker process, per port]
        │  frame N: gamestate  ──────────►  deserialize
        │                                   bot.act(gamestate, port)
        │  action / timeout / crash  ◄────  serialize action
        │
   apply to controller, or
   fall back on timeout/crash
```

### Process model

- **One worker per player port** (up to 4). Spawn on match start, tear down on
  match end. Keep them warm across frames — do **not** fork per frame (fork cost
  would blow the frame budget).
- Use `multiprocessing` with the **`spawn`** start method (not `fork`) so the
  child does not inherit the parent's imported modules, open file descriptors,
  or loaded secrets. A `spawn` child starts from a clean interpreter.
- The worker's entry point: apply the sandbox (below), import the validated bot
  module, then loop reading frames from a `multiprocessing.Connection` pipe.

### Per-frame deadline (this is the availability fix)

The orchestrator must never block on a bot. Give each `act()` call a hard wall
clock budget (well under one frame, e.g. **5 ms**, tunable):

- Send the frame, then `conn.poll(timeout=0.005)`.
- If no result arrives in time, **use neutral input for that frame** and mark
  the bot "slow." After K consecutive misses, kill the worker and fall back to
  the LLM path for that port (the orchestrator already has a fallback when a bot
  is absent).
- A killed/crashed worker never stalls the loop — the timeout is enforced by the
  *parent*, so even a `while True` in the child is harmless.

Do **not** try to enforce this with in-process threads and `KeyboardInterrupt`:
Python cannot reliably interrupt a tight C loop or a bare `while True` in a
thread. Process boundaries + `kill` are the only robust mechanism.

### Resource limits (POSIX `resource`, set in the child before running bot code)

In the worker, before importing the bot, drop limits with `setrlimit`:

| Limit | rlimit | Purpose |
|---|---|---|
| CPU seconds | `RLIMIT_CPU` | Backstop against runaway compute (SIGXCPU) |
| Address space | `RLIMIT_AS` | Cap memory; defeats `[0]*10**10` |
| File size | `RLIMIT_FSIZE` (0) | Bot cannot write files |
| Open files | `RLIMIT_NOFILE` (low) | Limit fd exhaustion |
| Core dump | `RLIMIT_CORE` (0) | No core dumps leaking memory to disk |

```python
import resource
resource.setrlimit(resource.RLIMIT_AS,    (256 * 1024**2,) * 2)  # 256 MB
resource.setrlimit(resource.RLIMIT_CPU,   (2, 2))                # 2s CPU backstop
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))                # no file writes
resource.setrlimit(resource.RLIMIT_CORE,  (0, 0))
```

### Filesystem and environment

- Start the worker with `cwd` set to an empty scratch dir it cannot escape, and
  a scrubbed `env` (drop everything; the bot needs nothing from the environment).
- `RLIMIT_FSIZE = 0` already prevents writes. For read isolation, the strongest
  option on this Mac without containers is to run the worker as a **dedicated
  low-privilege macOS user** that has no read access to the repo secrets
  (`config/twitch.key`, `config/wireguard/`, the ISO). Create the user once,
  `os.setuid`/`setgid` to it in the child (parent must launch as root or use a
  pre-created setuid helper — evaluate whether that is worth it for a
  home setup).

### Network isolation

`RLIMIT` cannot block sockets. Options, strongest first:

1. **Seccomp-style syscall filtering** — not available natively on macOS.
2. **macOS sandbox profile** (`sandbox-exec` / `sandbox_init`) — wrap the worker
   in a profile that denies `network*` and file writes. Deprecated but still
   functional; the pragmatic choice on macOS.
3. **Run the workers inside a container** (e.g. Docker/Podman) with
   `--network none`, a read-only rootfs, `--memory`, `--cpus`, and a non-root
   user. This gives network, filesystem, and resource isolation in one place and
   is portable off the Mac. **This is the cleanest target if we are willing to
   containerize bot execution.**

### Serialization boundary

Do not pickle the live `melee.GameState` across the pipe (pickle of arbitrary
objects is itself an execution risk, and it couples the child to the parent's
object graph). Instead pass a **plain-dict snapshot** of only the fields bots
need (positions, stocks, percents, actions, character, port). The worker
reconstructs a lightweight read-only view. The return value is a small,
validated action dict (`stick_x`, `stick_y`, `buttons`) — clamp and type-check
it in the parent before applying, so a malformed return can never reach libmelee.

---

## Alternatives considered

- **RestrictedPython** — a more principled in-process static/bytecode
  restriction than our AST denylist. Still in-process, still no runtime resource
  bounds, and its guarantees are narrower than they appear. Useful only as an
  incremental hardening of the static layer, not as the boundary.
- **Subinterpreters (PEP 554 / 3.12+)** — better isolation of module state, but
  still share the process, so no memory/CPU limits and a crash can still take
  down the host. Not a security boundary today.
- **WASM (e.g. Pyodide/wasmtime)** — strong sandbox, but bots would lose direct
  `libmelee` access and we would need a marshalling layer; heavy for this use
  case.
- **gVisor / Firecracker microVM** — strongest isolation, overkill for a
  self-hosted MacBook setup and not native to macOS.

**Recommendation:** implement the **spawn-based subprocess-per-port worker with a
per-frame deadline and `setrlimit`** first — it retires the RCE-via-reflection
risk and the DoS/hang risk in one move, using only the stdlib. Layer network
isolation on top via either a macOS sandbox profile or (preferred long-term)
running the workers in a `--network none` container.

---

## Rollout sketch

1. Define the frame snapshot + action schema (plain dicts) and a strict action
   validator/clamp in the parent.
2. Write the worker entry point: `setrlimit`, scrub env, `chdir` to scratch,
   import the (already statically validated) bot, then the read-eval-reply loop.
3. Switch `BotLoader` / the orchestrator to spawn a worker per port instead of
   calling `bot.act()` in-process; enforce the per-frame `poll` deadline and the
   consecutive-miss fallback.
4. Add network isolation (sandbox profile or container).
5. Keep `core/bot_validator.py` as the fast pre-flight check — reject obviously
   bad uploads before we bother spawning a worker.

Keep the static validator; it is cheap and catches the obvious cases early. The
subprocess sandbox is what makes running untrusted bot code actually safe.
