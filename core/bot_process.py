"""Parent-side manager for one subprocess bot worker.

A `BotWorker` owns a single subprocess running core/bot_worker.py and exposes
a synchronous `act(snapshot, port) -> Optional[dict]` with a hard wall-clock
deadline enforced via select() on the child's stdout pipe. A stuck or slow bot
therefore cannot stall the 60fps game loop: the orchestrator runs act() inside
`asyncio.to_thread`, and on a deadline miss it just gets None (neutral input
for that frame).

Failure handling:
- If the child crashes (exits, EOF on stdout, malformed JSON, broken pipe),
  the worker is marked dead and act() returns None forever after. The
  orchestrator is expected to fall back to the trusted in-process default
  bot for that port once it sees is_dead.
- After `max_misses` consecutive deadline misses, the child is killed and
  marked dead (the bot is too slow to be useful).

Hot-reload:
- On each act() call the bot file's mtime is checked. If it changed, the
  child is killed and respawned with the new code. The first frame after a
  respawn will likely miss (cold Python startup) - acceptable for an edit
  event.

The child is launched with a scrubbed env and cwd=scratch (see _scrub_env
and the scratch_dir argument). rlimits are applied inside the child before
it imports the bot module; the parent does not need privileges for that.
"""

import json
import logging
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from core.frame import clamp_action

log = logging.getLogger(__name__)

# Path to the worker entry point (sibling of this file in core/).
WORKER_SCRIPT = Path(__file__).resolve().parent / "bot_worker.py"

# Environment variables the worker is allowed to inherit. We keep PATH (in
# case the bot shells out, though the AST validator already blocks that), HOME
# (stdlib bits), and locale/timezone. Everything else - Twitch keys, WireGuard
# config env, OLLAMA URLs, etc. - is dropped so the bot cannot read it.
# One-time cold-start grace: until the worker produces its first successful
# response, _recv is given this deadline instead of the tight per-frame one.
# Python subprocess startup plus `import melee` is ~130ms on this Mac, so the
# 10ms frame deadline would otherwise trip max_misses before the bot ever had
# a chance to respond. See BotWorker._recv and the `_is_warmed` flag.
_COLD_START_BUDGET_S = 2.0

_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
    }
)


def _scrub_env() -> dict:
    """Return a copy of os.environ with everything not in the allowlist dropped.

    The worker child inherits only the bare minimum it needs to import melee
    from the venv's site-packages. Every secret the server might have in its
    environment - Twitch keys, OLLAMA URLs, WireGuard config env, shell
    history refs, anything a `os.environ` walk could exfiltrate - is gone.
    """
    return {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}


class WorkerDead(Exception):
    """Internal: the child has exited, EOF'd, or sent malformed output."""


class BotWorker:
    """Owns one sandboxed subprocess running a single bot file.

    Not thread-safe: call act() from one thread at a time. The orchestrator
    does this by running each port's worker in its own asyncio.to_thread call.
    """

    def __init__(
        self,
        bot_path: Path,
        scratch_dir: Path,
        deadline_s: float = 0.010,
        max_misses: int = 3,
    ):
        self.bot_path = Path(bot_path)
        self.scratch_dir = Path(scratch_dir)
        self.deadline_s = float(deadline_s)
        self.max_misses = int(max_misses)

        self._proc: Optional[subprocess.Popen] = None
        self._stdout_fd: Optional[int] = None
        self._read_buf: bytes = b""
        self._bot_mtime: float = 0.0
        self._misses: int = 0
        self.is_dead: bool = False
        # Whether the worker has produced at least one successful response
        # since the last spawn. Until that happens, the first _recv is given
        # a cold-start grace deadline (_COLD_START_BUDGET_S) instead of the
        # tight per-frame deadline, because Python subprocess startup plus
        # importing melee (~130ms on this Mac) would otherwise blow the 10ms
        # deadline and trip max_misses before the bot ever had a chance.
        # In production the worker is spawned at queue_match time (during
        # CSS navigation) and the first act() happens seconds later when
        # IN_GAME starts, so the worker is already warm. The grace only
        # matters for cold respawns (hot-reload) or tight unit tests.
        self._is_warmed: bool = False
        # frame id of the snapshot most recently sent via _send(); used by
        # _recv to discard stale responses.
        self._current_frame: object = None
        self._env = _scrub_env()

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def spawn(self) -> bool:
        """Start the subprocess. Returns True on success, False (and marks
        the worker dead) on failure."""
        try:
            self._bot_mtime = self.bot_path.stat().st_mtime
        except OSError as exc:
            log.error("BotWorker: bot file missing %s: %s", self.bot_path, exc)
            self.is_dead = True
            return False
        try:
            self.scratch_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("BotWorker: scratch dir unusable %s: %s", self.scratch_dir, exc)
            self.is_dead = True
            return False
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-u", str(WORKER_SCRIPT), str(self.bot_path)],
                cwd=str(self.scratch_dir),
                env=self._env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,  # inherit parent stderr for debug visibility
                bufsize=0,
                close_fds=True,
            )
        except OSError as exc:
            log.error("BotWorker spawn failed for %s: %s", self.bot_path.name, exc)
            self.is_dead = True
            return False
        self._stdout_fd = self._proc.stdout.fileno()
        self._read_buf = b""
        self._misses = 0
        self._is_warmed = False  # cold start: first _recv gets the grace budget
        self.is_dead = False
        log.info("BotWorker spawned pid=%d for %s", self._proc.pid, self.bot_path.name)
        return True

    def close(self):
        """Kill the subprocess if alive and close our pipe ends."""
        proc = self._proc
        if proc is None:
            self._stdout_fd = None
            self._read_buf = b""
            return
        try:
            if proc.poll() is None:
                proc.kill()
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except Exception:
                    pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        except Exception:
            pass
        finally:
            self._proc = None
            self._stdout_fd = None
            self._read_buf = b""

    # ------------------------------------------------------------------ #
    #  Per-frame call                                                     #
    # ------------------------------------------------------------------ #

    def act(self, snapshot: dict, port: int) -> Optional[dict]:
        """Send a frame snapshot and wait up to deadline_s for the action.

        Returns a clamped action dict, or None on:
        - deadline miss (neutral input for this frame)
        - child crash / EOF / malformed response (worker marked dead)
        - worker already dead (caller should fall back)

        `port` is the player port (1-4) the bot is controlling this frame;
        it is forwarded to the worker as part of the snapshot so the bot's
        act(gamestate, port) signature still matches the in-process contract.
        """
        if self.is_dead:
            return None

        # Hot-reload: respawn if the bot file changed on disk.
        if self._bot_file_changed():
            log.info("BotWorker: bot file changed - respawning %s", self.bot_path.name)
            self.close()
            if not self.spawn():
                return None  # spawn failure already marked us dead
            # First frame after a cold respawn will likely miss the deadline.
            # That's fine; the next frame's response is back to normal.

        # Child crashed without us noticing (e.g. import-time exception)?
        if self._proc is None or self._proc.poll() is not None:
            self._record_miss()
            return None

        try:
            self._send(snapshot, port)
            return self._recv(self.deadline_s)
        except WorkerDead:
            self._mark_dead()
            return None
        except (BrokenPipeError, OSError):
            self._mark_dead()
            return None

    # ------------------------------------------------------------------ #
    #  Internals                                                          #
    # ------------------------------------------------------------------ #

    def _bot_file_changed(self) -> bool:
        try:
            m = self.bot_path.stat().st_mtime
        except OSError:
            return False
        if m != self._bot_mtime:
            self._bot_mtime = m
            return True
        return False

    def _record_miss(self):
        self._misses += 1
        if self._misses >= self.max_misses:
            self._mark_dead()

    def _mark_dead(self):
        if self.is_dead:
            return
        log.warning(
            "BotWorker for %s marked dead after %d consecutive misses",
            self.bot_path.name,
            self._misses,
        )
        self.close()
        self.is_dead = True

    def _send(self, snapshot: dict, port: int):
        """Write one frame snapshot to the child's stdin. Raises WorkerDead
        on broken pipe (child gone)."""
        # Stash the frame id so _recv can discard stale responses (a slow
        # child may flush an old reply just as we send the next frame).
        self._current_frame = snapshot.get("frame")
        msg = dict(snapshot, port=port)
        data = (json.dumps(msg) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise WorkerDead()

    def _recv(self, deadline_s: float) -> Optional[dict]:
        """Read one response line within deadline_s. Returns a clamped action
        dict on success, None on timeout. Raises WorkerDead on EOF or
        malformed output.

        Frame-id matching: if the child returns a response for a stale frame
        (we sent a new one before the old reply landed), the stale line is
        drained and we keep waiting. This handles the case where a slow
        child finally replied after we already timed out and sent the next
        frame - we never apply a stale action.
        """
        # Cold-start grace: until the worker's first successful response, give it
        # up to _COLD_START_BUDGET_S instead of the tight per-frame deadline,
        # so Python startup + melee import doesn't immediately trip max_misses.
        effective_deadline = (
            deadline_s if self._is_warmed else max(deadline_s, _COLD_START_BUDGET_S)
        )
        end = time.monotonic() + effective_deadline
        while True:
            # Have we already buffered a full line?
            idx = self._read_buf.find(b"\n")
            if idx != -1:
                line = self._read_buf[:idx]
                self._read_buf = self._read_buf[idx + 1 :]
                try:
                    resp = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.warning(
                        "BotWorker: malformed response from %s: %r",
                        self.bot_path.name,
                        line[:80],
                    )
                    raise WorkerDead()
                # Drain stale responses (frame id mismatch) - they belong to
                # a frame we already timed out on. Keep reading.
                if resp.get("frame") != self._current_frame:
                    continue
                # A bot that raised an exception (or whose gamestate failed to
                # reconstruct) is flagged by the worker with error=True.
                # Distinguish this from a deliberate `return None` (release_all)
                # by counting errors toward the dead-worker threshold, just like
                # a deadline miss; a perpetually crashing bot falls back to the
                # default after K consecutive errors rather than standing still
                # silently.
                if resp.get("error"):
                    self._record_miss()
                    return None  # neutral input this frame (release_all)
                self._misses = 0
                self._is_warmed = True  # subsequent calls get the tight deadline
                return clamp_action(resp.get("action"))

            remaining = end - time.monotonic()
            if remaining <= 0:
                self._record_miss()
                return None  # deadline miss -> neutral input this frame

            try:
                ready, _, _ = select.select([self._stdout_fd], [], [], remaining)
            except (OSError, ValueError):
                raise WorkerDead()
            if not ready:
                continue  # select timed out; loop will hit `remaining <= 0`
            try:
                chunk = os.read(self._stdout_fd, 4096)
            except OSError:
                raise WorkerDead()
            if not chunk:
                # EOF: child exited. Surface as dead.
                raise WorkerDead()
            self._read_buf += chunk
