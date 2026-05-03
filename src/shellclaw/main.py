"""shellclaw entry point.

Subcommands:
  shellclaw                   Launch the TUI
  shellclaw check "<cmd>"     Analyse a command for safety (no TUI)
  shellclaw history           Print recent session history
  shellclaw history search    Search session history by keyword
  shellclaw undo              Reverse the last reversible action
  shellclaw health            Run a manual health scan
"""

from __future__ import annotations

import argparse
import asyncio


def _cmd_tui() -> None:
    from .tui.app import shellclawApp
    app = shellclawApp()
    app.run()


def _cmd_check(command: str) -> None:
    from .config import load_config
    from .safety.check import run_check
    config = load_config()
    run_check(command, config)


def _cmd_history(query: str | None) -> None:
    from .session.store import SessionStore
    store = SessionStore()

    if query:
        sessions = store.search_sessions(query)
        if not sessions:
            print(f"No sessions found matching '{query}'.")
            return
    else:
        sessions = store.recent_sessions(limit=20)
        if not sessions:
            print("No session history yet.")
            return

    for s in sessions:
        date = s.started_at[:10]
        outcome = s.outcome or "open"
        outcome_icon = "✓" if outcome == "approved" else ("✗" if outcome == "rejected" else "·")
        print(f"{date}  {s.problem[:60]:<60}  {outcome_icon} {outcome}")


def _cmd_undo() -> None:
    from .safety.undo import UndoLog
    log = UndoLog.load()
    entry = log.last_reversible()
    if entry is None:
        print("Nothing reversible to undo.")
        return

    print(f"Last reversible action: {entry.description}")
    try:
        confirm = input("Undo this action? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if confirm == "y":
        result = log.undo_last()
        print(result)
    else:
        print("Undo cancelled.")


def _cmd_health() -> None:
    from .health.snapshot import HealthStatus, run_health_snapshot

    print("Scanning your computer...\n")
    items = asyncio.run(run_health_snapshot())

    icons = {
        HealthStatus.OK: "🟢",
        HealthStatus.WARN: "🟡",
        HealthStatus.CRITICAL: "🔴",
    }

    for item in items:
        icon = icons.get(item.status, "⚪")
        print(f"  {icon}  {item.label:<12} {item.message}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shellclaw",
        description="shellclaw — A Linux assistant in your terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  shellclaw                       Launch the TUI\n"
            "  shellclaw check 'rm -rf /tmp'   Check if a command is safe\n"
            "  shellclaw history               Show recent sessions\n"
            "  shellclaw history search wifi   Search sessions\n"
            "  shellclaw undo                  Reverse the last action\n"
            "  shellclaw health                Run a health scan\n"
        ),
    )
    parser.add_argument("--version", action="version", version="shellclaw 1.0.0")

    subparsers = parser.add_subparsers(dest="command")

    check_parser = subparsers.add_parser("check", help="Analyse a command for safety")
    check_parser.add_argument("cmd", help="The command to analyse (quoted)")

    history_parser = subparsers.add_parser("history", help="Show session history")
    history_subparsers = history_parser.add_subparsers(dest="history_command")
    history_search = history_subparsers.add_parser("search", help="Search session history by keyword")
    history_search.add_argument("query", help="Search term")

    subparsers.add_parser("undo", help="Reverse the last reversible action")
    subparsers.add_parser("health", help="Run a manual system health scan")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    match args.command:
        case "check":
            _cmd_check(args.cmd)
        case "history":
            query = getattr(args, "query", None) if getattr(args, "history_command", None) == "search" else None
            _cmd_history(query)
        case "undo":
            _cmd_undo()
        case "health":
            _cmd_health()
        case _:
            _cmd_tui()


if __name__ == "__main__":
    main()
