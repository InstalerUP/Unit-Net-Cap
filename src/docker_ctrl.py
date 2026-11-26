"""Docker container discovery, stats parsing, and per-container quota control."""

import subprocess


def _run_docker(args: list[str], timeout: int = 20) -> tuple[int, str, str]:
    """Run docker CLI safely and return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def is_docker_available() -> bool:
    """Return True if Docker CLI and daemon are available."""
    rc, _, _ = _run_docker(["info"], timeout=10)
    return rc == 0


def list_docker_containers() -> list[dict[str, str]]:
    """
    List all containers (running and stopped).

    Returns list of dicts: {"name": ..., "status": ..., "network_mode": ...}
    """
    if not is_docker_available():
        return []

    rc, out, _ = _run_docker(["ps", "-a", "--format", "{{.Names}}|{{.State}}"])
    if rc != 0 or not out:
        return []

    containers: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        name = parts[0].strip()
        state = parts[1].strip()
        if not name:
            continue

        net_rc, net_out, _ = _run_docker(
            ["inspect", "-f", "{{.HostConfig.NetworkMode}}", name],
            timeout=10,
        )
        net_mode = net_out.strip() if net_rc == 0 else "default"
        containers.append(
            {
                "name": name,
                "active_state": state,
                "network_mode": net_mode,
            }
        )

    containers.sort(key=lambda item: item["name"])
    return containers


def get_container_state(container_name: str) -> str:
    """Return container state ('running', 'exited', etc.) or 'unknown'."""
    rc, out, _ = _run_docker(["inspect", "-f", "{{.State.Status}}", container_name], timeout=10)
    return out.strip() if rc == 0 and out.strip() else "unknown"


def get_container_restart_policy(container_name: str) -> str:
    """Return container RestartPolicy Name ('no', 'always', 'unless-stopped', 'on-failure')."""
    rc, out, _ = _run_docker(
        ["inspect", "-f", "{{.HostConfig.RestartPolicy.Name}}", container_name],
        timeout=10,
    )
    policy = out.strip() if rc == 0 else ""
    return policy or "no"


def _parse_size_to_bytes(size_str: str) -> int:
    """
    Parse Docker size string like '12.4MB', '1.5GB', '512kB', '0B' into integer bytes.
    Supports both 1000-based (KB/MB/GB/TB) and binary (KiB/MiB/GiB/TiB) suffixes.
    """
    s = size_str.strip()
    if not s or s.startswith("0B"):
        return 0

    units_1024 = [
        ("tib", 1024 ** 4),
        ("gib", 1024 ** 3),
        ("mib", 1024 ** 2),
        ("kib", 1024),
        ("b", 1),
    ]
    units_1000 = [
        ("tb", 1000 ** 4),
        ("gb", 1000 ** 3),
        ("mb", 1000 ** 2),
        ("kb", 1000),
    ]

    low = s.lower()
    for suffix, mult in units_1024 + units_1000:
        if low.endswith(suffix):
            num_part = low[: -len(suffix)].strip()
            try:
                return max(0, int(round(float(num_part) * mult)))
            except ValueError:
                return 0
    return 0


def parse_net_io_pair(net_io_str: str) -> tuple[int, int]:
    """
    Parse Docker NetIO string like '1.24GB / 850MB' into (rx_bytes, tx_bytes).
    """
    if "/" not in net_io_str:
        return 0, 0
    left, right = net_io_str.split("/", 1)
    return _parse_size_to_bytes(left), _parse_size_to_bytes(right)


def _read_netdev_counters(pid: int) -> tuple[int, int] | None:
    """Read exact RX/TX byte counters from a container network namespace."""
    if pid <= 0:
        return None
    rx_total = 0
    tx_total = 0
    found_interface = False
    try:
        with open(f"/proc/{pid}/net/dev", "r", encoding="utf-8") as handle:
            lines = handle.readlines()[2:]
    except (OSError, ValueError):
        return None

    for line in lines:
        if ":" not in line:
            continue
        interface, values = line.rsplit(":", 1)
        interface = interface.strip()
        if not interface or interface == "lo":
            continue
        fields = values.split()
        if len(fields) < 16:
            continue
        try:
            rx_bytes = int(fields[0])
            tx_bytes = int(fields[8])
        except ValueError:
            continue
        rx_total += max(0, rx_bytes)
        tx_total += max(0, tx_bytes)
        found_interface = True

    # An empty interface list is valid for an isolated network namespace.
    return (rx_total, tx_total) if found_interface or not lines else None


def _read_stats_fallback() -> dict[str, tuple[int, int]]:
    """Read human-readable Docker stats if procfs counters are unavailable."""
    rc, out, _ = _run_docker(
        ["stats", "--no-stream", "--format", "{{.Name}}|{{.NetIO}}"],
        timeout=15,
    )
    result: dict[str, tuple[int, int]] = {}
    if rc != 0 or not out:
        return result
    for line in out.splitlines():
        if "|" not in line:
            continue
        name, net_io = line.split("|", 1)
        name = name.strip().lstrip("/")
        if not name:
            continue
        pair = parse_net_io_pair(net_io.strip())
        result[name] = pair
        result[name.lower()] = pair
    return result


def read_all_containers_net_bytes() -> dict[str, tuple[int, int]]:
    """
    Read cumulative raw RX/TX bytes for running Docker containers.

    The primary source is /proc/<container-pid>/net/dev. Unlike docker stats,
    procfs exposes exact byte counters without human-readable rounding.
    Host-network containers are skipped because their counters belong to the
    host network namespace and cannot be attributed to one container.
    """
    rc, ids_out, _ = _run_docker(["ps", "-q"], timeout=10)
    if rc != 0 or not ids_out:
        return {}
    container_ids = [item.strip() for item in ids_out.splitlines() if item.strip()]
    if not container_ids:
        return {}

    template = '{{.Name}}|{{.State.Pid}}|{{.HostConfig.NetworkMode}}'
    inspect_rc, inspect_out, _ = _run_docker(
        ["inspect", "-f", template, *container_ids],
        timeout=15,
    )
    result: dict[str, tuple[int, int]] = {}
    procfs_unavailable = inspect_rc != 0 or not inspect_out

    if inspect_rc == 0 and inspect_out:
        for line in inspect_out.splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3:
                procfs_unavailable = True
                continue
            name = parts[0].strip().lstrip("/")
            try:
                pid = int(parts[1].strip())
            except ValueError:
                procfs_unavailable = True
                continue
            network_mode = parts[2].strip()
            if not name or network_mode == "host":
                continue

            counters = _read_netdev_counters(pid)
            if counters is None:
                procfs_unavailable = True
                continue
            result[name] = counters
            result[name.lower()] = counters

    # Keep exact procfs readings and fall back only for missing containers.
    if procfs_unavailable:
        fallback = _read_stats_fallback()
        for name, counters in fallback.items():
            if name not in result:
                result[name] = counters
    return result


def block_container(container_name: str) -> tuple[bool, str]:
    """
    Save current restart policy, set restart policy to 'no', and stop container.
    Returns (success, previous_restart_policy).
    """
    prev_policy = get_container_restart_policy(container_name)
    update_rc, _, _ = _run_docker(["update", "--restart=no", container_name], timeout=15)
    if update_rc != 0:
        return False, prev_policy
    rc, _, _ = _run_docker(["stop", container_name], timeout=30)
    return rc == 0, prev_policy


def enforce_container_blocked(container_name: str) -> None:
    """Ensure a quota-blocked container stays stopped and restart policy remains 'no'."""
    current_policy = get_container_restart_policy(container_name)
    if current_policy != "no":
        _run_docker(["update", "--restart=no", container_name], timeout=15)
    if get_container_state(container_name) == "running":
        _run_docker(["stop", container_name], timeout=30)


def restore_container(container_name: str, previous_policy: str | None) -> bool:
    """Restore previous restart policy (if not 'no') and start container."""
    policy = (previous_policy or "").strip()
    if policy and policy != "no":
        _run_docker(["update", f"--restart={policy}", container_name], timeout=15)
    rc, _, _ = _run_docker(["start", container_name], timeout=30)
    return rc == 0

def docker_container_exists(container_name: str) -> bool:
    """Return True if container exists in Docker (running, stopped, or exited)."""
    if not is_docker_available():
        return False
    clean = container_name.strip()
    if not clean:
        return False
    rc, _, _ = _run_docker(["inspect", clean], timeout=10)
    return rc == 0
