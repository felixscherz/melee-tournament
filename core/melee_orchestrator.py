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
import toml

from core.bot_loader import BotLoader
from core.game_state import AppState, Phase, PlayerConfig, PlayerScore

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.toml"
STAGE = melee.Stage.FINAL_DESTINATION

CHARACTER_MAP: dict[str, melee.Character] = {
    "FOX":    melee.Character.FOX,
    "MARTH":  melee.Character.MARTH,
    "FALCON": melee.Character.CPTFALCON,
    "FALCO":  melee.Character.FALCO,
}
PORTS = [1, 2, 3, 4]


class MeleeOrchestrator:
    def __init__(self, config: dict, state: AppState):
        self.cfg   = config
        self.state = state
        self.console: Optional[melee.Console]              = None
        self._controllers: dict[int, melee.Controller]    = {}
        self._bot_loaders: dict[int, BotLoader]           = {}
        self._actions: dict[int, Optional[dict]]          = {p: None for p in PORTS}
        self._lock            = asyncio.Lock()
        self._menu_helpers    = {p: melee.MenuHelper() for p in PORTS}
        self._pending_players: Optional[list[PlayerConfig]] = None
        self._active_players:  Optional[list[PlayerConfig]] = None
        self._latest_gs: Optional[melee.GameState]        = None
        self._loop_task: Optional[asyncio.Task]           = None
        self._prev_menu: Optional[melee.Menu]             = None

    # ------------------------------------------------------------------ #
    #  Public API                                                           #
    # ------------------------------------------------------------------ #

    async def launch(self):
        """Start Dolphin once and keep it running. Called at server startup."""
        import subprocess as _sp
        try:
            result = _sp.run(
                ["lsof", "-ti", f":{self.cfg['dolphin']['port']}"],
                capture_output=True, text=True
            )
            for pid_str in result.stdout.split():
                pid = int(pid_str.strip())
                log.info("Killing stale Dolphin on port %s (pid %d)", self.cfg['dolphin']['port'], pid)
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
        self._bot_loaders = {}
        for p in players:
            loader = BotLoader(upload_dir=Path("uploads"))
            loader.load(p.bot_path)
            self._bot_loaders[p.port] = loader
            self._actions[p.port] = None

        self.state.phase   = Phase.STARTING
        self.state.players = players
        self.state.scores  = {
            p.port: PlayerScore(port=p.port, name=p.name, character=p.character)
            for p in players
        }
        self.state.winner     = None
        self._pending_players = players
        # Fresh MenuHelper instances so prior CSS state doesn't carry over
        self._menu_helpers = {p: melee.MenuHelper() for p in PORTS}

    def stop(self):
        if self._loop_task:
            self._loop_task.cancel()
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

    async def _handle_frame(self, gs: melee.GameState):
        menu = gs.menu_state

        # On any menu transition, flush held inputs once. Entering CSS with A
        # still held (from mashing through the main menu) is how a cursor
        # accidentally grabs the CPU slider or the HMN/CPU toggle.
        if menu != self._prev_menu:
            self._prev_menu = menu
            for ctrl in self._controllers.values():
                ctrl.release_all()
            return

        if menu == melee.Menu.CHARACTER_SELECT:
            players = self._pending_players or self._active_players
            if players is None:
                return
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
                    start=(i == len(players) - 1),
                )

        elif menu in (melee.Menu.MAIN_MENU, melee.Menu.PRESS_START):
            players = self._pending_players or self._active_players
            char = CHARACTER_MAP.get(players[0].character, melee.Character.FOX) if players else melee.Character.FOX
            self._menu_helpers[PORTS[0]].menu_helper_simple(
                gamestate=gs,
                controller=self._controllers[PORTS[0]],
                character_selected=char,
                stage_selected=STAGE,
                cpu_level=0,
                autostart=True,
            )

        elif menu == melee.Menu.STAGE_SELECT:
            players = self._pending_players or self._active_players
            char = CHARACTER_MAP.get(players[0].character, melee.Character.FOX) if players else melee.Character.FOX
            self._menu_helpers[PORTS[0]].choose_stage(
                stage=STAGE,
                gamestate=gs,
                controller=self._controllers[PORTS[0]],
                character=char,
                autostart=True,
            )

        elif menu == melee.Menu.IN_GAME:
            if self._pending_players is not None:
                self._active_players  = self._pending_players
                self._pending_players = None
                self.state.phase      = Phase.IN_GAME
                asyncio.create_task(self._decision_loop())

            self._update_scores(gs)

            if self._active_players:
                alive = [p for p in self._active_players
                         if gs.players.get(p.port) and gs.players[p.port].stock > 0]
                if len(alive) == 1 and self.state.phase == Phase.IN_GAME:
                    self.state.winner = alive[0].name
                    self.state.phase  = Phase.POSTGAME

            async with self._lock:
                actions = dict(self._actions)
            if self._active_players:
                for p in self._active_players:
                    await self._apply(self._controllers[p.port], actions.get(p.port))

        elif menu == melee.Menu.POSTGAME_SCORES:
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
            score.stock   = int(player_gs.stock)
            score.percent = round(float(player_gs.percent), 1)
            score.action  = getattr(player_gs.action, "name", str(player_gs.action))

    async def _apply(self, ctrl: melee.Controller, action: Optional[dict]):
        if action is None:
            ctrl.release_all()
            return
        ctrl.tilt_analog(melee.Button.BUTTON_MAIN, action.get("stick_x", 0.5), action.get("stick_y", 0.5))
        for btn, pressed in action.get("buttons", {}).items():
            try:
                b = melee.Button[btn]
                ctrl.press_button(b) if pressed else ctrl.release_button(b)
            except KeyError:
                pass

    async def _decision_loop(self):
        """Runs for the duration of one match, fetching bot actions off the hot path."""
        while self.state.phase == Phase.IN_GAME and self._active_players:
            gs = self._latest_gs
            if gs is None or gs.menu_state != melee.Menu.IN_GAME:
                await asyncio.sleep(0.016)
                continue
            for p in self._active_players:
                loader = self._bot_loaders.get(p.port)
                if loader is None:
                    continue
                bot = loader.get_active_bot()
                if bot is None:
                    continue
                try:
                    action = await asyncio.wait_for(
                        asyncio.to_thread(bot.act, gs, p.port), timeout=0.05)
                    async with self._lock:
                        self._actions[p.port] = action
                except asyncio.TimeoutError:
                    pass
                except Exception as exc:
                    log.error("Bot port %d: %s", p.port, exc)
            await asyncio.sleep(0.016)
