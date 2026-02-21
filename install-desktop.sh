#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# install-desktop.sh — Bootstrap installer for yt-dlp GUI
#
# Creates venv, installs deps, generates .desktop file + icon,
# and validates system dependencies (ffmpeg, python3).
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_NAME="yt-dlp GUI"
APP_ID="ytdlp-gui"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"
MAIN_SCRIPT="${SCRIPT_DIR}/ytdlp_gui.py"
ICON_DIR="${SCRIPT_DIR}/assets"
ICON_FILE="${ICON_DIR}/icon.png"
DESKTOP_DIR="${HOME}/.local/share/applications"
DESKTOP_FILE="${DESKTOP_DIR}/${APP_ID}.desktop"

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
echo "  ${APP_NAME} — Installer"
echo "═══════════════════════════════════════════"
echo ""

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

# ── Create / rebuild venv ──
if [ -d "$VENV_DIR" ]; then
    warn "Existing venv found — rebuilding."
    rm -rf "$VENV_DIR"
fi

info "Creating virtual environment…"
"$PYTHON" -m venv "$VENV_DIR"

# Verify venv activation script exists
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
    # Fallback: install directly
    pip install customtkinter yt-dlp --quiet
fi

info "Dependencies installed."

# ── Verify yt-dlp import ──
python -c "import yt_dlp; print(f'  yt-dlp {yt_dlp.version.__version__}')" || fail "yt-dlp failed to import"
python -c "import customtkinter; print(f'  CustomTkinter {customtkinter.__version__}')" || fail "CustomTkinter failed to import"

# ── Create icon directory ──
mkdir -p "$ICON_DIR"
if [ ! -f "$ICON_FILE" ]; then
    warn "No icon.png found in assets/. You can add one later."
    # Generate a placeholder SVG-to-PNG would require imagemagick,
    # so we skip and let the app run without an icon.
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

# Update desktop database if available
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
echo "═══════════════════════════════════════════"
echo ""