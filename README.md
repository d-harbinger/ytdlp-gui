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

- Python 3.10+
- ffmpeg
- Linux desktop environment (tested on Linux Mint + CinTile)

## Install

```bash
git clone <repo-url> ~/Projects/ytdlp-gui
cd ~/Projects/ytdlp-gui
chmod +x install-desktop.sh
./install-desktop.sh
```

The installer creates a virtual environment, installs dependencies, and adds a desktop entry to your application menu.

## Usage

Launch from your app menu, or:

```bash
cd ~/Projects/ytdlp-gui
venv/bin/python ytdlp_gui.py
```

## Credits

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — video/audio download engine
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — modern UI framework
- App icon: [Snake SVG](https://www.svgrepo.com/svg/500119/snake) from [SVG Repo](https://www.svgrepo.com) (CC0 / Public Domain)

## License

MIT