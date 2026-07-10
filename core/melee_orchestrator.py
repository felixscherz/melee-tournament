"""
Main async game loop for the Smash Tournament platform.

Runs Dolphin at 60fps via libmelee. Bot decisions (LLM or script) are
fetched asynchronously so the emulation never blocks waiting for a response.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

import melee
import toml

from core.bot_loader import BotLoader
from core.llm_client import LLMClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.toml"

P1_CHARACTER = melee.Character.FOX
P2_CHARACTER = melee.Character.MARTH
STAGE = melee.Stage.FINAL_DESTINATION


class MeleeOrchestrator:
    def __init__(self, config: dict):
        self.cfg = config
        self.console: Optional[melee.Console] = None
        self.controller_p1: Optional[melee.Controller] = None
        self.controller_p2: Optional[melee.Controller] = None
        self.menu_helper = melee.MenuHelper()
        self.bot_loader = BotLoader(upload_dir=Path("uploads"))
        self.llm = LLMClient(
            model=config["ollama"]["model"],
            base_url=config["ollama"]["base_url"],
        )
        self._latest_gamestate: Optional[melee.GameState] = None
        self._p1_action: Optional[dict] = None
        self._lock = asyncio.Lock()

    def _setup_dolphin(self):
        self.console = melee.Console(
            path=self.cfg["dolphin"]["path"],
            slippi_address="127.0.0.1",
            slippi_port=self.cfg["dolphin"]["port"],
            blocking_input=False,
            polling_mode=False,
            fullscreen=False,
            disable_audio=False,
        )
        self.controller_p1 = melee.Controller(
            console=self.console,
            port=1,
            type=melee.ControllerType.STANDARD,
        )
        self.controller_p2 = melee.Controller(
            console=self.console,
            port=2,
            type=melee.ControllerType.STANDARD,
        )

    async def _apply_action(self, controller: melee.Controller, action: Optional[dict]):
        if action is None:
            controller.release_all()
            return
        stick_x = action.get("stick_x", 0.5)
        stick_y = action.get("stick_y", 0.5)
        controller.tilt_analog(melee.Button.BUTTON_MAIN, stick_x, stick_y)
        for btn_name, pressed in action.get("buttons", {}).items():
            try:
                btn = melee.Button[btn_name]
                if pressed:
                    controller.press_button(btn)
                else:
                    controller.release_button(btn)
            except KeyError:
                pass

    async def _bot_decision_loop(self):
        """Fetch bot/LLM decisions off the main game loop thread."""
        while True:
            gamestate = self._latest_gamestate
            if gamestate is None or gamestate.menu_state != melee.Menu.IN_GAME:
                await asyncio.sleep(0.016)
                continue

            bot = self.bot_loader.get_active_bot()
            try:
                if bot is not None:
                    action = await asyncio.wait_for(
                        asyncio.to_thread(bot.act, gamestate, 1),
                        timeout=0.05,
                    )
                else:
                    action = await asyncio.wait_for(
                        self.llm.decide(gamestate),
                        timeout=0.1,
                    )
                async with self._lock:
                    self._p1_action = action
            except asyncio.TimeoutError:
                log.warning("Bot/LLM timed out — holding previous action")
            except Exception as exc:
                log.error("Decision error: %s", exc)

            await asyncio.sleep(0.016)

    async def run(self):
        self._setup_dolphin()
        self.console.run(iso_path=self.cfg["dolphin"]["iso"])
        log.info("Connecting to Dolphin...")
        if not self.console.connect():
            raise RuntimeError("Failed to connect to Dolphin. Is it running?")
        log.info("Connected. Starting game loop.")

        self.controller_p1.connect()
        self.controller_p2.connect()

        asyncio.create_task(self._bot_decision_loop())

        frame_target = 1 / 60
        while True:
            frame_start = time.perf_counter()

            gamestate = self.console.step()
            if gamestate is None:
                await asyncio.sleep(0.001)
                continue

            self._latest_gamestate = gamestate

            if gamestate.menu_state == melee.Menu.CHARACTER_SELECT:
                # Drive P1 — human/bot controlled (cpu_level=0), don't start yet
                self.menu_helper.choose_character(
                    character=P1_CHARACTER,
                    gamestate=gamestate,
                    controller=self.controller_p1,
                    cpu_level=0,
                    costume=0,
                    swag=False,
                    start=False,
                )
                # Drive P2 — CPU level 3, and P2 presses Start once both are locked in
                self.menu_helper.choose_character(
                    character=P2_CHARACTER,
                    gamestate=gamestate,
                    controller=self.controller_p2,
                    cpu_level=3,
                    costume=1,
                    swag=False,
                    start=True,
                )

            elif gamestate.menu_state in (
                melee.Menu.MAIN_MENU,
                melee.Menu.STAGE_SELECT,
                melee.Menu.POSTGAME_SCORES,
                melee.Menu.PRESS_START,
            ):
                # menu_helper_simple handles everything outside CSS
                self.menu_helper.menu_helper_simple(
                    gamestate=gamestate,
                    controller=self.controller_p1,
                    character_selected=P1_CHARACTER,
                    stage_selected=STAGE,
                    cpu_level=0,
                    costume=0,
                    autostart=True,
                )

            elif gamestate.menu_state == melee.Menu.IN_GAME:
                async with self._lock:
                    p1_action = self._p1_action
                await self._apply_action(self.controller_p1, p1_action)
                self.controller_p2.release_all()

            elapsed = time.perf_counter() - frame_start
            await asyncio.sleep(max(0, frame_target - elapsed))


async def main():
    config = toml.load(CONFIG_PATH)
    orchestrator = MeleeOrchestrator(config)
    await orchestrator.run()


if __name__ == "__main__":
    asyncio.run(main())
