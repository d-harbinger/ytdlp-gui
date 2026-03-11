#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# install-desktop.sh — Bootstrap installer for yt-dlp GUI v1.3.0
#
# Creates venv, installs deps, generates .desktop file + icon,
# creates config directory, and validates system dependencies.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_NAME="yt-dlp GUI"
APP_ID="ytdlp-gui"
APP_VERSION="1.3.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"
MAIN_SCRIPT="${SCRIPT_DIR}/ytdlp_gui.py"
ICON_DIR="${SCRIPT_DIR}/assets"
ICON_FILE="${ICON_DIR}/icon.png"
DESKTOP_DIR="${HOME}/.local/share/applications"
DESKTOP_FILE="${DESKTOP_DIR}/${APP_ID}.desktop"
CONFIG_DIR="${HOME}/.config/${APP_ID}"

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✔]${NC} $*"; }
warn()  { echo -e "${YELLOW}[⚠]${NC} $*"; }
fail()  { echo -e "${RED}[✖]${NC} $*"; exit 1; }

# ── Pre-flight checks ──
echo ""
echo "═══════════════════════════════════════════"
echo "  ${APP_NAME} v${APP_VERSION} — Installer"
echo "═══════════════════════════════════════════"
echo ""

# Verify main script exists
[ -f "$MAIN_SCRIPT" ] || fail "Main script not found: ${MAIN_SCRIPT}"

# Python 3.10+
PYTHON=""
for py in python3 python; do
    if command -v "$py" &>/dev/null; then
        ver=$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$py"
            break
        fi
    fi
done
[ -z "$PYTHON" ] && fail "Python 3.10+ is required. Found: $(python3 --version 2>/dev/null || echo 'none')"
info "Python: $($PYTHON --version)"

# python3-venv
"$PYTHON" -c "import ensurepip" 2>/dev/null || fail "python3-venv is missing. Install with: sudo apt install python3-venv python3-pip"

# ffmpeg
if command -v ffmpeg &>/dev/null; then
    info "ffmpeg: $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"
else
    warn "ffmpeg not found. Required for merging video/audio and audio extraction."
    warn "Install with: sudo apt install ffmpeg"
fi

# deno (required for YouTube — yt-dlp EJS challenge solver)
if command -v deno &>/dev/null; then
    info "deno: $(deno --version 2>&1 | head -1 | awk '{print $2}')"
else
    warn "deno not found. Required for YouTube downloads (EJS JS runtime)."
    warn "Install with: curl -fsSL https://deno.land/install.sh | sh"
    warn "Then add to PATH: export PATH=\"\$HOME/.deno/bin:\$PATH\""
fi

# ── Create / rebuild venv ──
if [ -d "$VENV_DIR" ]; then
    warn "Existing venv found — rebuilding."
    rm -rf "$VENV_DIR"
fi

info "Creating virtual environment…"
"$PYTHON" -m venv "$VENV_DIR"

[ -f "${VENV_DIR}/bin/activate" ] || fail "venv creation failed — missing activate script. Try: sudo apt install python3-venv python3-pip"
source "${VENV_DIR}/bin/activate"
info "venv activated: $(which python)"

# ── Install dependencies ──
info "Upgrading pip…"
pip install --upgrade pip --quiet

info "Installing requirements…"
if [ -f "${SCRIPT_DIR}/requirements.txt" ]; then
    pip install -r "${SCRIPT_DIR}/requirements.txt" --quiet
else
    pip install customtkinter yt-dlp --quiet
fi

info "Dependencies installed."

# ── Verify imports ──
python -c "import yt_dlp; print(f'  yt-dlp {yt_dlp.version.__version__}')" || fail "yt-dlp failed to import"
python -c "import customtkinter; print(f'  CustomTkinter {customtkinter.__version__}')" || fail "CustomTkinter failed to import"
python -c "import yt_dlp_ejs; print(f'  yt-dlp-ejs {yt_dlp_ejs.__version__}')" || fail "yt-dlp-ejs failed to import"

# ── Config directory ──
mkdir -p "$CONFIG_DIR"
info "Config directory: ${CONFIG_DIR}"
if [ -f "${CONFIG_DIR}/settings.conf" ]; then
    info "Existing settings.conf preserved."
fi

# ── Icon directory ──
mkdir -p "$ICON_DIR"
if [ ! -f "$ICON_FILE" ]; then
    warn "No icon.png found in assets/. You can add one later."
fi

# ── Desktop Entry ──
mkdir -p "$DESKTOP_DIR"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=${APP_NAME}
Comment=Download videos and audio with yt-dlp
Exec=${VENV_DIR}/bin/python ${MAIN_SCRIPT}
Icon=${ICON_FILE}
Terminal=false
Categories=AudioVideo;Network;Utility;
StartupWMClass=${APP_ID}
EOF

chmod +x "$DESKTOP_FILE"

if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

info "Desktop entry created: ${DESKTOP_FILE}"

# ── Done ──
echo ""
echo "═══════════════════════════════════════════"
echo -e "  ${GREEN}Installation complete!${NC}"
echo ""
echo "  Launch from your app menu, or run:"
echo "    ${VENV_DIR}/bin/python ${MAIN_SCRIPT}"
echo ""
echo "  Config:  ${CONFIG_DIR}/settings.conf"
echo "  Desktop: ${DESKTOP_FILE}"
echo "═══════════════════════════════════════════"
echo ""