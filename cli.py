"""smash-tournament command line — run the server, with optional state cleanup.

Usage:
    uv run smash-tournament serve                # same as `uv run main.py`
    uv run smash-tournament serve --fresh        # factory-reset state, then serve
    uv run smash-tournament clean --clear-code   # cleanup only, no server

Cleanup flags (combinable, on both subcommands):
    --clear-code     clear all captain code overrides (and uploads/player*.py)
    --clear-names    reset team names to TEAM 1..4
    --clear-bots     delete generated bots and clear the version index
    --reset-teams    clear captains, contributions, ready flags, and code
                     overrides (keeps names and the active set)
    --fresh          factory reset: all of the above plus names and active set
"""

import argparse
import asyncio
import logging

_CLEANUP_FLAGS = ("fresh", "reset_teams", "clear_code", "clear_names", "clear_bots")


def _add_cleanup_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("cleanup")
    g.add_argument(
        "--clear-code",
        action="store_true",
        help="clear all captain code overrides (and uploads/player*.py)",
    )
    g.add_argument(
        "--clear-names",
        action="store_true",
        help="reset team names to TEAM 1..4",
    )
    g.add_argument(
        "--clear-bots",
        action="store_true",
        help="delete generated bots and clear the version index",
    )
    g.add_argument(
        "--reset-teams",
        action="store_true",
        help="clear captains, contributions, ready flags, and code overrides "
        "(keeps names and the active set)",
    )
    g.add_argument(
        "--fresh",
        action="store_true",
        help="factory reset: all of the above plus names and the active set",
    )


def _run_cleanup(args: argparse.Namespace) -> None:
    # Imported lazily: pulls in the TeamRegistry singleton, which loads
    # generated/teams.json on import.
    from core import cleanup

    if args.fresh:
        actions = cleanup.factory_reset()
    else:
        actions = []
        if args.reset_teams:
            actions += cleanup.reset_teams()
        if args.clear_names:
            actions += cleanup.clear_names()
        if args.clear_code:
            actions += cleanup.clear_code()
        if args.clear_bots:
            actions += cleanup.clear_bots()
    for a in actions:
        print(f"cleanup: {a}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="smash-tournament",
        description="Self-hosted Melee bot tournament platform.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser(
        "serve",
        help="run the server (FastAPI + orchestrator), cleaning up first if "
        "cleanup flags are given",
    )
    _add_cleanup_flags(serve)

    clean = sub.add_parser("clean", help="clean up persisted state and exit")
    _add_cleanup_flags(clean)

    args = parser.parse_args()
    wants_cleanup = any(getattr(args, f) for f in _CLEANUP_FLAGS)

    if args.command == "clean" and not wants_cleanup:
        clean.error("pick at least one cleanup flag (see --help)")

    if wants_cleanup:
        _run_cleanup(args)

    if args.command == "serve":
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
        )
        from main import main as serve_main

        asyncio.run(serve_main())


if __name__ == "__main__":
    main()
