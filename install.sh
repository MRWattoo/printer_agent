#!/usr/bin/env bash
# =============================================================================
# install.sh — Install Printer Application on Ubuntu from GitHub
#
# Usage:
#   # Fresh install (pulls latest from GitHub):
#   bash <(curl -fsSL https://raw.githubusercontent.com/MRWattoo/printer_agent/main/install.sh)
#
#   # Or after cloning:
#   sudo bash install.sh
#
#   # Uninstall:
#   sudo bash install.sh --uninstall
#
# What it does:
#   1. Installs system dependencies (python3-venv, git, libusb, libjpeg)
#   2. Creates a dedicated system user 'printer-app'
#   3. Clones the repo to /opt/printer_application_src
#   4. Installs the package into /opt/printer_app_venv
#   5. Creates data directory /var/lib/printer_app
#   6. Installs and enables the printer-app systemd service
#   7. Installs a daily auto-update systemd timer
# =============================================================================

set -euo pipefail

# --------------------------------------------------------------------------- #
# Config — edit GITHUB_REPO before uploading to GitHub
# --------------------------------------------------------------------------- #
GITHUB_REPO="MRWattoo/printer_agent"
GITHUB_BRANCH="main"

APP_USER="printer-app"
SRC_DIR="/opt/printer_application_src"
VENV_DIR="/opt/printer_app_venv"
DATA_DIR="/var/lib/printer_app"
SERVICE_NAME="printer-app"
UPDATE_SERVICE_NAME="printer-app-update"

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
info() { echo "[INFO]  $*"; }
ok()   { echo "[OK]    $*"; }
err()  { echo "[ERROR] $*" >&2; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || err "This script must be run as root:  sudo bash install.sh"
}

# --------------------------------------------------------------------------- #
# 1. System dependencies
# --------------------------------------------------------------------------- #
install_system_deps() {
    info "Updating package lists..."
    apt-get update -qq

    info "Installing system dependencies..."
    apt-get install -y -qq \
        python3 \
        python3-pip \
        python3-venv \
        git \
        curl \
        libusb-1.0-0 \
        libusb-1.0-0-dev \
        libjpeg-dev \
        zlib1g-dev

    ok "System dependencies installed."
}

# --------------------------------------------------------------------------- #
# 2. Dedicated system user
# --------------------------------------------------------------------------- #
create_user() {
    if id "$APP_USER" &>/dev/null; then
        info "User '$APP_USER' already exists — skipping."
    else
        info "Creating system user '$APP_USER'..."
        useradd \
            --system \
            --no-create-home \
            --shell /usr/sbin/nologin \
            --comment "Printer Application service account" \
            "$APP_USER"
        ok "User '$APP_USER' created."
    fi
}

# --------------------------------------------------------------------------- #
# 3. Clone / update source from GitHub
# --------------------------------------------------------------------------- #
fetch_source() {
    if [[ -d "$SRC_DIR/.git" ]]; then
        info "Source already cloned — pulling latest..."
        git -C "$SRC_DIR" fetch --quiet origin "$GITHUB_BRANCH"
        git -C "$SRC_DIR" reset --hard "origin/$GITHUB_BRANCH" --quiet
    else
        info "Cloning https://github.com/${GITHUB_REPO} ..."
        git clone --quiet --branch "$GITHUB_BRANCH" \
            "https://github.com/${GITHUB_REPO}.git" "$SRC_DIR"
    fi
    ok "Source at $SRC_DIR is up to date."
}

# --------------------------------------------------------------------------- #
# 4. Python virtual environment + package install
# --------------------------------------------------------------------------- #
install_package() {
    if [[ ! -d "$VENV_DIR" ]]; then
        info "Creating virtual environment at $VENV_DIR..."
        python3 -m venv "$VENV_DIR"
    fi

    info "Upgrading pip..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip

    info "Installing / upgrading printer-app package..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade "$SRC_DIR"

    ok "Package installed into $VENV_DIR."
}

# --------------------------------------------------------------------------- #
# 5. Data directory
# --------------------------------------------------------------------------- #
setup_data_dir() {
    info "Ensuring data directory $DATA_DIR exists..."
    mkdir -p "$DATA_DIR"
    chown "$APP_USER:$APP_USER" "$DATA_DIR"
    chmod 750 "$DATA_DIR"
    ok "Data directory ready."
}

# --------------------------------------------------------------------------- #
# 6. Main systemd service
# --------------------------------------------------------------------------- #
install_service() {
    local SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
    info "Writing $SERVICE_FILE..."
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Printer Application (Flask + ESC/POS Agent)
After=network.target

[Service]
Type=simple
User=${APP_USER}
ExecStart=${VENV_DIR}/bin/printer-app
Restart=always
RestartSec=5
Environment=PRINTER_APP_DATA=${DATA_DIR}
# Uncomment to override defaults:
# Environment=PRINTER_APP_PORT=5000
# Environment=PRINTER_APP_HOST=0.0.0.0
StandardOutput=journal
StandardError=journal
SyslogIdentifier=printer-app

[Install]
WantedBy=multi-user.target
EOF
    ok "Service file written."
}

# --------------------------------------------------------------------------- #
# 7. Auto-update systemd timer (runs daily at 03:00)
# --------------------------------------------------------------------------- #
install_update_timer() {
    local UPDATE_SVC="/etc/systemd/system/${UPDATE_SERVICE_NAME}.service"
    local UPDATE_TMR="/etc/systemd/system/${UPDATE_SERVICE_NAME}.timer"

    info "Writing auto-update service and timer..."

    cat > "$UPDATE_SVC" <<EOF
[Unit]
Description=Printer Application — daily auto-update from GitHub
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=${SRC_DIR}/update.sh
StandardOutput=journal
StandardError=journal
SyslogIdentifier=printer-app-update
EOF

    cat > "$UPDATE_TMR" <<EOF
[Unit]
Description=Run Printer Application auto-update daily at 03:00

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

    ok "Auto-update timer written."
}

# --------------------------------------------------------------------------- #
# Reload systemd and start / restart everything
# --------------------------------------------------------------------------- #
enable_services() {
    info "Reloading systemd..."
    systemctl daemon-reload

    info "Enabling and starting $SERVICE_NAME..."
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"

    info "Enabling auto-update timer..."
    systemctl enable "${UPDATE_SERVICE_NAME}.timer"
    systemctl start  "${UPDATE_SERVICE_NAME}.timer"

    ok "All services started."
}

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
main() {
    require_root

    echo "================================================"
    echo "  Printer Application Installer"
    echo "  GitHub: https://github.com/${GITHUB_REPO}"
    echo "================================================"

    install_system_deps
    create_user
    fetch_source
    install_package
    setup_data_dir
    install_service
    install_update_timer
    enable_services

    echo ""
    echo "================================================"
    ok "Installation complete!"
    echo ""
    echo "  Service status  : sudo systemctl status printer-app"
    echo "  Live logs       : sudo journalctl -u printer-app -f"
    echo "  Update logs     : sudo journalctl -u printer-app-update"
    echo "  Web UI          : http://$(hostname -I | awk '{print $1}'):5000"
    echo ""
    echo "  Manual update   : sudo ${SRC_DIR}/update.sh"
    echo "  Uninstall       : sudo bash install.sh --uninstall"
    echo "================================================"
}

# --------------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------------- #
uninstall() {
    require_root

    info "Stopping services..."
    systemctl stop  "$SERVICE_NAME"                   2>/dev/null || true
    systemctl stop  "${UPDATE_SERVICE_NAME}.timer"    2>/dev/null || true
    systemctl disable "$SERVICE_NAME"                 2>/dev/null || true
    systemctl disable "${UPDATE_SERVICE_NAME}.timer"  2>/dev/null || true

    info "Removing systemd unit files..."
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    rm -f "/etc/systemd/system/${UPDATE_SERVICE_NAME}.service"
    rm -f "/etc/systemd/system/${UPDATE_SERVICE_NAME}.timer"
    systemctl daemon-reload

    info "Removing virtual environment..."
    rm -rf "$VENV_DIR"

    info "Removing source clone..."
    rm -rf "$SRC_DIR"

    info "Removing data directory (SQLite DB)..."
    rm -rf "$DATA_DIR"

    info "Removing system user..."
    userdel "$APP_USER" 2>/dev/null || true

    ok "Uninstall complete."
}

# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
case "${1:-}" in
    --uninstall) uninstall ;;
    *)           main      ;;
esac
