"""Behavior tests for updater.py (version ordering, throttling, upgrade guard).

No case here reaches the network. The one function that would, latest_version,
is replaced per case, because a test that depends on PyPI answering is a test
that fails for reasons that have nothing to do with this code.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _harness import case, run  # noqa: E402
import config  # noqa: E402
import updater  # noqa: E402


def _isolate_config():
    """Point config at a fresh directory so cases cannot see each other."""
    tmp = tempfile.mkdtemp()
    config.CONFIG_DIR = tmp
    config.CONFIG_FILE = os.path.join(tmp, "settings.conf")
    return tmp


# ── Version ordering ──────────────────────────────────────────────────────────
# The releases that mattered on 2026-09-21 were 2026.06.09, 2026.7.4 and
# 2026.08.19. String comparison puts 2026.7.4 last, which would have reported
# a vulnerable install as current.


@case
def unpadded_month_sorts_before_later_padded_month():
    assert updater.parse_version("2026.7.4") < updater.parse_version("2026.08.19")


@case
def same_month_orders_by_day():
    assert updater.parse_version("2026.06.09") < updater.parse_version("2026.06.10")


@case
def equal_versions_are_not_behind():
    s = updater.UpdateStatus(installed="2026.08.19", latest="2026.08.19")
    assert s.behind is False


@case
def older_installed_is_behind():
    s = updater.UpdateStatus(installed="2026.06.09", latest="2026.08.19")
    assert s.behind is True


@case
def missing_latest_is_never_behind():
    """A failed check must not be reported as an available update."""
    s = updater.UpdateStatus(installed="2026.06.09", latest="", error="URLError")
    assert s.behind is False
    assert "failed" in s.summary()


# ── Throttle ──────────────────────────────────────────────────────────────────


@case
def first_check_is_due():
    _isolate_config()
    assert updater.check_is_due(now=1_000_000) is True


@case
def check_is_not_due_again_within_the_interval():
    _isolate_config()
    config.write_config({updater.KEY_LAST_CHECK: "1000000"})
    assert updater.check_is_due(now=1_000_000 + 60) is False


@case
def check_is_due_again_after_the_interval():
    _isolate_config()
    config.write_config({updater.KEY_LAST_CHECK: "1000000"})
    later = 1_000_000 + updater.CHECK_INTERVAL_SECONDS + 1
    assert updater.check_is_due(now=later) is True


@case
def disabled_checks_are_never_due():
    _isolate_config()
    config.write_config({updater.KEY_ENABLED: "0"})
    assert updater.check_is_due(now=1_000_000) is False


@case
def throttled_check_reports_the_last_version_seen_without_network():
    _isolate_config()
    config.write_config(
        {updater.KEY_LAST_CHECK: "99999999999", updater.KEY_LAST_SEEN: "2026.08.19"}
    )
    original = updater.latest_version
    updater.latest_version = lambda timeout=0: (_ for _ in ()).throw(
        AssertionError("network must not be reached while throttled")
    )
    try:
        status = updater.check()
    finally:
        updater.latest_version = original
    assert status.latest == "2026.08.19"
    assert status.checked is False


@case
def a_failed_check_is_reported_not_raised():
    _isolate_config()
    original = updater.latest_version

    def _boom(timeout=0):
        raise OSError("no route to host")

    updater.latest_version = _boom
    try:
        status = updater.check(force=True)
    finally:
        updater.latest_version = original
    assert status.error == "OSError"
    assert status.behind is False


@case
def a_successful_check_records_what_it_saw():
    _isolate_config()
    original = updater.latest_version
    updater.latest_version = lambda timeout=0: "2026.08.19"
    try:
        status = updater.check(force=True)
    finally:
        updater.latest_version = original
    assert status.latest == "2026.08.19"
    assert status.checked is True
    assert config.read_config()[updater.KEY_LAST_SEEN] == "2026.08.19"


# ── Upgrade guard ─────────────────────────────────────────────────────────────


@case
def upgrade_refuses_outside_a_virtualenv():
    original = updater.in_virtualenv
    updater.in_virtualenv = lambda: False
    try:
        ok, message = updater.upgrade()
    finally:
        updater.in_virtualenv = original
    assert ok is False
    assert "virtual environment" in message


def _captured_upgrade_command():
    """Run upgrade() with pip replaced, and return the command it built."""
    seen = {}

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Done()

    original_venv, original_run = updater.in_virtualenv, updater.subprocess.run
    updater.in_virtualenv = lambda: True
    updater.subprocess.run = _fake_run
    try:
        ok, _ = updater.upgrade()
    finally:
        updater.in_virtualenv = original_venv
        updater.subprocess.run = original_run
    assert ok is True
    return seen["cmd"]


@case
def upgrade_moves_transitive_dependencies_too():
    # pip's default strategy leaves requests, urllib3 and certifi wherever
    # install day put them, as long as they still satisfy yt-dlp's floors.
    cmd = _captured_upgrade_command()
    assert "--upgrade" in cmd
    assert cmd[cmd.index("--upgrade-strategy") + 1] == "eager"


@case
def upgrade_covers_every_scraping_dependency():
    cmd = _captured_upgrade_command()
    assert "yt-dlp[default]" in cmd
    assert "youtube-transcript-api" in cmd


@case
def an_unexpected_check_failure_is_still_reported_not_raised():
    # http.client.IncompleteRead is neither URLError nor OSError; raised on a
    # worker thread it would end the check silently.
    _isolate_config()
    original = updater.latest_version

    def _boom(timeout=0):
        raise TypeError("info was not a mapping")

    updater.latest_version = _boom
    try:
        status = updater.check(force=True)
    finally:
        updater.latest_version = original
    assert status.error == "TypeError"


@case
def a_check_does_not_overwrite_settings_saved_while_it_ran():
    _isolate_config()
    original = updater.latest_version

    def _slow(timeout=0):
        config.save_config_key("ui_scale", "1.5")  # the person, mid-request
        return "2026.08.19"

    updater.latest_version = _slow
    try:
        updater.check(force=True)
    finally:
        updater.latest_version = original
    assert config.read_config().get("ui_scale") == "1.5"


if __name__ == "__main__":
    run()
