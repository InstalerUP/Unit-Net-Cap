#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="${NETCAP_REPO_OWNER:-InstalerUP}"
REPO_NAME="${NETCAP_REPO_NAME:-unit-net-cap}"
REPO_BRANCH="${NETCAP_REPO_BRANCH:-main}"
ARCHIVE_URL="https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/refs/heads/${REPO_BRANCH}"

INSTALL_LIB_DIR="/usr/local/lib/unit-net-cap"
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

    # If run inside an existing repository checkout with src/ and systemd/
    local script_dir=""
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
    if [ -n "$script_dir" ] && [ -d "$script_dir/src" ] && [ -f "$script_dir/systemd/netcap.service" ]; then
        src_root="$script_dir"
        echo "Installing from local directory: $src_root"
    else
        tmp_dir="$(mktemp -d)"
        trap '[ -n "${tmp_dir:-}" ] && rm -rf "$tmp_dir"' EXIT
        echo "Downloading ${REPO_OWNER}/${REPO_NAME} (${REPO_BRANCH})..."
        curl -fsSL "$ARCHIVE_URL" | tar -xz -C "$tmp_dir"
        src_root="$(find "$tmp_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
        if [ -z "$src_root" ] || [ ! -d "$src_root/src" ]; then
            echo "Error: failed to download or unpack repository archive." >&2
            exit 1
        fi
    fi

    cleanup_line_endings "$src_root"

    mkdir -p "$INSTALL_LIB_DIR" "$STATE_DIR"
    chmod 755 "$STATE_DIR"

    rm -rf "${INSTALL_LIB_DIR:?}/"*
    cp -r "$src_root/src/"* "$INSTALL_LIB_DIR/"
    find "$INSTALL_LIB_DIR" -type f -name "*.py" -exec chmod 644 {} +

    cat > "$INSTALL_BIN_FILE" <<'EOF'
#!/usr/bin/env python3
import sys

sys.path.insert(0, "/usr/local/lib/unit-net-cap")
from main import main

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