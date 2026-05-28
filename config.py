"""Persistent config I/O and UI-scale resolution.

No customtkinter import on purpose — this module stays pure so it can be tested
headless. The UI shell calls detect_scale() and applies ctk scaling itself.
"""
import os

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


# UI-scale resolution. Priority: env var > saved config > 1.0 default.
# "Auto" resolves to 1.0 — on Linux every DPI-probing heuristic (xrandr physical
# DPI, Xft.dpi, tkinter.winfo_fpixels) is unreliable across VMs, XWayland, remote
# desktops, and misconfigured DEs. A predictable 1.0 default plus the in-UI Scale
# dropdown is the only thing that works everywhere.
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

    return 1.0
