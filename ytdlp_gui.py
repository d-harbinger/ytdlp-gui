#!/usr/bin/env python3
"""
yt-dlp GUI — A modern CustomTkinter frontend for yt-dlp.
Supports single video, audio-only extraction, playlist/batch downloads,
format selection, subtitles, SponsorBlock, metadata embedding, chapter
splitting, thumbnail extraction, rate limiting, and archive tracking.
"""

import json
import os
import sys
import re
import random
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog
from datetime import datetime, timedelta
from pathlib import Path
import urllib.parse

import customtkinter as ctk

try:
    import yt_dlp
except ImportError:
    print("ERROR: yt_dlp not found. Run: pip install yt-dlp")
    sys.exit(1)

try:
    from youtube_transcript_api import YouTubeTranscriptApi
except ImportError:
    print("ERROR: youtube-transcript-api not found. Run: pip install youtube-transcript-api")
    sys.exit(1)

# Rate-limit exception classes from youtube-transcript-api 1.x. Imported
# defensively: older/newer versions may shuffle the public surface, so we fall
# back to message-string matching in `_is_transcript_rate_limit_error` if any
# of these are missing.
_TRANSCRIPT_RATE_LIMIT_EXC: tuple = ()
for _name in ("IpBlocked", "RequestBlocked", "TooManyRequests", "YouTubeRequestFailed"):
    try:
        _TRANSCRIPT_RATE_LIMIT_EXC += (getattr(__import__("youtube_transcript_api", fromlist=[_name]), _name),)
    except (ImportError, AttributeError):
        pass


def _is_transcript_rate_limit_error(exc: BaseException) -> bool:
    if _TRANSCRIPT_RATE_LIMIT_EXC and isinstance(exc, _TRANSCRIPT_RATE_LIMIT_EXC):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in (
        "too many requests", "429", "ip blocked", "ip-blocked",
        "ipblocked", "request blocked", "requestblocked", "blocked by youtube",
        "youtube is blocking", "youtuberequestfailed",
    ))


# Transcript-loop pacing + retry tuning. Designed for "let it finish in one
# go even on a 121-video playlist" rather than "fastest possible".
TRANSCRIPT_BASE_DELAY = 1.5         # seconds between successive video fetches
TRANSCRIPT_BASE_JITTER = 0.6        # +random[0, jitter] on each delay
TRANSCRIPT_MAX_RETRIES = 4          # per-video retries on rate-limit errors
TRANSCRIPT_BACKOFF_BASE = 8.0       # first backoff (s); doubles each retry
TRANSCRIPT_BACKOFF_CAP = 240.0      # max backoff (s) per retry
TRANSCRIPT_THROTTLE_FLOOR = 5.0     # once throttled, raise base delay to ≥ this
# Circuit breaker: if N consecutive videos each fully exhaust their retries
# with rate-limit errors AND the yt-dlp fallback also fails, the network is
# hard-blocked at every endpoint we know how to reach. Abort fast.
# 1 = abort after the very first fully-failed video — that's already proof.
TRANSCRIPT_HARD_BLOCK_THRESHOLD = 1


@dataclass
class _TranscriptSnippet:
    """Minimal mirror of youtube_transcript_api's FetchedTranscriptSnippet.
    The transcript formatters iterate over snippets reading .start and .text;
    keeping the same attribute shape lets yt-dlp-sourced data flow through
    the existing format pipeline unchanged.
    """
    text: str
    start: float
    duration: float


def _parse_json3_captions(raw: str) -> list[_TranscriptSnippet]:
    """Parse YouTube's json3 caption format into our snippet shape.

    json3 events look like: {"tStartMs": 1234, "dDurationMs": 5678,
    "segs": [{"utf8": "Hello"}, {"utf8": " world"}]}. Some events have no
    `segs` (caption track formatting markers) — skip those.
    """
    data = json.loads(raw)
    snippets: list[_TranscriptSnippet] = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text:
            continue
        start_ms = ev.get("tStartMs", 0) or 0
        dur_ms = ev.get("dDurationMs", 0) or 0
        snippets.append(_TranscriptSnippet(
            text=text,
            start=start_ms / 1000.0,
            duration=dur_ms / 1000.0,
        ))
    return snippets


# yt-dlp player clients used for transcript extraction. Order matters: yt-dlp
# tries them left to right and uses the first one that returns playable info.
# Empirically validated 2026-05-01 against an IP that had been bot-checked by
# the default mix (web / web_safari): `web_embedded` (the iframe-embed client
# used for video embeds on third-party sites) routes through a less-policed
# endpoint and works without cookies or a YouTube account. `tv` and
# `android_vr` are kept as additional fallbacks for the rare cases where
# `web_embedded` itself is restricted.
TRANSCRIPT_YT_DLP_PLAYER_CLIENTS = ["tv", "android_vr", "web_embedded"]


def _fetch_transcript_via_yt_dlp(vid: str, lang: str,
                                  cookies_browser: str | None = None) -> list[_TranscriptSnippet]:
    """Fallback transcript fetcher that goes through yt-dlp instead of
    youtube-transcript-api. Uses a hand-picked player-client list that
    avoids the bot-checked `web` / `web_safari` clients — works without
    cookies or a YouTube account on most networks.

    `cookies_browser` (firefox/chrome/etc.) is optional: pass it when the
    user is logged into YouTube and wants extra cred for borderline cases.
    Won't help users without a YouTube account; the player-client tweak is
    what actually breaks through the bot-check.

    Raises RuntimeError on any failure (no captions found, yt-dlp blocked
    too, parse failure, etc.) — the caller decides whether to count it as
    rate-limit-shaped for circuit-breaker purposes.
    """
    url = f"https://www.youtube.com/watch?v={vid}"
    # "auto" means "any English-ish track we can find". Listing variants
    # explicitly is more reliable than yt-dlp's own auto-selection here.
    sub_langs = [lang] if lang != "auto" else ["en", "en-US", "en-GB", "en.*"]

    with tempfile.TemporaryDirectory(prefix="ytdlp-gui-trans-") as td:
        opts = {
            "skip_download": True,
            "writeautomaticsub": True,
            "writesubtitles": True,
            "subtitlesformat": "json3",
            "subtitleslangs": sub_langs,
            "outtmpl": os.path.join(td, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "ignoreerrors": False,
            "extractor_args": {
                "youtube": {"player_client": TRANSCRIPT_YT_DLP_PLAYER_CLIENTS},
            },
        }
        if cookies_browser:
            opts["cookiesfrombrowser"] = (cookies_browser,)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        # yt-dlp inserts the language code before the extension:
        # "{vid}.{lang}.json3" (e.g. "KiEptGbnEBc.en.json3").
        files = sorted(Path(td).glob(f"{vid}.*.json3"))
        if not files:
            raise RuntimeError("yt-dlp produced no captions file")
        raw = files[0].read_text(encoding="utf-8")

    snippets = _parse_json3_captions(raw)
    if not snippets:
        raise RuntimeError("yt-dlp captions were empty")
    return snippets


# ── Persistent Config ─────────────────────────────────────────────────────────
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "ytdlp-gui")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.conf")


def _read_config() -> dict:
    conf = {}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        conf[k.strip()] = v.strip()
        except OSError:
            pass
    return conf


def _write_config(conf: dict):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            for k, v in sorted(conf.items()):
                f.write(f"{k}={v}\n")
    except OSError:
        pass


def _save_config_key(key: str, value: str):
    conf = _read_config()
    conf[key] = value
    _write_config(conf)


SCALE_OPTIONS = ["Auto", "1.0", "1.25", "1.5", "1.75", "2.0", "2.25", "2.5"]


# ── UI Scale Resolution ───────────────────────────────────────────────────────
# Priority: env var > saved config > 1.0 default.
# "Auto" in saved config resolves to 1.0 — on Linux every DPI-probing heuristic
# (xrandr physical DPI, Xft.dpi, tkinter.winfo_fpixels) is unreliable across
# VMs, XWayland, remote desktops, and misconfigured DEs. A predictable 1.0
# default plus an in-UI Scale dropdown is the only thing that works everywhere.
def _detect_scale():
    env = os.environ.get("YTDLP_GUI_SCALE")
    if env:
        try:
            return float(env)
        except ValueError:
            pass

    saved = _read_config().get("scale", "Auto")
    if saved != "Auto":
        try:
            return float(saved)
        except ValueError:
            pass

    return 1.0


_dpi_scale = _detect_scale()
ctk.set_widget_scaling(_dpi_scale)
ctk.set_window_scaling(_dpi_scale)


# ── URL Validation ────────────────────────────────────────────────────────────
ALLOWED_SCHEMES = ("http", "https")
YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "youtu.be",
    "m.youtube.com", "music.youtube.com",
    "www.youtube-nocookie.com",
})
YOUTUBE_ONLY = os.environ.get("YTDLP_GUI_YOUTUBE_ONLY", "0") == "1"


def _validate_url(url: str) -> tuple[bool, str]:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "Could not parse URL."
    if parsed.scheme not in ALLOWED_SCHEMES:
        return False, f"Blocked scheme '{parsed.scheme}://'. Only http/https allowed."
    if not parsed.hostname:
        return False, "URL has no hostname."
    if YOUTUBE_ONLY:
        host = parsed.hostname.lower().lstrip(".")
        if host not in YOUTUBE_HOSTS:
            return False, (
                f"Host '{host}' not in allowed YouTube domains. "
                "Unset YTDLP_GUI_YOUTUBE_ONLY to allow all sites."
            )
    return True, ""


# ── Input Validation ──────────────────────────────────────────────────────────
_PLAYLIST_RANGE_RE = re.compile(r'^[\d,\-:\s]+$')


def _validate_playlist_range(rng: str) -> bool:
    return bool(_PLAYLIST_RANGE_RE.fullmatch(rng))


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

MAX_PLAYLIST_DOWNLOADS = 500

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
MAX_TRANSCRIPT_VIDEOS = 500


# ── Logger ────────────────────────────────────────────────────────────────────
class GUILogger:
    def __init__(self, cb):
        self.cb = cb

    def debug(self, msg):
        if not msg.startswith("[debug]"):
            self.cb(msg)

    def info(self, msg):
        self.cb(msg)

    def warning(self, msg):
        self.cb(f"⚠ {msg}")

    def error(self, msg):
        self.cb(f"✖ {msg}")


# ── Main Application ─────────────────────────────────────────────────────────
class YtDlpGUI(ctk.CTk):
    def __init__(self):
        super().__init__(className="ytdlp-gui")
        self.title(APP_NAME)
        self.minsize(WINDOW_MIN_W, WINDOW_MIN_H)
        self.geometry(f"{WINDOW_MIN_W}x{WINDOW_MIN_H}")

        self._download_thread = None
        self._cancel_flag = threading.Event()
        self._fetched_formats = []
        self._video_info = None

        self._build_ui()
        self._bind_x11_scroll()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        icon_path = os.path.join(os.path.dirname(__file__), "assets", "icon.png")
        if os.path.exists(icon_path):
            try:
                self.iconphoto(True, tk.PhotoImage(file=icon_path))
            except Exception:
                pass

    def _on_close(self):
        self._cancel_flag.set()
        if self._download_thread and self._download_thread.is_alive():
            self._download_thread.join(timeout=3)
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
        conf = _read_config()

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
        conf = _read_config()
        current_scale = conf.get("scale", "Auto")
        self._scale_var = ctk.StringVar(value=current_scale)
        self._scale_menu = ctk.CTkOptionMenu(
            scale_f, variable=self._scale_var, values=SCALE_OPTIONS,
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
            command=lambda v: _save_config_key("rate_limit", v),
        ).grid(row=rr, column=1, sticky="w", padx=(8, 0), pady=(0, 4))
        rr += 1

        ctk.CTkLabel(right, text="Cookies from:").grid(row=rr, column=0, sticky="w", pady=(4, 4))
        # cookie_var is created earlier in _setup_ui — bind to the existing
        # StringVar so the transcript-mode dropdown stays in sync.
        ctk.CTkOptionMenu(
            right, variable=self.cookie_var, values=COOKIE_BROWSERS, width=120,
            command=lambda v: _save_config_key("cookie_browser", v),
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
        conf = _read_config()
        conf["scale"] = choice
        _write_config(conf)
        # Apply live — CTk's ScalingTracker propagates to all existing widgets
        if choice == "Auto":
            new_scale = _detect_scale()
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
        conf = _read_config()
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
        _save_config_key("audio_codec", codec)

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
        conf = _read_config()

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
                command=lambda v=val: _save_config_key("transcript_format", v),
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
            command=lambda v: _save_config_key("transcript_lang", v),
        ).grid(row=1, column=1, padx=12, pady=(4, 4), sticky="w")

        self.transcript_timestamps_var = ctk.BooleanVar(value=conf.get("transcript_ts", "0") == "1")
        ctk.CTkCheckBox(
            self.opts_frame, text="Timestamps", variable=self.transcript_timestamps_var,
            command=lambda: _save_config_key(
                "transcript_ts", "1" if self.transcript_timestamps_var.get() else "0"
            ),
        ).grid(row=1, column=2, padx=12, pady=(4, 4), sticky="w")

        self.transcript_per_file_var = ctk.BooleanVar(value=conf.get("transcript_per_file", "0") == "1")
        ctk.CTkCheckBox(
            self.opts_frame, text="One file per video", variable=self.transcript_per_file_var,
            command=lambda: _save_config_key(
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
            command=lambda v: _save_config_key("cookie_browser", v),
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
        _save_config_key("mode", m)
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
        valid, err = _validate_url(url)
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
                self.after(0, lambda: (
                    self._set_status(f"Fetch failed: {e}", color="red"),
                    self._log_append(str(e)),
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
            _save_config_key("save_dir", path)

    def _persist_dir_entry(self, _event=None):
        path = self.dir_entry.get().strip()
        if path and os.path.isdir(path):
            _save_config_key("save_dir", path)

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

    def _build_ydl_opts(self, output_dir):
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
            "restrictfilenames": True,
            "max_downloads": MAX_PLAYLIST_DOWNLOADS,
            "postprocessors": [],
        }

        if mode == "audio":
            opts["format"] = "bestaudio/best"
            codec = self.audio_codec_var.get()
            pp = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
            if codec not in ("flac", "wav"):
                pp["preferredquality"] = self.audio_quality_var.get()
            opts["postprocessors"].append(pp)
            del opts["merge_output_format"]

        if mode == "playlist":
            opts["outtmpl"]["default"] = "%(playlist_title)s/%(playlist_index)03d - %(title)s [%(id)s].%(ext)s"
            opts["noplaylist"] = False
            if hasattr(self, "playlist_range_entry"):
                rng = self.playlist_range_entry.get().strip()
                if rng:
                    if _validate_playlist_range(rng):
                        opts["playlist_items"] = rng
                    else:
                        self.after(0, lambda: self._log_append(
                            "⚠ Invalid playlist range. Downloading all items."
                        ))

        if self.subs_var.get():
            lang = self.subs_lang_var.get()
            opts["writesubtitles"] = True
            opts["subtitleslangs"] = [lang] if lang != "all" else ["all"]
            if self.subs_auto_var.get():
                opts["writeautomaticsub"] = True
            if self.subs_embed_var.get():
                opts["postprocessors"].append({"key": "FFmpegEmbedSubtitle"})

        if self.thumb_var.get():
            opts["writethumbnail"] = True
        if self.thumb_embed_var.get():
            opts["postprocessors"].append({"key": "EmbedThumbnail"})

        if self.meta_var.get():
            opts["postprocessors"].append({"key": "FFmpegMetadata"})

        if self.chapters_split_var.get():
            opts["postprocessors"].append({
                "key": "FFmpegSplitChapters",
                "force_keyframes": False,
            })

        if self.sb_var.get():
            cats = [c for c, (v, _) in self.sb_cat_vars.items() if v.get()]
            if cats:
                action = self.sb_action_var.get()
                if action == "remove":
                    opts["postprocessors"].append({
                        "key": "SponsorBlock",
                        "categories": cats,
                    })
                    opts["postprocessors"].append({
                        "key": "ModifyChapters",
                        "remove_sponsor_segments": cats,
                    })
                else:
                    opts["postprocessors"].append({
                        "key": "SponsorBlock",
                        "categories": cats,
                    })

        rl = self.rate_var.get()
        if rl != "No limit":
            opts["ratelimit"] = self._parse_rate(rl)

        browser = self.cookie_var.get()
        if browser != "-- none --":
            opts["cookiesfrombrowser"] = (browser,)

        if self.archive_var.get():
            opts["download_archive"] = os.path.join(output_dir, ".ytdlp_archive.txt")

        return opts

    @staticmethod
    def _parse_rate(val: str) -> int | None:
        m = re.match(r'^(\d+)([KMG]?)$', val.strip(), re.IGNORECASE)
        if not m:
            return None
        n = int(m.group(1))
        unit = m.group(2).upper()
        mult = {"": 1, "K": 1024, "M": 1048576, "G": 1073741824}
        return n * mult.get(unit, 1)

    def _start_download(self):
        url = self.url_entry.get().strip()
        if not url:
            self._set_status("Please enter a URL.", color="orange")
            return
        valid, err = _validate_url(url)
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

        self._cancel_flag.clear()
        self.dl_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress_bar.set(0)
        self._log_clear()

        # Transcript mode takes a different path entirely (no yt-dlp download).
        if self.mode_var.get() == "transcript":
            self._set_status("Starting transcript extraction…")
            self._start_transcript_extraction(url, output_dir)
            return

        self._set_status("Starting download…")
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
            except yt_dlp.utils.MaxDownloadsReached:
                self.after(0, lambda: self._set_status(
                    f"✔ Reached limit ({MAX_PLAYLIST_DOWNLOADS}). Done.", color="green"))
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
        if self._cancel_flag.is_set():
            raise yt_dlp.utils.DownloadError("Cancelled by user")
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            dl = d.get("downloaded_bytes", 0)
            speed = d.get("speed")
            eta = d.get("eta")
            if total > 0:
                self.after(0, lambda p=dl / total: self.progress_bar.set(p))
            parts = []
            if total > 0:
                parts.append(f"{dl / 1048576:.1f}/{total / 1048576:.1f} MB")
            if speed:
                parts.append(f"{speed / 1048576:.1f} MB/s")
            if eta:
                parts.append(f"ETA {timedelta(seconds=eta)}")
            msg = "Downloading: " + "  •  ".join(parts) if parts else "Downloading…"
            self.after(0, lambda m=msg: self._set_status(m))
        elif status == "finished":
            fn = os.path.basename(d.get("filename", ""))
            self.after(0, lambda: self._log_append(f"✔ Finished: {fn}"))

    def _cancel_download(self):
        self._cancel_flag.set()
        self._set_status("Cancelling…", color="orange")

    def _download_finished(self):
        self.dl_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._download_thread = None

    # ═══════════════════════════════════════════════════════════════════════════
    # Transcript Extraction
    # ═══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _fmt_ts(seconds: float) -> str:
        total = int(seconds)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    @staticmethod
    def _safe_filename(name: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip().strip(".")
        cleaned = re.sub(r'\s+', " ", cleaned)
        return cleaned

    @staticmethod
    def _yaml_str(text: str) -> str:
        """Escape a string for use inside a YAML double-quoted scalar."""
        return (
            text.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\r", " ")
                .replace("\n", " ")
        )

    def _build_body_flat(self, data, include_ts: bool) -> str:
        if include_ts:
            return "\n".join(f"[{self._fmt_ts(s.start)}] {s.text}" for s in data)
        return " ".join(s.text for s in data)

    def _build_body_paragraphs(self, data, include_ts: bool) -> str:
        paragraphs, current = [], []
        para_start = 0.0
        last_flush_ts = 0.0
        for s in data:
            if not current:
                para_start = s.start
            current.append(s.text)
            if len(current) >= 5 or (s.start - last_flush_ts) >= 30:
                txt = " ".join(current)
                if include_ts:
                    txt = f"**[{self._fmt_ts(para_start)}]** {txt}"
                paragraphs.append(txt)
                current, last_flush_ts = [], s.start
        if current:
            txt = " ".join(current)
            if include_ts:
                txt = f"**[{self._fmt_ts(para_start)}]** {txt}"
            paragraphs.append(txt)
        return "\n\n".join(paragraphs)

    def _format_single_transcript(self, fmt: str, include_ts: bool,
                                   video_id: str, video_title: str, data) -> str:
        if fmt == "plain":
            return self._build_body_flat(data, include_ts)

        body = self._build_body_paragraphs(data, include_ts)
        url = f"https://youtube.com/watch?v={video_id}"

        if fmt == "markdown":
            header = (
                f"# {video_title}\n\n"
                f"**Video ID:** {video_id}\n"
                f"**Source:** {url}\n"
                f"**Extracted:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                "---\n\n"
            )
            return header + body

        # obsidian
        front = (
            "---\n"
            f'title: "{self._yaml_str(video_title)}"\n'
            f"source: {url}\n"
            "type: video-transcript\n"
            f"created: {datetime.now().strftime('%Y-%m-%d')}\n"
            "tags:\n  - youtube\n  - transcript\n"
            "---\n\n"
            f"# {video_title}\n\n"
            f"🔗 [Watch on YouTube]({url})\n\n"
            "## Transcript\n\n"
        )
        return front + body

    def _format_playlist_transcripts(self, fmt: str, include_ts: bool,
                                      playlist_title: str, videos: list[dict]) -> str:
        ok_count = sum(1 for v in videos if v["data"] is not None)
        total = len(videos)
        lines: list[str] = []

        if fmt == "markdown":
            lines.append(f"# {playlist_title}\n\n")
            lines.append(f"**Extracted:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            lines.append(f"**Transcripts available:** {ok_count} / {total}\n\n---\n")
        elif fmt == "obsidian":
            lines.append("---\n")
            lines.append(f'title: "{self._yaml_str(playlist_title)}"\n')
            lines.append("type: playlist-transcript\n")
            lines.append(f"created: {datetime.now().strftime('%Y-%m-%d')}\n")
            lines.append(f"videos: {ok_count}\n")
            lines.append("tags:\n  - youtube\n  - transcript\n  - playlist\n")
            lines.append("---\n\n")
            lines.append(f"# {playlist_title}\n\n")
            lines.append(f"**Transcripts available:** {ok_count} / {total}\n")
        else:
            bar = "=" * max(10, len(playlist_title))
            lines.append(f"{playlist_title}\n{bar}\n")
            lines.append(f"Transcripts available: {ok_count} / {total}\n")

        for i, v in enumerate(videos, 1):
            url = f"https://youtube.com/watch?v={v['id']}"
            if fmt == "markdown":
                lines.append(f"\n## {i}. {v['title']}\n\n🔗 {url}\n\n")
            elif fmt == "obsidian":
                lines.append(f"\n## {i}. {v['title']}\n\n🔗 [Watch]({url})\n\n")
            else:
                lines.append(f"\n--- Video {i}: {v['title']} ---\n\n")

            if v["data"] is None:
                note = f"[No transcript available: {v['error']}]"
                lines.append((note if fmt == "plain" else f"*{note}*") + "\n")
                if fmt in ("markdown", "obsidian"):
                    lines.append("\n---\n")
                continue

            if fmt == "plain":
                lines.append(self._build_body_flat(v["data"], include_ts) + "\n")
            else:
                lines.append(self._build_body_paragraphs(v["data"], include_ts) + "\n\n---\n")

        return "".join(lines)

    def _resolve_videos(self, url: str) -> tuple[str, list[tuple[str, str]]]:
        """Use yt_dlp (already a dep) to resolve URL → (collection_title, [(id, title), ...]).

        For a single video URL, returns (video_title, [(id, title)]).
        For a playlist, returns (playlist_title, [(id, title), ...]) for all entries.
        """
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info is None:
            raise RuntimeError("Could not resolve URL.")

        if info.get("_type") == "playlist" or "entries" in info:
            title = (info.get("title") or "YouTube Playlist").strip()
            entries = []
            for e in info.get("entries") or []:
                if not e:
                    continue
                vid = e.get("id")
                vtitle = (e.get("title") or "").strip() or f"Video {vid}"
                if vid:
                    entries.append((vid, vtitle))
            return title, entries

        vid = info.get("id")
        vtitle = (info.get("title") or "").strip() or f"Video {vid}"
        return vtitle, [(vid, vtitle)]

    def _start_transcript_extraction(self, url: str, output_dir: str):
        """Run transcript extraction in a background thread with progress + cancel."""
        fmt = self.transcript_format_var.get()
        include_ts = self.transcript_timestamps_var.get()
        per_file = self.transcript_per_file_var.get()
        lang = self.transcript_lang_var.get()
        rng = self.transcript_range_entry.get().strip()

        if rng and not _validate_playlist_range(rng):
            self._set_status("Invalid playlist range. Use formats like 1-10 or 1,3,5.", color="red")
            self._download_finished()
            return

        ext = ".md" if fmt in ("markdown", "obsidian") else ".txt"

        def _worker():
            try:
                self.after(0, lambda: self._set_status("Resolving URL…"))
                self.after(0, lambda: self._log_append(f"→ Resolving: {url}"))

                collection_title, all_entries = self._resolve_videos(url)
                if not all_entries:
                    raise RuntimeError("No videos found for this URL.")

                # Apply playlist range filter (if any) for multi-video URLs.
                entries = self._apply_range(all_entries, rng) if (len(all_entries) > 1 and rng) else all_entries

                if len(entries) > MAX_TRANSCRIPT_VIDEOS:
                    self.after(0, lambda: self._log_append(
                        f"⚠ Capping at {MAX_TRANSCRIPT_VIDEOS} videos (was {len(entries)})."
                    ))
                    entries = entries[:MAX_TRANSCRIPT_VIDEOS]

                is_collection = len(entries) > 1
                total = len(entries)
                self.after(0, lambda: self._log_append(
                    f"✓ {total} video(s) to process.  Format: {fmt}.  Lang: {lang}.  Per-file: {per_file}"
                ))

                # Resolve cookies-from-browser once for the whole run.
                cookie_browser = self.cookie_var.get()
                if cookie_browser == "-- none --":
                    cookie_browser = None

                ytt = YouTubeTranscriptApi()
                videos: list[dict] = []
                # Adaptive base pacing: starts low, ratchets up the first time
                # YouTube rate-limits us so the rest of the run stays under the
                # threshold rather than re-tripping it every video.
                adaptive_delay = TRANSCRIPT_BASE_DELAY
                # Circuit-breaker counter: incremented when both the API AND
                # the yt-dlp fallback fail for a video with rate-limit-shaped
                # errors. Reset on any success or non-rate-limit failure.
                consecutive_rl_failures = 0
                hard_blocked = False
                # Once the yt-dlp fallback proves successful, skip the API for
                # all remaining videos — the API is clearly blocked on this
                # network and grinding through retries wastes ~2 min per video.
                yt_dlp_sticky = False

                def _interruptible_sleep(secs: float) -> bool:
                    """Sleep up to `secs`, returning True if cancelled mid-wait."""
                    end = time.monotonic() + secs
                    while time.monotonic() < end:
                        if self._cancel_flag.is_set():
                            return True
                        time.sleep(min(0.5, end - time.monotonic()))
                    return False

                for i, (vid, vtitle) in enumerate(entries, 1):
                    if self._cancel_flag.is_set():
                        self.after(0, lambda: self._set_status("Cancelled.", color="orange"))
                        return

                    short = (vtitle[:60] + "…") if len(vtitle) > 61 else vtitle

                    # Pace requests (skip before the first one). Small jitter
                    # avoids robotic timing patterns YouTube can flag on.
                    if i > 1:
                        if _interruptible_sleep(adaptive_delay + random.uniform(0, TRANSCRIPT_BASE_JITTER)):
                            self.after(0, lambda: self._set_status("Cancelled.", color="orange"))
                            return

                    self.after(0, lambda i=i, t=short: self._set_status(
                        f"[{i}/{total}] {t}"
                    ))
                    self.after(0, lambda p=i / total: self.progress_bar.set(p))

                    entry = {"id": vid, "title": vtitle, "data": None, "error": None}
                    api_exhausted_with_rl = False

                    if not yt_dlp_sticky:
                        # ── Primary path: youtube-transcript-api with retries ──
                        for attempt in range(TRANSCRIPT_MAX_RETRIES + 1):
                            if self._cancel_flag.is_set():
                                break
                            try:
                                if lang != "auto":
                                    entry["data"] = ytt.fetch(vid, languages=[lang])
                                else:
                                    # "auto" = any available transcript. The 1.x API's
                                    # fetch() default is languages=('en',), so we must
                                    # list first and pick any entry.
                                    tl = ytt.list(vid)
                                    picked = next(iter(tl), None)
                                    if picked is None:
                                        raise RuntimeError("no transcripts listed")
                                    entry["data"] = picked.fetch()
                                entry["error"] = None
                                self.after(0, lambda t=short: self._log_append(f"✔ [{t}]"))
                                consecutive_rl_failures = 0
                                break
                            except Exception as ex:
                                is_rl = _is_transcript_rate_limit_error(ex)
                                if is_rl and attempt < TRANSCRIPT_MAX_RETRIES:
                                    # Bump base delay so the rest of the run is gentler.
                                    adaptive_delay = max(adaptive_delay, TRANSCRIPT_THROTTLE_FLOOR)
                                    wait = min(
                                        TRANSCRIPT_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 3.0),
                                        TRANSCRIPT_BACKOFF_CAP,
                                    )
                                    self.after(0, lambda t=short, w=wait, a=attempt + 1: self._log_append(
                                        f"⏳ [{t}] rate-limited; waiting {w:.0f}s before retry {a}/{TRANSCRIPT_MAX_RETRIES}…"
                                    ))
                                    self.after(0, lambda i=i, t=short, w=wait: self._set_status(
                                        f"[{i}/{total}] rate-limited, waiting {w:.0f}s…", color="orange"
                                    ))
                                    if _interruptible_sleep(wait):
                                        self.after(0, lambda: self._set_status("Cancelled.", color="orange"))
                                        return
                                    continue

                                entry["error"] = (str(ex).splitlines()[0] or "no transcript")[:200]
                                if is_rl:
                                    api_exhausted_with_rl = True
                                else:
                                    # Non-rate-limit failure (genuinely missing
                                    # transcript, etc.) — proves the IP isn't
                                    # hard-blocked, so reset the breaker and
                                    # don't try yt-dlp (yt-dlp can't conjure
                                    # captions that don't exist).
                                    consecutive_rl_failures = 0
                                    self.after(0, lambda t=short, e=entry["error"]: self._log_append(
                                        f"✖ [{t}] {e}"
                                    ))
                                break

                    # ── Fallback path: yt-dlp ──
                    # Triggered when sticky mode is on (API previously proven
                    # blocked this run) OR when the API just exhausted retries
                    # with rate-limit errors for THIS video.
                    if entry["data"] is None and (yt_dlp_sticky or api_exhausted_with_rl) and not self._cancel_flag.is_set():
                        if api_exhausted_with_rl and not yt_dlp_sticky:
                            self.after(0, lambda t=short: self._log_append(
                                f"↪ [{t}] API blocked, trying yt-dlp fallback…"
                            ))
                            self.after(0, lambda i=i, t=short: self._set_status(
                                f"[{i}/{total}] yt-dlp fallback…", color="orange"
                            ))
                        try:
                            entry["data"] = _fetch_transcript_via_yt_dlp(vid, lang, cookie_browser)
                            entry["error"] = None
                            consecutive_rl_failures = 0
                            if not yt_dlp_sticky:
                                yt_dlp_sticky = True
                                self.after(0, lambda t=short: self._log_append(
                                    f"✔ [{t}] (via yt-dlp) — switching all "
                                    "remaining videos to yt-dlp."
                                ))
                            else:
                                self.after(0, lambda t=short: self._log_append(f"✔ [{t}] (yt-dlp)"))
                        except Exception as ex:
                            yt_err = (str(ex).splitlines()[0] or "yt-dlp failed")[:200]
                            yt_is_rl = _is_transcript_rate_limit_error(ex)
                            if api_exhausted_with_rl:
                                # Both paths failed, both rate-limit-shaped =
                                # network truly blocked at every endpoint.
                                entry["error"] = f"API blocked + yt-dlp failed: {yt_err}"
                                consecutive_rl_failures += 1
                                self.after(0, lambda t=short, e=entry["error"]: self._log_append(
                                    f"✖ [{t}] {e}"
                                ))
                            else:
                                # Sticky mode (API already known blocked); only
                                # yt-dlp failed. Treat rate-limit-shaped failures
                                # as breaker fuel; treat real "no captions" as
                                # a per-video miss and reset the counter.
                                entry["error"] = f"yt-dlp failed: {yt_err}"
                                if yt_is_rl:
                                    consecutive_rl_failures += 1
                                else:
                                    consecutive_rl_failures = 0
                                self.after(0, lambda t=short, e=entry["error"]: self._log_append(
                                    f"✖ [{t}] {e}"
                                ))

                    videos.append(entry)

                    # Circuit breaker: bail out the moment we have proof the
                    # network is blocked at every endpoint. Continuing would
                    # burn ~2 min per video for nothing.
                    if consecutive_rl_failures >= TRANSCRIPT_HARD_BLOCK_THRESHOLD:
                        hard_blocked = True
                        remaining = total - i
                        # Differentiate: "Sign in to confirm" means YouTube
                        # accepted the request but demanded auth — fixable
                        # with cookies-from-browser. Pure 429s/IpBlocked mean
                        # the IP itself is rejected — only an IP change helps.
                        last_err = (entry.get("error") or "").lower()
                        is_bot_check = (
                            "sign in to confirm" in last_err
                            or "not a bot" in last_err
                            or "use --cookies" in last_err
                        )
                        self.after(0, lambda: self._log_append("━" * 60))
                        if is_bot_check:
                            # The player-client tweak (`web_embedded` etc.) usually
                            # bypasses this without auth. Reaching here means even
                            # those clients got bot-checked — escalation territory.
                            self.after(0, lambda r=remaining: self._log_append(
                                f"🛑 YouTube bot-check refused every player client. "
                                f"Aborting with {r} video(s) unprocessed."
                            ))
                            self.after(0, lambda: self._log_append(
                                "   Try: (1) switch your VPN exit node, "
                                "(2) wait several hours for the IP reputation to "
                                "decay, or (3) if you have a YouTube account, set "
                                "'Cookies from:' to your logged-in browser."
                            ))
                        else:
                            self.after(0, lambda r=remaining: self._log_append(
                                f"🛑 Network appears hard-blocked by YouTube. "
                                f"Aborting with {r} video(s) unprocessed."
                            ))
                            self.after(0, lambda: self._log_append(
                                "   Next steps: switch your VPN exit node or "
                                "wait several hours."
                            ))
                        break

                # Write to disk
                target = Path(output_dir)
                target.mkdir(parents=True, exist_ok=True)
                written = self._write_transcripts(
                    videos, collection_title, target, fmt, ext, include_ts, per_file, is_collection
                )

                ok = sum(1 for v in videos if v["data"] is not None)
                self.after(0, lambda: self.progress_bar.set(1.0))
                if hard_blocked:
                    self.after(0, lambda: self._set_status(
                        f"🛑 Aborted at {len(videos)}/{total} — see log "
                        f"for fix. Wrote {written} file(s).", color="red"
                    ))
                else:
                    self.after(0, lambda: self._set_status(
                        f"✔ {ok}/{total} extracted  ·  wrote {written} file(s)", color="green"
                    ))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda: self._set_status(f"Transcript error: {msg}", color="red"))
                self.after(0, lambda: self._log_append(f"✖ {msg}"))
            finally:
                self.after(0, self._download_finished)

        self._download_thread = threading.Thread(target=_worker, daemon=True)
        self._download_thread.start()

    @staticmethod
    def _apply_range(entries: list[tuple[str, str]], rng: str) -> list[tuple[str, str]]:
        """Apply yt-dlp-style range spec ('1-10', '1,3,5', '5-') to entries.

        Silently skips unparseable parts (including slice syntax like '1:5'
        which the shared validator permits for yt-dlp playlist mode but we
        don't support when driving youtube-transcript-api ourselves).
        """
        keep: set[int] = set()
        n = len(entries)
        for part in rng.split(","):
            part = part.strip()
            if not part or ":" in part:
                continue
            try:
                if "-" in part:
                    a, b = part.split("-", 1)
                    a_i = int(a) if a.strip() else 1
                    b_i = int(b) if b.strip() else n
                    for i in range(a_i, b_i + 1):
                        if 1 <= i <= n:
                            keep.add(i)
                else:
                    i = int(part)
                    if 1 <= i <= n:
                        keep.add(i)
            except ValueError:
                continue
        return [entries[i - 1] for i in sorted(keep)]

    def _write_transcripts(self, videos: list[dict], collection_title: str,
                            target: Path, fmt: str, ext: str, include_ts: bool,
                            per_file: bool, is_collection: bool) -> int:
        """Write transcripts to disk. Returns count of files written."""
        if not is_collection:
            v = videos[0]
            if v["data"] is None:
                raise RuntimeError(f"No transcript available: {v['error']}")
            content = self._format_single_transcript(fmt, include_ts, v["id"], v["title"], v["data"])
            safe = self._safe_filename(v["title"])[:80] or v["id"]
            out_path = target / f"{safe} [{v['id']}]{ext}"
            out_path.write_text(content, encoding="utf-8")
            self.after(0, lambda p=out_path: self._log_append(f"📄 Wrote: {p.name}"))
            return 1

        # Collection (playlist or multi-video resolution)
        if per_file:
            subdir_name = self._safe_filename(collection_title)[:80] or "playlist"
            subdir = target / subdir_name
            subdir.mkdir(parents=True, exist_ok=True)
            written = 0
            pad = len(str(len(videos)))
            for i, v in enumerate(videos, 1):
                if v["data"] is None:
                    continue
                content = self._format_single_transcript(fmt, include_ts, v["id"], v["title"], v["data"])
                safe = self._safe_filename(v["title"])[:80] or v["id"]
                out_path = subdir / f"{str(i).zfill(pad)} - {safe} [{v['id']}]{ext}"
                out_path.write_text(content, encoding="utf-8")
                written += 1
            self.after(0, lambda d=subdir, w=written: self._log_append(f"📁 Wrote {w} file(s) to: {d.name}/"))
            return written

        # Concatenated single file
        content = self._format_playlist_transcripts(fmt, include_ts, collection_title, videos)
        safe = self._safe_filename(collection_title)[:80] or "playlist"
        out_path = target / f"{safe}_transcripts{ext}"
        out_path.write_text(content, encoding="utf-8")
        self.after(0, lambda p=out_path: self._log_append(f"📄 Wrote: {p.name}"))
        return 1

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