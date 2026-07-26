#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# install.sh — Bootstrap installer for yt-dlp GUI v1.4.0
#
# Creates venv, installs deps, generates .desktop file + icon,
# creates config directory, and validates system dependencies.
#
# Distro-agnostic: detects the host package manager and prints the
# correct install command for whatever is missing. Supports apt, dnf/yum,
# pacman, zypper, apk, xbps, emerge and Homebrew; falls back to a plain
# description of the needed package when the manager is unrecognised.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_NAME="yt-dlp GUI"
APP_ID="ytdlp-gui"
APP_VERSION="1.4.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# VENV_DIR is resolved per-host in the "Create / rebuild venv" section
# below (plain venv/ when it runs here, else venv-<host>).
MAIN_SCRIPT="${SCRIPT_DIR}/ytdlp_gui.py"
ICON_DIR="${SCRIPT_DIR}/assets"
ICON_FILE="${ICON_DIR}/icon.png"
DESKTOP_DIR="${HOME}/.local/share/applications"
DESKTOP_FILE="${DESKTOP_DIR}/${APP_ID}.desktop"
CONFIG_DIR="${HOME}/.config/${APP_ID}"
LAUNCHER="${XDG_DATA_HOME:-${HOME}/.local/share}/${APP_ID}/launch.sh"

# ── Colors (disabled when not a terminal, so logs stay readable) ──
if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; NC=''
fi

info()  { echo -e "${GREEN}[✔]${NC} $*"; }
warn()  { echo -e "${YELLOW}[⚠]${NC} $*"; }
fail()  { echo -e "${RED}[✖]${NC} $*"; exit 1; }

# ── Options ──
WITH_DENO=0
for arg in "$@"; do
    case "$arg" in
        --with-deno) WITH_DENO=1 ;;
        --help|-h)
            sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
            echo ""
            echo "Options:"
            echo "  --with-deno   Also install deno (verified download) — needed"
            echo "                for YouTube downloads. Off by default: deno is"
            echo "                third-party software from outside your distro."
            echo "  YTDLP_GUI_VENV=/path   Put the venv somewhere specific."
            exit 0 ;;
        *) fail "Unknown option: ${arg}  (try --help)" ;;
    esac
done

# ── Package-manager detection ─────────────────────────────────────────
# One place that knows how to name things per distro family. Every
# missing-dependency message routes through pkg_hint so no message
# hardcodes a single distro's package manager.
detect_pm() {
    # Prefer an actual binary over /etc/os-release: derivatives (CachyOS,
    # Mint, Pop!_OS, Manjaro…) report their own ID but inherit the parent's
    # tooling, and the binary is the thing that will actually run.
    if   command -v apt-get   &>/dev/null; then echo "apt"
    elif command -v dnf       &>/dev/null; then echo "dnf"
    elif command -v yum       &>/dev/null; then echo "yum"
    elif command -v pacman    &>/dev/null; then echo "pacman"
    elif command -v zypper    &>/dev/null; then echo "zypper"
    elif command -v apk       &>/dev/null; then echo "apk"
    elif command -v xbps-install &>/dev/null; then echo "xbps"
    elif command -v emerge    &>/dev/null; then echo "emerge"
    elif command -v brew      &>/dev/null; then echo "brew"
    else echo "unknown"
    fi
}
PM="$(detect_pm)"

# Distro label, for the banner only — never for logic (that is detect_pm's job).
# Written as an explicit if/else rather than `source && echo || uname`: in that
# form the fallback also fires when sourcing SUCCEEDED but the echo failed, and
# it silently yields an empty label when neither PRETTY_NAME nor NAME is set.
PRETTY_DISTRO=""
if [ -r /etc/os-release ]; then
    # shellcheck source=/dev/null
    PRETTY_DISTRO="$( . /etc/os-release; printf '%s' "${PRETTY_NAME:-${NAME:-}}" )"
fi
[ -n "$PRETTY_DISTRO" ] || PRETTY_DISTRO="$(uname -s)"

# pkg_hint <component> — print the install command for this host.
# Components: venv, pip, ffmpeg, tk, deno
pkg_hint() {
    local what="$1" pkgs=""
    case "$PM:$what" in
        apt:venv)      pkgs="python3-venv python3-pip" ;;
        apt:tk)        pkgs="python3-tk" ;;
        apt:ffmpeg)    pkgs="ffmpeg" ;;

        dnf:venv|yum:venv)     pkgs="python3-pip" ;;
        dnf:tk|yum:tk)         pkgs="python3-tkinter tk" ;;
        dnf:ffmpeg|yum:ffmpeg) pkgs="ffmpeg" ;;

        pacman:venv)   pkgs="python python-pip" ;;
        pacman:tk)     pkgs="tk" ;;
        pacman:ffmpeg) pkgs="ffmpeg" ;;
        pacman:deno)   pkgs="deno" ;;

        zypper:venv)   pkgs="python3-pip" ;;
        zypper:tk)     pkgs="python3-tk" ;;
        zypper:ffmpeg) pkgs="ffmpeg" ;;

        apk:venv)      pkgs="py3-pip" ;;
        apk:tk)        pkgs="python3-tkinter" ;;
        apk:ffmpeg)    pkgs="ffmpeg" ;;
        apk:deno)      pkgs="deno" ;;

        xbps:venv)     pkgs="python3-pip" ;;
        xbps:tk)       pkgs="python3-tkinter" ;;
        xbps:ffmpeg)   pkgs="ffmpeg" ;;

        emerge:venv)   pkgs="dev-python/pip" ;;
        emerge:tk)     pkgs="dev-lang/python (rebuild with USE=tk)" ;;
        emerge:ffmpeg) pkgs="media-video/ffmpeg" ;;

        brew:venv)     pkgs="python" ;;
        brew:tk)       pkgs="python-tk" ;;
        brew:ffmpeg)   pkgs="ffmpeg" ;;
        brew:deno)     pkgs="deno" ;;
    esac

    if [ -z "$pkgs" ]; then
        # No mapping: either an unknown manager, or a component this
        # distro doesn't package (deno on apt/dnf/zypper/xbps/emerge).
        return 1
    fi

    case "$PM" in
        apt)    echo "sudo apt install ${pkgs}" ;;
        dnf)    echo "sudo dnf install ${pkgs}" ;;
        yum)    echo "sudo yum install ${pkgs}" ;;
        pacman) echo "sudo pacman -S ${pkgs}" ;;
        zypper) echo "sudo zypper install ${pkgs}" ;;
        apk)    echo "sudo apk add ${pkgs}" ;;
        xbps)   echo "sudo xbps-install -S ${pkgs}" ;;
        emerge) echo "sudo emerge ${pkgs}" ;;
        brew)   echo "brew install ${pkgs}" ;;
    esac
}

# suggest <component> <fallback-text> — emit the best available hint.
suggest() {
    local hint
    if hint="$(pkg_hint "$1")"; then
        warn "Install with: ${hint}"
    else
        warn "${2}"
    fi
}

# ── Pre-flight checks ──
echo ""
echo "═══════════════════════════════════════════"
echo "  ${APP_NAME} v${APP_VERSION} — Installer"
echo "═══════════════════════════════════════════"
echo ""

[ -f "$MAIN_SCRIPT" ] || fail "Main script not found: ${MAIN_SCRIPT}"

info "Host: ${PRETTY_DISTRO:-unknown} (package manager: ${PM})"

# Python 3.10+
PYTHON=""
for py in python3 python; do
    if command -v "$py" &>/dev/null; then
        ver=$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || continue
        major=${ver%%.*}
        minor=${ver##*.}
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$py"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    warn "Python 3.10+ is required. Found: $(python3 --version 2>/dev/null || echo 'none')"
    suggest venv "Install Python 3.10 or newer from your distribution."
    fail "Cannot continue without Python 3.10+."
fi
info "Python: $($PYTHON --version)"

# venv / ensurepip
if ! "$PYTHON" -c "import ensurepip" &>/dev/null; then
    warn "Python's venv/ensurepip module is missing."
    suggest venv "Install your distribution's python venv + pip packages."
    fail "Cannot create a virtual environment without it."
fi

# Tk — checked BEFORE building the venv.
#
# This is a SYSTEM package, not a pip dependency: a venv links against the
# interpreter's _tkinter, so no amount of `pip install customtkinter` will
# supply it. Checking here turns what used to be an opaque ImportError
# traceback at the verify step (after minutes of downloads) into one clear
# line up front. The import is what matters — a present libtk with a
# version mismatch fails exactly the same way as a missing one.
if TKV="$("$PYTHON" -c "import tkinter; print(tkinter.TkVersion)" 2>/dev/null)"; then
    info "Tk: ${TKV} (tkinter OK)"
else
    warn "Python cannot import tkinter — the GUI toolkit CustomTkinter builds on."
    warn "This is a system package; pip cannot supply it."
    suggest tk "Install your distribution's python3-tk / tkinter package."
    warn "Typical error without it: 'ImportError: libtk8.6.so: cannot open shared object file'"
    fail "Cannot continue without tkinter."
fi

# ffmpeg (optional but strongly recommended)
if command -v ffmpeg &>/dev/null; then
    info "ffmpeg: $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"
else
    warn "ffmpeg not found. Required for merging video/audio and audio extraction."
    suggest ffmpeg "Install ffmpeg from your distribution or https://ffmpeg.org/download.html"
fi

# ── deno (required for YouTube — yt-dlp EJS challenge solver) ─────────
# Deliberately opt-in via --with-deno. deno is third-party software from
# outside your distro's repos, so this script will not fetch and run it
# without being asked. When asked, it downloads the official release
# archive and verifies the published SHA-256 before extracting — rather
# than piping a remote script into a shell, which is the vendor's
# suggested method but gives you nothing to inspect or verify.
install_deno() {
    local os arch target tmp url sum_url expected actual
    os="$(uname -s)"; arch="$(uname -m)"
    case "${os}:${arch}" in
        Linux:x86_64)   target="x86_64-unknown-linux-gnu" ;;
        Linux:aarch64)  target="aarch64-unknown-linux-gnu" ;;
        Darwin:x86_64)  target="x86_64-apple-darwin" ;;
        Darwin:arm64)   target="aarch64-apple-darwin" ;;
        *) warn "No prebuilt deno for ${os}/${arch} — see https://deno.land"; return 1 ;;
    esac

    command -v unzip &>/dev/null || { warn "unzip is required to install deno."; return 1; }

    tmp="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" RETURN

    url="https://github.com/denoland/deno/releases/latest/download/deno-${target}.zip"
    sum_url="${url}.sha256sum"

    info "Downloading deno (${target})…"
    curl -fsSL --proto '=https' --tlsv1.2 -o "${tmp}/deno.zip" "$url" \
        || { warn "deno download failed."; return 1; }
    curl -fsSL --proto '=https' --tlsv1.2 -o "${tmp}/deno.sha256sum" "$sum_url" \
        || { warn "could not fetch deno checksum — refusing to install unverified."; return 1; }

    # The .sha256sum file is "<hash>  <filename>"; compare hashes only, since
    # the recorded filename won't match our temp path.
    expected="$(awk '{print $1}' "${tmp}/deno.sha256sum" | head -1)"
    actual="$(sha256sum "${tmp}/deno.zip" | awk '{print $1}')"
    if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        warn "deno checksum MISMATCH — refusing to install."
        warn "  expected: ${expected:-<none>}"
        warn "  actual:   ${actual}"
        return 1
    fi
    info "deno checksum verified (${actual:0:16}…)"

    mkdir -p "${HOME}/.local/bin"
    unzip -q -o "${tmp}/deno.zip" -d "$tmp" || { warn "unzip failed."; return 1; }
    install -m 755 "${tmp}/deno" "${HOME}/.local/bin/deno" || return 1
    info "deno installed: ${HOME}/.local/bin/deno"

    # The tildes below are literal text the reader is meant to see (and, for the
    # fish line, to type — fish expands it), not paths this script resolves.
    # shellcheck disable=SC2088
    case ":$PATH:" in
        *":${HOME}/.local/bin:"*) ;;
        *) warn "~/.local/bin is not on PATH — add it so the app can find deno:"
           warn "  bash/zsh:  export PATH=\"\$HOME/.local/bin:\$PATH\""
           warn "  fish:      fish_add_path ~/.local/bin" ;;
    esac
}

if command -v deno &>/dev/null; then
    info "deno: $(deno --version 2>&1 | head -1 | awk '{print $2}')"
elif [ "$WITH_DENO" -eq 1 ]; then
    install_deno || warn "deno was not installed — YouTube downloads will fail."
else
    warn "deno not found — YouTube downloads will fail without it (EJS JS runtime)."
    # Only some distros package deno; pkg_hint returns non-zero elsewhere.
    if hint="$(pkg_hint deno)"; then
        warn "Install with: ${hint}"
    else
        warn "Install with: bash install.sh --with-deno   (verified download)"
        warn "  or the vendor's script: curl -fsSL https://deno.land/install.sh | sh"
    fi
fi

# ── Create / rebuild venv ──
# Pick a venv. The per-host pattern is the default so a shared project
# folder mounted across machines keeps separate venvs (their internal
# shebangs are absolute and won't survive a host change). A plain
# venv/ is only honored if it actually runs on this machine — stale
# cross-host venvs would otherwise fail with "bad interpreter" from
# their baked-in shebangs.
venv_works() {
    # Probe via pip specifically: pip is a Python script with the venv
    # path baked into its shebang, so it dies with "bad interpreter" on
    # cross-host venvs. bin/python itself is a symlink to the system
    # interpreter and would pass a naive probe even on a stale venv.
    [ -x "$1/bin/pip" ] && "$1/bin/pip" --version &>/dev/null
}

# uname -n rather than `hostname`: the latter is a separate package on
# several minimal distros, and this script must not assume it exists.
HOST_TAG="$(uname -n 2>/dev/null || echo "local")"

# Where should the venv live?
#
# Normally: inside the project, the familiar layout. But when the project
# sits on a shared or network mount (a VM passthrough, NFS, an SMB share),
# putting a venv there is actively bad — site-packages is thousands of
# small files and every stat crosses the mount, and each machine leaves its
# own venv-<host>/ tree behind in a directory other machines are reading.
# In that case the venv goes to local disk under XDG_DATA_HOME instead.
#
# Override either behaviour explicitly with YTDLP_GUI_VENV=/path/to/venv.
project_fs_is_shared() {
    local fstype=""
    if command -v findmnt &>/dev/null; then
        fstype="$(findmnt -no FSTYPE --target "$SCRIPT_DIR" 2>/dev/null || true)"
    fi
    if [ -z "$fstype" ] && command -v stat &>/dev/null; then
        fstype="$(stat -f -c %T "$SCRIPT_DIR" 2>/dev/null || true)"
    fi
    case "$fstype" in
        virtiofs|9p|nfs|nfs4|cifs|smbfs|fuse|fuse.*|fuseblk|afpfs|sshfs) return 0 ;;
        *) return 1 ;;
    esac
}

XDG_DATA="${XDG_DATA_HOME:-${HOME}/.local/share}"

if [ -n "${YTDLP_GUI_VENV:-}" ]; then
    VENV_DIR="$YTDLP_GUI_VENV"
    VENV_BASE="$(dirname "$VENV_DIR")"
    info "Using venv from YTDLP_GUI_VENV: ${VENV_DIR}"
elif venv_works "${SCRIPT_DIR}/venv"; then
    # An existing, working in-project venv wins — don't relocate someone's
    # already-good setup out from under them.
    VENV_DIR="${SCRIPT_DIR}/venv"
    VENV_BASE="$SCRIPT_DIR"
elif project_fs_is_shared; then
    VENV_BASE="${XDG_DATA}/${APP_ID}"
    VENV_DIR="${VENV_BASE}/venv-${HOST_TAG}"
    mkdir -p "$VENV_BASE"
    info "Project is on a shared/network mount — keeping the venv on local disk:"
    info "  ${VENV_DIR}"
else
    VENV_BASE="$SCRIPT_DIR"
    VENV_DIR="${SCRIPT_DIR}/venv-${HOST_TAG}"
fi

if venv_works "$VENV_DIR"; then
    info "Reusing existing venv: ${VENV_DIR}"
else
    if [ -d "$VENV_DIR" ]; then
        # Guard the rm: only ever remove a venv directory in one of the
        # locations this script itself chooses, never a bare or
        # unexpected path (including a user-supplied YTDLP_GUI_VENV,
        # which we refuse to delete on their behalf).
        case "$VENV_DIR" in
            "${SCRIPT_DIR}"/venv|"${SCRIPT_DIR}"/venv-*) ;;
            "${XDG_DATA}/${APP_ID}"/venv-*) ;;
            *) fail "Refusing to remove unexpected venv path: ${VENV_DIR}
       Remove it yourself if you want it rebuilt." ;;
        esac
        warn "Existing venv is stale (likely from another machine) — recreating."
        rm -rf "$VENV_DIR"
    else
        info "Creating virtual environment…"
    fi
    "$PYTHON" -m venv "$VENV_DIR" || {
        suggest venv "Install your distribution's python venv package."
        fail "venv creation failed."
    }
    [ -f "${VENV_DIR}/bin/activate" ] || {
        suggest venv "Install your distribution's python venv package."
        fail "venv creation failed — missing activate script."
    }
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
info "venv activated: $(command -v python)"

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
python -c "import customtkinter; print(f'  CustomTkinter {customtkinter.__version__}')" || {
    warn "CustomTkinter failed to import despite the tkinter pre-flight passing."
    suggest tk "Install your distribution's python3-tk / tkinter package."
    fail "CustomTkinter failed to import"
}
python -c "import yt_dlp_ejs; print(f'  yt-dlp-ejs {getattr(yt_dlp_ejs, \"__version__\", \"installed\")}')" || fail "yt-dlp-ejs failed to import"
python -c "from youtube_transcript_api import YouTubeTranscriptApi; print('  youtube-transcript-api OK')" || fail "youtube-transcript-api failed to import"

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
# Skipped on macOS, which has no XDG desktop-entry system.
if [ "$(uname -s)" = "Darwin" ]; then
    warn "macOS detected — skipping .desktop entry (not applicable)."
else
    mkdir -p "$DESKTOP_DIR"

    # A desktop-menu launch does NOT inherit your shell's PATH, so a deno in
    # ~/.local/bin (where --with-deno puts it) would be invisible to the app
    # even though it works fine from a terminal. Fixing that inline in Exec=
    # is not possible cleanly: the Desktop Entry spec reserves ' and $, so a
    # `sh -c` one-liner fails desktop-file-validate. A small launcher script
    # keeps Exec= a plain path and puts the shell logic where it belongs.
    mkdir -p "$(dirname "$LAUNCHER")"
    cat > "$LAUNCHER" <<EOF
#!/bin/sh
# Generated by install.sh — regenerated on every run, do not edit.
# Prepends ~/.local/bin so a locally-installed deno is found when the app
# is started from the desktop menu rather than a shell.
PATH="\$HOME/.local/bin:\$PATH"
export PATH
exec "${VENV_DIR}/bin/python" "${MAIN_SCRIPT}" "\$@"
EOF
    chmod +x "$LAUNCHER"

    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=${APP_NAME}
Comment=Download videos and audio with yt-dlp
Exec=${LAUNCHER}
Icon=${ICON_FILE}
Terminal=false
Categories=AudioVideo;Recorder;
StartupWMClass=${APP_ID}
EOF

    chmod +x "$DESKTOP_FILE"

    if command -v update-desktop-database &>/dev/null; then
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    fi

    info "Desktop entry created: ${DESKTOP_FILE}"
fi

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
