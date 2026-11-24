### Language:</br>
<img src="https://etf2l.org/images/flags/UnitedKingdom.gif" height="13" width="20"> - [Read in English](README.md)</br>
<img src="https://etf2l.org/images/flags/Russia.gif" height="13" width="20"> - [Читать на русском языке](README.ru.md)

# Unit Net Cap

A console utility for accounting and automatically limiting monthly network traffic of `systemd` services and Docker containers on Ubuntu Server / Linux.

## ✨ Features and Capabilities

- Traffic accounting for `systemd` services and `docker` containers;
- Automatic enforcement based on traffic volume;
- Protection against device reboots and manual service starts;
- Automatic monthly reset;
- Protection of critical services from being added to monitoring;
- Interactive TUI interface;
- CLI command support.

## 🚀 Installation, Updating, and Removal

### Installation and Updating
To install or update the utility on Ubuntu Server, run the following command with `root` privileges:
```bash
curl -fsSL https://raw.githubusercontent.com/InstalerUP/unit-net-cap/main/install.sh | sudo bash
```
<details>
  <summary><b>What the installation script</b> <code>install.sh</code> <b>does:</b></summary>

1. Checks for superuser privileges and a `Python 3` interpreter.
2. Downloads the latest version of the repository (or uses a local directory).
3. Copies package files to `/usr/local/lib/unit-net-cap/unit_net_cap`.
4. Creates the executable entry point `/usr/local/bin/netcap`.
5. Installs the `systemd` service `netcap.service` to `/etc/systemd/system/netcap.service`.
6. Starts and enables the background daemon via `systemctl enable --now netcap.service`.
</details>

### Uninstallation
To remove the utility and clean up the system, run:
```bash
sudo bash /usr/local/lib/unit-net-cap/unit_net_cap/install.sh --uninstall
```

## 💻 Terminal User Interface (TUI)
`netcap` — launches the interactive text-based interface (`sudo` required).

## ⌨️ CLI Commands Reference
- `netcap status` — view application and traffic status.
  - `--json` — output the current state in JSON format.
  - `--all-interfaces` — show all network interfaces without filtering.
- `netcap add <name> --limit <GB>` — add a target to monitoring.
  - `--limit <float>` — monthly limit in gigabytes (`0` activates monitoring-only mode).
  - `--type <systemd|docker>` — target type (auto-detected if not specified).
  - `--mode <both|egress|ingress>` — accounting mode (`both` — incoming + outgoing, `egress` — outgoing, `ingress` — incoming).
  - `--no-restart` — do not restart an active system service immediately after applying changes.
- `netcap set <name>` — modify parameters of an existing target.
  - `--limit <float>` — monthly limit in gigabytes (`0` activates monitoring-only mode).
  - `--mode <both|egress|ingress>` — accounting mode (`both` — incoming + outgoing, `egress` — outgoing, `ingress` — incoming).
- `netcap remove <name>` — remove a target from monitoring.
- `netcap reset <name>` — reset a traffic counter.
  - `--all` — reset counters for all targets.
- `netcap config` — view and modify settings (prints the current configuration when run without arguments).
  - `--interval <sec>` — traffic polling interval (`30` – `10800` seconds).
  - `--default-mode <both|egress|ingress>` — default accounting mode for new targets.
  - `--reset-day <1..28>` — day of the month for automatic counter resets.
  - `--interface-view-mode <main|all>` — display mode for system interfaces.
  - `--add-iface <name>` — always show the specified network interface in `main` view.
  - `--remove-iface <name>` — remove an interface from the always-visible list in `main` view.
- `netcap daemon` — run the background daemon (started automatically).
- `netcap uninstall` — remove the program, stop the service, and restore all quota-blocked services and containers (`sudo` required).

## ⚙ Technical Details
<details>
  <summary>Click to expand</summary>

### Technologies Used
- **Language:** Python 3 (no third-party `pip` packages).
- **System Integrations:**
  - `systemd` (service management via `systemctl`, counter parsing via `systemctl show`, drop-in directories).
  - Docker Engine API (via `docker stats` and `docker inspect` / `docker update` CLI commands).
  - Linux network interfaces (`/proc/net/dev`).
- **Data Storage Format:** JSON.

### System Requirements
- **Operating System:** Linux (tested on Ubuntu Server / Debian-based distributions).
- **Privileges:** Superuser privileges (`root` / `sudo`) are required to manage `systemd` services, configure `IPAccounting`, and manage Docker containers.

### Dependencies
The project is built **with no external third-party dependencies**:

1. **Python 3** (version 3.8 or higher) — *included in the OS / Ubuntu repositories*.
2. **systemd** — *standard init system on Ubuntu / Debian*.
3. **Docker Engine** (optional, required only if you plan to monitor and limit Docker container traffic).
4. **Python 3 standard library modules** (`argparse`, `json`, `pathlib`, `shutil`, `signal`, `subprocess`, `time`, `datetime`, `os`, `sys`).

### Architecture and How It Works

1. **Interface Monitoring (`net_proc.py`):**
   The daemon periodically reads `/proc/net/dev`, measuring incoming (`RX`) and outgoing (`TX`) traffic across network interfaces (with support for filtering virtual/bridge interfaces).
2. **`systemd` Traffic Accounting (`systemd_ctrl.py`):**
   When a service is added, the utility creates `/etc/systemd/system/<unit>.d/netcap.conf` with the `IPAccounting=yes` directive. After restarting the service, `systemd` tracks network packets and bytes for that unit, which are read via `systemctl show`.
3. **Docker Traffic Accounting (`docker_ctrl.py`):**
   The daemon calls `docker stats --no-stream --format json` to retrieve current cumulative network counters (`net_input`, `net_output`) for each running container.
4. **Background Daemon (`daemon.py`):**
   Runs in a continuous loop at a configurable interval. It calculates the traffic delta since the last poll, adds it to the monthly counter, saves state to `/var/lib/unit-net-cap/state.json`, and checks limits.

### Configuration and State Files

- **State and statistics file:** `/var/lib/unit-net-cap/state.json`
- **Daemon `systemd` unit configuration:** `/etc/systemd/system/netcap.service`
- **Service accounting drop-in files:** `/etc/systemd/system/<unit>.d/netcap.conf`
</details>

## 📄 License

This project is licensed under the [MIT](LICENSE) license. You are free to use, modify, and distribute this software.