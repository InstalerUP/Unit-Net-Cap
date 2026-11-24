"""System network interface traffic reader using /proc/net/dev."""

from pathlib import Path
import subprocess

PROC_NET_DEV = Path("/proc/net/dev")
SYS_CLASS_NET = Path("/sys/class/net")


def read_interface_counters(proc_path: Path = PROC_NET_DEV) -> dict[str, tuple[int, int]]:
    """
    Read RX and TX byte counters for all network interfaces.

    Returns:
        dict mapping interface_name -> (rx_bytes, tx_bytes)
    """
    result: dict[str, tuple[int, int]] = {}
    try:
        lines = proc_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result

    # Skip first two header lines of /proc/net/dev
    for line in lines[2:]:
        if ":" not in line:
            continue
        iface_part, stats_part = line.split(":", 1)
        iface = iface_part.strip()
        if not iface:
            continue

        fields = stats_part.split()
        # In /proc/net/dev:
        # index 0 = receive bytes (RX), index 8 = transmit bytes (TX)
        if len(fields) < 9:
            continue
        try:
            rx_bytes = int(fields[0])
            tx_bytes = int(fields[8])
        except ValueError:
            continue

        result[iface] = (max(0, rx_bytes), max(0, tx_bytes))

    return result


def default_route_interfaces() -> set[str]:
    """Return interfaces used by IPv4 or IPv6 default routes."""
    try:
        proc = subprocess.run(
            ["ip", "-o", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode != 0:
        return set()

    result: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        for index, value in enumerate(parts[:-1]):
            if value == "dev" and parts[index + 1]:
                result.add(parts[index + 1])
                break
    return result


def main_interface_names(
    interface_names: set[str] | list[str], extra_interfaces: list[str] | tuple[str, ...] = ()
) -> set[str]:
    """Return physical, default-route, and explicitly selected interfaces."""
    available = set(interface_names)
    extras = {name.strip() for name in extra_interfaces if isinstance(name, str) and name.strip()}
    physical = {
        name for name in available
        if name != "lo" and (SYS_CLASS_NET / name / "device").exists()
    }
    return (physical | default_route_interfaces() | extras) & available


def visible_interface_names(
    interface_names: set[str] | list[str], view_mode: str, extra_interfaces: list[str] | tuple[str, ...] = ()
) -> set[str]:
    """Select interfaces for display without changing collected counters."""
    available = set(interface_names)
    if view_mode == "all":
        return available
    return main_interface_names(available, extra_interfaces)
