#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# uninstall.sh — Remove what install.sh created, for yt-dlp GUI
#
# NOTE: you do not need to run this before re-installing. install.sh is
# idempotent — it reuses a working venv and rebuilds a stale one in place.
# This is for actually removing the app from a machine.
#
# Removes (for THIS machine only, by default):
#   • the venv — in-project venv/ and venv-<host>/, and the local-disk
#     copy under XDG_DATA_HOME/ytdlp-gui/
#   • the .desktop entry
#
# Keeps your settings unless --purge is given.
#
#   bash uninstall.sh              # show what would go, then confirm
#   bash uninstall.sh --yes        # no prompt
#   bash uninstall.sh --purge      # also delete ~/.config/ytdlp-gui
#   bash uninstall.sh --all-hosts  # also other machines' venv-* trees
#   bash uninstall.sh --dry-run    # show only, change nothing
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_NAME="yt-dlp GUI"
APP_ID="ytdlp-gui"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="${HOME}/.local/share/applications"
DESKTOP_FILE="${DESKTOP_DIR}/${APP_ID}.desktop"
CONFIG_DIR="${HOME}/.config/${APP_ID}"
XDG_DATA="${XDG_DATA_HOME:-${HOME}/.local/share}"
DATA_DIR="${XDG_DATA}/${APP_ID}"
LAUNCHER="${DATA_DIR}/launch.sh"
HOST_TAG="$(uname -n 2>/dev/null || echo "local")"

if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; NC=''
fi
info() { echo -e "${GREEN}[✔]${NC} $*"; }
warn() { echo -e "${YELLOW}[⚠]${NC} $*"; }
fail() { echo -e "${RED}[✖]${NC} $*"; exit 1; }

ASSUME_YES=0; PURGE=0; ALL_HOSTS=0; DRY_RUN=0; FOREIGN_VENV=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y)    ASSUME_YES=1 ;;
        --purge)     PURGE=1 ;;
        --all-hosts) ALL_HOSTS=1 ;;
        --dry-run|-n) DRY_RUN=1 ;;
        --help|-h)   sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) fail "Unknown option: ${arg}  (try --help)" ;;
    esac
done

# ── Collect targets ───────────────────────────────────────────────────
# Only ever paths this project's installer creates. Every candidate is
# checked against those shapes before it can reach the removal loop.
TARGETS=()

add_target() {
    local p="$1"
    [ -e "$p" ] || return 0
    case "$p" in
        "${SCRIPT_DIR}"/venv|"${SCRIPT_DIR}"/venv-*) ;;
        "${DATA_DIR}"|"${DATA_DIR}"/venv-*|"${LAUNCHER}") ;;
        "${DESKTOP_FILE}") ;;
        "${CONFIG_DIR}") ;;
        *) warn "skipping unexpected path: ${p}"; return 0 ;;
    esac
    TARGETS+=("$p")
}

# The plain venv/ is ambiguous on a shared mount: it may belong to a
# DIFFERENT machine (the project folder is shared; the venv is not).
# Probe pip's baked-in shebang — if it doesn't run here, the venv isn't
# ours and removing it would break the other machine's install.
venv_is_ours() {
    [ -x "$1/bin/pip" ] && "$1/bin/pip" --version &>/dev/null
}

if venv_is_ours "${SCRIPT_DIR}/venv"; then
    add_target "${SCRIPT_DIR}/venv"
elif [ -d "${SCRIPT_DIR}/venv" ]; then
    if [ "$ALL_HOSTS" -eq 1 ]; then
        add_target "${SCRIPT_DIR}/venv"
    else
        FOREIGN_VENV=1
    fi
fi

if [ "$ALL_HOSTS" -eq 1 ]; then
    for d in "${SCRIPT_DIR}"/venv-*; do add_target "$d"; done
    add_target "${DATA_DIR}"
else
    add_target "${SCRIPT_DIR}/venv-${HOST_TAG}"
    add_target "${DATA_DIR}/venv-${HOST_TAG}"
fi

add_target "$LAUNCHER"
add_target "$DESKTOP_FILE"
[ "$PURGE" -eq 1 ] && add_target "$CONFIG_DIR"

# deno is deliberately NOT removed: install.sh only ever installs it on
# explicit --with-deno, it lives outside this app's directories, and other
# tools may rely on it. Mentioned in the report instead.

# ── Report ────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════"
echo "  ${APP_NAME} — Uninstall"
echo "═══════════════════════════════════════════"
echo ""

if [ "${#TARGETS[@]}" -eq 0 ]; then
    info "Nothing to remove — no ${APP_NAME} artifacts found for this machine."
    [ "$PURGE" -eq 0 ] && [ -d "$CONFIG_DIR" ] && \
        warn "Settings still present at ${CONFIG_DIR} (use --purge to remove)."
    exit 0
fi

echo "  The following will be removed:"
for t in "${TARGETS[@]}"; do
    size="$(du -sh "$t" 2>/dev/null | cut -f1 || echo '?')"
    printf '    %-8s %s\n' "$size" "$t"
done
echo ""

if [ "$PURGE" -eq 0 ] && [ -d "$CONFIG_DIR" ]; then
    info "Settings KEPT at ${CONFIG_DIR} (use --purge to remove them too)."
    echo ""
fi
if [ "$FOREIGN_VENV" -eq 1 ]; then
    info "${SCRIPT_DIR}/venv belongs to ANOTHER machine — left alone."
    echo "     (this project folder is shared; that venv is not ours to delete)"
    echo ""
fi
if [ "$ALL_HOSTS" -eq 0 ]; then
    other=$(find "$SCRIPT_DIR" "$DATA_DIR" -maxdepth 1 -name 'venv-*' \
              ! -name "venv-${HOST_TAG}" 2>/dev/null | wc -l)
    if [ "$other" -gt 0 ]; then
        info "${other} venv(s) from OTHER machines left alone (use --all-hosts to remove)."
        echo ""
    fi
fi

if [ "$DRY_RUN" -eq 1 ]; then
    info "Dry run — nothing was changed."
    exit 0
fi

if [ "$ASSUME_YES" -eq 0 ]; then
    if [ ! -t 0 ]; then
        fail "Not a terminal and --yes not given — refusing to remove anything."
    fi
    printf "  Proceed? [y/N] "
    read -r reply
    case "$reply" in
        [yY]|[yY][eE][sS]) ;;
        *) info "Aborted — nothing removed."; exit 0 ;;
    esac
fi

# ── Remove ────────────────────────────────────────────────────────────
for t in "${TARGETS[@]}"; do
    rm -rf -- "$t"
    info "removed ${t}"
done

# Drop the data dir if the venv removal emptied it.
if [ -d "$DATA_DIR" ] && [ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
    rmdir "$DATA_DIR" && info "removed empty ${DATA_DIR}"
fi

if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

echo ""
info "${APP_NAME} uninstalled."
[ "$PURGE" -eq 0 ] && [ -d "$CONFIG_DIR" ] && \
    echo "  Settings preserved: ${CONFIG_DIR}"
if [ -x "${HOME}/.local/bin/deno" ]; then
    echo "  deno left in place: ${HOME}/.local/bin/deno"
    echo "    (other tools may use it — remove by hand if you want it gone)"
fi
echo "  Reinstall any time with: bash ${SCRIPT_DIR}/install.sh"
echo ""
