"""yt-dlp and deno version reporting and in-place upgrade.

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
import shutil
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

# deno is the second engine. yt-dlp hands YouTube's player challenge — code the
# site supplies — to it, sandboxed, on every YouTube download. A deno fetched
# once by `install.sh --with-deno` has nothing updating it afterwards, so it
# gets the same three answers on screen. The vendor's own pointer file is what
# `deno upgrade` reads; it is one line of text.
DENO = "deno"
DENO_LATEST_URL = "https://dl.deno.land/release-latest.txt"
# The floor yt-dlp documents for its challenge solver (yt-dlp wiki, "EJS").
DENO_MINIMUM = "2.3.0"
KEY_DENO_LAST_CHECK = "deno_last_check"
KEY_DENO_LAST_SEEN = "deno_last_seen"

NETWORK_TIMEOUT = 8


@dataclass
class UpdateStatus:
    """What the last check established. `error` and `latest` are exclusive."""

    installed: str
    latest: str = ""
    error: str = ""
    checked: bool = False
    name: str = PACKAGE
    # False when something other than this application owns the install — a
    # distribution package, say. It is then shown, and never offered an upgrade.
    upgradable: bool = True
    minimum: str = ""

    @property
    def behind(self) -> bool:
        if not self.latest or not self.installed:
            return False
        return parse_version(self.installed) < parse_version(self.latest)

    @property
    def too_old(self) -> bool:
        if not self.minimum or not self.installed:
            return False
        return parse_version(self.installed) < parse_version(self.minimum)

    def summary(self) -> str:
        """One line for the status area, phrased for someone who did not ask."""
        if not self.installed:
            return f"{self.name} is not installed"
        if self.too_old:
            return f"{self.name} {self.installed} — yt-dlp needs {self.minimum} or newer"
        if self.error:
            return f"{self.name} {self.installed} (update check failed: {self.error})"
        if self.behind:
            return f"{self.name} {self.installed} — {self.latest} is available"
        if not self.upgradable:
            return f"{self.name} {self.installed} (system package)"
        if self.latest:
            return f"{self.name} {self.installed} (current)"
        return f"{self.name} {self.installed}"


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


def check_is_due(conf: dict = None, now: float = None, key: str = KEY_LAST_CHECK) -> bool:
    """True when the throttle has expired. False also when checks are off."""
    conf = config.read_config() if conf is None else conf
    if not checks_enabled(conf):
        return False
    now = time.time() if now is None else now
    try:
        last = float(conf.get(key, 0))
    except ValueError:
        last = 0
    return (now - last) >= CHECK_INTERVAL_SECONDS


def _consult(status, fetch_latest, key_check, key_seen, force, timeout):
    """Fill in `status.latest`, from the network when a check is due.

    With the throttle unexpired this touches no network and reports the last
    version seen, so the caller can still say something truthful about whether
    an update is waiting.
    """
    conf = config.read_config()
    if not force and not check_is_due(conf, key=key_check):
        status.latest = conf.get(key_seen, "")
        return status

    try:
        status.latest = fetch_latest(timeout=timeout)
        status.checked = True
        # Re-read before writing: the request above can take seconds on a
        # worker thread, and writing back the snapshot taken before it would
        # discard any setting the person saved in the meantime.
        config.write_config(
            {
                **config.read_config(),
                key_check: str(int(time.time())),
                key_seen: status.latest,
            }
        )
    except Exception as e:
        # A failed check is reported, never raised: no download should be
        # blocked because an index was unreachable. Deliberately broad — this
        # runs on a worker thread, where an uncaught http.client or TypeError
        # would end the check without a word on screen.
        status.error = type(e).__name__
        status.latest = conf.get(key_seen, "")
    return status


def check(force: bool = False, timeout: int = NETWORK_TIMEOUT) -> UpdateStatus:
    """Report the installed yt-dlp, and the newest one when a check is due."""
    status = UpdateStatus(installed=installed_version())
    # Looked up at call time so a test can replace latest_version.
    return _consult(
        status, lambda timeout: latest_version(timeout=timeout),
        KEY_LAST_CHECK, KEY_LAST_SEEN, force, timeout,
    )


# ── deno ──────────────────────────────────────────────────────────────────────


def deno_path() -> str:
    """The deno a download would use, or "". PATH lookup, as yt-dlp does it."""
    return shutil.which(DENO) or ""


def parse_deno_version(output: str) -> str:
    """Pull 2.9.7 out of `deno 2.9.7 (stable, release, x86_64-…)`."""
    words = output.split()
    if len(words) >= 2 and words[0] == DENO and words[1][:1].isdigit():
        return words[1]
    return ""


def deno_version(path: str = None) -> str:
    path = deno_path() if path is None else path
    if not path:
        return ""
    try:
        r = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return parse_deno_version(r.stdout)


def deno_is_self_managed(path: str = None) -> bool:
    """True when this account can replace the binary — so nobody else will.

    `install.sh --with-deno` puts deno in ~/.local/bin, where nothing updates
    it. A deno under /usr belongs to the distribution: its package manager
    keeps it current, `deno upgrade` could not write there anyway, and holding
    it against upstream would show a permanent warning over an upgrade this
    application has no way to perform.
    """
    path = deno_path() if path is None else path
    if not path:
        return False
    real = os.path.realpath(path)
    return os.access(real, os.W_OK) and os.access(os.path.dirname(real), os.W_OK)


def deno_latest(timeout: int = NETWORK_TIMEOUT) -> str:
    """Ask the vendor for the newest stable release. The body is `v2.9.7`."""
    req = urllib.request.Request(DENO_LATEST_URL, headers={"User-Agent": "ytdlp-gui"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read(64).decode("ascii").strip()
    version = text.lstrip("v")
    if not version[:1].isdigit():
        raise ValueError("unexpected release pointer")
    return version


def check_deno(force: bool = False, timeout: int = NETWORK_TIMEOUT) -> UpdateStatus:
    """Report the installed deno. Consults the network only for one it owns."""
    path = deno_path()
    status = UpdateStatus(
        installed=deno_version(path), name=DENO, minimum=DENO_MINIMUM,
        upgradable=deno_is_self_managed(path),
    )
    if not status.installed or not status.upgradable:
        return status
    return _consult(
        status, lambda timeout: deno_latest(timeout=timeout),
        KEY_DENO_LAST_CHECK, KEY_DENO_LAST_SEEN, force, timeout,
    )


def upgrade_deno(timeout: int = 300) -> tuple:
    """Run the vendor's own upgrade on a deno this account owns.

    Returns (ok, message). Unlike yt-dlp, deno is a separate process started
    per download, so the new version is in effect immediately.
    """
    path = deno_path()
    if not path:
        return False, "deno is not installed — run: bash install.sh --with-deno"
    if not deno_is_self_managed(path):
        return (
            False,
            f"{path} belongs to the system — upgrade deno with the package "
            "manager that provided it.",
        )
    try:
        r = subprocess.run(
            [path, "upgrade"], capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"deno upgrade could not run: {e}"

    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        return False, tail[-1] if tail else f"deno upgrade exited {r.returncode}"
    return True, f"deno upgraded to {deno_version(path) or 'the latest release'}."


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
