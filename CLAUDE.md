# CLAUDE.md — ytdlp-gui

A desktop front-end for [yt-dlp](https://github.com/yt-dlp/yt-dlp): paste a
URL, pick a format, download video or extract audio. It also fetches and
parses YouTube transcripts. MIT-licensed.

## Project at a glance

- **Stack**: Python 3.10+ with CustomTkinter (Tk). Runtime dependencies are
  `customtkinter`, `yt-dlp[default]`, `youtube-transcript-api`.
- **Modules**: `ytdlp_gui.py` is the UI shell; `downloader.py` drives yt-dlp;
  `transcript.py` fetches and parses transcripts; `config.py` handles
  persisted settings and UI-scale resolution; `validation.py` guards URLs and
  playlist ranges.
- **Settings**: `~/.config/ytdlp-gui/settings.conf`, a flat `key=value` file.

## Run, install, test

| Task | Command |
|---|---|
| Install | `bash install.sh` — idempotent, re-run any time; `--with-deno`, `--help` |
| Uninstall | `bash uninstall.sh` — `--dry-run`, `--purge`, `--all-hosts` |
| Tests | run each file directly, e.g. `venv/bin/python tests/test_validation.py` |

`install-desktop.sh` is a compatibility shim that execs `install.sh`; the
installer is the real entry point. It detects the host package manager
(apt, dnf/yum, pacman, zypper, apk, xbps, emerge, brew) and prints the right
command for anything missing.

There is **no test framework and none should be added**. `tests/_harness.py`
provides `case`/`run`, each test file registers checks and is executed
directly, and `run()` exits non-zero on failure so it works as a gate.

## Verification boundary

The GUI is host-verified. This environment has no display: nothing about
layout, scaling, or interaction can be claimed as working from here. The
headless-testable surface is the reason `config.py` deliberately imports no
CustomTkinter — keep that module pure so it stays testable.

## Gotchas

- **Three requirements pip cannot supply.** Tk (`python3-tk` and friends) is
  needed for the GUI itself and fails with a `libtk8.6.so` `ImportError`;
  ffmpeg is needed to merge streams and extract audio; **deno** is needed for
  YouTube specifically (yt-dlp's JS player challenge), and its absence looks
  like a YouTube-only extractor failure while other sites still work. Deno
  installation is opt-in because it is third-party software from outside the
  distribution — the installer verifies the published SHA-256 before
  extracting.
- **Per-host virtual environments.** A venv's shebangs are absolute, so a
  checkout shared across machines gets `venv-<host>`. On a network or
  passthrough mount the installer moves the venv to local disk under
  `~/.local/share/ytdlp-gui/`. Override with `YTDLP_GUI_VENV=/path`.
- **Environment overrides** (all read at runtime): `YTDLP_GUI_SCALE` (UI
  scale factor, outranks the in-app dropdown), `YTDLP_GUI_DPI` (assumed
  display DPI), `YTDLP_GUI_YOUTUBE_ONLY=1` (restricts the URL allow-list to
  YouTube), `YTDLP_GUI_VENV` (installer only).

## Where things are documented

- `README.md` — install, system requirements, uninstall, display scaling,
  and the DPI/Tk mismatch explanation. Read it before touching scaling code.
- `docs/security-audit-2026-05-31.md` — the threat model and the 2026-05-31
  clean result (command injection, URL/scheme handling, path traversal,
  post-download execution). Read it before changing anything that builds a
  yt-dlp argument list, handles a URL, or picks an output path, and update it
  if a new sink is introduced.
- `~/Projects/claude-settings/workspace/audits/ytdlp-gui/TEACHING.md` — the
  slop audit and the decision record; the finding that matters is structural
  (one class doing everything), not the grep counts. Record new decisions
  there. Do **not** create a `TEACHING.md` at this repo's root.
