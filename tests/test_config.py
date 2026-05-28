"""Behavior tests for config.py (config I/O + scale detection)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _harness import case, run  # noqa: E402
import config  # noqa: E402


@case
def write_then_read_roundtrips():
    tmp = tempfile.mkdtemp()
    config.CONFIG_DIR = tmp
    config.CONFIG_FILE = os.path.join(tmp, "settings.conf")
    config.write_config({"scale": "1.5", "last_dir": "/tmp/x"})
    got = config.read_config()
    assert got == {"scale": "1.5", "last_dir": "/tmp/x"}, got


@case
def read_missing_file_is_empty():
    tmp = tempfile.mkdtemp()
    config.CONFIG_DIR = tmp
    config.CONFIG_FILE = os.path.join(tmp, "does-not-exist.conf")
    assert config.read_config() == {}


@case
def save_key_updates_single_value():
    tmp = tempfile.mkdtemp()
    config.CONFIG_DIR = tmp
    config.CONFIG_FILE = os.path.join(tmp, "settings.conf")
    config.write_config({"a": "1"})
    config.save_config_key("b", "2")
    got = config.read_config()
    assert got == {"a": "1", "b": "2"}, got


@case
def detect_scale_honors_env():
    old = os.environ.get("YTDLP_GUI_SCALE")
    try:
        os.environ["YTDLP_GUI_SCALE"] = "1.75"
        assert config.detect_scale() == 1.75
        os.environ["YTDLP_GUI_SCALE"] = "garbage"
        # falls through to saved/default; with a fresh temp config -> 1.0
        tmp = tempfile.mkdtemp()
        config.CONFIG_DIR = tmp
        config.CONFIG_FILE = os.path.join(tmp, "settings.conf")
        assert config.detect_scale() == 1.0
    finally:
        if old is None:
            os.environ.pop("YTDLP_GUI_SCALE", None)
        else:
            os.environ["YTDLP_GUI_SCALE"] = old


if __name__ == "__main__":
    run()
