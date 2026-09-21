"""yt-dlp version reporting and in-place upgrade.

yt-dlp is not an ordinary dependency. Sites change their delivery constantly,
so extractors break on a timescale of weeks, and the project ships releases at
roughly that cadence — several a month. Security fixes arrive in the same
stream, with no separate backport branch: on 2026-09-21 a scanner reported five
CVEs against this application's declared dependency, four of which had been
fixed months earlier and would never have been reported had the installed copy
followed the release stream.

An install that resolves the dependency once and never looks again is therefore
broken by default some weeks later, in a way the person running it cannot see.
This module exists so the application can answer three questions out loud:
which version is installed, whether a newer one exists, and whether the upgrade
succeeded.

Tkinter-free by design, like config.py and downloader.py: every function here
is callable from a worker thread or a test with no display attached.

Nothing here upgrades anything on its own. The check is throttled and can be
switched off, and the upgrade runs only when something calls upgrade(). An
application that silently reaches the network on every launch, or replaces its
own dependencies without being asked, is exactly the behaviour this is meant to
make visible.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass

import config

PACKAGE = "yt-dlp"

# What the Update button upgrades. youtube-transcript-api scrapes the same site
# yt-dlp does and decays the same way, so it moves with it. customtkinter is
# left out on purpose: it has no network surface, and a toolkit upgrade that
# broke the window would take the Update button down with it.
REQUIREMENTS = ("yt-dlp[default]", "youtube-transcript-api")
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"

# How long a check is trusted before the network is consulted again. A release
# lands every few weeks, so a daily question is already generous; the point of
# the throttle is that launching the application ten times in an afternoon
# reaches PyPI once, not ten times.
CHECK_INTERVAL_SECONDS = 24 * 60 * 60

# Config keys, written to the same settings.conf the rest of the app uses.
KEY_ENABLED = "check_updates"
KEY_LAST_CHECK = "update_last_check"
KEY_LAST_SEEN = "update_last_seen"

NETWORK_TIMEOUT = 8


@dataclass
class UpdateStatus:
    """What the last check established. `error` and `latest` are exclusive."""

    installed: str
    latest: str = ""
    error: str = ""
    checked: bool = False

    @property
    def behind(self) -> bool:
        if not self.latest or not self.installed:
            return False
        return parse_version(self.installed) < parse_version(self.latest)

    def summary(self) -> str:
        """One line for the status area, phrased for someone who did not ask."""
        if not self.installed:
            return "yt-dlp is not installed"
        if self.error:
            return f"yt-dlp {self.installed} (update check failed: {self.error})"
        if self.behind:
            return f"yt-dlp {self.installed} — {self.latest} is available"
        if self.latest:
            return f"yt-dlp {self.installed} (current)"
        return f"yt-dlp {self.installed}"


def parse_version(v: str) -> tuple:
    """Order two yt-dlp versions.

    Releases are calendar-versioned and not zero-padded consistently:
    2026.08.19 and 2026.7.4 are both real. Comparing the strings would put
    2026.7.4 after 2026.08.19, so each segment is compared as an integer.
    A segment that is not numeric (a release candidate suffix, say) sorts
    before a numeric one rather than raising.
    """
    parts = []
    for seg in str(v).split("."):
        digits = ""
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else -1)
    return tuple(parts)


def installed_version() -> str:
    """The version of yt-dlp this process would actually use, or ""."""
    try:
        import yt_dlp

        return str(yt_dlp.version.__version__)
    except Exception:
        return ""


def latest_version(timeout: int = NETWORK_TIMEOUT) -> str:
    """Ask PyPI for the newest release. Raises urllib/JSON errors to the caller."""
    req = urllib.request.Request(
        PYPI_URL, headers={"Accept": "application/json", "User-Agent": "ytdlp-gui"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return str(data["info"]["version"])


def checks_enabled(conf: dict = None) -> bool:
    conf = config.read_config() if conf is None else conf
    return conf.get(KEY_ENABLED, "1") not in ("0", "false", "no")


def check_is_due(conf: dict = None, now: float = None) -> bool:
    """True when the throttle has expired. False also when checks are off."""
    conf = config.read_config() if conf is None else conf
    if not checks_enabled(conf):
        return False
    now = time.time() if now is None else now
    try:
        last = float(conf.get(KEY_LAST_CHECK, 0))
    except ValueError:
        last = 0
    return (now - last) >= CHECK_INTERVAL_SECONDS


def check(force: bool = False, timeout: int = NETWORK_TIMEOUT) -> UpdateStatus:
    """Report the installed version, and the newest one when a check is due.

    With the throttle unexpired this touches no network and reports the last
    version seen, so the caller can still say something truthful about whether
    an update is waiting.
    """
    conf = config.read_config()
    status = UpdateStatus(installed=installed_version())

    if not force and not check_is_due(conf):
        status.latest = conf.get(KEY_LAST_SEEN, "")
        return status

    try:
        status.latest = latest_version(timeout=timeout)
        status.checked = True
        # Re-read before writing: the request above can take seconds on a
        # worker thread, and writing back the snapshot taken before it would
        # discard any setting the person saved in the meantime.
        config.write_config(
            {
                **config.read_config(),
                KEY_LAST_CHECK: str(int(time.time())),
                KEY_LAST_SEEN: status.latest,
            }
        )
    except Exception as e:
        # A failed check is reported, never raised: no download should be
        # blocked because PyPI was unreachable. Deliberately broad — this runs
        # on a worker thread, where an uncaught http.client or TypeError would
        # end the check without a word on screen.
        status.error = type(e).__name__
        status.latest = conf.get(KEY_LAST_SEEN, "")
    return status


def in_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def upgrade(timeout: int = 300) -> tuple:
    """Upgrade yt-dlp in this interpreter's environment.

    Returns (ok, message). Refuses outside a virtual environment: installing
    into a system interpreter with pip is how a distribution's package manager
    and pip end up disagreeing about the same files, and this application has
    no business doing that to someone's machine. The installer creates a venv,
    so the supported install always satisfies this.
    """
    if not in_virtualenv():
        return (
            False,
            "Not running inside a virtual environment — upgrade skipped. "
            f"Install with install.sh, or upgrade {PACKAGE} with the package "
            "manager that provided it.",
        )
    # Eager, because pip's default strategy upgrades a transitive dependency
    # only when the new yt-dlp refuses the installed one — and yt-dlp's floors
    # are years old. Without it requests, urllib3 and certifi stay wherever
    # install day put them: the HTTP stack and the CA bundle, which is where
    # the advisories this module exists to clear actually land.
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--upgrade", "--upgrade-strategy", "eager", *REQUIREMENTS,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"Upgrade could not run: {e}"

    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        return False, tail[-1] if tail else f"pip exited {r.returncode}"

    # The running process has already imported the old module, so the new
    # version is not in effect until the application is restarted. Saying so is
    # the difference between an upgrade the person can trust and one that
    # appears to do nothing.
    return True, "Upgrade installed. Restart the application to use it."
