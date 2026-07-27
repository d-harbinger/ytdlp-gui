"""Behavior tests for config.py (config I/O + scale detection)."""
import contextlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _harness import case, run  # noqa: E402
import config  # noqa: E402


@contextlib.contextmanager
def _env(**pairs):
    """Set env vars for the block, restoring them after. A value of None unsets.

    Scale detection reads the environment *and* the running desktop, so every
    case pins both — otherwise the result depends on the machine the tests run
    on.
    """
    saved = {k: os.environ.get(k) for k in pairs}
    try:
        for k, v in pairs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
    with _env(YTDLP_GUI_SCALE="1.75", YTDLP_GUI_DPI="192"):
        assert config.detect_scale() == 1.75  # env wins over the desktop's DPI
    with _env(YTDLP_GUI_SCALE="garbage", YTDLP_GUI_DPI="96"):
        # falls through to saved/desktop; fresh config + an unscaled desktop -> 1.0
        tmp = tempfile.mkdtemp()
        config.CONFIG_DIR = tmp
        config.CONFIG_FILE = os.path.join(tmp, "settings.conf")
        assert config.detect_scale() == 1.0


@case
def read_xft_dpi_honors_env():
    with _env(YTDLP_GUI_DPI="192"):
        assert config.read_xft_dpi() == 192.0
    # Out-of-range and unparseable values are dropped and the desktop lookup
    # runs instead, so all that can be asserted is a sane result: a plausible
    # DPI or nothing at all.
    for bad in ("9000", "0", "garbage", ""):
        with _env(YTDLP_GUI_DPI=bad):
            got = config.read_xft_dpi()
            assert got is None or 48.0 <= got <= 480.0, (bad, got)


@case
def auto_scale_follows_published_dpi():
    tmp = tempfile.mkdtemp()
    config.CONFIG_DIR = tmp
    config.CONFIG_FILE = os.path.join(tmp, "settings.conf")  # no saved scale -> Auto
    with _env(YTDLP_GUI_SCALE=None, YTDLP_GUI_DPI="192"):
        assert config.detect_scale() == 2.0
    with _env(YTDLP_GUI_SCALE=None, YTDLP_GUI_DPI="96"):
        assert config.detect_scale() == 1.0
    # A desktop asking for less than the design size is not followed: the fixed
    # pixel dimensions in the layout cannot shrink with it.
    with _env(YTDLP_GUI_SCALE=None, YTDLP_GUI_DPI="72"):
        assert config.detect_scale() == 1.0


@case
def saved_scale_outranks_published_dpi():
    tmp = tempfile.mkdtemp()
    config.CONFIG_DIR = tmp
    config.CONFIG_FILE = os.path.join(tmp, "settings.conf")
    config.write_config({"scale": "1.25"})
    with _env(YTDLP_GUI_SCALE=None, YTDLP_GUI_DPI="192"):
        assert config.detect_scale() == 1.25


if __name__ == "__main__":
    run()
