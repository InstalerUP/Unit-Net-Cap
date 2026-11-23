"""Command-line interface (CLI) for Unit Net Cap."""

import argparse
import json
from pathlib import Path
import shutil
import sys
from typing import Any

from . import __version__
from .config import (
    DEFAULT_MODE,
    DEFAULT_POLL_INTERVAL_SEC,
    DEFAULT_RESET_DAY,
    INSTALL_BIN_FILE,
    INSTALL_LIB_DIR,
    MAX_POLL_INTERVAL_SEC,
    MAX_RESET_DAY,
    MIN_POLL_INTERVAL_SEC,
    MIN_RESET_DAY,
    NETCAP_SERVICE_FILE,
    STATE_DIR,
    STATE_FILE,
    VALID_MODES,
    bytes_to_gb,
    format_bytes,
    gb_to_bytes,
    parse_limit_gb,
)
from .docker_ctrl import (
    get_container_state,
    is_docker_available,
    list_docker_containers,
    restore_container,
)
from .storage import (
    load_state,
    reset_month_counters,
    reset_target_counter,
    save_state,
)
from .systemd_ctrl import (
    ensure_ip_accounting_dropin,
    get_unit_active_state,
    is_protected_unit,
    list_systemd_units,
    remove_ip_accounting_dropin,
    restart_unit_if_active,
    restore_unit,
)


def _target_metric_bytes(target: dict[str, Any]) -> int:
    """Return traffic bytes matching target's mode."""
    rx = int(target.get("rx_month_bytes", 0))
    tx = int(target.get("tx_month_bytes", 0))
    mode = target.get("mode", "both")
    if mode == "egress":
        return tx
    if mode == "ingress":
        return rx
    return rx + tx


def _target_status_label(target: dict[str, Any]) -> str:
    """Return human-readable status label: BLOCKED, MONITOR, or ACTIVE."""
    if target.get("blocked_by_quota"):
        return "BLOCKED"
    limit_gb = float(target.get("limit_gb", 0.0) or 0.0)
    if limit_gb <= 0.0:
        return "MONITOR"
    return "ACTIVE"


def _resolve_target_key(
    state: dict[str, Any], name: str, explicit_type: str | None = None
) -> str | None:
    """Find matching target key in state['targets'] by name and optional type."""
    targets: dict[str, dict[str, Any]] = state.get("targets", {})
    clean_name = name.strip()

    if explicit_type in ("systemd", "docker"):
        candidate_name = clean_name
        if explicit_type == "systemd" and not candidate_name.endswith(".service"):
            candidate_name = f"{candidate_name}.service"
        key = f"{explicit_type}:{candidate_name}"
        return key if key in targets else None

    # Auto-detect if type is omitted
    candidates = [clean_name]
    if not clean_name.endswith(".service"):
        candidates.append(f"{clean_name}.service")

    for cand in candidates:
        for prefix in ("systemd", "docker"):
            key = f"{prefix}:{cand}"
            if key in targets:
                return key
    return None


def _detect_new_target_type(name: str, explicit_type: str | None) -> tuple[str, str] | None:
    """Resolve target type ('systemd' or 'docker') and normalized name."""
    clean = name.strip()
    if not clean:
        return None

    if explicit_type == "systemd":
        unit = clean if clean.endswith(".service") else f"{clean}.service"
        if is_protected_unit(unit):
            return None
        return "systemd", unit

    if explicit_type == "docker":
        return "docker", clean

    # Auto-detect: check systemd first (if .service or exists), then docker
    unit_candidate = clean if clean.endswith(".service") else f"{clean}.service"
    if is_protected_unit(unit_candidate) or is_protected_unit(clean):
        return None

    systemd_names = {u["name"] for u in list_systemd_units(include_hidden=True)}
    if clean in systemd_names:
        return "systemd", clean
    if unit_candidate in systemd_names:
        return "systemd", unit_candidate

    if is_docker_available():
        docker_names = {c["name"] for c in list_docker_containers()}
        if clean in docker_names:
            return "docker", clean

    # Fallback if adding a unit file by name
    if clean.endswith(".service"):
        return "systemd", clean
    return "systemd", unit_candidate


def _cmd_status(args: argparse.Namespace) -> int:
    """Handle 'netcap status [--json]'."""
    state = load_state()
    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0

    settings = state.get("settings", {})
    interval = settings.get("poll_interval_sec", DEFAULT_POLL_INTERVAL_SEC)
    reset_day = settings.get("reset_day", DEFAULT_RESET_DAY)
    last_reset = state.get("last_reset_period", "-")

    print(f"UNIT NET CAP | Poll: every {interval} s | Reset day: {reset_day} | Last reset: {last_reset}")
    print()
    print("Interfaces:")
    interfaces = state.get("interfaces", {})
    if not interfaces:
        print("  (no interface data yet)")
    else:
        for if_name in sorted(interfaces.keys()):
            info = interfaces[if_name]
            rx = int(info.get("rx_month_bytes", 0))
            tx = int(info.get("tx_month_bytes", 0))
            print(
                f"  {if_name:<14} In: {format_bytes(rx):<10} "
                f"Out: {format_bytes(tx):<10} Total: {format_bytes(rx + tx)}"
            )

    print()
    print("Monitored targets:")
    targets = state.get("targets", {})
    if not targets:
        print("  (no monitored targets)")
    else:
        print(
            f"  {'Type':<9} {'Name':<24} {'Mode':<8} {'In':<10} {'Out':<10} "
            f"{'Total':<10} {'Limit':<8} {'Used%':<6} {'Status'}"
        )
        print("  " + "-" * 91)
        for key in sorted(targets.keys()):
            t = targets[key]
            t_type = str(t.get("type", "")).upper()
            name = str(t.get("name", ""))
            mode = str(t.get("mode", DEFAULT_MODE))
            rx = int(t.get("rx_month_bytes", 0))
            tx = int(t.get("tx_month_bytes", 0))
            total = rx + tx
            limit_gb = float(t.get("limit_gb", 0.0) or 0.0)
            status = _target_status_label(t)

            if limit_gb > 0.0:
                limit_str = f"{limit_gb:g} GB"
                metric_gb = bytes_to_gb(_target_metric_bytes(t))
                pct_str = f"{int((metric_gb / limit_gb) * 100)}%"
            else:
                limit_str = "-"
                pct_str = "-"

            print(
                f"  {t_type:<9} {name:<24} {mode:<8} {format_bytes(rx):<10} {format_bytes(tx):<10} "
                f"{format_bytes(total):<10} {limit_str:<8} {pct_str:<6} {status}"
            )

    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    """Handle 'netcap add <name> --limit <GB> ...'."""
    limit_gb = parse_limit_gb(args.limit)
    if limit_gb is None:
        print("Error: invalid --limit value.", file=sys.stderr)
        return 1

    resolved = _detect_new_target_type(args.name, args.type)
    if resolved is None:
        print(f"Error: '{args.name}' cannot be monitored (protected or invalid).", file=sys.stderr)
        return 1

    t_type, norm_name = resolved
    state = load_state()
    mode = args.mode or state.get("settings", {}).get("default_mode", DEFAULT_MODE)
    if mode not in VALID_MODES:
        print(f"Error: invalid mode '{mode}'.", file=sys.stderr)
        return 1

    key = f"{t_type}:{norm_name}"
    targets: dict[str, dict[str, Any]] = state.setdefault("targets", {})

    if t_type == "systemd":
        if not ensure_ip_accounting_dropin(norm_name):
            print(f"Error: failed to create drop-in config for '{norm_name}'.", file=sys.stderr)
            return 1
        if not args.no_restart:
            ok, msg = restart_unit_if_active(norm_name)
            if not ok:
                print(f"Error: {msg}", file=sys.stderr)
                return 1
            print(msg)
        else:
            from .systemd_ctrl import daemon_reload

            daemon_reload()
            print(f"Drop-in created for '{norm_name}' (restart skipped by --no-restart).")

    targets[key] = {
        "type": t_type,
        "name": norm_name,
        "limit_gb": float(args.limit),
        "mode": mode,
        "rx_month_bytes": 0,
        "tx_month_bytes": 0,
        "last_rx_raw": 0,
        "last_tx_raw": 0,
        "blocked_by_quota": False,
        "original_restart_policy": None,
        "limit_gb": limit_gb,
    }

    save_state(state)
    print(f"Added [{t_type}] {norm_name} (limit: {float(args.limit):g} GB, mode: {mode}).")
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    """Handle 'netcap set <name> [--limit <GB>] [--mode <mode>]'."""
    if args.limit is None and args.mode is None:
        print("Error: specify at least --limit or --mode.", file=sys.stderr)
        return 1
    parsed_limit: float | None = None
    if args.limit is not None and args.limit < 0:
        parsed_limit = parse_limit_gb(args.limit)
        if parsed_limit is None:
            print("Error: --limit must be >= 0.", file=sys.stderr)
            return 1

    state = load_state()
    key = _resolve_target_key(state, args.name, args.type)
    if not key:
        print(f"Error: target '{args.name}' is not in monitored list.", file=sys.stderr)
        return 1

    target = state["targets"][key]
    if parsed_limit is not None:
        target["limit_gb"] = parsed_limit
        if parsed_limit == 0.0:
            target["blocked_by_quota"] = False
    if args.mode is not None:
        target["mode"] = args.mode

    save_state(state)
    print(f"Updated {key}: limit={target['limit_gb']:g} GB, mode={target['mode']}.")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    """Handle 'netcap remove <name>'."""
    state = load_state()
    key = _resolve_target_key(state, args.name, args.type)
    if not key:
        print(f"Error: target '{args.name}' is not in monitored list.", file=sys.stderr)
        return 1

    target = state["targets"].pop(key)
    if target.get("type") == "systemd":
        unit_name = str(target.get("name", ""))
        remove_ip_accounting_dropin(unit_name)
        from .systemd_ctrl import daemon_reload

        daemon_reload()

    save_state(state)
    print(f"Removed {key}.")
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    """Handle 'netcap reset [name | --all]'."""
    state = load_state()
    if args.all:
        to_restore = reset_month_counters(state)
        for item in to_restore:
            if item.get("type") == "systemd":
                restore_unit(str(item.get("name", "")))
            elif item.get("type") == "docker":
                restore_container(str(item.get("name", "")), item.get("original_restart_policy"))
        save_state(state)
        print("All monthly counters reset.")
        return 0

    if not args.name:
        print("Error: specify a target name or pass --all.", file=sys.stderr)
        return 1

    key = _resolve_target_key(state, args.name, args.type)
    if not key:
        print(f"Error: target '{args.name}' not found.", file=sys.stderr)
        return 1

    target = state["targets"][key]
    was_blocked = bool(target.get("blocked_by_quota"))
    reset_target_counter(target)
    if was_blocked:
        if target.get("type") == "systemd":
            restore_unit(str(target.get("name", "")))
        elif target.get("type") == "docker":
            restore_container(str(target.get("name", "")), target.get("original_restart_policy"))

    save_state(state)
    print(f"Counter reset for {key}.")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    """Handle 'netcap config [--interval ...] [--default-mode ...] [--reset-day ...]'."""
    state = load_state()
    settings = state.setdefault("settings", {})
    changed = False

    if args.interval is not None:
        if not (MIN_POLL_INTERVAL_SEC <= args.interval <= MAX_POLL_INTERVAL_SEC):
            print(
                f"Error: --interval must be between {MIN_POLL_INTERVAL_SEC} and {MAX_POLL_INTERVAL_SEC} seconds.",
                file=sys.stderr,
            )
            return 1
        settings["poll_interval_sec"] = int(args.interval)
        changed = True

    if args.default_mode is not None:
        settings["default_mode"] = args.default_mode
        changed = True

    if args.reset_day is not None:
        if not (MIN_RESET_DAY <= args.reset_day <= MAX_RESET_DAY):
            print(
                f"Error: --reset-day must be between {MIN_RESET_DAY} and {MAX_RESET_DAY}.",
                file=sys.stderr,
            )
            return 1
        settings["reset_day"] = int(args.reset_day)
        changed = True

    if not changed:
        print(f"poll_interval_sec = {settings.get('poll_interval_sec', DEFAULT_POLL_INTERVAL_SEC)}")
        print(f"default_mode      = {settings.get('default_mode', DEFAULT_MODE)}")
        print(f"reset_day         = {settings.get('reset_day', DEFAULT_RESET_DAY)}")
        return 0

    save_state(state)
    print("Configuration saved.")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    """Cleanly remove service, restore quota-blocked targets, clean drop-ins, and remove program files."""
    if not args.yes:
        ans = input("This will remove Unit Net Cap and restore quota-blocked targets. Continue? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("Uninstall cancelled.")
            return 0

    state = load_state()
    targets: dict[str, dict[str, Any]] = state.get("targets", {})

    # 1. Stop and disable netcap.service
    import subprocess

    subprocess.run(["systemctl", "disable", "--now", "netcap.service"], check=False, capture_output=True)
    try:
        if NETCAP_SERVICE_FILE.exists():
            NETCAP_SERVICE_FILE.unlink()
    except OSError:
        pass

    # 2. Restore targets blocked by quota and clean drop-ins
    for key, target in targets.items():
        t_type = target.get("type")
        name = str(target.get("name", ""))
        if not name:
            continue
        if t_type == "systemd":
            if target.get("blocked_by_quota") and not is_protected_unit(name):
                restore_unit(name)
            remove_ip_accounting_dropin(name)
        elif t_type == "docker":
            if target.get("blocked_by_quota"):
                restore_container(name, target.get("original_restart_policy"))

    # Also clean any leftover netcap.conf drop-ins in /etc/systemd/system/*.d/
    try:
        if Path("/etc/systemd/system").exists():
            for dropin in Path("/etc/systemd/system").glob("*.d/netcap.conf"):
                parent = dropin.parent
                try:
                    dropin.unlink()
                    if parent.is_dir() and not any(parent.iterdir()):
                        parent.rmdir()
                except OSError:
                    pass
    except OSError:
        pass

    subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True)

    # 3. Remove data directory, library directory, and executable
    for path_to_remove in (STATE_DIR, INSTALL_LIB_DIR):
        try:
            if path_to_remove.exists():
                shutil.rmtree(path_to_remove, ignore_errors=True)
        except OSError:
            pass

    try:
        if INSTALL_BIN_FILE.exists():
            INSTALL_BIN_FILE.unlink()
    except OSError:
        pass

    print("Unit Net Cap has been cleanly removed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create argument parser for CLI subcommands."""
    parser = argparse.ArgumentParser(
        prog="netcap",
        description="Control and limit monthly network traffic of systemd services and Docker containers.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    # status
    p_status = subparsers.add_parser("status", help="Show network interfaces and monitored targets.")
    p_status.add_argument("--json", action="store_true", help="Output state as JSON.")

    # add
    p_add = subparsers.add_parser("add", help="Add a systemd service or Docker container to monitoring.")
    p_add.add_argument("name", help="Service name (e.g. nginx.service) or container name.")
    p_add.add_argument("--limit", type=float, required=True, help="Monthly limit in GB (0 = monitor only).")
    p_add.add_argument("--type", choices=("systemd", "docker"), help="Target type (auto-detected if omitted).")
    p_add.add_argument("--mode", choices=VALID_MODES, help="Traffic counting mode.")
    p_add.add_argument("--no-restart", action="store_true", help="Do not restart active systemd unit on add.")

    # set
    p_set = subparsers.add_parser("set", help="Update limit or counting mode for a monitored target.")
    p_set.add_argument("name", help="Target name.")
    p_set.add_argument("--type", choices=("systemd", "docker"), help="Target type.")
    p_set.add_argument("--limit", type=float, help="New monthly limit in GB (0 = monitor only).")
    p_set.add_argument("--mode", choices=VALID_MODES, help="New counting mode.")

    # remove
    p_remove = subparsers.add_parser("remove", help="Remove a target from monitoring.")
    p_remove.add_argument("name", help="Target name.")
    p_remove.add_argument("--type", choices=("systemd", "docker"), help="Target type.")

    # reset
    p_reset = subparsers.add_parser("reset", help="Reset monthly traffic counters.")
    p_reset.add_argument("name", nargs="?", help="Target name to reset.")
    p_reset.add_argument("--type", choices=("systemd", "docker"), help="Target type.")
    p_reset.add_argument("--all", action="store_true", help="Reset all counters and restore quota-blocked targets.")

    # config
    p_config = subparsers.add_parser("config", help="View or update global settings.")
    p_config.add_argument("--interval", type=int, help="Polling interval in seconds (30..10800).")
    p_config.add_argument("--default-mode", choices=VALID_MODES, help="Default counting mode for new targets.")
    p_config.add_argument("--reset-day", type=int, help="Day of month for automatic reset (1..28).")

    # uninstall
    p_uninstall = subparsers.add_parser("uninstall", help="Uninstall Unit Net Cap and restore blocked targets.")
    p_uninstall.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")

    # daemon (also handled in main.py, kept here for --help completeness)
    subparsers.add_parser("daemon", help="Run background monitoring daemon.")

    return parser


def run_cli(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and run corresponding handler."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        return _cmd_status(args)
    if args.command == "add":
        return _cmd_add(args)
    if args.command == "set":
        return _cmd_set(args)
    if args.command == "remove":
        return _cmd_remove(args)
    if args.command == "reset":
        return _cmd_reset(args)
    if args.command == "config":
        return _cmd_config(args)
    if args.command == "uninstall":
        return _cmd_uninstall(args)

    parser.print_help()
    return 0