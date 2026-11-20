"""Systemd unit discovery, IP accounting drop-in management, and quota control."""

from pathlib import Path
import subprocess

from .config import DROPIN_FILENAME, HIDDEN_UNIT_PREFIXES, PROTECTED_UNITS, SYSTEMD_DIR


def _run_systemctl(args: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """Run systemctl safely and return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def is_protected_unit(unit_name: str) -> bool:
    """Return True if unit is in the safety blacklist."""
    return unit_name.strip() in PROTECTED_UNITS


def is_hidden_unit(unit_name: str) -> bool:
    """Return True if unit looks like system noise and should be hidden by default."""
    name = unit_name.strip()
    if is_protected_unit(name):
        return True
    return any(name.startswith(prefix) for prefix in HIDDEN_UNIT_PREFIXES)


def list_systemd_units(include_hidden: bool = False) -> list[dict[str, str]]:
    """
    List service units from systemd.

    Returns list of dicts: {"name": ..., "active_state": ..., "sub_state": ...}
    """
    rc, out, _ = _run_systemctl(
        ["list-units", "--type=service", "--all", "--no-legend", "--no-pager"]
    )
    units: list[dict[str, str]] = []
    seen: set[str] = set()

    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            unit_name = parts[0].strip()
            active_state = parts[2].strip()
            sub_state = parts[3].strip()
            if not unit_name.endswith(".service") or unit_name in seen:
                continue
            if is_protected_unit(unit_name):
                continue
            if not include_hidden and is_hidden_unit(unit_name):
                continue
            seen.add(unit_name)
            units.append(
                {
                    "name": unit_name,
                    "active_state": active_state,
                    "sub_state": sub_state,
                }
            )

    # Also include known unit files that might not appear in list-units
    rc_files, out_files, _ = _run_systemctl(
        ["list-unit-files", "--type=service", "--no-legend", "--no-pager"]
    )
    if rc_files == 0 and out_files:
        for line in out_files.splitlines():
            parts = line.split()
            if not parts:
                continue
            unit_name = parts[0].strip()
            if not unit_name.endswith(".service") or unit_name in seen:
                continue
            if is_protected_unit(unit_name):
                continue
            if not include_hidden and is_hidden_unit(unit_name):
                continue
            seen.add(unit_name)
            state_rc, state_out, _ = _run_systemctl(["is-active", unit_name])
            active_state = state_out if state_out else ("active" if state_rc == 0 else "inactive")
            units.append(
                {
                    "name": unit_name,
                    "active_state": active_state,
                    "sub_state": active_state,
                }
            )

    units.sort(key=lambda item: item["name"])
    return units


def get_unit_active_state(unit_name: str) -> str:
    """Return unit active state ('active', 'inactive', 'failed', etc.)."""
    _, out, _ = _run_systemctl(["is-active", unit_name])
    return out or "unknown"


def is_unit_enabled(unit_name: str) -> bool:
    """Return True if unit is enabled for autostart."""
    rc, _, _ = _run_systemctl(["is-enabled", unit_name])
    return rc == 0


def read_unit_ip_bytes(unit_name: str) -> tuple[int, int] | None:
    """
    Read (ingress_bytes, egress_bytes) for a systemd service via systemctl show.
    Returns None if counters are unavailable or cannot be parsed.
    """
    rc, out, _ = _run_systemctl(
        ["show", unit_name, "-p", "IPIngressBytes", "-p", "IPEgressBytes"]
    )
    if rc != 0 or not out:
        return None

    ingress: int | None = None
    egress: int | None = None
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        # systemd reports "[not set]" or huge sentinel (~18446744073709551615) when accounting is off
        if not val.isdigit():
            continue
        num = int(val)
        if num >= (1 << 63):
            continue
        if key == "IPIngressBytes":
            ingress = num
        elif key == "IPEgressBytes":
            egress = num

    if ingress is None or egress is None:
        return None
    return max(0, ingress), max(0, egress)


def dropin_path_for_unit(unit_name: str, systemd_dir: Path = SYSTEMD_DIR) -> Path:
    """Return path to /etc/systemd/system/<unit>.d/netcap.conf."""
    return systemd_dir / f"{unit_name}.d" / DROPIN_FILENAME


def ensure_ip_accounting_dropin(unit_name: str, systemd_dir: Path = SYSTEMD_DIR) -> bool:
    """
    Create drop-in file enabling IPAccounting=yes for the unit.
    Returns True on success.
    """
    if is_protected_unit(unit_name):
        return False
    dropin_file = dropin_path_for_unit(unit_name, systemd_dir)
    try:
        dropin_file.parent.mkdir(parents=True, exist_ok=True)
        content = "[Service]\nIPAccounting=yes\n"
        dropin_file.write_text(content, encoding="utf-8")
        return True
    except OSError:
        return False


def remove_ip_accounting_dropin(unit_name: str, systemd_dir: Path = SYSTEMD_DIR) -> bool:
    """
    Remove drop-in file for the unit and clean up empty .d directory if empty.
    Returns True on success.
    """
    dropin_file = dropin_path_for_unit(unit_name, systemd_dir)
    dropin_dir = dropin_file.parent
    try:
        if dropin_file.exists():
            dropin_file.unlink()
        if dropin_dir.exists() and dropin_dir.is_dir():
            # Remove directory only if it has no remaining files
            if not any(dropin_dir.iterdir()):
                dropin_dir.rmdir()
        return True
    except OSError:
        return False


def daemon_reload() -> bool:
    """Reload systemd manager configuration."""
    rc, _, _ = _run_systemctl(["daemon-reload"])
    return rc == 0


def restart_unit_if_active(unit_name: str) -> tuple[bool, str]:
    """
    Reload systemd daemon and restart unit ONLY if it is currently active.
    If inactive, only reload daemon so drop-in is ready without starting stopped service.
    """
    if not daemon_reload():
        return False, "Failed to reload systemd daemon."

    state = get_unit_active_state(unit_name)
    if state == "active":
        rc, _, err = _run_systemctl(["restart", unit_name], timeout=30)
        if rc != 0:
            return False, f"Failed to restart {unit_name}: {err}"
        return True, f"Unit '{unit_name}' restarted (IP accounting active)."

    return True, f"Drop-in loaded for '{unit_name}' (unit is {state}, not started)."


def block_unit(unit_name: str) -> bool:
    """Disable autostart and stop the unit due to quota exceeded."""
    if is_protected_unit(unit_name):
        return False
    rc, _, _ = _run_systemctl(["disable", "--now", unit_name], timeout=30)
    if rc != 0:
        # Fallback: stop explicitly if disable returned non-zero (e.g., static unit)
        _run_systemctl(["stop", unit_name], timeout=30)
        rc_act, _, _ = _run_systemctl(["is-active", unit_name])
        return rc_act != 0
    return True


def enforce_unit_blocked(unit_name: str) -> None:
    """Ensure a quota-blocked unit stays stopped and autostart remains disabled."""
    if is_protected_unit(unit_name):
        return
    if is_unit_enabled(unit_name):
        _run_systemctl(["disable", unit_name], timeout=15)
    if get_unit_active_state(unit_name) == "active":
        _run_systemctl(["stop", unit_name], timeout=30)


def restore_unit(unit_name: str) -> bool:
    """Enable autostart and start unit after quota reset."""
    if is_protected_unit(unit_name):
        return False
    rc, _, _ = _run_systemctl(["enable", "--now", unit_name], timeout=30)
    return rc == 0