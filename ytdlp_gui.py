#!/usr/bin/env python3
"""
yt-dlp GUI — A modern CustomTkinter frontend for yt-dlp.
Supports single video, audio-only extraction, playlist/batch downloads,
and format selection with a file-picker for output directory.
"""

import os
import sys
import re
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog
from datetime import timedelta

import customtkinter as ctk

try:
    import yt_dlp
except ImportError:
    print("ERROR: yt_dlp not found. Run: pip install yt-dlp")
    sys.exit(1)


# ── HiDPI Auto-Scaling (mirrors transcript extractor pattern) ────────────────
# ── HiDPI Auto-Scaling ────────────────────────────────────────────────────────
# DPI-based detection first; resolution fallback for Linux native 1x scaling
_dpi_root = tk.Tk()
_dpi_root.withdraw()
_dpi = _dpi_root.winfo_fpixels("1i")
_dpi_scale = _dpi / 96.0

# Linux at native 1x reports 96 DPI even on 5K — use resolution fallback
if _dpi_scale < 1.25:
    _longest = max(_dpi_root.winfo_screenwidth(), _dpi_root.winfo_screenheight())
    if _longest >= 5120:
        _dpi_scale = 2.5
    elif _longest >= 3840:
        _dpi_scale = 2.0
    elif _longest >= 2560:
        _dpi_scale = 1.5

_dpi_root.destroy()
ctk.set_widget_scaling(_dpi_scale)
ctk.set_window_scaling(_dpi_scale)


# ── Native Directory Picker ───────────────────────────────────────────────────
def _native_askdirectory(title="Select Directory"):
    """Use zenity/kdialog for native file picker, fallback to tkinter."""
    if shutil.which("zenity"):
        try:
            result = subprocess.run(
                ["zenity", "--file-selection", "--directory", f"--title={title}"],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return ""
        except (subprocess.TimeoutExpired, OSError):
            pass
    elif shutil.which("kdialog"):
        try:
            result = subprocess.run(
                ["kdialog", "--getexistingdirectory", os.path.expanduser("~"), "--title", title],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return ""
        except (subprocess.TimeoutExpired, OSError):
            pass
    # Fallback: tkinter
    return filedialog.askdirectory(title=title)


# ── Application Constants ────────────────────────────────────────────────────
APP_NAME = "yt-dlp GUI"
APP_VERSION = "1.0.0"
WINDOW_MIN_W = 700
WINDOW_MIN_H = 580

AUDIO_CODECS = ["mp3", "opus", "m4a", "flac", "wav", "vorbis"]
AUDIO_QUALITIES = ["320", "256", "192", "128", "96"]

VIDEO_PRESET_FORMATS = [
    ("Best video + audio", "bv*+ba/b"),
    ("Best MP4 (≤1080p)", "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4]"),
    ("Best MP4 (≤720p)", "bv*[ext=mp4][height<=720]+ba[ext=m4a]/b[ext=mp4]"),
    ("Best MP4 (≤480p)", "bv*[ext=mp4][height<=480]+ba[ext=m4a]/b[ext=mp4]"),
    ("Worst quality (smallest)", "worst"),
]


# ── Custom Logger for yt-dlp (routes to GUI) ─────────────────────────────────
class GUILogger:
    """Redirect yt-dlp log output to a callback function."""

    def __init__(self, callback):
        self.callback = callback

    def debug(self, msg):
        # Filter out overly verbose debug lines
        if msg.startswith("[debug]"):
            return
        self.callback(msg)

    def info(self, msg):
        self.callback(msg)

    def warning(self, msg):
        self.callback(f"⚠ {msg}")

    def error(self, msg):
        self.callback(f"✖ {msg}")


# ── Main Application ─────────────────────────────────────────────────────────
class YtDlpGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        # ── Window setup ──
        self.title(APP_NAME)
        self.minsize(WINDOW_MIN_W, WINDOW_MIN_H)
        self.geometry(f"{WINDOW_MIN_W}x{WINDOW_MIN_H}")

        # WM_CLASS for proper desktop integration (panel recognition)
        try:
            self.wm_attributes("-class", "ytdlp-gui")
        except Exception:
            pass

        # ── State ──
        self._download_thread = None
        self._cancel_flag = threading.Event()
        self._fetched_formats = []  # populated by Fetch Info
        self._video_info = None

        # ── Build UI ──
        self._build_ui()

        # ── Icon ──
        icon_path = os.path.join(os.path.dirname(__file__), "assets", "icon.png")
        if os.path.exists(icon_path):
            try:
                self.iconphoto(True, tk.PhotoImage(file=icon_path))
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════════════════════
    # UI Construction
    # ═══════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        row = 0

        # ── Header ──
        hdr = ctk.CTkLabel(self, text=APP_NAME, font=ctk.CTkFont(size=20, weight="bold"))
        hdr.grid(row=row, column=0, padx=16, pady=(16, 4), sticky="w")
        row += 1

        ver = ctk.CTkLabel(self, text=f"v{APP_VERSION}", text_color="gray")
        ver.grid(row=row, column=0, padx=16, pady=(0, 12), sticky="w")
        row += 1

        # ── URL Entry ──
        url_frame = ctk.CTkFrame(self, fg_color="transparent")
        url_frame.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="ew")
        url_frame.grid_columnconfigure(0, weight=1)

        self.url_entry = ctk.CTkEntry(url_frame, placeholder_text="Paste YouTube URL or playlist link…")
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.url_entry.bind("<Return>", lambda e: self._fetch_info())

        self.fetch_btn = ctk.CTkButton(url_frame, text="Fetch Info", width=100, command=self._fetch_info)
        self.fetch_btn.grid(row=0, column=1)
        row += 1

        # ── Info Display ──
        self.info_label = ctk.CTkLabel(
            self, text="Enter a URL and click Fetch Info to begin.",
            wraplength=650, justify="left", text_color="gray"
        )
        self.info_label.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="w")
        row += 1

        # ── Mode Selection ──
        mode_frame = ctk.CTkFrame(self)
        mode_frame.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="ew")
        mode_frame.grid_columnconfigure((0, 1, 2), weight=1)

        ctk.CTkLabel(mode_frame, text="Download Mode:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=3, padx=12, pady=(8, 4), sticky="w"
        )

        self.mode_var = ctk.StringVar(value="video")
        modes = [("🎬  Video", "video"), ("🎵  Audio Only", "audio"), ("📋  Playlist", "playlist")]
        for i, (label, val) in enumerate(modes):
            ctk.CTkRadioButton(mode_frame, text=label, variable=self.mode_var, value=val,
                               command=self._on_mode_change).grid(row=1, column=i, padx=12, pady=(0, 8))
        row += 1

        # ── Options Frame (swaps content based on mode) ──
        self.opts_frame = ctk.CTkFrame(self)
        self.opts_frame.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="ew")
        self.opts_frame.grid_columnconfigure(1, weight=1)
        row += 1

        # Video options (default)
        self._build_video_opts()

        # ── Output Directory ──
        dir_frame = ctk.CTkFrame(self, fg_color="transparent")
        dir_frame.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="ew")
        dir_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(dir_frame, text="Save to:").grid(row=0, column=0, padx=(0, 8))
        self.dir_entry = ctk.CTkEntry(dir_frame, placeholder_text="Select output directory…")
        self.dir_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))

        self.browse_btn = ctk.CTkButton(dir_frame, text="Browse…", width=90, command=self._browse_dir)
        self.browse_btn.grid(row=0, column=2)
        row += 1

        # ── Progress ──
        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.grid(row=row, column=0, padx=16, pady=(8, 4), sticky="ew")
        self.progress_bar.set(0)
        row += 1

        self.status_label = ctk.CTkLabel(self, text="Idle", text_color="gray")
        self.status_label.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="w")
        row += 1

        # ── Log Output ──
        self.log_box = ctk.CTkTextbox(self, height=100, state="disabled", font=ctk.CTkFont(family="monospace", size=11))
        self.log_box.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="ew")
        row += 1

        # ── Action Buttons ──
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=row, column=0, padx=16, pady=(0, 16), sticky="ew")
        btn_frame.grid_columnconfigure(0, weight=1)

        self.cancel_btn = ctk.CTkButton(
            btn_frame, text="Cancel", fg_color="gray", hover_color="#666",
            width=100, command=self._cancel_download, state="disabled"
        )
        self.cancel_btn.grid(row=0, column=0, padx=(0, 8), sticky="e")

        self.dl_btn = ctk.CTkButton(
            btn_frame, text="⬇  Download", width=160,
            font=ctk.CTkFont(size=14, weight="bold"), command=self._start_download
        )
        self.dl_btn.grid(row=0, column=1, sticky="e")

    # ── Mode-Specific Option Panels ──────────────────────────────────────────

    def _clear_opts(self):
        for w in self.opts_frame.winfo_children():
            w.destroy()

    def _build_video_opts(self):
        self._clear_opts()
        ctk.CTkLabel(self.opts_frame, text="Format:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=12, pady=8, sticky="w"
        )

        # Preset dropdown
        self.video_format_var = ctk.StringVar(value=VIDEO_PRESET_FORMATS[0][0])
        labels = [f[0] for f in VIDEO_PRESET_FORMATS]

        # If we have fetched formats, append them
        if self._fetched_formats:
            for f in self._fetched_formats:
                label = self._format_label(f)
                labels.append(label)

        self.format_menu = ctk.CTkOptionMenu(self.opts_frame, variable=self.video_format_var, values=labels)
        self.format_menu.grid(row=0, column=1, padx=12, pady=8, sticky="ew")

    def _build_audio_opts(self):
        self._clear_opts()

        ctk.CTkLabel(self.opts_frame, text="Codec:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=12, pady=8, sticky="w"
        )
        self.audio_codec_var = ctk.StringVar(value="mp3")
        ctk.CTkOptionMenu(self.opts_frame, variable=self.audio_codec_var, values=AUDIO_CODECS).grid(
            row=0, column=1, padx=12, pady=8, sticky="w"
        )

        ctk.CTkLabel(self.opts_frame, text="Quality (kbps):").grid(
            row=0, column=2, padx=(24, 8), pady=8, sticky="w"
        )
        self.audio_quality_var = ctk.StringVar(value="192")
        ctk.CTkOptionMenu(self.opts_frame, variable=self.audio_quality_var, values=AUDIO_QUALITIES).grid(
            row=0, column=3, padx=12, pady=8, sticky="w"
        )

    def _build_playlist_opts(self):
        self._clear_opts()
        ctk.CTkLabel(self.opts_frame, text="Format:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=12, pady=8, sticky="w"
        )

        self.video_format_var = ctk.StringVar(value=VIDEO_PRESET_FORMATS[0][0])
        labels = [f[0] for f in VIDEO_PRESET_FORMATS]
        self.format_menu = ctk.CTkOptionMenu(self.opts_frame, variable=self.video_format_var, values=labels)
        self.format_menu.grid(row=0, column=1, padx=12, pady=8, sticky="ew")

        # Playlist range
        ctk.CTkLabel(self.opts_frame, text="Items:").grid(
            row=0, column=2, padx=(24, 8), pady=8, sticky="w"
        )
        self.playlist_range_entry = ctk.CTkEntry(self.opts_frame, placeholder_text="e.g. 1-10 or 1,3,5", width=120)
        self.playlist_range_entry.grid(row=0, column=3, padx=12, pady=8, sticky="w")

    def _on_mode_change(self):
        mode = self.mode_var.get()
        if mode == "video":
            self._build_video_opts()
        elif mode == "audio":
            self._build_audio_opts()
        elif mode == "playlist":
            self._build_playlist_opts()

    # ═══════════════════════════════════════════════════════════════════════════
    # Fetch Info
    # ═══════════════════════════════════════════════════════════════════════════

    def _fetch_info(self):
        url = self.url_entry.get().strip()
        if not url:
            self._set_status("Please enter a URL.", color="orange")
            return

        self.fetch_btn.configure(state="disabled", text="Fetching…")
        self._set_status("Fetching video info…")
        self._log_clear()

        def _worker():
            try:
                opts = {"quiet": True, "no_warnings": True, "skip_download": True}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)

                if info is None:
                    self.after(0, lambda: self._set_status("Could not extract info.", color="red"))
                    return

                self._video_info = info
                is_playlist = info.get("_type") == "playlist" or "entries" in info

                if is_playlist:
                    entries = list(info.get("entries", []))
                    count = len(entries)
                    title = info.get("title", "Unknown Playlist")
                    display = f"📋 Playlist: {title}  ({count} items)"
                    self._fetched_formats = []
                else:
                    title = info.get("title", "Unknown")
                    dur = str(timedelta(seconds=info.get("duration", 0)))
                    ch = info.get("channel", info.get("uploader", "Unknown"))
                    display = f"🎬 {title}\n⏱ {dur}  •  📺 {ch}"
                    self._fetched_formats = info.get("formats", [])

                def _update_ui():
                    self.info_label.configure(text=display, text_color=("white", "white"))
                    self._set_status("Info fetched.", color="green")
                    self.fetch_btn.configure(state="normal", text="Fetch Info")
                    # Auto-switch to playlist mode if detected
                    if is_playlist:
                        self.mode_var.set("playlist")
                        self._build_playlist_opts()
                    else:
                        self._on_mode_change()  # rebuild to include fetched formats

                self.after(0, _update_ui)

            except Exception as e:
                def _err():
                    self._set_status(f"Fetch failed: {e}", color="red")
                    self._log_append(str(e))
                    self.fetch_btn.configure(state="normal", text="Fetch Info")
                self.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _format_label(f):
        """Build a human-readable label for a yt-dlp format dict."""
        fid = f.get("format_id", "?")
        ext = f.get("ext", "?")
        res = f.get("resolution", "audio only") if f.get("vcodec", "none") != "none" else "audio only"
        h = f.get("height")
        fps = f.get("fps")
        note = f.get("format_note", "")
        size = f.get("filesize") or f.get("filesize_approx")

        parts = [f"[{fid}]", ext]
        if h:
            parts.append(f"{h}p")
        if fps:
            parts.append(f"{fps}fps")
        if note:
            parts.append(note)
        if size:
            mb = size / (1024 * 1024)
            parts.append(f"~{mb:.0f}MB")

        return " ".join(parts)

    # ═══════════════════════════════════════════════════════════════════════════
    # Download
    # ═══════════════════════════════════════════════════════════════════════════

    def _browse_dir(self):
        path = _native_askdirectory(title="Select Download Directory")
        if path:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, path)

    def _resolve_format_string(self):
        """Map the UI selection back to a yt-dlp format string."""
        mode = self.mode_var.get()

        if mode == "audio":
            return "bestaudio/best"

        selected = self.video_format_var.get()

        # Check presets first
        for label, fmt in VIDEO_PRESET_FORMATS:
            if selected == label:
                return fmt

        # If it's a fetched format, extract the format_id from [xxx]
        match = re.match(r"\[(\S+)\]", selected)
        if match:
            return match.group(1)

        return "bv*+ba/b"  # safe fallback

    def _build_ydl_opts(self, output_dir):
        """Construct the yt-dlp options dict based on current UI state."""
        mode = self.mode_var.get()
        fmt = self._resolve_format_string()

        opts = {
            "format": fmt,
            "paths": {"home": output_dir},
            "outtmpl": {"default": "%(title)s [%(id)s].%(ext)s"},
            "progress_hooks": [self._progress_hook],
            "logger": GUILogger(lambda msg: self.after(0, lambda m=msg: self._log_append(m))),
            "noplaylist": mode != "playlist",
            "quiet": True,
            "no_warnings": False,
            "merge_output_format": "mp4",
        }

        if mode == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": self.audio_codec_var.get(),
                "preferredquality": self.audio_quality_var.get(),
            }]
            # Don't force mp4 merge for audio
            del opts["merge_output_format"]

        if mode == "playlist":
            opts["outtmpl"]["default"] = "%(playlist_title)s/%(playlist_index)03d - %(title)s [%(id)s].%(ext)s"
            opts["noplaylist"] = False
            # Handle playlist range
            if hasattr(self, "playlist_range_entry"):
                rng = self.playlist_range_entry.get().strip()
                if rng:
                    opts["playlist_items"] = rng

        return opts

    def _start_download(self):
        url = self.url_entry.get().strip()
        if not url:
            self._set_status("Please enter a URL.", color="orange")
            return

        # Always prompt for output directory
        output_dir = self.dir_entry.get().strip()
        if not output_dir:
            output_dir = _native_askdirectory(title="Select Download Directory")
            if not output_dir:
                return
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, output_dir)

        if not os.path.isdir(output_dir):
            try:
                os.makedirs(output_dir, exist_ok=True)
            except OSError as e:
                self._set_status(f"Cannot create directory: {e}", color="red")
                return

        # Prepare UI
        self._cancel_flag.clear()
        self.dl_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress_bar.set(0)
        self._set_status("Starting download…")
        self._log_clear()

        opts = self._build_ydl_opts(output_dir)

        def _worker():
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])

                if self._cancel_flag.is_set():
                    self.after(0, lambda: self._set_status("Download cancelled.", color="orange"))
                else:
                    self.after(0, lambda: self._set_status("✔ Download complete!", color="green"))
                    self.after(0, lambda: self.progress_bar.set(1.0))

            except yt_dlp.utils.DownloadError as e:
                if self._cancel_flag.is_set():
                    self.after(0, lambda: self._set_status("Download cancelled.", color="orange"))
                else:
                    self.after(0, lambda: self._set_status(f"Download error: {e}", color="red"))
                    self.after(0, lambda: self._log_append(str(e)))
            except Exception as e:
                self.after(0, lambda: self._set_status(f"Unexpected error: {e}", color="red"))
                self.after(0, lambda: self._log_append(str(e)))
            finally:
                self.after(0, self._download_finished)

        self._download_thread = threading.Thread(target=_worker, daemon=True)
        self._download_thread.start()

    def _progress_hook(self, d):
        """Called by yt-dlp during download with progress data."""
        if self._cancel_flag.is_set():
            raise yt_dlp.utils.DownloadError("Cancelled by user")

        status = d.get("status")

        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed")
            eta = d.get("eta")

            if total > 0:
                pct = downloaded / total
                self.after(0, lambda p=pct: self.progress_bar.set(p))

            parts = []
            if total > 0:
                parts.append(f"{downloaded / 1048576:.1f}/{total / 1048576:.1f} MB")
            if speed:
                parts.append(f"{speed / 1048576:.1f} MB/s")
            if eta:
                parts.append(f"ETA {timedelta(seconds=eta)}")

            msg = "Downloading: " + "  •  ".join(parts) if parts else "Downloading…"
            self.after(0, lambda m=msg: self._set_status(m))

        elif status == "finished":
            fname = os.path.basename(d.get("filename", ""))
            self.after(0, lambda: self._log_append(f"✔ Finished: {fname}"))

    def _cancel_download(self):
        self._cancel_flag.set()
        self._set_status("Cancelling…", color="orange")

    def _download_finished(self):
        self.dl_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._download_thread = None

    # ═══════════════════════════════════════════════════════════════════════════
    # UI Helpers
    # ═══════════════════════════════════════════════════════════════════════════

    def _set_status(self, text, color="gray"):
        self.status_label.configure(text=text, text_color=color)

    def _log_append(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _log_clear(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = YtDlpGUI()
    app.mainloop()