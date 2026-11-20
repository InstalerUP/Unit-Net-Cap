"""Configuration constants, blacklists, and formatting helpers."""

from pathlib import Path

APP_NAME = "Unit Net Cap"
CLI_NAME = "netcap"

# System paths
STATE_DIR = Path("/var/lib/unit-net-cap")
STATE_FILE = STATE_DIR / "state.json"
SYSTEMD_DIR = Path("/etc/systemd/system")
NETCAP_SERVICE_FILE = SYSTEMD_DIR / "netcap.service"
INSTALL_LIB_DIR = Path("/usr/local/lib/unit-net-cap")
INSTALL_BIN_FILE = Path("/usr/local/bin/netcap")

DROPIN_FILENAME = "netcap.conf"

# Binary multiplier (1024-based)
BYTES_PER_KB = 1024
BYTES_PER_MB = 1024 ** 2
BYTES_PER_GB = 1024 ** 3

# Polling and schedule limits
MIN_POLL_INTERVAL_SEC = 30
MAX_POLL_INTERVAL_SEC = 10800  # 3 hours
DEFAULT_POLL_INTERVAL_SEC = 60

MIN_RESET_DAY = 1
MAX_RESET_DAY = 28
DEFAULT_RESET_DAY = 1

VALID_MODES = ("both", "egress", "ingress")
DEFAULT_MODE = "both"

# Safety blacklist: never allow monitoring/blocking these units
PROTECTED_UNITS = {
    "ssh.service",
    "sshd.service",
    "netcap.service",
    "systemd-networkd.service",
    "networking.service",
    "systemd-journald.service",
    "systemd-udevd.service",
    "dbus.service",
}

# System prefixes hidden by default in interactive unit selection
HIDDEN_UNIT_PREFIXES = (
    "systemd-",
    "getty@",
    "serial-getty@",
    "user@",
    "session-",
    "dbus",
    "udev",
    "polkit",
    "rsyslog",
    "cron",
    "snapd",
    "apport",
    "motd-news",
    "multipathd",
    "iscsid",
    "open-iscsi",
    "lvm2-",
    "mdadm",
    "plymouth-",
    "console-setup",
    "keyboard-setup",
    "proc-sys-fs-binfmt_misc",
    "dev-",
    "sys-",
    "run-",
    "-.mount",
    "boot-",
    "var-",
)


def format_bytes(num_bytes: int) -> str:
    """Format byte count into a clean human-readable string (1024-based)."""
    val = float(max(0, num_bytes))
    if val < BYTES_PER_KB:
        return f"{int(val)} B"
    if val < BYTES_PER_MB:
        return f"{val / BYTES_PER_KB:.1f} KB"
    if val < BYTES_PER_GB:
        return f"{val / BYTES_PER_MB:.1f} MB"
    return f"{val / BYTES_PER_GB:.2f} GB"


def gb_to_bytes(gb_value: float) -> int:
    """Convert GB (1024-based) to integer bytes."""
    return int(round(max(0.0, float(gb_value)) * BYTES_PER_GB))


def bytes_to_gb(num_bytes: int) -> float:
    """Convert integer bytes to GB (1024-based)."""
    return max(0, num_bytes) / BYTES_PER_GB