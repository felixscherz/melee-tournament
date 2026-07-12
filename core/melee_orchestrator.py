"""
Async 60fps Melee game loop.

Dolphin is launched ONCE at server startup and kept alive permanently.
OBS captures the same stable window across all matches.

Flow:
  launch()       — start Dolphin, connect controllers, spin up the game loop task
  queue_match()  — set pending player configs; the game loop picks them up at CSS
  stop()         — shut everything down
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

import melee
import melee.enums

from core.bot_loader import BotLoader
from core.bot_process import BotWorker
from core.frame import clamp_action, frame_snapshot
from core.game_state import AppState, Phase, PlayerConfig, PlayerScore
from core.roster import CHARACTER_MAP, default_bot_path

log = logging.getLogger(__name__)

STAGE = melee.Stage.FINAL_DESTINATION

# Offline Melee VS mode has no controller-driven quit: L+R+A+Start (LRAS) only
# returns to CSS in Slippi *online* play, and libmelee exposes no reset. So to
# end a running match we force it the only reliable way — drive every bot off
# the stage until one player is left and Melee declares GAME on its own. The
# normal postgame -> CSS navigation then idles at the menus. See
# _drive_self_destruct / abort_match.

# Character shown while idling at CSS with no match queued (see _handle_frame).
IDLE_CHARACTER = melee.Character.FOX
PORTS = [1, 2, 3, 4]


class MeleeOrchestrator:
    def __init__(self, config: dict, state: AppState):
        self.cfg = config
        self.state = state
        self.console: Optional[melee.Console] = None
        self._controllers: dict[int, melee.Controller] = {}
        # Subprocess sandbox workers for user bot code. One per port, spawned
        # in queue_match, torn down on match end / abort. See
        # docs/IMPROVE_BOT_ISOLATION.md and core/bot_process.py.
        self._bot_workers: dict[int, BotWorker] = {}
        # Trusted in-process fallbacks (core/bots/<char>.py), used when the
        # subprocess worker for a port dies or hits the deadline too many
        # times in a row. Run in-process: they are our code, not user code.
        self._fallback_loaders: dict[int, BotLoader] = {}
        self._actions: dict[int, Optional[dict]] = {p: None for p in PORTS}
        self._lock = asyncio.Lock()
        self._menu_helpers = {p: melee.MenuHelper() for p in PORTS}
        self._pending_players: Optional[list[PlayerConfig]] = None
        self._active_players: Optional[list[PlayerConfig]] = None
        self._latest_gs: Optional[melee.GameState] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._prev_menu: Optional[melee.Menu] = None
        # When True, the game loop ignores bots and drives every controller off
        # the stage to force the current match to end (see abort_match /
        # _drive_self_destruct). Cleared once we reach the postgame screen.
        self._force_end = False
        # Bot sandbox tunables (see [bots] in config/settings.toml).
        bot_cfg = config.get("bots") or {}
        self._bot_deadline_s = float(bot_cfg.get("deadline_ms", 10)) / 1000.0
        self._bot_max_misses = int(bot_cfg.get("max_misses", 3))
        repo_root = Path(__file__).resolve().parent.parent
        self._scratch_dir = Path(
            bot_cfg.get("scratch_dir", str(repo_root / ".bot_scratch"))
        )

    # ------------------------------------------------------------------ #
    #  Public API                                                           #
    # ------------------------------------------------------------------ #

    async def launch(self):
        """Start Dolphin once and keep it running. Called at server startup."""
        import subprocess as _sp

        try:
            result = _sp.run(
                ["lsof", "-ti", f":{self.cfg['dolphin']['port']}"],
                capture_output=True,
                text=True,
            )
            for pid_str in result.stdout.split():
                pid = int(pid_str.strip())
                log.info(
                    "Killing stale Dolphin on port %s (pid %d)",
                    self.cfg["dolphin"]["port"],
                    pid,
                )
                _sp.run(["kill", "-9", str(pid)])
        except Exception as exc:
            log.debug("Port cleanup skipped: %s", exc)

        self.console = melee.Console(
            path=self.cfg["dolphin"]["path"],
            slippi_address="127.0.0.1",
            slippi_port=self.cfg["dolphin"]["port"],
            blocking_input=False,
            polling_mode=False,
            fullscreen=False,
        )
        for port in PORTS:
            self._controllers[port] = melee.Controller(
                console=self.console,
                port=port,
                type=melee.ControllerType.STANDARD,
            )

        self.console.run(iso_path=self.cfg["dolphin"]["iso"])
        log.info("Connecting to Dolphin...")
        if not self.console.connect():
            raise RuntimeError("Could not connect to Dolphin")
        for ctrl in self._controllers.values():
            ctrl.connect()
        log.info("Dolphin connected — game loop starting")

        self._loop_task = asyncio.create_task(self._game_loop())

    def queue_match(self, players: list[PlayerConfig]):
        """Queue a new match. Picked up by the game loop at the next CSS frame."""
        self._teardown_workers()
        self._bot_workers = {}
        self._fallback_loaders = {}
        for p in players:
            worker = BotWorker(
                bot_path=p.bot_path,
                scratch_dir=self._scratch_dir,
                deadline_s=self._bot_deadline_s,
                max_misses=self._bot_max_misses,
            )
            worker.spawn()
            self._bot_workers[p.port] = worker
            # Trusted in-process fallback for this port's character - loaded
            # once per match, used only if the subprocess worker dies.
            fallback = BotLoader(upload_dir=Path("uploads"))
            fallback.load(default_bot_path(p.character))
            self._fallback_loaders[p.port] = fallback
            self._actions[p.port] = None

        self.state.phase = Phase.STARTING
        self.state.players = players
        self.state.scores = {
            p.port: PlayerScore(port=p.port, name=p.name, character=p.character)
            for p in players
        }
        self.state.winner = None
        self._pending_players = players
        # Fresh MenuHelper instances so prior CSS state doesn't carry over
        self._menu_helpers = {p: melee.MenuHelper() for p in PORTS}

    def abort_match(self):
        """End the current match and return to the lobby.

        Offline Melee can't be quit with a controller, so if a match is live we
        set _force_end and let the game loop drive every bot off the stage until
        Melee declares GAME on its own; the postgame -> CSS navigation then
        idles at the menus. _active_players is kept so the loop knows which
        ports to drive and can detect the natural end — shared web state is
        reset to IDLE once we reach the postgame screen. If nothing is live,
        reset now. Dolphin stays alive (OBS keeps capturing the same window).
        """
        in_game = (
            self._latest_gs is not None
            and self._latest_gs.menu_state == melee.Menu.IN_GAME
        )
        self._pending_players = None
        if in_game:
            self._force_end = True
            # _drive_self_destruct ignores bots and writes controllers
            # directly, so the subprocess workers are no longer needed.
            self._teardown_workers()
            return
        self._force_end = False
        self._active_players = None
        self._teardown_workers()
        self._actions = {p: None for p in PORTS}
        self.state.reset()

    def stop(self):
        if self._loop_task:
            self._loop_task.cancel()
        self._teardown_workers()
        if self.console:
            try:
                self.console.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Game loop                                                            #
    # ------------------------------------------------------------------ #

    async def _game_loop(self):
        """Runs forever — handles menus and drives bot actions each frame.

        console.step() blocks until Dolphin emits the next frame, so it is the
        60fps pacer. No extra sleep: if we ever fall behind, step() returns
        buffered frames instantly and the backlog drains. Sleeping here would
        pin us one frame behind forever, and stale cursor positions make the
        CSS navigation overshoot and oscillate.
        """
        while True:
            try:
                # step() is a blocking socket read — keep it off the event
                # loop thread so the web server and game loop don't starve
                # each other.
                gs = await asyncio.to_thread(self.console.step)
            except Exception as exc:
                log.error("console.step() error: %s", exc)
                await asyncio.sleep(0.1)
                continue

            if gs is None:
                await asyncio.sleep(0.001)
                continue

            self._latest_gs = gs

            try:
                await self._handle_frame(gs)
            except Exception as exc:
                log.error("Frame handler error: %s", exc, exc_info=True)

    def _drive_self_destruct(self, gs: melee.GameState):
        """Force the match to end by walking every fighter off the stage.

        Offline Melee has no controller quit, so each frame we push every port
        full-tilt toward the blast edge it is already closest to (nothing else
        held) — they walk or fall off and lose stocks until one player is left
        and Melee declares GAME. Continuous horizontal input also peels anyone
        who happens to grab a ledge straight back off it.
        """
        for port, ctrl in self._controllers.items():
            ctrl.release_all()
            p = gs.players.get(port)
            if p is None:
                continue
            toward = 1.0 if p.position.x >= 0 else 0.0
            ctrl.tilt_analog(melee.Button.BUTTON_MAIN, toward, 0.5)

    @staticmethod
    def _tap_b(ctrl: melee.Controller):
        """Press B as a clean single tap (alternating press/release frames).

        Never *hold* B: held down it backs all the way out of a menu (CSS ->
        main menu). Alternating gives a fresh press edge every other frame,
        which is what Melee reads as a discrete B press.
        """
        if ctrl.prev.button[melee.Button.BUTTON_B]:
            ctrl.release_button(melee.Button.BUTTON_B)
        else:
            ctrl.release_all()
            ctrl.press_button(melee.Button.BUTTON_B)

    def _park_idle(self, gs: melee.GameState):
        """Idle at CSS with all four controllers present but nobody locked in.

        Called every CSS frame when no match is queued (fresh boot, or right
        after a match ends). We never press A or START here, so the game can't
        leave CSS until the lobby queues a match. Two things to keep clean:

        - A cursor that drifted onto the CPU-level slider reports
          is_holding_cpu_slider; release_all drops the grab.
        - Returning from a match restores the previous CSS with every token
          still placed (coin_down). Tap B to reclaim each coin so nobody is
          committed.
        """
        for port, ctrl in self._controllers.items():
            p = gs.players.get(port)
            if p is None or p.is_holding_cpu_slider:
                ctrl.release_all()
                continue
            if p.coin_down:
                self._tap_b(ctrl)
                continue
            ctrl.release_all()

    async def _handle_frame(self, gs: melee.GameState):
        menu = gs.menu_state

        # A forced end (admin "END MATCH") leaves IN_GAME the instant Melee ends
        # the match — and it drops us straight back at CHARACTER_SELECT, NOT
        # POSTGAME_SCORES (confirmed from logs). Clear the force-end state as
        # soon as we're out of the game at any menu; otherwise _active_players
        # lingers and the CSS/stage branches below re-select the same roster and
        # drive us right back into a new match (the "stuck at stage select" bug).
        if self._force_end and menu != melee.Menu.IN_GAME:
            log.info(
                "Forced end complete at %s — resetting to idle CSS",
                getattr(menu, "name", menu),
            )
            self._force_end = False
            self._active_players = None
            self._pending_players = None
            self._teardown_workers()
            self._actions = {p: None for p in PORTS}
            self.state.reset()

        # On any menu transition, flush held inputs once. Entering CSS with A
        # still held (from mashing through the main menu) is how a cursor
        # accidentally grabs the CPU slider or the HMN/CPU toggle.
        if menu != self._prev_menu:
            coins = {p: getattr(pl, "coin_down", None) for p, pl in gs.players.items()}
            log.info(
                "MENU %s -> %s | frame=%s ready_to_start=%s force_end=%s "
                "pending=%s active=%s coin_down=%s",
                getattr(self._prev_menu, "name", self._prev_menu),
                getattr(menu, "name", menu),
                gs.frame,
                getattr(gs, "ready_to_start", "?"),
                self._force_end,
                self._pending_players is not None,
                self._active_players is not None,
                coins,
            )
            self._prev_menu = menu
            for ctrl in self._controllers.values():
                ctrl.release_all()
            return

        if menu == melee.Menu.CHARACTER_SELECT:
            players = self._pending_players or self._active_players
            if players is None:
                # No match queued: keep all four controllers present at CSS
                # with nobody locked in, waiting for the lobby to queue a match.
                # While no player is committed Melee can't advance past CSS, so
                # an ended match parks here instead of sliding into stage
                # select, and a fresh boot shows four idle controllers rather
                # than P1 pre-selecting a character. See _park_idle.
                self._park_idle(gs)
                return
            # START must fire only once EVERY player has locked in their
            # target character. choose_character(start=True) presses START the
            # instant the last controller's own coin is down — regardless of
            # whether the other ports are still navigating to their picks. On a
            # fresh match this is usually harmless, but returning to CSS after a
            # finish leaves the previous roster still committed: the last port's
            # coin is already down, so START fires before the newly-toggled
            # characters lock in and we slide into stage select with the wrong
            # (old) roster. Gate START on all four being on the correct char
            # with the coin down — the exact per-port condition the helper uses.
            all_locked = all(
                (ps := gs.players.get(p.port)) is not None
                and ps.coin_down
                and ps.character is CHARACTER_MAP.get(p.character, melee.Character.FOX)
                for p in players
            )
            for i, p in enumerate(players):
                p_state = gs.players.get(p.port)
                # menuhelper bug: `or is_holding_cpu_slider` overrides
                # cpu_level=0 and drives the cursor to the slider rows.
                # Dropping the grab is the only way back out.
                if p_state is not None and p_state.is_holding_cpu_slider:
                    self._controllers[p.port].release_all()
                    continue
                target_char = CHARACTER_MAP.get(p.character, melee.Character.FOX)
                self._menu_helpers[p.port].choose_character(
                    character=target_char,
                    gamestate=gs,
                    controller=self._controllers[p.port],
                    cpu_level=0,
                    costume=0,
                    swag=False,
                    start=(i == len(players) - 1) and all_locked,
                )

        elif menu in (melee.Menu.MAIN_MENU, melee.Menu.PRESS_START):
            players = self._pending_players or self._active_players
            # Even with no match queued, walk through the login/main menu into
            # CSS and wait there — no reason to sit on the login screen. With no
            # players we use a default character and autostart=False so we stop
            # at CSS; once a match is queued the CSS branch above takes over.
            char = (
                CHARACTER_MAP.get(players[0].character, IDLE_CHARACTER)
                if players
                else IDLE_CHARACTER
            )
            self._menu_helpers[PORTS[0]].menu_helper_simple(
                gamestate=gs,
                controller=self._controllers[PORTS[0]],
                character_selected=char,
                stage_selected=STAGE,
                cpu_level=0,
                autostart=bool(players),
            )

        elif menu == melee.Menu.STAGE_SELECT:
            players = self._pending_players or self._active_players
            if players is None:
                # No match queued but we slipped into stage select — e.g. a
                # START press carried over from skipping the postgame scores
                # onto a still-locked-in CSS. Tap B on every controller to back
                # out to CSS (any port can cancel the stage pick), then wait
                # there; _park_idle clears the leftover tokens so it can't
                # happen again.
                if gs.frame % 30 == 0:
                    log.info(
                        "STAGE_SELECT with no match — tapping B to back out (frame=%s)",
                        gs.frame,
                    )
                for ctrl in self._controllers.values():
                    self._tap_b(ctrl)
                return
            char = CHARACTER_MAP.get(players[0].character, melee.Character.FOX)
            self._menu_helpers[PORTS[0]].choose_stage(
                stage=STAGE,
                gamestate=gs,
                controller=self._controllers[PORTS[0]],
                character=char,
                autostart=True,
            )

        elif menu == melee.Menu.IN_GAME:
            # Force-ending (stop pressed): ignore bots and walk everyone off the
            # stage until Melee ends the match itself. Scores still tick so the
            # watch page reflects the wind-down; state resets the moment we
            # leave IN_GAME (see the force-end check at the top of _handle_frame).
            if self._force_end:
                self._update_scores(gs)
                self._drive_self_destruct(gs)
                return

            if self._pending_players is not None:
                self._active_players = self._pending_players
                self._pending_players = None
                self.state.phase = Phase.IN_GAME
                asyncio.create_task(self._decision_loop())

            self._update_scores(gs)

            if self._active_players:
                alive = [
                    p
                    for p in self._active_players
                    if gs.players.get(p.port) and gs.players[p.port].stock > 0
                ]
                if len(alive) == 1 and self.state.phase == Phase.IN_GAME:
                    self.state.winner = alive[0].name
                    self.state.phase = Phase.POSTGAME

            async with self._lock:
                actions = dict(self._actions)
            if self._active_players:
                for p in self._active_players:
                    await self._apply(self._controllers[p.port], actions.get(p.port))

        elif menu == melee.Menu.POSTGAME_SCORES:
            # (A forced end is already cleared to IDLE at the top of the frame,
            # before we ever get here — so this only handles a natural finish.)
            if self.state.phase == Phase.IN_GAME:
                self.state.phase = Phase.POSTGAME
            self._menu_helpers[PORTS[0]].menu_helper_simple(
                gamestate=gs,
                controller=self._controllers[PORTS[0]],
                character_selected=melee.Character.FOX,
                stage_selected=STAGE,
                cpu_level=0,
                autostart=False,
            )
            self._active_players = None
            # Match over - kill the subprocess workers so they aren't sitting
            # idle holding memory between matches. They are respawned fresh in
            # the next queue_match().
            self._teardown_workers()

    def _update_scores(self, gs: melee.GameState):
        if not self._active_players:
            return
        for p in self._active_players:
            player_gs = gs.players.get(p.port)
            if player_gs is None:
                continue
            score = self.state.scores.get(p.port)
            if score is None:
                continue
            score.stock = int(player_gs.stock)
            score.percent = round(float(player_gs.percent), 1)
            score.action = getattr(player_gs.action, "name", str(player_gs.action))

    async def _apply(self, ctrl: melee.Controller, action: Optional[dict]):
        if action is None:
            ctrl.release_all()
            return
        ctrl.tilt_analog(
            melee.Button.BUTTON_MAIN,
            action.get("stick_x", 0.5),
            action.get("stick_y", 0.5),
        )
        for btn, pressed in action.get("buttons", {}).items():
            try:
                b = melee.Button[btn]
                ctrl.press_button(b) if pressed else ctrl.release_button(b)
            except KeyError:
                pass

    async def _decision_loop(self):
        """Runs for the duration of one match, fetching bot actions off the hot path.

        Each iteration builds one frame snapshot from the live GameState and
        dispatches it to all four per-port BotWorker subprocesses in parallel
        (each runs synchronously in its own thread via asyncio.to_thread, with
        the worker's internal 10ms deadline bounding the wait). If a worker
        has died (crash, or K consecutive deadline misses), the port falls
        back to its trusted in-process default bot for the remainder of the
        match. Results land in self._actions under the lock; the 60fps frame
        loop reads them back and applies them to the controllers.
        """
        while self.state.phase == Phase.IN_GAME and self._active_players:
            gs = self._latest_gs
            if gs is None or gs.menu_state != melee.Menu.IN_GAME:
                await asyncio.sleep(0.016)
                continue
            snap = frame_snapshot(gs, gs.frame)
            ports = [p.port for p in self._active_players]
            coros = [
                asyncio.to_thread(self._port_action_sync, port, snap, gs)
                for port in ports
            ]
            results = await asyncio.gather(*coros, return_exceptions=True)
            async with self._lock:
                for port, result in zip(ports, results):
                    if isinstance(result, Exception):
                        log.error("Port %d decision error: %s", port, result)
                        continue
                    self._actions[port] = result
            await asyncio.sleep(0.016)

    def _port_action_sync(self, port: int, snap: dict, gs) -> Optional[dict]:
        """Synchronous per-port decision: subprocess worker first, in-process
        default-bot fallback if the worker is dead. Runs in a worker thread
        via asyncio.to_thread so the 10ms+ deadline on worker.act() cannot
        stall the event loop.
        """
        worker = self._bot_workers.get(port)
        if worker is None or worker.is_dead:
            return self._fallback_action_sync(port, gs)
        action = worker.act(snap, port)
        # The worker may have died on this very call (crash / K-th miss).
        # Fall back immediately so the player keeps a working character.
        if worker.is_dead:
            return self._fallback_action_sync(port, gs)
        return action

    def _fallback_action_sync(self, port: int, gs) -> Optional[dict]:
        """Trusted in-process default bot for this port's character."""
        loader = self._fallback_loaders.get(port)
        if loader is None:
            return None
        bot = loader.get_active_bot()
        if bot is None:
            return None
        try:
            return clamp_action(bot.act(gs, port))
        except Exception as exc:
            log.error("Fallback bot port %d: %s", port, exc)
            return None

    def _teardown_workers(self):
        """Kill and forget all subprocess bot workers."""
        if not self._bot_workers:
            return
        for worker in self._bot_workers.values():
            try:
                worker.close()
            except Exception:
                pass
        self._bot_workers = {}
