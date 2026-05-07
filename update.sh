#!/usr/bin/env bash
# =============================================================================
# update.sh — Pull latest release from GitHub and restart the service
#
# Called automatically by the printer-app-update systemd timer (daily 03:00).
# Can also be run manually:
#   sudo /opt/printer_application/update.sh
# =============================================================================

set -euo pipefail

GITHUB_BRANCH="main"
BASE_DIR="/opt/printer_application"
SRC_DIR="$BASE_DIR"
VENV_DIR="$BASE_DIR/.venv"
SERVICE_NAME="printer-app"

info() { echo "[INFO]  $*"; }
ok()   { echo "[OK]    $*"; }
err()  { echo "[ERROR] $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || err "update.sh must be run as root"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
    FORCE=1
fi

if [[ $FORCE -eq 0 ]]; then
    DB_PATH="/var/lib/printer_app/printers.db"
    if [[ -f "$DB_PATH" ]]; then
        AUTO_UPDATE=$(sqlite3 "$DB_PATH" "SELECT auto_update FROM settings WHERE id=1;" 2>/dev/null || echo "1")
        if [[ "$AUTO_UPDATE" == "0" ]]; then
            info "Auto-update is disabled in settings. Skipping update. (Use --force to override)"
            exit 0
        fi
    fi
fi

# --------------------------------------------------------------------------- #
# 1. Pull latest source

# --------------------------------------------------------------------------- #
info "Fetching latest source from GitHub..."
git -C "$SRC_DIR" fetch --quiet origin "$GITHUB_BRANCH"

LOCAL=$(git  -C "$SRC_DIR" rev-parse HEAD)
REMOTE=$(git -C "$SRC_DIR" rev-parse "origin/$GITHUB_BRANCH")

if [[ "$LOCAL" == "$REMOTE" ]]; then
    ok "Already up to date ($(git -C "$SRC_DIR" log -1 --format='%h %s')). Nothing to do."
    exit 0
fi

info "Update available: $LOCAL -> $REMOTE"
git -C "$SRC_DIR" reset --hard "origin/$GITHUB_BRANCH" --quiet
ok "Source updated to $(git -C "$SRC_DIR" log -1 --format='%h %s')."

# --------------------------------------------------------------------------- #
# 2. Upgrade the package inside the venv
# --------------------------------------------------------------------------- #
info "Upgrading printer-app package..."
"$VENV_DIR/bin/pip" install --quiet --upgrade "$SRC_DIR"
ok "Package upgraded."

# --------------------------------------------------------------------------- #
# 3. Restart the service
# --------------------------------------------------------------------------- #
info "Restarting $SERVICE_NAME..."
systemctl restart "$SERVICE_NAME"
ok "Service restarted."

echo ""
ok "Update complete — running $(${VENV_DIR}/bin/printer-app --version 2>/dev/null || echo 'latest')."
