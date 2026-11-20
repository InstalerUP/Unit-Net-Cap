"""Background daemon loop for polling traffic, enforcing quotas, and monthly resets."""

import signal
import sys
import time
from typing import Any

from .config import DEFAULT_POLL_INTERVAL_SEC, gb_to_bytes
from .docker_ctrl import (
    block_container,
    enforce_container_blocked,
    read_all_containers_net_bytes,
    restore_container,
)
from .net_proc import read_interface_counters
from .storage import (
    compute_delta,
    is_reset_due,
    load_state,
    reset_month_counters,
    save_state,
)
from .systemd_ctrl import (
    block_unit,
    enforce_unit_blocked,
    is_protected_unit,
    read_unit_ip_bytes,
    restore_unit,
)

_STOP_REQUESTED = False


def _handle_signal(signum: int, _frame: Any) -> None:
    """Handle SIGTERM and SIGINT for graceful shutdown."""
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    _ = signum


def _target_metric_bytes(target: dict[str, Any]) -> int:
    """Return measured byte count according to target's counting mode."""
    rx = int(target.get("rx_month_bytes", 0))
    tx = int(target.get("tx_month_bytes", 0))
    mode = target.get("mode", "both")
    if mode == "egress":
        return tx
    if mode == "ingress":
        return rx
    return rx + tx


def _restore_quota_blocked_targets(targets_to_restore: list[dict[str, Any]]) -> None:
    """Restore autostart and running state for targets blocked by quota."""
    for item in targets_to_restore:
        t_type = item.get("type")
        name = item.get("name", "")
        if not name:
            continue
        if t_type == "systemd":
            if not is_protected_unit(name):
                restore_unit(name)
        elif t_type == "docker":
            restore_container(name, item.get("original_restart_policy"))


def run_poll_cycle(state: dict[str, Any]) -> dict[str, Any]:
    """Execute a single monitoring and enforcement cycle."""
    # 1. Check scheduled monthly reset
    if is_reset_due(state):
        to_restore = reset_month_counters(state)
        _restore_quota_blocked_targets(to_restore)

    # 2. Update system network interfaces
    iface_counters = read_interface_counters()
    interfaces_state: dict[str, dict[str, int]] = state.setdefault("interfaces", {})

    for iface_name, (curr_rx, curr_tx) in iface_counters.items():
        if iface_name not in interfaces_state:
            interfaces_state[iface_name] = {
                "rx_month_bytes": 0,
                "tx_month_bytes": 0,
                "last_rx_raw": curr_rx,
                "last_tx_raw": curr_tx,
            }
            continue

        entry = interfaces_state[iface_name]
        d_rx, new_rx_raw = compute_delta(curr_rx, entry.get("last_rx_raw", 0), is_first_sample=False)
        d_tx, new_tx_raw = compute_delta(curr_tx, entry.get("last_tx_raw", 0), is_first_sample=False)
        entry["rx_month_bytes"] = int(entry.get("rx_month_bytes", 0)) + d_rx
        entry["tx_month_bytes"] = int(entry.get("tx_month_bytes", 0)) + d_tx
        entry["last_rx_raw"] = new_rx_raw
        entry["last_tx_raw"] = new_tx_raw

    # 3. Fetch Docker stats once per cycle if any docker target exists
    targets: dict[str, dict[str, Any]] = state.setdefault("targets", {})
    has_docker = any(t.get("type") == "docker" for t in targets.values())
    docker_stats = read_all_containers_net_bytes() if has_docker else {}

    # 4. Poll and enforce each monitored target
    for target in targets.values():
        t_type = target.get("type")
        name = target.get("name", "")
        if not name:
            continue

        # Never enforce quota on protected system units
        if t_type == "systemd" and is_protected_unit(name):
            continue

        # If already blocked by quota, keep it stopped and disabled
        if target.get("blocked_by_quota"):
            if t_type == "systemd":
                enforce_unit_blocked(name)
            elif t_type == "docker":
                enforce_container_blocked(name)
            continue

        # Read current raw counters
        raw_pair: tuple[int, int] | None = None
        if t_type == "systemd":
            raw_pair = read_unit_ip_bytes(name)
        elif t_type == "docker":
            raw_pair = docker_stats.get(name)

        if raw_pair is not None:
            curr_rx, curr_tx = raw_pair
            has_baseline = ("last_rx_raw" in target and "last_tx_raw" in target)
            is_first = not has_baseline or (
                int(target.get("rx_month_bytes", 0)) == 0
                and int(target.get("tx_month_bytes", 0)) == 0
                and int(target.get("last_rx_raw", 0)) == 0
                and int(target.get("last_tx_raw", 0)) == 0
            )
            d_rx, new_rx_raw = compute_delta(curr_rx, target.get("last_rx_raw", 0), is_first_sample=is_first)
            d_tx, new_tx_raw = compute_delta(curr_tx, target.get("last_tx_raw", 0), is_first_sample=is_first)

            target["rx_month_bytes"] = int(target.get("rx_month_bytes", 0)) + d_rx
            target["tx_month_bytes"] = int(target.get("tx_month_bytes", 0)) + d_tx
            target["last_rx_raw"] = new_rx_raw
            target["last_tx_raw"] = new_tx_raw

        # Check quota limit (0 means monitor-only)
        limit_gb = float(target.get("limit_gb", 0.0) or 0.0)
        if limit_gb > 0.0:
            limit_bytes = gb_to_bytes(limit_gb)
            used_bytes = _target_metric_bytes(target)
            if used_bytes >= limit_bytes:
                if t_type == "systemd":
                    if block_unit(name):
                        target["blocked_by_quota"] = True
                elif t_type == "docker":
                    ok, prev_policy = block_container(name)
                    if ok:
                        target["original_restart_policy"] = prev_policy
                        target["blocked_by_quota"] = True

    return state


def run_daemon() -> int:
    """Main entry point for the background daemon."""
    global _STOP_REQUESTED
    _STOP_REQUESTED = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    state = load_state()
    try:
        save_state(state)
    except OSError as exc:
        print(f"Error: cannot write initial state file: {exc}", file=sys.stderr)
        return 1

    while not _STOP_REQUESTED:
        state = load_state()
        try:
            state = run_poll_cycle(state)
            save_state(state)
        except OSError as exc:
            print(f"Error: failed to save state during poll cycle: {exc}", file=sys.stderr)

        interval = int(state.get("settings", {}).get("poll_interval_sec", DEFAULT_POLL_INTERVAL_SEC))
        # Sleep in short steps so SIGTERM exits promptly
        slept = 0
        while not _STOP_REQUESTED and slept < interval:
            step = min(1, interval - slept)
            time.sleep(step)
            slept += step

    # Final save before exit
    try:
        save_state(state)
    except OSError:
        pass

    return 0