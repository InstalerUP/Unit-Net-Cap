"""Main entry point for Unit Net Cap (CLI, TUI, and Daemon)."""

import os
import sys

from .cli import run_cli
from .daemon import run_daemon
from .tui import run_tui


def _is_root() -> bool:
    """Return True if running with root privileges."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def main(argv: list[str] | None = None) -> int:
    """Route execution to daemon, CLI subcommand, or interactive TUI."""
    args = sys.argv[1:] if argv is None else argv

    # Help / version flags can run without root
    if args and args[0] in ("-h", "--help", "-v", "--version"):
        return run_cli(args)

    # Read-only status command can run without root
    if args and args[0] == "status":
        return run_cli(args)

    # Daemon mode
    if args and args[0] == "daemon":
        if not _is_root():
            print("Error: 'daemon' requires root privileges. Run with sudo.", file=sys.stderr)
            return 1
        return run_daemon()

    # Interactive TUI when no args or '-i' / '--interactive'
    if not args or args[0] in ("-i", "--interactive"):
        if not _is_root():
            print("Error: interactive mode requires root privileges. Run with sudo.", file=sys.stderr)
            return 1
        return run_tui()

    # All other CLI mutating commands require root
    if not _is_root():
        print("Error: this command requires root privileges. Run with sudo.", file=sys.stderr)
        return 1
    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())