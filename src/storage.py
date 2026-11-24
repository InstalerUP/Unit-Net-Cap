"""State persistence, atomic writes, delta calculation, and monthly reset logic."""

from datetime import date
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .config import (
    DEFAULT_MODE,
    DEFAULT_POLL_INTERVAL_SEC,
    DEFAULT_RESET_DAY,
    MAX_POLL_INTERVAL_SEC,
    MAX_RESET_DAY,
    MIN_POLL_INTERVAL_SEC,
    MIN_RESET_DAY,
    STATE_DIR,
    STATE_FILE,
    VALID_MODES,
)


def current_period_str(today: date | None = None) -> str:
    """Return period string in YYYY-MM format."""
    d = today or date.today()
    return f"{d.year:04d}-{d.month:02d}"


def default_state(today: date | None = None) -> dict[str, Any]:
    """Create initial default state dictionary."""
    return {
        "schema_version": 1,
        "settings": {
            "poll_interval_sec": DEFAULT_POLL_INTERVAL_SEC,
            "default_mode": DEFAULT_MODE,
            "reset_day": DEFAULT_RESET_DAY,
        },
        "last_reset_period": current_period_str(today),
        "interfaces": {},
        "targets": {},
    }


def _normalize_state(raw: Any) -> dict[str, Any]:
    """Ensure loaded state has all required keys and safe types."""
    base = default_state()
    if not isinstance(raw, dict):
        return base

    settings = raw.get("settings")
    if isinstance(settings, dict):
        interval = settings.get("poll_interval_sec", DEFAULT_POLL_INTERVAL_SEC)
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            interval = DEFAULT_POLL_INTERVAL_SEC
        interval = max(MIN_POLL_INTERVAL_SEC, min(MAX_POLL_INTERVAL_SEC, interval))

        mode = settings.get("default_mode", DEFAULT_MODE)
        if mode not in VALID_MODES:
            mode = DEFAULT_MODE

        reset_day = settings.get("reset_day", DEFAULT_RESET_DAY)
        try:
            reset_day = int(reset_day)
        except (TypeError, ValueError):
            reset_day = DEFAULT_RESET_DAY
        reset_day = max(MIN_RESET_DAY, min(MAX_RESET_DAY, reset_day))

        base["settings"] = {
            "poll_interval_sec": interval,
            "default_mode": mode,
            "reset_day": reset_day,
        }

    period = raw.get("last_reset_period")
    if isinstance(period, str) and len(period) == 7 and "-" in period:
        base["last_reset_period"] = period

    interfaces = raw.get("interfaces")
    if isinstance(interfaces, dict):
        clean_ifaces: dict[str, dict[str, int]] = {}
        for if_name, info in interfaces.items():
            if not isinstance(if_name, str) or not isinstance(info, dict):
                continue
            clean_ifaces[if_name] = {
                "rx_month_bytes": max(0, int(info.get("rx_month_bytes", 0) or 0)),
                "tx_month_bytes": max(0, int(info.get("tx_month_bytes", 0) or 0)),
                "last_rx_raw": max(0, int(info.get("last_rx_raw", 0) or 0)),
                "last_tx_raw": max(0, int(info.get("last_tx_raw", 0) or 0)),
            }
        base["interfaces"] = clean_ifaces

    targets = raw.get("targets")
    if isinstance(targets, dict):
        clean_targets: dict[str, dict[str, Any]] = {}
        for key, item in targets.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                continue
            t_type = item.get("type", "systemd")
            if t_type not in ("systemd", "docker"):
                continue
            t_name = item.get("name", "")
            if not isinstance(t_name, str) or not t_name:
                continue
            mode = item.get("mode", DEFAULT_MODE)
            if mode not in VALID_MODES:
                mode = DEFAULT_MODE
            try:
                limit_gb = max(0.0, float(item.get("limit_gb", 0.0) or 0.0))
            except (TypeError, ValueError):
                limit_gb = 0.0

            policy = item.get("original_restart_policy")
            if policy is not None and not isinstance(policy, str):
                policy = None

            clean_targets[key] = {
                "type": t_type,
                "name": t_name,
                "limit_gb": limit_gb,
                "mode": mode,
                "rx_month_bytes": max(0, int(item.get("rx_month_bytes", 0) or 0)),
                "tx_month_bytes": max(0, int(item.get("tx_month_bytes", 0) or 0)),
                "last_rx_raw": max(0, int(item.get("last_rx_raw", 0) or 0)),
                "last_tx_raw": max(0, int(item.get("last_tx_raw", 0) or 0)),
                "blocked_by_quota": bool(item.get("blocked_by_quota", False)),
                "original_restart_policy": policy,
            }
        base["targets"] = clean_targets

    return base


def load_state(state_file: Path = STATE_FILE) -> dict[str, Any]:
    """Load state from JSON file. Returns default state if missing or corrupted."""
    if not state_file.exists():
        return default_state()
    try:
        content = state_file.read_text(encoding="utf-8")
        data = json.loads(content)
        return _normalize_state(data)
    except (OSError, json.JSONDecodeError, ValueError):
        return default_state()


def save_state(state: dict[str, Any], state_file: Path = STATE_FILE, state_dir: Path = STATE_DIR) -> None:
    """Atomically write state JSON using a temporary file and os.replace."""
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o755)

    normalized = _normalize_state(state)
    payload = json.dumps(normalized, indent=2, sort_keys=True) + "\n"

    fd, tmp_path_str = tempfile.mkstemp(prefix=".state_", suffix=".tmp", dir=str(state_dir))
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, state_file)
        os.chmod(state_file, 0o644)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def compute_delta(current_raw: int, last_raw: int, is_first_sample: bool) -> tuple[int, int]:
    """
    Calculate traffic delta and new raw baseline.

    - If first sample: baseline is set to current_raw, added delta is 0.
    - If current_raw >= last_raw: normal growth -> delta = current_raw - last_raw.
    - If current_raw < last_raw: counter reset / service or host restart -> delta = current_raw.
    """
    curr = max(0, int(current_raw))
    prev = max(0, int(last_raw))
    if is_first_sample:
        return 0, curr
    if curr >= prev:
        return curr - prev, curr
    return curr, curr


def is_reset_due(state: dict[str, Any], today: date | None = None) -> bool:
    """Check whether monthly counter reset should trigger on or after reset_day."""
    d = today or date.today()
    reset_day = int(state.get("settings", {}).get("reset_day", DEFAULT_RESET_DAY))
    if d.day < reset_day:
        return False
    last_period = state.get("last_reset_period", "")
    return last_period != current_period_str(d)


def reset_month_counters(state: dict[str, Any], today: date | None = None) -> list[dict[str, Any]]:
    """
    Reset monthly counters and return quota-blocked targets for restoration.
    The block marker remains until the target has actually been restored.
    """
    for iface in state.get("interfaces", {}).values():
        iface["rx_month_bytes"] = 0
        iface["tx_month_bytes"] = 0

    to_restore: list[dict[str, Any]] = []
    for target in state.get("targets", {}).values():
        target["rx_month_bytes"] = 0
        target["tx_month_bytes"] = 0
        if target.get("blocked_by_quota"):
            to_restore.append(target)

    state["last_reset_period"] = current_period_str(today)
    return to_restore


def reset_target_counter(target: dict[str, Any]) -> None:
    """Reset a target counter while keeping its raw baseline and block marker."""
    target["rx_month_bytes"] = 0
    target["tx_month_bytes"] = 0
