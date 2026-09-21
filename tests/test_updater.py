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


@case
def a_forced_check_runs_even_with_the_daily_check_switched_off():
    # check_updates=0 stops the application asking on its own. Pressing Check
    # is the person asking, which is the consent the switch exists to protect.
    _isolate_config()
    config.write_config({updater.KEY_ENABLED: "0"})
    original = updater.latest_version
    updater.latest_version = lambda timeout=0: "2026.08.19"
    try:
        unforced = updater.check()
        forced = updater.check(force=True)
    finally:
        updater.latest_version = original
    assert unforced.checked is False
    assert forced.checked is True
    assert config.read_config()[updater.KEY_ENABLED] == "0"


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


# ── deno ──────────────────────────────────────────────────────────────────────


class _Deno:
    """Stand in for a deno on this machine: where it is, whose it is, its version."""

    def __init__(self, path="~/.local/bin/deno", version="2.9.1",
                 self_managed=True, latest="2.9.7"):
        self.calls = 0
        self._saved = (
            updater.deno_path, updater.deno_version,
            updater.deno_is_self_managed, updater.deno_latest,
        )
        updater.deno_path = lambda: path
        updater.deno_version = lambda p=None: version if path else ""
        updater.deno_is_self_managed = lambda p=None: self_managed

        def _latest(timeout=0):
            self.calls += 1
            return latest

        updater.deno_latest = _latest

    def restore(self):
        (updater.deno_path, updater.deno_version,
         updater.deno_is_self_managed, updater.deno_latest) = self._saved


@case
def deno_version_line_is_parsed():
    line = "deno 2.9.7 (stable, release, x86_64-unknown-linux-gnu)\nv8 14.0\n"
    assert updater.parse_deno_version(line) == "2.9.7"
    assert updater.parse_deno_version("") == ""
    assert updater.parse_deno_version("something else entirely") == ""


@case
def a_self_managed_deno_is_checked_and_can_be_behind():
    _isolate_config()
    fake = _Deno()
    try:
        status = updater.check_deno(force=True)
    finally:
        fake.restore()
    assert status.name == "deno"
    assert status.behind is True
    assert status.upgradable is True
    assert config.read_config()[updater.KEY_DENO_LAST_SEEN] == "2.9.7"


@case
def a_system_packaged_deno_is_shown_but_never_asked_about():
    # The distribution owns that binary. Comparing it against upstream would
    # turn the label orange for ever, over an upgrade this program cannot do.
    _isolate_config()
    fake = _Deno(path="/usr/bin/deno", self_managed=False)
    try:
        status = updater.check_deno(force=True)
    finally:
        fake.restore()
    assert fake.calls == 0
    assert status.upgradable is False
    assert status.behind is False
    assert "system package" in status.summary()


@case
def a_missing_deno_reaches_no_network():
    _isolate_config()
    fake = _Deno(path="")
    try:
        status = updater.check_deno(force=True)
    finally:
        fake.restore()
    assert fake.calls == 0
    assert "not installed" in status.summary()


@case
def a_deno_below_the_yt_dlp_minimum_says_so():
    _isolate_config()
    fake = _Deno(path="/usr/bin/deno", version="2.1.4", self_managed=False)
    try:
        status = updater.check_deno()
    finally:
        fake.restore()
    assert status.too_old is True
    assert updater.DENO_MINIMUM in status.summary()


@case
def the_deno_throttle_is_separate_from_the_yt_dlp_one():
    _isolate_config()
    config.write_config({updater.KEY_LAST_CHECK: str(int(updater.time.time()))})
    fake = _Deno()
    try:
        updater.check_deno()
        updater.check_deno()
    finally:
        fake.restore()
    assert fake.calls == 1  # due once despite a fresh yt-dlp check; then throttled


@case
def deno_upgrade_refuses_a_binary_it_does_not_own():
    fake = _Deno(path="/usr/bin/deno", self_managed=False)
    try:
        ok, message = updater.upgrade_deno()
    finally:
        fake.restore()
    assert ok is False
    assert "package manager" in message


if __name__ == "__main__":
    run()
