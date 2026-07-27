#!/usr/bin/env python3
"""
yt-dlp GUI — A modern CustomTkinter frontend for yt-dlp.
Supports single video, audio-only extraction, playlist/batch downloads,
format selection, subtitles, SponsorBlock, metadata embedding, chapter
splitting, thumbnail extraction, rate limiting, and archive tracking.
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

import config
import validation

try:
    import yt_dlp
except ImportError:
    print("ERROR: yt_dlp not found. Run: pip install yt-dlp")
    sys.exit(1)

# Preflight: fail fast with a friendly message if the transcript dep is missing
# (it's imported for real inside transcript.py, below).
import importlib.util
if importlib.util.find_spec("youtube_transcript_api") is None:
    print("ERROR: youtube-transcript-api not found. Run: pip install youtube-transcript-api")
    sys.exit(1)

import transcript
import downloader


# ── deno (external JS runtime) ────────────────────────────────────────────────
# yt-dlp solves YouTube's player challenge with an external JS runtime, deno.
# It is NOT a pip dependency — a venv cannot supply it — so neither
# requirements.txt nor a reinstall will fix a missing one. Checked at run time
# so the user gets a plain explanation instead of an opaque yt-dlp extractor
# error, which is what surfaces otherwise.
import urllib.parse

_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def deno_missing_for(url):
    """True when this URL needs deno for playback extraction and it isn't on PATH."""
    if shutil.which("deno"):
        return False
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _YOUTUBE_HOSTS)


# UI scale is resolved in config.py (pure); applied here because it needs ctk,
# and must run before any widgets are built.
_dpi_scale = config.detect_scale()
ctk.set_widget_scaling(_dpi_scale)
ctk.set_window_scaling(_dpi_scale)


# ── Native Directory Picker ───────────────────────────────────────────────────
def _native_askdirectory(title="Select Directory"):
    """SECURITY: title must remain hardcoded — never pass user input. (CWE-78)"""
    if shutil.which("zenity"):
        try:
            r = subprocess.run(
                ["zenity", "--file-selection", "--directory", f"--title={title}"],
                capture_output=True, text=True, timeout=120
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
            return ""
        except (subprocess.TimeoutExpired, OSError):
            pass
    elif shutil.which("kdialog"):
        try:
            r = subprocess.run(
                ["kdialog", "--getexistingdirectory", os.path.expanduser("~"), "--title", title],
                capture_output=True, text=True, timeout=120
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
            return ""
        except (subprocess.TimeoutExpired, OSError):
            pass
    return filedialog.askdirectory(title=title)


# ── Constants ─────────────────────────────────────────────────────────────────
APP_NAME = "yt-dlp GUI"
APP_VERSION = "1.4.0"
WINDOW_MIN_W = 740
WINDOW_MIN_H = 700

AUDIO_CODECS = ["mp3", "opus", "m4a", "flac", "wav", "vorbis"]
AUDIO_QUALITIES = ["320", "256", "192", "128", "96"]

VIDEO_PRESET_FORMATS = [
    ("Best video + audio", "bv*+ba/b"),
    ("Best MP4 (≤1080p)", "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4]"),
    ("Best MP4 (≤720p)", "bv*[ext=mp4][height<=720]+ba[ext=m4a]/b[ext=mp4]"),
    ("Best MP4 (≤480p)", "bv*[ext=mp4][height<=480]+ba[ext=m4a]/b[ext=mp4]"),
    ("Worst quality (smallest)", "worst"),
]

SUBTITLE_LANGS = ["en", "es", "fr", "de", "ja", "ko", "pt", "zh", "ar", "ru", "it", "nl", "all"]

SPONSORBLOCK_ACTIONS = ["skip", "remove"]
SPONSORBLOCK_CATS = [
    "sponsor", "intro", "outro", "selfpromo",
    "preview", "music_offtopic", "interaction", "filler"
]

COOKIE_BROWSERS = ["-- none --", "firefox", "chrome", "chromium", "brave", "edge", "opera", "safari", "vivaldi"]

RATE_LIMITS = ["No limit", "1M", "2M", "5M", "10M", "20M", "50M"]

# ── Transcript mode ──
TRANSCRIPT_FORMATS = [("Plain Text", "plain"), ("Markdown", "markdown"), ("Obsidian Note", "obsidian")]
TRANSCRIPT_LANGS = ["auto", "en", "es", "fr", "de", "ja", "ko", "pt", "zh", "ar", "ru", "it", "nl"]


# ── Main Application ─────────────────────────────────────────────────────────
class YtDlpGUI(ctk.CTk):
    def __init__(self):
        super().__init__(className="ytdlp-gui")
        self.title(APP_NAME)
        self._align_font_dpi()
        start_w, start_h = self._start_size()
        self.minsize(start_w, start_h)
        self.geometry(f"{start_w}x{start_h}")

        self._fetched_formats = []
        self._video_info = None
        self._active = None                 # controller currently running, for cancel/close
        _cb = dict(
            on_status=lambda text, color="gray": self.after(0, lambda: self._set_status(text, color)),
            on_log=lambda m: self.after(0, lambda mm=m: self._log_append(mm)),
            on_progress=lambda p: self.after(0, lambda pp=p: self.progress_bar.set(pp)),
            on_finished=lambda: self.after(0, self._download_finished),
        )
        self._downloader = downloader.Downloader(**_cb)
        self._extractor = transcript.TranscriptExtractor(**_cb)

        self._build_ui()
        self._bind_x11_scroll()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        icon_path = os.path.join(os.path.dirname(__file__), "assets", "icon.png")
        if os.path.exists(icon_path):
            try:
                self.iconphoto(True, tk.PhotoImage(file=icon_path))
            except tk.TclError:
                pass  # window icon is cosmetic; a bad image must not block startup

    # ── Display scaling ───────────────────────────────────────────────────────
    # customtkinter sizes every font — and the glyphs it draws rounded widget
    # corners with — in pixels. Tk converts those pixel sizes to points using
    # its own scaling factor before the font renderer converts them back to
    # pixels at the desktop's DPI, so a disagreement between the two blows up
    # all text and every corner glyph inside correctly-sized widgets (see the
    # long note in config.py). Telling Tk the DPI the renderer is actually using
    # makes a pixel mean a pixel again. Must run before the first widget is
    # built: fonts already created keep the size they were created with.
    def _align_font_dpi(self):
        dpi = config.read_xft_dpi()
        if not dpi:
            return
        wanted = dpi / 72.0  # tk scaling is pixels per point
        try:
            if abs(float(self.tk.call("tk", "scaling")) - wanted) > 0.01:
                self.tk.call("tk", "scaling", wanted)
        except (tk.TclError, ValueError):
            pass  # cosmetic; a display that refuses this must not block startup

    # Start size, in the unscaled units the layout is written in — customtkinter
    # multiplies both by the window scaling on the way out. On a scaled desktop
    # that product can exceed the screen, so it is capped here; everything lives
    # in a scrollable frame, so a shorter window only means more scrolling.
    def _start_size(self):
        scale = _dpi_scale if _dpi_scale > 0 else 1.0
        avail_w = int(self.winfo_screenwidth() * 0.9 / scale)
        avail_h = int(self.winfo_screenheight() * 0.9 / scale)
        return min(WINDOW_MIN_W, avail_w), min(WINDOW_MIN_H, avail_h)

    def _on_close(self):
        if self._active:
            self._active.cancel()
            self._active.join(timeout=3)
        self.destroy()

    # ── FIX: Linux/X11 mouse wheel scrolling ─────────────────────────────────
    # X11 fires <Button-4> (up) / <Button-5> (down) instead of <MouseWheel>.
    # CTkScrollableFrame only binds <MouseWheel>, so scrolling is dead on Linux.
    # bind_all catches events from any child widget inside the scroll area.
    def _bind_x11_scroll(self):
        canvas = self._scroll._parent_canvas
        self.bind_all(
            "<Button-4>",
            lambda e: canvas.yview_scroll(-3, "units"),
            add="+"
        )
        self.bind_all(
            "<Button-5>",
            lambda e: canvas.yview_scroll(3, "units"),
            add="+"
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # UI Construction
    # ═══════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        conf = config.read_config()

        # ── Scrollable container ──
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._scroll = ctk.CTkScrollableFrame(self)
        self._scroll.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        self._scroll.grid_columnconfigure(0, weight=1)

        p = self._scroll  # parent shorthand
        row = 0

        # ── Header with scale control ──
        hdr_f = ctk.CTkFrame(p, fg_color="transparent")
        hdr_f.grid(row=row, column=0, padx=16, pady=(16, 4), sticky="ew")
        hdr_f.grid_columnconfigure(0, weight=1)

        title_f = ctk.CTkFrame(hdr_f, fg_color="transparent")
        title_f.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(title_f, text=APP_NAME, font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, sticky="w"
        )
        ctk.CTkLabel(title_f, text=f"v{APP_VERSION}", text_color="gray").grid(
            row=0, column=1, padx=(8, 0), sticky="w"
        )

        # ── UI Scale control (persists to config) ──
        scale_f = ctk.CTkFrame(hdr_f, fg_color="transparent")
        scale_f.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(scale_f, text="UI Scale:", text_color="gray",
                      font=ctk.CTkFont(size=11)).grid(row=0, column=0, padx=(0, 4))
        conf = config.read_config()
        current_scale = conf.get("scale", "Auto")
        self._scale_var = ctk.StringVar(value=current_scale)
        self._scale_menu = ctk.CTkOptionMenu(
            scale_f, variable=self._scale_var, values=config.SCALE_OPTIONS,
            width=80, font=ctk.CTkFont(size=11), command=self._on_scale_change
        )
        self._scale_menu.grid(row=0, column=1)
        row += 1

        # ── URL Entry with clear button ──
        url_f = ctk.CTkFrame(p, fg_color="transparent")
        url_f.grid(row=row, column=0, padx=16, pady=(8, 8), sticky="ew")
        url_f.grid_columnconfigure(0, weight=1)

        self.url_entry = ctk.CTkEntry(url_f, placeholder_text="Paste any supported URL…")
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.url_entry.bind("<Return>", lambda e: self._fetch_info())
        # FIX: Select all — Ctrl+A always works; click-to-focus selects after
        # the Button-1 release so the cursor repositioning doesn't kill it.
        self.url_entry.bind("<Control-a>", self._url_select_all)
        self._url_had_focus = False
        self.url_entry.bind("<FocusIn>", self._url_on_focus_in)
        self.url_entry.bind("<ButtonRelease-1>", self._url_on_click_release)

        self._url_paste_btn = ctk.CTkButton(
            url_f, text="📋", width=32, fg_color="gray", hover_color="#666",
            font=ctk.CTkFont(size=13), command=self._paste_url
        )
        self._url_paste_btn.grid(row=0, column=1, padx=(0, 4))

        self._url_clear_btn = ctk.CTkButton(
            url_f, text="✕", width=32, fg_color="gray", hover_color="#666",
            font=ctk.CTkFont(size=13), command=self._clear_url
        )
        self._url_clear_btn.grid(row=0, column=2, padx=(0, 4))

        self.fetch_btn = ctk.CTkButton(url_f, text="Fetch Info", width=100, command=self._fetch_info)
        self.fetch_btn.grid(row=0, column=3)
        row += 1

        # ── Info Display ──
        self.info_label = ctk.CTkLabel(
            p, text="Enter a URL and click Fetch Info to begin.",
            wraplength=680, justify="left", text_color="gray"
        )
        self.info_label.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="w")
        row += 1

        # ── Mode Selection ──
        mode_f = ctk.CTkFrame(p)
        mode_f.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="ew")
        mode_f.grid_columnconfigure((0, 1, 2, 3), weight=1)
        ctk.CTkLabel(mode_f, text="Mode:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=4, padx=12, pady=(8, 4), sticky="w"
        )
        saved_mode = conf.get("mode", "video")
        if saved_mode not in ("video", "audio", "playlist", "transcript"):
            saved_mode = "video"
        self.mode_var = ctk.StringVar(value=saved_mode)
        modes = [
            ("🎬  Video", "video"),
            ("🎵  Audio Only", "audio"),
            ("📋  Playlist", "playlist"),
            ("📝  Transcripts", "transcript"),
        ]
        for i, (lbl, val) in enumerate(modes):
            ctk.CTkRadioButton(mode_f, text=lbl, variable=self.mode_var, value=val,
                               command=self._on_mode_change).grid(row=1, column=i, padx=12, pady=(0, 8))
        row += 1

        # Persistent state vars created here (before the first
        # _on_mode_change call) so transcript-mode opts can read them.
        # The matching widgets in the Extras section bind to these existing
        # vars rather than creating their own.
        saved_cookie = conf.get("cookie_browser", "-- none --")
        if saved_cookie not in COOKIE_BROWSERS:
            saved_cookie = "-- none --"
        self.cookie_var = ctk.StringVar(value=saved_cookie)

        # ── Format Options (swaps per mode) ──
        self.opts_frame = ctk.CTkFrame(p)
        self.opts_frame.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="ew")
        self.opts_frame.grid_columnconfigure(1, weight=1)
        row += 1
        self._on_mode_change()

        # ── Extras Section ──
        # Stored on self so _on_mode_change can hide the whole section in
        # transcript mode — every Extras control is a yt-dlp download option
        # and none of them reach the youtube-transcript-api code path.
        self.extras_label = ctk.CTkLabel(p, text="Extras", font=ctk.CTkFont(size=14, weight="bold"))
        self.extras_label.grid(row=row, column=0, padx=16, pady=(8, 4), sticky="w")
        self._extras_label_grid = {"row": row, "column": 0, "padx": 16, "pady": (8, 4), "sticky": "w"}
        row += 1

        self.extras_frame = ctk.CTkFrame(p)
        self.extras_frame.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="ew")
        self.extras_frame.grid_columnconfigure((0, 1), weight=1)
        self._extras_frame_grid = {"row": row, "column": 0, "padx": 16, "pady": (0, 8), "sticky": "ew"}
        extras_f = self.extras_frame
        row += 1

        # ── Left column: Subtitles, Thumbnails, Chapters, Metadata ──
        left = ctk.CTkFrame(extras_f, fg_color="transparent")
        left.grid(row=0, column=0, padx=(8, 4), pady=8, sticky="nsew")
        lr = 0

        self.subs_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(left, text="Download subtitles", variable=self.subs_var,
                        command=self._toggle_subs).grid(row=lr, column=0, columnspan=2, sticky="w", pady=(0, 4))
        lr += 1
        self.subs_lang_var = ctk.StringVar(value="en")
        self.subs_lang_menu = ctk.CTkOptionMenu(left, variable=self.subs_lang_var, values=SUBTITLE_LANGS, width=80)
        self.subs_lang_menu.grid(row=lr, column=0, sticky="w", pady=(0, 4))
        self.subs_lang_menu.configure(state="disabled")

        self.subs_auto_var = ctk.BooleanVar(value=True)
        self.subs_auto_cb = ctk.CTkCheckBox(left, text="Include auto-generated", variable=self.subs_auto_var)
        self.subs_auto_cb.grid(row=lr, column=1, sticky="w", padx=(8, 0), pady=(0, 4))
        self.subs_auto_cb.configure(state="disabled")

        self.subs_embed_var = ctk.BooleanVar(value=True)
        self.subs_embed_cb = ctk.CTkCheckBox(left, text="Embed in file", variable=self.subs_embed_var)
        lr += 1
        self.subs_embed_cb.grid(row=lr, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.subs_embed_cb.configure(state="disabled")
        lr += 1

        self.thumb_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(left, text="Save thumbnail", variable=self.thumb_var).grid(
            row=lr, column=0, columnspan=2, sticky="w", pady=(0, 4))
        lr += 1

        self.thumb_embed_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(left, text="Embed thumbnail in file", variable=self.thumb_embed_var).grid(
            row=lr, column=0, columnspan=2, sticky="w", pady=(0, 8))
        lr += 1

        self.chapters_split_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(left, text="Split by chapters", variable=self.chapters_split_var).grid(
            row=lr, column=0, columnspan=2, sticky="w", pady=(0, 4))
        lr += 1

        self.meta_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(left, text="Embed metadata (title, artist, date)", variable=self.meta_var).grid(
            row=lr, column=0, columnspan=2, sticky="w", pady=(0, 4))
        lr += 1

        # ── Right column: SponsorBlock, Rate limit, Cookies, Archive ──
        right = ctk.CTkFrame(extras_f, fg_color="transparent")
        right.grid(row=0, column=1, padx=(4, 8), pady=8, sticky="nsew")
        rr = 0

        self.sb_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(right, text="SponsorBlock", variable=self.sb_var,
                        command=self._toggle_sb).grid(row=rr, column=0, columnspan=2, sticky="w", pady=(0, 4))
        rr += 1

        self.sb_action_var = ctk.StringVar(value="skip")
        self.sb_action_menu = ctk.CTkOptionMenu(right, variable=self.sb_action_var,
                                                 values=SPONSORBLOCK_ACTIONS, width=90)
        self.sb_action_menu.grid(row=rr, column=0, sticky="w", pady=(0, 4))
        self.sb_action_menu.configure(state="disabled")

        self.sb_cats_label = ctk.CTkLabel(right, text="sponsor, selfpromo", text_color="gray",
                                           font=ctk.CTkFont(size=11))
        self.sb_cats_label.grid(row=rr, column=1, sticky="w", padx=(8, 0), pady=(0, 4))
        rr += 1

        self.sb_cat_frame = ctk.CTkFrame(right, fg_color="transparent")
        self.sb_cat_frame.grid(row=rr, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.sb_cat_vars = {}
        defaults_on = {"sponsor", "selfpromo"}
        for ci, cat in enumerate(SPONSORBLOCK_CATS):
            v = ctk.BooleanVar(value=cat in defaults_on)
            cb = ctk.CTkCheckBox(self.sb_cat_frame, text=cat, variable=v,
                                 font=ctk.CTkFont(size=11), checkbox_width=16, checkbox_height=16,
                                 command=self._update_sb_label)
            cb.grid(row=ci // 2, column=ci % 2, sticky="w", padx=(0, 8), pady=1)
            cb.configure(state="disabled")
            self.sb_cat_vars[cat] = (v, cb)
        rr += 1

        ctk.CTkLabel(right, text="Rate limit:").grid(row=rr, column=0, sticky="w", pady=(0, 4))
        saved_rate = conf.get("rate_limit", "No limit")
        if saved_rate not in RATE_LIMITS:
            saved_rate = "No limit"
        self.rate_var = ctk.StringVar(value=saved_rate)
        ctk.CTkOptionMenu(
            right, variable=self.rate_var, values=RATE_LIMITS, width=100,
            command=lambda v: config.save_config_key("rate_limit", v),
        ).grid(row=rr, column=1, sticky="w", padx=(8, 0), pady=(0, 4))
        rr += 1

        ctk.CTkLabel(right, text="Cookies from:").grid(row=rr, column=0, sticky="w", pady=(4, 4))
        # cookie_var is created earlier in _setup_ui — bind to the existing
        # StringVar so the transcript-mode dropdown stays in sync.
        ctk.CTkOptionMenu(
            right, variable=self.cookie_var, values=COOKIE_BROWSERS, width=120,
            command=lambda v: config.save_config_key("cookie_browser", v),
        ).grid(row=rr, column=1, sticky="w", padx=(8, 0), pady=(4, 4))
        rr += 1

        self.archive_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(right, text="Track downloads (skip duplicates)",
                        variable=self.archive_var).grid(
            row=rr, column=0, columnspan=2, sticky="w", pady=(8, 4))
        rr += 1

        # ── Output Directory ──
        dir_f = ctk.CTkFrame(p, fg_color="transparent")
        dir_f.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="ew")
        dir_f.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(dir_f, text="Save to:").grid(row=0, column=0, padx=(0, 8))
        self.dir_entry = ctk.CTkEntry(dir_f, placeholder_text="Select output directory…")
        self.dir_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        saved_dir = conf.get("save_dir", "")
        if saved_dir and os.path.isdir(saved_dir):
            self.dir_entry.insert(0, saved_dir)
        self.dir_entry.bind("<FocusOut>", self._persist_dir_entry)
        self.browse_btn = ctk.CTkButton(dir_f, text="Browse…", width=90, command=self._browse_dir)
        self.browse_btn.grid(row=0, column=2)
        self.open_dir_btn = ctk.CTkButton(
            dir_f, text="📂", width=40, fg_color="gray", hover_color="#666",
            font=ctk.CTkFont(size=13), command=self._open_output_dir
        )
        self.open_dir_btn.grid(row=0, column=3, padx=(8, 0))
        row += 1

        # ── Progress ──
        self.progress_bar = ctk.CTkProgressBar(p)
        self.progress_bar.grid(row=row, column=0, padx=16, pady=(8, 4), sticky="ew")
        self.progress_bar.set(0)
        row += 1

        self.status_label = ctk.CTkLabel(p, text="Idle", text_color="gray")
        self.status_label.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="w")
        row += 1

        # ── Log Output ──
        log_hdr = ctk.CTkFrame(p, fg_color="transparent")
        log_hdr.grid(row=row, column=0, padx=16, pady=(0, 2), sticky="ew")
        log_hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(log_hdr, text="Log", text_color="gray",
                      font=ctk.CTkFont(size=11)).grid(row=0, column=0, sticky="w")
        self.copy_log_btn = ctk.CTkButton(
            log_hdr, text="Copy", width=60, fg_color="gray", hover_color="#666",
            font=ctk.CTkFont(size=11), command=self._copy_log
        )
        self.copy_log_btn.grid(row=0, column=1, sticky="e")
        row += 1

        self.log_box = ctk.CTkTextbox(p, height=100, state="disabled",
                                       font=ctk.CTkFont(family="monospace", size=11))
        self.log_box.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="ew")
        row += 1

        # ── Action Buttons ──
        btn_f = ctk.CTkFrame(p, fg_color="transparent")
        btn_f.grid(row=row, column=0, padx=16, pady=(0, 16), sticky="ew")
        btn_f.grid_columnconfigure(0, weight=1)

        self.reset_btn = ctk.CTkButton(
            btn_f, text="↺ Reset", fg_color="#555", hover_color="#777",
            width=80, command=self._reset_all
        )
        self.reset_btn.grid(row=0, column=0, sticky="w")

        self.cancel_btn = ctk.CTkButton(
            btn_f, text="Cancel", fg_color="gray", hover_color="#666",
            width=100, command=self._cancel_download, state="disabled"
        )
        self.cancel_btn.grid(row=0, column=1, padx=(0, 8), sticky="e")

        self.dl_btn = ctk.CTkButton(
            btn_f, text="⬇  Download", width=160,
            font=ctk.CTkFont(size=14, weight="bold"), command=self._start_download
        )
        self.dl_btn.grid(row=0, column=2, sticky="e")

        # Re-run _on_mode_change now that ALL widgets exist (dl_btn,
        # extras_frame, etc.). The earlier call during opts_frame creation
        # ran while those late-built widgets were still missing, so the
        # button-label and Extras-visibility branches were skipped.
        # _build_*_opts is idempotent (it calls _clear_opts first), so this
        # extra invocation is safe and just resyncs everything.
        self._on_mode_change()

    # ── URL helpers ───────────────────────────────────────────────────────────

    def _url_select_all(self, event=None):
        """Select all text in URL entry (Ctrl+A handler)."""
        self.url_entry.select_range(0, "end")
        self.url_entry.icursor("end")
        return "break"  # Prevent default Ctrl+A behavior

    def _url_on_focus_in(self, event=None):
        """Flag that focus was just gained — next click-release will select all."""
        self._url_had_focus = False

    def _url_on_click_release(self, event=None):
        """After the focus-granting click completes, select all text."""
        if not self._url_had_focus:
            self._url_had_focus = True
            self.url_entry.select_range(0, "end")
            self.url_entry.icursor("end")

    def _clear_url(self):
        """Clear URL entry and reset info display."""
        self.url_entry.delete(0, "end")
        self.info_label.configure(
            text="Enter a URL and click Fetch Info to begin.", text_color="gray"
        )
        self._fetched_formats = []
        self._video_info = None
        self._on_mode_change()

    def _paste_url(self):
        """Paste clipboard contents into the URL entry."""
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            self._set_status("Clipboard is empty.", color="orange")
            return
        if not text:
            return
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, text)
        self.url_entry.focus_set()

    # ── Full Reset ────────────────────────────────────────────────────────────

    def _reset_all(self):
        """Reset entire UI to initial state. Save-dir is preserved."""
        # URL + info
        self._clear_url()
        # Mode back to video (and persist)
        self.mode_var.set("video")
        self._on_mode_change()
        # Output dir — keep persisted value, don't wipe
        # Progress
        self.progress_bar.set(0)
        self._set_status("Idle")
        self._log_clear()
        # Extras — uncheck everything
        self.subs_var.set(False)
        self._toggle_subs()
        self.thumb_var.set(False)
        self.thumb_embed_var.set(False)
        self.chapters_split_var.set(False)
        self.meta_var.set(False)
        self.sb_var.set(False)
        self._toggle_sb()
        self.rate_var.set("No limit")
        self.cookie_var.set("-- none --")
        self.archive_var.set(False)
        # Buttons
        self.dl_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.fetch_btn.configure(state="normal", text="Fetch Info")

    # ── Scale change handler ──────────────────────────────────────────────────

    def _on_scale_change(self, choice):
        conf = config.read_config()
        conf["scale"] = choice
        config.write_config(conf)
        # Apply live — CTk's ScalingTracker propagates to all existing widgets
        if choice == "Auto":
            new_scale = config.detect_scale()
        else:
            try:
                new_scale = float(choice)
            except ValueError:
                new_scale = 1.0
        ctk.set_widget_scaling(new_scale)
        ctk.set_window_scaling(new_scale)
        self._set_status(f"Scale set to {new_scale:.2f}×", color="green")

    # ── Toggle helpers ────────────────────────────────────────────────────────

    def _toggle_subs(self):
        st = "normal" if self.subs_var.get() else "disabled"
        self.subs_lang_menu.configure(state=st)
        self.subs_auto_cb.configure(state=st)
        self.subs_embed_cb.configure(state=st)

    def _toggle_sb(self):
        st = "normal" if self.sb_var.get() else "disabled"
        self.sb_action_menu.configure(state=st)
        for _, (_, cb) in self.sb_cat_vars.items():
            cb.configure(state=st)

    def _update_sb_label(self):
        active = [c for c, (v, _) in self.sb_cat_vars.items() if v.get()]
        self.sb_cats_label.configure(text=", ".join(active) if active else "none selected")

    # ── Mode-Specific Option Panels ──────────────────────────────────────────

    def _clear_opts(self):
        for w in self.opts_frame.winfo_children():
            w.destroy()

    def _build_video_opts(self):
        self._clear_opts()
        ctk.CTkLabel(self.opts_frame, text="Format:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=12, pady=8, sticky="w"
        )
        self.video_format_var = ctk.StringVar(value=VIDEO_PRESET_FORMATS[0][0])
        labels = [f[0] for f in VIDEO_PRESET_FORMATS]
        if self._fetched_formats:
            for f in self._fetched_formats:
                labels.append(self._format_label(f))
        self.format_menu = ctk.CTkOptionMenu(self.opts_frame, variable=self.video_format_var, values=labels)
        self.format_menu.grid(row=0, column=1, padx=12, pady=8, sticky="ew")

    def _build_audio_opts(self):
        self._clear_opts()
        ctk.CTkLabel(self.opts_frame, text="Codec:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=12, pady=8, sticky="w"
        )
        conf = config.read_config()
        saved_codec = conf.get("audio_codec", "mp3")
        if saved_codec not in AUDIO_CODECS:
            saved_codec = "mp3"
        self.audio_codec_var = ctk.StringVar(value=saved_codec)
        ctk.CTkOptionMenu(
            self.opts_frame, variable=self.audio_codec_var, values=AUDIO_CODECS,
            command=self._on_audio_codec_change,
        ).grid(row=0, column=1, padx=12, pady=8, sticky="w")
        self.audio_quality_label = ctk.CTkLabel(self.opts_frame, text="Quality (kbps):")
        self.audio_quality_label.grid(row=0, column=2, padx=(24, 8), pady=8, sticky="w")
        self.audio_quality_var = ctk.StringVar(value="192")
        self.audio_quality_menu = ctk.CTkOptionMenu(
            self.opts_frame, variable=self.audio_quality_var, values=AUDIO_QUALITIES
        )
        self.audio_quality_menu.grid(row=0, column=3, padx=12, pady=8, sticky="w")
        self._on_audio_codec_change(saved_codec)

    def _on_audio_codec_change(self, codec):
        lossless = codec in ("flac", "wav")
        if hasattr(self, "audio_quality_menu"):
            self.audio_quality_menu.configure(state="disabled" if lossless else "normal")
        if hasattr(self, "audio_quality_label"):
            self.audio_quality_label.configure(
                text="Lossless" if lossless else "Quality (kbps):"
            )
        config.save_config_key("audio_codec", codec)

    def _build_playlist_opts(self):
        self._clear_opts()
        ctk.CTkLabel(self.opts_frame, text="Format:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=12, pady=8, sticky="w"
        )
        self.video_format_var = ctk.StringVar(value=VIDEO_PRESET_FORMATS[0][0])
        labels = [f[0] for f in VIDEO_PRESET_FORMATS]
        self.format_menu = ctk.CTkOptionMenu(self.opts_frame, variable=self.video_format_var, values=labels)
        self.format_menu.grid(row=0, column=1, padx=12, pady=8, sticky="ew")
        ctk.CTkLabel(self.opts_frame, text="Items:").grid(
            row=0, column=2, padx=(24, 8), pady=8, sticky="w"
        )
        self.playlist_range_entry = ctk.CTkEntry(self.opts_frame, placeholder_text="e.g. 1-10 or 1,3,5", width=120)
        self.playlist_range_entry.grid(row=0, column=3, padx=12, pady=8, sticky="w")

    def _build_transcript_opts(self):
        self._clear_opts()
        conf = config.read_config()

        # Row 0: Format radios
        ctk.CTkLabel(self.opts_frame, text="Format:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=12, pady=(8, 4), sticky="w"
        )
        saved_fmt = conf.get("transcript_format", "markdown")
        if saved_fmt not in {v for _, v in TRANSCRIPT_FORMATS}:
            saved_fmt = "markdown"
        self.transcript_format_var = ctk.StringVar(value=saved_fmt)
        fmt_frame = ctk.CTkFrame(self.opts_frame, fg_color="transparent")
        fmt_frame.grid(row=0, column=1, columnspan=3, padx=12, pady=(8, 4), sticky="w")
        for i, (lbl, val) in enumerate(TRANSCRIPT_FORMATS):
            ctk.CTkRadioButton(
                fmt_frame, text=lbl, variable=self.transcript_format_var, value=val,
                command=lambda v=val: config.save_config_key("transcript_format", v),
            ).grid(row=0, column=i, padx=(0, 14), sticky="w")

        # Row 1: Language + Timestamps + Per-file
        ctk.CTkLabel(self.opts_frame, text="Language:").grid(
            row=1, column=0, padx=12, pady=(4, 4), sticky="w"
        )
        saved_lang = conf.get("transcript_lang", "auto")
        if saved_lang not in TRANSCRIPT_LANGS:
            saved_lang = "auto"
        self.transcript_lang_var = ctk.StringVar(value=saved_lang)
        ctk.CTkOptionMenu(
            self.opts_frame, variable=self.transcript_lang_var, values=TRANSCRIPT_LANGS,
            width=90,
            command=lambda v: config.save_config_key("transcript_lang", v),
        ).grid(row=1, column=1, padx=12, pady=(4, 4), sticky="w")

        self.transcript_timestamps_var = ctk.BooleanVar(value=conf.get("transcript_ts", "0") == "1")
        ctk.CTkCheckBox(
            self.opts_frame, text="Timestamps", variable=self.transcript_timestamps_var,
            command=lambda: config.save_config_key(
                "transcript_ts", "1" if self.transcript_timestamps_var.get() else "0"
            ),
        ).grid(row=1, column=2, padx=12, pady=(4, 4), sticky="w")

        self.transcript_per_file_var = ctk.BooleanVar(value=conf.get("transcript_per_file", "0") == "1")
        ctk.CTkCheckBox(
            self.opts_frame, text="One file per video", variable=self.transcript_per_file_var,
            command=lambda: config.save_config_key(
                "transcript_per_file", "1" if self.transcript_per_file_var.get() else "0"
            ),
        ).grid(row=1, column=3, padx=12, pady=(4, 4), sticky="w")

        # Row 2: Playlist range (only meaningful for playlist URLs, but always shown)
        ctk.CTkLabel(self.opts_frame, text="Items:").grid(
            row=2, column=0, padx=12, pady=(4, 4), sticky="w"
        )
        self.transcript_range_entry = ctk.CTkEntry(
            self.opts_frame,
            placeholder_text="Playlist range — e.g. 1-10 or 1,3,5 (leave blank for all)",
            width=320,
        )
        self.transcript_range_entry.grid(row=2, column=1, columnspan=3, padx=12, pady=(4, 4), sticky="ew")

        # Row 3: Cookies-from-browser. Used by the yt-dlp fallback path when
        # the API is rate-limited and YouTube demands "Sign in to confirm
        # you're not a bot". Bound to self.cookie_var so it stays in sync
        # with the (currently hidden) Extras dropdown.
        ctk.CTkLabel(self.opts_frame, text="Cookies from:").grid(
            row=3, column=0, padx=12, pady=(4, 8), sticky="w"
        )
        ctk.CTkOptionMenu(
            self.opts_frame, variable=self.cookie_var, values=COOKIE_BROWSERS,
            width=120,
            command=lambda v: config.save_config_key("cookie_browser", v),
        ).grid(row=3, column=1, padx=12, pady=(4, 8), sticky="w")
        ctk.CTkLabel(
            self.opts_frame,
            text="(only needed for age-restricted or members-only videos — leave at \"none\" otherwise)",
            text_color="gray", font=ctk.CTkFont(size=11),
        ).grid(row=3, column=2, columnspan=2, padx=(0, 12), pady=(4, 8), sticky="w")

    def _on_mode_change(self):
        m = self.mode_var.get()
        if m == "video":
            self._build_video_opts()
        elif m == "audio":
            self._build_audio_opts()
        elif m == "playlist":
            self._build_playlist_opts()
        elif m == "transcript":
            self._build_transcript_opts()
        config.save_config_key("mode", m)
        # Update download button label/icon to match mode
        if hasattr(self, "dl_btn"):
            if m == "transcript":
                self.dl_btn.configure(text="📝  Extract Transcripts")
            else:
                self.dl_btn.configure(text="⬇  Download")
        # Extras section is yt-dlp-only — hide in transcript mode so the user
        # can't waste time tuning rate-limit/cookies/etc. that never reach the
        # youtube-transcript-api path.
        if hasattr(self, "extras_frame"):
            if m == "transcript":
                self.extras_label.grid_remove()
                self.extras_frame.grid_remove()
            else:
                self.extras_label.grid(**self._extras_label_grid)
                self.extras_frame.grid(**self._extras_frame_grid)

    # ═══════════════════════════════════════════════════════════════════════════
    # Fetch Info
    # ═══════════════════════════════════════════════════════════════════════════

    def _fetch_info(self):
        url = self.url_entry.get().strip()
        if not url:
            self._set_status("Please enter a URL.", color="orange")
            return
        valid, err = validation.validate_url(url)
        if not valid:
            self._set_status(err, color="red")
            self._log_append(f"Blocked URL: {err}")
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
                is_pl = info.get("_type") == "playlist" or "entries" in info

                if is_pl:
                    entries = list(info.get("entries", []))
                    title = info.get("title", "Unknown Playlist")
                    display = f"📋 Playlist: {title}  ({len(entries)} items)"
                    self._fetched_formats = []
                else:
                    title = info.get("title", "Unknown")
                    dur = str(timedelta(seconds=info.get("duration", 0)))
                    ch = info.get("channel", info.get("uploader", "Unknown"))
                    chaps = info.get("chapters")
                    chap_str = f"  •  📑 {len(chaps)} chapters" if chaps else ""
                    display = f"🎬 {title}\n⏱ {dur}  •  📺 {ch}{chap_str}"
                    self._fetched_formats = info.get("formats", [])

                def _update():
                    self.info_label.configure(text=display, text_color=("white", "white"))
                    self._set_status("Info fetched.", color="green")
                    self.fetch_btn.configure(state="normal", text="Fetch Info")
                    # Auto-switch to playlist mode only if not already in
                    # transcript mode (transcripts handle playlists natively).
                    if is_pl and self.mode_var.get() != "transcript":
                        self.mode_var.set("playlist")
                        self._build_playlist_opts()
                    else:
                        self._on_mode_change()

                self.after(0, _update)
            except Exception as e:
                # Capture the message now: Python clears `e` when the except block
                # exits, but this lambda runs later on the Tk thread (would NameError).
                err = str(e)
                self.after(0, lambda: (
                    self._set_status(f"Fetch failed: {err}", color="red"),
                    self._log_append(err),
                    self.fetch_btn.configure(state="normal", text="Fetch Info")
                ))

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _format_label(f):
        fid = f.get("format_id", "?")
        ext = f.get("ext", "?")
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
            parts.append(f"~{size / 1048576:.0f}MB")
        return " ".join(parts)

    # ═══════════════════════════════════════════════════════════════════════════
    # Download
    # ═══════════════════════════════════════════════════════════════════════════

    def _browse_dir(self):
        path = _native_askdirectory(title="Select Download Directory")
        if path:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, path)
            config.save_config_key("save_dir", path)

    def _persist_dir_entry(self, _event=None):
        path = self.dir_entry.get().strip()
        if path and os.path.isdir(path):
            config.save_config_key("save_dir", path)

    def _open_output_dir(self):
        path = self.dir_entry.get().strip()
        if not path:
            self._set_status("No output directory selected.", color="orange")
            return
        if not os.path.isdir(path):
            self._set_status("Directory does not exist yet.", color="orange")
            return
        opener = shutil.which("xdg-open") or shutil.which("gio")
        if not opener:
            self._set_status("No file manager opener found (xdg-open/gio).", color="red")
            return
        try:
            args = [opener, "open", path] if opener.endswith("gio") else [opener, path]
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self._set_status(f"Could not open folder: {e}", color="red")

    def _resolve_format_string(self):
        mode = self.mode_var.get()
        if mode == "audio":
            return "bestaudio/best"
        selected = self.video_format_var.get()
        for label, fmt in VIDEO_PRESET_FORMATS:
            if selected == label:
                return fmt
        match = re.match(r"\[(\S+)\]", selected)
        if match:
            return match.group(1)
        return "bv*+ba/b"


    def _start_download(self):
        url = self.url_entry.get().strip()
        if not url:
            self._set_status("Please enter a URL.", color="orange")
            return
        valid, err = validation.validate_url(url)
        if not valid:
            self._set_status(err, color="red")
            self._log_append(f"Blocked URL: {err}")
            return

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

        self.dl_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress_bar.set(0)
        self._log_clear()

        # Transcript mode takes a different path entirely (no yt-dlp download).
        if self.mode_var.get() == "transcript":
            self._set_status("Starting transcript extraction…")
            self._active = self._extractor
            self._extractor.start(self._build_transcript_request(url, output_dir))
            return

        # Placed after _log_clear() so the warning survives, and after the
        # transcript branch, which needs no JS runtime.
        if deno_missing_for(url):
            self._log_append(
                "⚠ deno not found — YouTube needs it to solve the player "
                "challenge (yt-dlp EJS runtime).\n"
                "  This download will likely fail with an extractor error.\n"
                "  Install it, then restart:  bash install.sh --with-deno\n"
                "  (or your package manager / https://deno.land — see README)"
            )

        self._set_status("Starting download…")
        self._active = self._downloader
        self._downloader.start(self._build_download_request(url, output_dir))

    def _build_download_request(self, url, output_dir):
        return downloader.DownloadRequest(
            url=url,
            output_dir=output_dir,
            mode=self.mode_var.get(),
            video_format=self._resolve_format_string(),
            audio_codec=(self.audio_codec_var.get() if hasattr(self, "audio_codec_var") else "mp3"),
            audio_quality=(self.audio_quality_var.get() if hasattr(self, "audio_quality_var") else "192"),
            playlist_range=(self.playlist_range_entry.get().strip() if hasattr(self, "playlist_range_entry") else ""),
            subs=self.subs_var.get(),
            subs_lang=self.subs_lang_var.get(),
            subs_auto=self.subs_auto_var.get(),
            subs_embed=self.subs_embed_var.get(),
            thumb=self.thumb_var.get(),
            thumb_embed=self.thumb_embed_var.get(),
            meta=self.meta_var.get(),
            chapters_split=self.chapters_split_var.get(),
            sb=self.sb_var.get(),
            sb_action=self.sb_action_var.get(),
            sb_categories=[c for c, (v, _) in self.sb_cat_vars.items() if v.get()],
            rate_limit=self.rate_var.get(),
            cookie_browser=self.cookie_var.get(),
            archive=self.archive_var.get(),
        )

    def _build_transcript_request(self, url, output_dir):
        cookie = self.cookie_var.get()
        return transcript.TranscriptRequest(
            url=url,
            output_dir=output_dir,
            fmt=self.transcript_format_var.get(),
            include_timestamps=self.transcript_timestamps_var.get(),
            per_file=self.transcript_per_file_var.get(),
            lang=self.transcript_lang_var.get(),
            playlist_range=self.transcript_range_entry.get().strip(),
            cookie_browser=None if cookie == "-- none --" else cookie,
        )


    def _cancel_download(self):
        if self._active:
            self._active.cancel()
        self._set_status("Cancelling…", color="orange")

    def _download_finished(self):
        self.dl_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._active = None

    # ═══════════════════════════════════════════════════════════════════════════
    # Transcript Extraction
    # ═══════════════════════════════════════════════════════════════════════════


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

    def _copy_log(self):
        text = self.log_box.get("1.0", "end").strip()
        if not text:
            self._set_status("Log is empty.", color="orange")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._set_status("Log copied to clipboard.", color="green")


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = YtDlpGUI()
    app.mainloop()