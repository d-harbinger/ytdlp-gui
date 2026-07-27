"""Persistent config I/O and UI-scale resolution.

No customtkinter import on purpose — this module stays pure so it can be tested
headless. The UI shell calls detect_scale() and read_xft_dpi(), and applies the
result itself.
"""
import os
import shutil
import subprocess

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "ytdlp-gui")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.conf")

SCALE_OPTIONS = ["Auto", "1.0", "1.25", "1.5", "1.75", "2.0", "2.25", "2.5"]


def read_config() -> dict:
    conf = {}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        conf[k.strip()] = v.strip()
        except OSError:
            pass
    return conf


def write_config(conf: dict):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            for k, v in sorted(conf.items()):
                f.write(f"{k}={v}\n")
    except OSError:
        pass


def save_config_key(key: str, value: str):
    conf = read_config()
    conf[key] = value
    write_config(conf)


# ── Display DPI ───────────────────────────────────────────────────────────────
# Two numbers decide how the window looks, and they have to agree.
#
#   1. The font renderer turns a point size into pixels using the DPI the
#      desktop publishes as "Xft.dpi" in the X resource database.
#   2. Tk turns a *pixel* font size into a point size by dividing by its own
#      "tk scaling" factor, which it derives from the screen dimensions the
#      display server reports — not from Xft.dpi.
#
# customtkinter asks for every font, and for the glyphs it draws rounded widget
# corners with, in pixels. So both numbers are in play on every widget. When
# they disagree — which is exactly what a scaled desktop session produces, e.g.
# a KDE session at 200% publishes 192 while the X screen still reports 96 — all
# text and every corner glyph render (Xft.dpi / (72 x tk scaling)) times too
# large while the widgets keep their true-pixel sizes. Text then spills out of
# its buttons and the corner glyphs smear across them.
#
# read_xft_dpi() reports number 1 so the UI shell can set number 2 to match.
DPI_AT_100_PERCENT = 96.0  # what an unscaled desktop publishes

# Sanity window for a published DPI. Anything outside it is a broken resource
# database, not a real display, and is safer ignored than obeyed.
_DPI_MIN, _DPI_MAX = 48.0, 480.0


def read_xft_dpi():
    """DPI the desktop publishes for font rendering, or None if it publishes none.

    YTDLP_GUI_DPI overrides the lookup, for sessions that publish nothing or
    publish a value that does not match what is on screen.
    """
    env = os.environ.get("YTDLP_GUI_DPI")
    if env:
        try:
            dpi = float(env)
        except ValueError:
            dpi = 0.0
        if _DPI_MIN <= dpi <= _DPI_MAX:
            return dpi

    xrdb = shutil.which("xrdb")
    if not xrdb:
        return None
    try:
        out = subprocess.run([xrdb, "-query"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None

    for line in out.stdout.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "xft.dpi":
            try:
                dpi = float(value.strip())
            except ValueError:
                return None
            return dpi if _DPI_MIN <= dpi <= _DPI_MAX else None
    return None


# UI-scale resolution. Priority: env var > saved config > the desktop's own
# scaling > 1.0 default.
# "Auto" follows the DPI the desktop publishes, because that is the one signal a
# user actually set on purpose (the display-scaling setting writes it). The
# other Linux heuristics — xrandr physical DPI, tkinter.winfo_fpixels — stay
# unused: they guess from screen dimensions, which VMs, XWayland, and remote
# desktops all report wrongly. With nothing published, 1.0 plus the in-UI Scale
# dropdown remains the predictable fallback.
def detect_scale():
    env = os.environ.get("YTDLP_GUI_SCALE")
    if env:
        try:
            return float(env)
        except ValueError:
            pass

    saved = read_config().get("scale", "Auto")
    if saved != "Auto":
        try:
            return float(saved)
        except ValueError:
            pass

    dpi = read_xft_dpi()
    if dpi:
        # Below 1.0 the desktop is asking for a UI smaller than the design size,
        # which the fixed pixel dimensions in the layout cannot follow.
        return max(1.0, min(3.0, round(dpi / DPI_AT_100_PERCENT, 2)))

    return 1.0
