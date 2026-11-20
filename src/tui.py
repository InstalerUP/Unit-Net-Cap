"""Simple terminal user interface (TUI) for Unit Net Cap."""

from datetime import date
import os
import sys
from typing import Any

from .config import (
    DEFAULT_MODE,
    DEFAULT_POLL_INTERVAL_SEC,
    DEFAULT_RESET_DAY,
    MAX_POLL_INTERVAL_SEC,
    MAX_RESET_DAY,
    MIN_POLL_INTERVAL_SEC,
    MIN_RESET_DAY,
    VALID_MODES,
    bytes_to_gb,
    format_bytes,
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
    daemon_reload,
    ensure_ip_accounting_dropin,
    get_unit_active_state,
    list_systemd_units,
    remove_ip_accounting_dropin,
    restart_unit_if_active,
    restore_unit,
)

# ANSI colors (gracefully disabled if NO_COLOR or dumb terminal)
_USE_COLOR = bool(sys.stdout.isatty() and os.environ.get("TERM") != "dumb" and not os.environ.get("NO_COLOR"))

_RESET = "\033[0m" if _USE_COLOR else ""
_BOLD = "\033[1m" if _USE_COLOR else ""
_GREEN = "\033[32m" if _USE_COLOR else ""
_YELLOW = "\033[33m" if _USE_COLOR else ""
_RED = "\033[31m" if _USE_COLOR else ""
_CYAN = "\033[36m" if _USE_COLOR else ""


def _clear_screen() -> None:
    """Clear terminal screen cleanly."""
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="", flush=True)


def _pause(message: str = "Press Enter to continue...") -> None:
    """Pause until user presses Enter."""
    try:
        input(f"\n{message}")
    except (EOFError, KeyboardInterrupt):
        pass


def _next_reset_date_str(reset_day: int, today: date | None = None) -> str:
    """Calculate next reset date in YYYY-MM-DD format."""
    d = today or date.today()
    day = max(MIN_RESET_DAY, min(MAX_RESET_DAY, int(reset_day)))
    if d.day < day:
        return f"{d.year:04d}-{d.month:02d}-{day:02d}"
    year = d.year + (1 if d.month == 12 else 0)
    month = 1 if d.month == 12 else d.month + 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def _is_daemon_running() -> bool:
    """Check if netcap.service is active."""
    return get_unit_active_state("netcap.service") == "active"


def _target_metric_bytes(target: dict[str, Any]) -> int:
    """Return measured bytes according to target counting mode."""
    rx = int(target.get("rx_month_bytes", 0))
    tx = int(target.get("tx_month_bytes", 0))
    mode = target.get("mode", "both")
    if mode == "egress":
        return tx
    if mode == "ingress":
        return rx
    return rx + tx


def _format_status(target: dict[str, Any]) -> str:
    """Format status string with optional ANSI color."""
    if target.get("blocked_by_quota"):
        return f"{_RED}BLOCKED{_RESET}"
    limit_gb = float(target.get("limit_gb", 0.0) or 0.0)
    if limit_gb <= 0.0:
        return f"{_YELLOW}MONITOR{_RESET}"
    return f"{_GREEN}ACTIVE{_RESET}"


def _render_main_screen(state: dict[str, Any]) -> None:
    """Draw main dashboard screen."""
    _clear_screen()
    settings = state.get("settings", {})
    interval = int(settings.get("poll_interval_sec", DEFAULT_POLL_INTERVAL_SEC))
    reset_day = int(settings.get("reset_day", DEFAULT_RESET_DAY))
    next_reset = _next_reset_date_str(reset_day)

    if _is_daemon_running():
        daemon_str = f"{_GREEN}RUNNING (every {interval} s){_RESET}"
    else:
        daemon_str = f"{_RED}STOPPED (every {interval} s){_RESET}"

    print("=" * 82)
    print(f"  {_BOLD}UNIT NET CAP{_RESET} | Daemon: {daemon_str} | Reset Day: {reset_day} | Next: {next_reset}")
    print("=" * 82)
    print()

    # System interfaces block
    print(f" {_CYAN}[ SYSTEM INTERFACES ]{_RESET}")
    print(f"  {'Interface':<14} {'RX (In)':<12} {'TX (Out)':<12} {'Total':<12}")
    print("  " + "-" * 52)
    interfaces = state.get("interfaces", {})
    if not interfaces:
        print("  No interface statistics collected yet.")
    else:
        for if_name in sorted(interfaces.keys()):
            info = interfaces[if_name]
            rx = int(info.get("rx_month_bytes", 0))
            tx = int(info.get("tx_month_bytes", 0))
            print(f"  {if_name:<14} {format_bytes(rx):<12} {format_bytes(tx):<12} {format_bytes(rx + tx):<12}")

    print()

    # Monitored targets table
    print(f" {_CYAN}[ MONITORED TARGETS ]{_RESET}")
    print(
        f"  {'ID':<3} {'Type':<9} {'Name':<20} {'In':<9} {'Out':<9} "
        f"{'Total':<9} {'Limit':<8} {'Used%':<6} {'Status'}"
    )
    print("  " + "-" * 82)

    targets = state.get("targets", {})
    sorted_keys = sorted(targets.keys())
    if not sorted_keys:
        print("  No targets added yet. Choose [1] to add a systemd service or Docker container.")
    else:
        for idx, key in enumerate(sorted_keys, start=1):
            t = targets[key]
            t_type = "[SYSTEMD]" if t.get("type") == "systemd" else "[DOCKER]"
            name = str(t.get("name", ""))[:20]
            rx = int(t.get("rx_month_bytes", 0))
            tx = int(t.get("tx_month_bytes", 0))
            total = rx + tx
            limit_gb = float(t.get("limit_gb", 0.0) or 0.0)
            status_str = _format_status(t)

            if limit_gb > 0.0:
                limit_str = f"{limit_gb:g} GB"
                metric_gb = bytes_to_gb(_target_metric_bytes(t))
                pct_str = f"{int((metric_gb / limit_gb) * 100)}%"
            else:
                limit_str = "-"
                pct_str = "-"

            print(
                f"  {idx:<3} {t_type:<9} {name:<20} {format_bytes(rx):<9} {format_bytes(tx):<9} "
                f"{format_bytes(total):<9} {limit_str:<8} {pct_str:<6} {status_str}"
            )

    print()
    print("=" * 82)
    print(f" {_BOLD}[ACTIONS]{_RESET}")
    print("  1) Add target (Systemd / Docker)      4) Reset counters")
    print("  2) Edit target (Limit / Mode)         5) Settings")
    print("  3) Remove target                      6) Refresh view")
    print("  0) Exit")
    print()


def _prompt_limit_gb(default_gb: float | None = None) -> float | None:
    """Prompt user for limit in GB (0 = monitor only). Returns None if invalid."""
    hint = f" (current {default_gb:g} GB)" if default_gb is not None else ""
    raw = input(f" > Enter monthly traffic limit in GB (0 = Monitor only){hint}: ").strip()
    if not raw and default_gb is not None:
        return default_gb
    try:
        val = float(raw.replace(",", "."))
    except ValueError:
        print(" [!] Invalid number.")
        return None
    if val < 0:
        print(" [!] Limit cannot be negative.")
        return None
    return val


def _prompt_mode(default_mode: str) -> str | None:
    """Prompt user for counting mode: 1=both, 2=egress, 3=ingress."""
    raw = input(
        f" > Select counting mode [1: Both (In+Out), 2: Egress (Out), 3: Ingress (In)] "
        f"(default {default_mode}): "
    ).strip().lower()
    if not raw:
        return default_mode
    mapping = {
        "1": "both",
        "both": "both",
        "2": "egress",
        "egress": "egress",
        "3": "ingress",
        "ingress": "ingress",
    }
    if raw in mapping:
        return mapping[raw]
    print(" [!] Invalid mode.")
    return None


def _screen_add_target(state: dict[str, Any]) -> None:
    """Interactive screen for adding a systemd unit or Docker container."""
    show_hidden = False
    default_mode = state.get("settings", {}).get("default_mode", DEFAULT_MODE)

    while True:
        _clear_screen()
        print("=" * 82)
        print(f"  {_BOLD}ADD TARGET TO MONITORING{_RESET}")
        print("=" * 82)
        if show_hidden:
            print(" Showing ALL systemd units and Docker containers.")
        else:
            print(" Showing application units and Docker containers. (System services hidden)")
        print()

        candidates: list[dict[str, str]] = []
        for u in list_systemd_units(include_hidden=show_hidden):
            candidates.append(
                {
                    "type": "systemd",
                    "badge": "[SYSTEMD]",
                    "state": u["active_state"],
                    "name": u["name"],
                    "network_mode": "",
                }
            )
        for c in list_docker_containers():
            candidates.append(
                {
                    "type": "docker",
                    "badge": "[DOCKER]",
                    "state": c["active_state"],
                    "name": c["name"],
                    "network_mode": c.get("network_mode", ""),
                }
            )

        print(f"  {'#':<4} {'Type':<10} {'State':<10} {'Name'}")
        print("  " + "-" * 76)
        if not candidates:
            print("  No available targets found.")
        else:
            for idx, item in enumerate(candidates, start=1):
                print(f"  {idx:<4} {item['badge']:<10} {item['state']:<10} {item['name']}")

        print()
        print("-" * 82)
        print("  [A] Toggle hidden systemd units   [0] Back to main menu")
        choice = input("\n Enter number to add (or A / 0): ").strip()

        if choice == "0":
            return
        if choice.lower() == "a":
            show_hidden = not show_hidden
            continue
        if not choice.isdigit():
            print(" [!] Enter a valid number, A, or 0.")
            _pause()
            continue

        idx_num = int(choice)
        if not (1 <= idx_num <= len(candidates)):
            print(" [!] Number out of range.")
            _pause()
            continue

        selected = candidates[idx_num - 1]
        t_type = selected["type"]
        name = selected["name"]
        key = f"{t_type}:{name}"

        if key in state.get("targets", {}):
            print(f" [!] '{name}' is already in the monitored list.")
            _pause()
            continue

        print(f"\n > Selected: {name} {selected['badge']}")
        if t_type == "docker" and selected.get("network_mode") == "host":
            print(" [!] Warning: Container uses 'host' network mode. Docker stats may be 0 B or match host.")

        limit_gb = _prompt_limit_gb()
        if limit_gb is None:
            _pause()
            continue

        mode = _prompt_mode(default_mode)
        if mode is None:
            _pause()
            continue

        if t_type == "systemd":
            if not ensure_ip_accounting_dropin(name):
                print(f" [!] Error: failed to create drop-in config for '{name}'.")
                _pause()
                continue
            if selected["state"] == "active":
                ans = input(f" > Restart '{name}' now to activate IP accounting? [Y/n]: ").strip().lower()
                if ans in ("", "y", "yes"):
                    ok, msg = restart_unit_if_active(name)
                    print(f" [{'OK' if ok else '!'}] {msg}")
                else:
                    daemon_reload()
                    print(" [OK] Drop-in created. Restart service later to start counting.")
            else:
                daemon_reload()
                print(f" [OK] Drop-in created for '{name}' (service is not active).")

        state.setdefault("targets", {})[key] = {
            "type": t_type,
            "name": name,
            "limit_gb": limit_gb,
            "mode": mode,
            "rx_month_bytes": 0,
            "tx_month_bytes": 0,
            "last_rx_raw": 0,
            "last_tx_raw": 0,
            "blocked_by_quota": False,
            "original_restart_policy": None,
        }
        save_state(state)
        print(f" [OK] '{name}' added to monitoring.")
        _pause()
        return


def _pick_monitored_target(state: dict[str, Any], title: str) -> tuple[str, dict[str, Any]] | None:
    """Helper to select one monitored target by ID."""
    targets: dict[str, dict[str, Any]] = state.get("targets", {})
    keys = sorted(targets.keys())
    if not keys:
        print("\n No monitored targets available.")
        _pause()
        return None

    _clear_screen()
    print("=" * 82)
    print(f"  {_BOLD}{title}{_RESET}")
    print("=" * 82)
    for idx, key in enumerate(keys, start=1):
        t = targets[key]
        badge = "[SYSTEMD]" if t.get("type") == "systemd" else "[DOCKER]"
        print(f"  {idx}) {badge:<10} {t.get('name')}  (Limit: {float(t.get('limit_gb', 0)):g} GB, Mode: {t.get('mode')})")
    print("  0) Cancel")

    raw = input("\n Select target number: ").strip()
    if raw == "0" or not raw:
        return None
    if not raw.isdigit():
        print(" [!] Enter a valid number.")
        _pause()
        return None
    num = int(raw)
    if not (1 <= num <= len(keys)):
        print(" [!] Number out of range.")
        _pause()
        return None
    chosen_key = keys[num - 1]
    return chosen_key, targets[chosen_key]


def _screen_edit_target(state: dict[str, Any]) -> None:
    """Screen to edit limit and mode of a monitored target."""
    picked = _pick_monitored_target(state, "EDIT TARGET")
    if picked is None:
        return
    _, target = picked

    current_limit = float(target.get("limit_gb", 0.0) or 0.0)
    current_mode = str(target.get("mode", DEFAULT_MODE))

    new_limit = _prompt_limit_gb(default_gb=current_limit)
    if new_limit is None:
        _pause()
        return

    new_mode = _prompt_mode(default_mode=current_mode)
    if new_mode is None:
        _pause()
        return

    target["limit_gb"] = new_limit
    target["mode"] = new_mode
    if new_limit == 0.0:
        target["blocked_by_quota"] = False

    save_state(state)
    print(" [OK] Target updated.")
    _pause()


def _screen_remove_target(state: dict[str, Any]) -> None:
    """Screen to remove a target from monitoring."""
    picked = _pick_monitored_target(state, "REMOVE TARGET")
    if picked is None:
        return
    key, target = picked

    ans = input(f" > Remove '{target.get('name')}' from monitoring? [y/N]: ").strip().lower()
    if ans not in ("y", "yes"):
        print(" Cancelled.")
        _pause()
        return

    state["targets"].pop(key, None)
    if target.get("type") == "systemd":
        remove_ip_accounting_dropin(str(target.get("name", "")))
        daemon_reload()

    save_state(state)
    print(" [OK] Target removed.")
    _pause()


def _screen_reset_counters(state: dict[str, Any]) -> None:
    """Screen to reset one target counter or all counters."""
    _clear_screen()
    print("=" * 82)
    print(f"  {_BOLD}RESET COUNTERS{_RESET}")
    print("=" * 82)
    print("  1) Reset single target counter")
    print("  2) Reset ALL counters (interfaces & targets)")
    print("  0) Back")

    choice = input("\n Select option [0-2]: ").strip()
    if choice == "0":
        return
    if choice == "1":
        picked = _pick_monitored_target(state, "RESET SINGLE TARGET COUNTER")
        if picked is None:
            return
        _, target = picked
        was_blocked = bool(target.get("blocked_by_quota"))
        reset_target_counter(target)
        if was_blocked:
            if target.get("type") == "systemd":
                restore_unit(str(target.get("name", "")))
            elif target.get("type") == "docker":
                restore_container(str(target.get("name", "")), target.get("original_restart_policy"))
        save_state(state)
        print(f" [OK] Counter reset for '{target.get('name')}'.")
        _pause()
    elif choice == "2":
        ans = input(" > Reset ALL monthly counters now? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print(" Cancelled.")
            _pause()
            return
        to_restore = reset_month_counters(state)
        for item in to_restore:
            if item.get("type") == "systemd":
                restore_unit(str(item.get("name", "")))
            elif item.get("type") == "docker":
                restore_container(str(item.get("name", "")), item.get("original_restart_policy"))
        save_state(state)
        print(" [OK] All counters reset.")
        _pause()


def _screen_settings(state: dict[str, Any]) -> None:
    """Screen for global settings: polling interval, reset day, and default mode."""
    while True:
        settings = state.setdefault("settings", {})
        interval = int(settings.get("poll_interval_sec", DEFAULT_POLL_INTERVAL_SEC))
        reset_day = int(settings.get("reset_day", DEFAULT_RESET_DAY))
        def_mode = str(settings.get("default_mode", DEFAULT_MODE))

        _clear_screen()
        print("=" * 82)
        print(f"  {_BOLD}SETTINGS{_RESET}")
        print("=" * 82)
        print(f"  1) Poll interval      : {interval} sec ({MIN_POLL_INTERVAL_SEC}..{MAX_POLL_INTERVAL_SEC})")
        print(f"  2) Monthly reset day  : {reset_day} ({MIN_RESET_DAY}..{MAX_RESET_DAY})")
        print(f"  3) Default mode       : {def_mode} (both / egress / ingress)")
        print("  0) Back to main menu")

        choice = input("\n Select setting to change [0-3]: ").strip()
        if choice == "0":
            return
        if choice == "1":
            raw = input(f" > Enter poll interval in seconds ({MIN_POLL_INTERVAL_SEC}..{MAX_POLL_INTERVAL_SEC}): ").strip()
            try:
                val = int(raw)
                if MIN_POLL_INTERVAL_SEC <= val <= MAX_POLL_INTERVAL_SEC:
                    settings["poll_interval_sec"] = val
                    save_state(state)
                    print(" [OK] Poll interval updated.")
                else:
                    print(" [!] Value out of allowed range.")
            except ValueError:
                print(" [!] Enter an integer.")
            _pause()
        elif choice == "2":
            raw = input(f" > Enter reset day of month ({MIN_RESET_DAY}..{MAX_RESET_DAY}): ").strip()
            try:
                val = int(raw)
                if MIN_RESET_DAY <= val <= MAX_RESET_DAY:
                    settings["reset_day"] = val
                    save_state(state)
                    print(" [OK] Reset day updated.")
                else:
                    print(" [!] Day must be between 1 and 28.")
            except ValueError:
                print(" [!] Enter an integer.")
            _pause()
        elif choice == "3":
            new_mode = _prompt_mode(def_mode)
            if new_mode is not None:
                settings["default_mode"] = new_mode
                save_state(state)
                print(" [OK] Default mode updated.")
            _pause()


def run_tui() -> int:
    """Main interactive TUI loop."""
    while True:
        state = load_state()
        _render_main_screen(state)
        try:
            choice = input(" Select action [0-6]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice == "0":
            return 0
        if choice == "1":
            _screen_add_target(state)
        elif choice == "2":
            _screen_edit_target(state)
        elif choice == "3":
            _screen_remove_target(state)
        elif choice == "4":
            _screen_reset_counters(state)
        elif choice == "5":
            _screen_settings(state)
        elif choice == "6":
            continue
        else:
            print(" [!] Please select a number from 0 to 6.")
            _pause()