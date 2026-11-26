#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="${NETCAP_REPO_OWNER:-InstalerUP}"
REPO_NAME="${NETCAP_REPO_NAME:-unit-net-cap}"

has_tty() {
    [ -t 0 ] || { [ -r /dev/tty ] && { : </dev/tty; } 2>/dev/null; }
}

if [ -z "${NETCAP_REPO_BRANCH:-}" ]; then
    if has_tty; then
        tty_in="/dev/stdin"
        if [ ! -t 0 ] && [ -r /dev/tty ]; then
            tty_in="/dev/tty"
        fi

        echo "=========================================="
        echo "  Unit Net Cap - Branch Selection"
        echo "=========================================="
        echo "1) main    - Stable main branch"
        echo "2) nightly - Nightly release / development"
        echo "3) Custom  - Enter branch name manually"
        echo "------------------------------------------"
        read -rp "Select option [1-3] (default 1): " choice <"$tty_in" || choice=""
        choice="${choice#"${choice%%[![:space:]]*}"}"
        choice="${choice%"${choice##*[![:space:]]}"}"

        case "$choice" in
            ""|"1")
                REPO_BRANCH="main"
                ;;
            "2")
                REPO_BRANCH="nightly"
                ;;
            "3")
                read -rp "Enter GitHub branch name: " custom_branch <"$tty_in" || custom_branch=""
                custom_branch="${custom_branch#"${custom_branch%%[![:space:]]*}"}"
                custom_branch="${custom_branch%"${custom_branch##*[![:space:]]}"}"
                if [ -n "$custom_branch" ]; then
                    REPO_BRANCH="$custom_branch"
                else
                    REPO_BRANCH="main"
                fi
                ;;
            *)
                REPO_BRANCH="$choice"
                ;;
        esac
    else
        REPO_BRANCH="main"
    fi
else
    REPO_BRANCH="${NETCAP_REPO_BRANCH}"
fi

ARCHIVE_URL="https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/refs/heads/${REPO_BRANCH}"

echo "Checking branch '${REPO_BRANCH}' on GitHub..."
HTTP_CODE="$(curl -sS --connect-timeout 10 --max-time 20 -o /dev/null -w "%{http_code}" "$ARCHIVE_URL" || echo "000")"
if [ "$HTTP_CODE" = "000" ]; then
    echo "Error: Failed to reach GitHub while checking branch '${REPO_BRANCH}' (network/timeout error)." >&2
    exit 1
fi
if [ "$HTTP_CODE" -ne 200 ]; then
    echo "Error: Branch '${REPO_BRANCH}' does not exist on GitHub (HTTP status: ${HTTP_CODE})." >&2
    exit 1
fi

INSTALL_LIB_DIR="/usr/local/lib/unit-net-cap"
INSTALL_PKG_DIR="${INSTALL_LIB_DIR}/unit_net_cap"
INSTALL_BIN_FILE="/usr/local/bin/netcap"
STATE_DIR="/var/lib/unit-net-cap"
SERVICE_FILE="/etc/systemd/system/netcap.service"

require_root() {
    if [ "${EUID:-$(id -u)}" -ne 0 ]; then
        echo "Error: Please run as root or with sudo." >&2
        exit 1
    fi
}

cleanup_line_endings() {
    local target_dir="$1"
    find "$target_dir" -type f \( -name "*.py" -o -name "*.sh" -o -name "*.service" -o -name "*.md" \) \
        -exec sed -i 's/\r$//' {} +
}

do_uninstall() {
    require_root
    echo "Uninstalling Unit Net Cap..."

    if [ -x "$INSTALL_BIN_FILE" ]; then
        "$INSTALL_BIN_FILE" uninstall --yes || true
    fi

    systemctl disable --now netcap.service >/dev/null 2>&1 || true
    rm -f "$SERVICE_FILE"
    rm -f /etc/systemd/system/*.d/netcap.conf
    find /etc/systemd/system -maxdepth 1 -type d -name "*.d" -empty -delete 2>/dev/null || true
    systemctl daemon-reload || true

    rm -rf "$STATE_DIR" "$INSTALL_LIB_DIR"
    rm -f "$INSTALL_BIN_FILE"

    echo "Unit Net Cap removed."
}

do_install_or_update() {
    require_root

    if ! command -v python3 >/dev/null 2>&1; then
        echo "Error: python3 is required." >&2
        exit 1
    fi

    local src_root=""
    local tmp_dir=""

    trap 'if [ -n "${tmp_dir:-}" ] && [ -d "${tmp_dir:-}" ]; then rm -rf "$tmp_dir"; fi' EXIT

    local script_dir=""
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
    if [ -n "$script_dir" ] && [ -d "$script_dir/src" ] && [ -f "$script_dir/systemd/netcap.service" ]; then
        src_root="$script_dir"
        echo "Installing from local directory: $src_root"
    else
        tmp_dir="$(mktemp -d)"
        echo "Downloading ${REPO_OWNER}/${REPO_NAME} (${REPO_BRANCH})..."
        curl -fsSL --connect-timeout 10 --max-time 120 "$ARCHIVE_URL" | tar -xz -C "$tmp_dir"
        src_root="$(find "$tmp_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
        if [ -z "$src_root" ] || [ ! -d "$src_root/src" ]; then
            echo "Error: failed to download or unpack repository archive." >&2
            exit 1
        fi
    fi

    cleanup_line_endings "$src_root"

    rm -rf "${INSTALL_LIB_DIR:?}/"*
    mkdir -p "$INSTALL_PKG_DIR" "$STATE_DIR"
    chmod 755 "$INSTALL_LIB_DIR" "$INSTALL_PKG_DIR" "$STATE_DIR"

    cp -r "$src_root/src/"* "$INSTALL_PKG_DIR/"
    find "$INSTALL_PKG_DIR" -type f -name "*.py" -exec chmod 644 {} +

    cat > "$INSTALL_BIN_FILE" <<'EOF'
#!/usr/bin/env python3
import sys

sys.path.insert(0, "/usr/local/lib/unit-net-cap")
from unit_net_cap.main import main

if __name__ == "__main__":
    sys.exit(main())
EOF
    chmod 755 "$INSTALL_BIN_FILE"

    cp "$src_root/systemd/netcap.service" "$SERVICE_FILE"
    chmod 644 "$SERVICE_FILE"

    systemctl daemon-reload
    systemctl enable --now netcap.service
    systemctl restart netcap.service

    echo "Unit Net Cap installed/updated and service is running."
}

if [ "${1:-}" = "--uninstall" ]; then
    do_uninstall
else
    do_install_or_update
fi