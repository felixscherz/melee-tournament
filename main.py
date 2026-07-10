"""
Unified launcher — runs FastAPI and the Melee orchestrator in the same event loop.

Usage:
    source .venv/bin/activate
    python main.py
"""
import asyncio
import logging
import signal
from pathlib import Path

import toml
import uvicorn

from core.game_state import app_state
from core.melee_orchestrator import MeleeOrchestrator
import frontend.app as webapp

log = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).parent / "config" / "settings.toml"


async def main():
    config = toml.load(CONFIG_PATH)

    orchestrator = MeleeOrchestrator(config=config, state=app_state)
    webapp._orchestrator = orchestrator

    host = config["server"]["host"]
    port = config["server"]["port"]
    log.info("Starting web server on http://%s:%d", host, port)

    server_config = uvicorn.Config(
        app=webapp.app,
        host=host,
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(server_config)

    loop = asyncio.get_running_loop()

    def _shutdown(sig):
        log.info("Signal %s received — shutting down", sig.name)
        orchestrator.stop()
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, sig)

    # Fail fast: check port is free before launching Dolphin
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
        _s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        try:
            _s.bind((host, port))
        except OSError:
            raise RuntimeError(
                f"Port {port} is already in use. "
                f"Run: lsof -ti :{port} | xargs kill -9"
            )

    try:
        await orchestrator.launch()
        await server.serve()
    finally:
        orchestrator.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(main())
