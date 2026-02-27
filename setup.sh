#!/usr/bin/env bash
# ==============================================================================
# Aldertech Dynamic Resource Governor (ADRG) — Setup Script
#
# Installs ADRG to /opt/adrg/, creates config at /etc/adrg/config.yaml,
# and enables the systemd service.
#
# Usage: sudo bash setup.sh
# ==============================================================================

set -euo pipefail

ADRG_SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ADRG_INSTALL_DIR="/opt/adrg"
CONFIG_DIR="/etc/adrg"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"
ENV_FILE="${CONFIG_DIR}/adrg.env"
SERVICE_NAME="adrg.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"

echo "======================================================"
echo "  Aldertech Dynamic Resource Governor — Setup"
echo "======================================================"
echo ""

# ── Preflight ──────────────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo bash setup.sh)."
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install it with: sudo apt install python3"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ $PYTHON_MAJOR -lt 3 ]] || [[ $PYTHON_MAJOR -eq 3 && $PYTHON_MINOR -lt 9 ]]; then
    echo "ERROR: Python 3.9 or higher is required (found ${PYTHON_VERSION})."
    exit 1
fi

echo "[1/6] Python ${PYTHON_VERSION} — OK"

# ── System dependencies ────────────────────────────────────────────────────

echo "[2/6] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3-pip
echo "      Done."

# ── Python dependencies ────────────────────────────────────────────────────

echo "[3/6] Installing Python dependencies..."
pip3 install --quiet --break-system-packages -r "${ADRG_SRC}/requirements.txt"
echo "      Done."

# ── Optional: systemd-python (enables sd_notify / watchdog integration) ───

echo "      Attempting optional systemd-python install..."
if apt-get install -y -qq libsystemd-dev pkg-config 2>/dev/null; then
    pip3 install --quiet --break-system-packages "systemd-python>=235" \
        && echo "      systemd-python installed — full systemd notify support enabled." \
        || echo "      systemd-python pip install failed — continuing without it (non-fatal)."
else
    echo "      libsystemd-dev not available — skipping systemd-python (non-fatal)."
fi

# ── Install application ────────────────────────────────────────────────────

echo "[4/6] Installing ADRG to ${ADRG_INSTALL_DIR}..."
mkdir -p "${ADRG_INSTALL_DIR}"
cp "${ADRG_SRC}/adrg.py" "${ADRG_INSTALL_DIR}/adrg.py"
cp -r "${ADRG_SRC}/modules" "${ADRG_INSTALL_DIR}/modules"
chmod 755 "${ADRG_INSTALL_DIR}/adrg.py"
echo "      Done."

# ── Configuration ──────────────────────────────────────────────────────────

echo "[5/6] Setting up configuration..."
mkdir -p "${CONFIG_DIR}"

if [[ ! -f "${CONFIG_FILE}" ]]; then
    cp "${ADRG_SRC}/config.yaml" "${CONFIG_FILE}"
    chmod 640 "${CONFIG_FILE}"
    echo "      Config created at ${CONFIG_FILE}"
    echo "      IMPORTANT: Edit this file before starting ADRG."
else
    echo "      Config already exists at ${CONFIG_FILE} — not overwritten."
fi

if [[ ! -f "${ENV_FILE}" ]]; then
    cat > "${ENV_FILE}" << 'EOF'
# ADRG secrets — loaded by the systemd service as environment variables.
# Reference these in config.yaml using ${VAR_NAME} placeholders.
#
# ADRG_MEDIA_API_KEY=your_jellyfin_or_plex_api_key
# ADRG_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
# ADRG_NTFY_URL=https://ntfy.sh/your-topic
# ADRG_NTFY_TOKEN=
# ADRG_GOTIFY_URL=http://gotify.local
# ADRG_GOTIFY_TOKEN=your_gotify_app_token
# ADRG_QB_PASSWORD=your_qbittorrent_password
EOF
    chmod 640 "${ENV_FILE}"
    echo "      Secrets template created at ${ENV_FILE}"
else
    echo "      Secrets file already exists at ${ENV_FILE} — not overwritten."
fi

# Create runtime directories
mkdir -p /run/adrg /var/log/adrg
echo "      Runtime directories created."

# ── Systemd service ────────────────────────────────────────────────────────

echo "[6/6] Installing and enabling systemd service..."
cp "${ADRG_SRC}/adrg.service" "${SERVICE_DST}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl start "${SERVICE_NAME}"
echo "      Done."

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "======================================================"
echo "  Setup complete"
echo "======================================================"
echo ""
echo "Next steps:"
echo "  1. Edit ${CONFIG_FILE} — assign your containers to tiers"
echo "  2. Edit ${ENV_FILE}  — add your API keys"
echo "  3. Run: sudo systemctl restart adrg"
echo ""
echo "Useful commands:"
echo "  systemctl status adrg             — Check daemon status"
echo "  journalctl -u adrg -f             — Follow live logs"
echo "  tail -f /var/log/adrg/adrg.log    — Follow ADRG log file"
echo "  python3 /opt/adrg/adrg.py --check-config  — Validate config"
echo "  python3 /opt/adrg/adrg.py --dry-run       — Observe without acting"
echo "  curl http://127.0.0.1:8765/status          — Live governor status"
echo "  kill -HUP \$(pidof adrg)           — Reload config (no restart)"
echo ""
