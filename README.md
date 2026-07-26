# yt-dlp GUI

A modern desktop frontend for [yt-dlp](https://github.com/yt-dlp/yt-dlp), built with CustomTkinter.

## Features

- **Video download** — single URL with format selection (resolution/codec)
- **Audio extraction** — mp3, opus, m4a, flac, wav, vorbis with quality control
- **Playlist/batch download** — full playlists with optional range filtering
- **Format selection** — presets or pick specific formats after fetching info
- **Native file picker** — uses your system's file browser (zenity/kdialog)
- **HiDPI support** — auto-scales for high-resolution displays
- **Progress tracking** — real-time speed, ETA, and progress bar

## Requirements

Three of these are **system** packages — a virtualenv links against them and
`pip` cannot supply them, so a missing one is not fixed by reinstalling.

| Requirement | Needed for | Package |
|---|---|---|
| Python 3.10+ | everything | |
| **Tk / tkinter** | the GUI itself | `python3-tk` (Debian) · `python3-tkinter tk` (Fedora) · `tk` (Arch) |
| ffmpeg | merging video+audio, audio extraction | `ffmpeg` — optional, but most downloads want it |
| **deno** | **YouTube downloads only** | JS runtime for yt-dlp's EJS player challenge |

`install.sh` checks all of these up front and prints the right command for your
distro — it detects apt, dnf/yum, pacman, zypper, apk, xbps, emerge and brew.

**Missing Tk** looks like this, and no amount of `pip install` will fix it:

```
ImportError: libtk8.6.so: cannot open shared object file: No such file or directory
```

**Missing deno** means YouTube URLs fail with an extractor error while other
sites still work. The app warns you in its log when you start a YouTube
download without it.

## Install

```bash
git clone <repo-url> ~/Projects/ytdlp-gui
cd ~/Projects/ytdlp-gui
bash install.sh
```

Creates a virtual environment, installs dependencies, and adds a desktop entry.
It is **idempotent** — re-run it any time; you never need to uninstall first.

### Options

```bash
bash install.sh --with-deno    # also install deno
bash install.sh --help
```

`--with-deno` is opt-in because deno is third-party software from outside your
distribution's repositories. When asked, the installer downloads the official
release archive and **verifies its published SHA-256** before extracting to
`~/.local/bin/deno` — rather than piping a remote script into a shell. If your
distro packages deno (Arch, Alpine, Homebrew), prefer that and skip the flag.

### Where the venv goes

Normally the project directory. **If the project sits on a shared or network
mount** — a VM passthrough, NFS, SMB — the installer detects it and puts the
venv on local disk under `~/.local/share/ytdlp-gui/` instead. Two reasons:
`site-packages` is thousands of small files and every stat would cross the
mount, and a venv's internal shebangs are absolute, so one shared between
machines breaks on whichever machine didn't create it.

Override with `YTDLP_GUI_VENV=/path/to/venv bash install.sh`.

## Uninstall

```bash
bash uninstall.sh              # list what would go, then confirm
bash uninstall.sh --dry-run    # list only, change nothing
bash uninstall.sh --purge      # also delete settings (~/.config/ytdlp-gui)
bash uninstall.sh --all-hosts  # also other machines' venvs (shared mounts)
```

Removes this machine's venv, the launcher, and the desktop entry; keeps your
settings unless `--purge`. On a shared project folder it will **not** touch a
venv belonging to another machine, and it never removes deno.

## Usage

Launch from your app menu, or run the path the installer prints at the end —
it varies by machine and by where the venv landed:

```bash
~/.local/share/ytdlp-gui/venv-$(uname -n)/bin/python ~/Projects/ytdlp-gui/ytdlp_gui.py
```

## Credits

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — video/audio download engine
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — modern UI framework
- App icon: [Snake SVG](https://www.svgrepo.com/svg/500119/snake) from [SVG Repo](https://www.svgrepo.com) (CC0 / Public Domain)

## License

MIT# ytdlp-gui
