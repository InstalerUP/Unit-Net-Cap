"""System network interface traffic reader using /proc/net/dev."""

from pathlib import Path

PROC_NET_DEV = Path("/proc/net/dev")


def read_interface_counters(proc_path: Path = PROC_NET_DEV) -> dict[str, tuple[int, int]]:
    """
    Read RX and TX byte counters for network interfaces (excluding 'lo').

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
        if not iface or iface == "lo":
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