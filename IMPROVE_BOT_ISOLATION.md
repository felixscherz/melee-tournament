# Improving Bot Isolation

Status: **implemented (v1)**. The subprocess sandbox (`core/bot_process.py` +
`core/bot_worker.py` + `core/frame.py`) is live. The static AST validator
(`core/bot_validator.py`) stays as the cheap pre-flight check at `/api/start`
and inside `BotLoader.load()`; it is defense-in-depth, not the boundary. The
items still outstanding are tagged **[TODO]** inline below.

---

## What v1 implements (the security and availability boundary)

- **One subprocess per port** (`core/bot_process.BotWorker`), spawned in
  `MeleeOrchestrator.queue_match` and torn down on match end / abort. Warm
  across frames (kept alive for the duration of a match).
- **JSON over stdio IPC** (not `multiprocessing.Pipe`). Pickle is itself an
  execution risk and would couple the child to the parent's object graph;
  JSON is debuggable (`echo '{"frame":1,...}' | python core/bot_worker.py
  bot.py`) and language-agnostic for the future. The payload is a plain-dict
  snapshot of only the documented bot-interface fields (`position.x/y`,
  `stock`, `percent`, `action`, `character`, `facing`) - exactly the surface
  `core/test_bot.py` already mocks with `types.SimpleNamespace`, so every
  existing bot and the test harness keep working unchanged.
- **`setrlimit` in the child before importing the bot** (`core/bot_worker._apply_limits`):
  `RLIMIT_CPU=3s`, `RLIMIT_FSIZE=0`, `RLIMIT_NOFILE=16`, `RLIMIT_CORE=0`,
  `RLIMIT_AS=256MB` (best-effort on macOS - see the inline notes). This is
  what defeats `while True`, `[0]*10**10`, file writes, and fd exhaustion.
- **Scrubbed environment + `cwd=scratch`** (`core/bot_process._scrub_env`):
  the child inherits only `PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`,
  `TZ`. Twitch keys, WireGuard config env, OLLAMA URLs, and everything else
  the server might have in its environment are dropped.
- **Per-frame deadline (10ms)** enforced by the *parent* via
  `select.select([fd], [], [], deadline)`. A stuck bot can never stall the
  60fps loop. On miss: neutral input (`release_all`) for that frame. After
  `max_misses` (3) consecutive misses, the worker is killed and marked dead.
- **Error-flag protocol**: the worker reports `{"frame":N,"action":null,
  "error":true}` when `bot.act()` raises, distinguishing a crash from a
  deliberate `return None` (release_all). Consecutive errors count toward
  the same dead-worker threshold as deadline misses, so a perpetually
  crashing bot falls back instead of silently standing still.
- **Trusted in-process fallback** (`core/roster.default_bot_path` +
  `MeleeOrchestrator._fallback_action_sync`): when a worker dies, that
  port's `core/bots/<char>.py` (or `generic.py`) is loaded once per match
  via `BotLoader` and called in-process with the live `GameState`. Trusted
  code we shipped, no sandbox needed; the character keeps playing simple AI
  for the rest of the match.
- **Hot-reload via parent-side mtime check + respawn**: editing a bot file
  on disk causes the next `BotWorker.act()` call to kill and respawn the
  child. The first post-respawn frame will likely miss the deadline (cold
  Python startup); subsequent frames are back to normal. The respawn itself
  is bounded by the same `_COLD_START_BUDGET_S` grace as the initial spawn
  (see below).
- **Cold-start grace**: until the worker produces its first successful
  response, `_recv` is given `_COLD_START_BUDGET_S = 2s` instead of the
  tight 10ms deadline. Python subprocess startup + `import melee` is
  ~130ms on this Mac, which would otherwise trip `max_misses` before the
  bot ever had a chance. In production the worker is spawned at
  `queue_match` time (during CSS navigation) and the first `act()` happens
  seconds later when `IN_GAME` starts, so the grace rarely matters - but
  it makes hot-reload respawns and tight unit tests robust.
- **Action clamping** (`core/frame.clamp_action`): every action coming back
  from a worker is type-checked and clamped (sticks to `[0.0, 1.0]`,
  buttons normalized to exactly the seven required keys with bool values)
  before it reaches libmelee via `_apply`. A malformed return can never
  reach the controller.
- **Frame-id matching in `_recv`**: stale responses from a previous frame
  are drained, not applied. If a slow child finally replies after we
  already timed out and sent the next frame, we never apply a stale
  action.

## What v1 does NOT do yet (TODO)

These are layered on top of the v1 boundary without rearchitecting:

- **Network isolation.** `RLIMIT` cannot block sockets. The next layer is
  either a macOS `sandbox-exec` profile that denies `network*` and file
  writes outside scratch, or (preferred long-term) running the workers in a
  `--network none` container with read-only rootfs, `--memory`, `--cpus`,
  and a non-root user.
- **Read-isolation.** `RLIMIT_FSIZE=0` blocks the *write* (`write(2)` raises
  `EFBIG`), but `open(...,"w")` itself can create an empty file in any
  directory the parent process can write (e.g. `/tmp`). For full read
  isolation we'd need a dedicated low-privilege macOS user (`os.setuid` in
  the child) with no read access to the repo secrets, or the same container
  / sandbox profile as above. The current set is the right v1: it defeats
  RCE-via-reflection (separate address space) and CPU/mem/file-write DoS,
  which is what the AST validator could never do.
- **Dedicated low-priv user.** Skipped for v1 (a `setuid` helper or
  root-launched parent is a big lift for a home setup).

---

## Why the static validator is not enough (still true - it stays as pre-flight)

User bots run **out of process** now (in `core/bot_worker.py`), so the
in-process RCE-via-reflection risk is gone. But the validator was already
not enough even before that, for the reasons below, and stays as a fast
fail-fast check at upload time so we don't bother spawning a worker for
obviously bad code:

- It cannot enumerate every reflection path. We already closed one
  (`gi_frame.f_builtins["__import__"]`); the general class of
  "reach a frame, reach builtins, subscript your way out" keeps producing new
  variants (coroutine frames, traceback frames, C-level helpers exposed by
  future stdlib versions).
- It cannot reason about **runtime behavior at all**. It is a static check,
  so it is blind to:
  - **Infinite loops / CPU exhaustion** - `while True: pass` inside `act()`
    stalls the decision thread. The per-frame parent deadline now bounds
    this; the validator alone couldn't.
  - **Memory bombs** - `[0] * 10**10` no longer OOMs the server; the
    subprocess's `RLIMIT_AS` (where macOS lets us set it) caps it, and even
    without that the parent just kills the child after K misses.
  - **Blocking calls** - a bot that blocks (even accidentally) is now
    bounded by the 10ms parent deadline.

The **security and availability boundary is the runtime sandbox** because
that is the only thing that can bound what code *does*, not just what it
*says*. The static validator catches the obvious cases early; the sandbox
catches everything the validator can't.

---

## Recommended approach: run each bot in a locked-down subprocess

This is now implemented. The description below stays as the design record.

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

1. **[DONE]** Define the frame snapshot + action schema (plain dicts) and a strict
   action validator/clamp in the parent. (`core/frame.py`.)
2. **[DONE]** Write the worker entry point: `setrlimit`, scrub env (the env scrub
   is at Popen time in the parent), `chdir` to scratch (also at Popen time),
   import the (already statically validated) bot, then the read-eval-reply
   loop. (`core/bot_worker.py`.)
3. **[DONE]** Switch the orchestrator to spawn a worker per port instead of calling
   `bot.act()` in-process; enforce the per-frame `poll` deadline and the
   consecutive-miss fallback to the trusted in-process default bot.
   (`core/bot_process.BotWorker`, `MeleeOrchestrator.queue_match` +
   `_decision_loop`.)
4. **[TODO]** Add network isolation (sandbox profile or container).
5. **[DONE]** Keep `core/bot_validator.py` as the fast pre-flight check - reject
   obviously bad uploads before we bother spawning a worker. (Still called in
   `frontend/app.py:_resolve_bot_path` for pasted code and in
   `core/bot_loader.py:load()` for defense-in-depth.)

The static validator stays; it is cheap and catches the obvious cases early.
The subprocess sandbox is what makes running untrusted bot code actually safe.
