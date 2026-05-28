"""yt-dlp download orchestration: options building + a threaded, cancellable
Downloader controller.

Tkinter-free by design. The controller talks to the UI only through the four
callbacks passed at construction; the UI shell is responsible for marshaling
those callbacks onto the Tk main thread.
"""
import os
import re
import threading
from datetime import timedelta
from dataclasses import dataclass, field

import yt_dlp

import validation

MAX_PLAYLIST_DOWNLOADS = 500


class GUILogger:
    """Adapter that funnels yt-dlp's internal logger into a single callback."""

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


def parse_rate(val: str) -> int | None:
    m = re.match(r'^(\d+)([KMG]?)$', val.strip(), re.IGNORECASE)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).upper()
    mult = {"": 1, "K": 1024, "M": 1048576, "G": 1073741824}
    return n * mult.get(unit, 1)


@dataclass(frozen=True)
class DownloadRequest:
    url: str
    output_dir: str
    mode: str                 # "video" | "audio" | "playlist"
    video_format: str         # already-resolved yt-dlp format expression
    audio_codec: str
    audio_quality: str
    playlist_range: str
    subs: bool
    subs_lang: str
    subs_auto: bool
    subs_embed: bool
    thumb: bool
    thumb_embed: bool
    meta: bool
    chapters_split: bool
    sb: bool
    sb_action: str            # "skip" | "remove"
    sb_categories: list = field(default_factory=list)
    rate_limit: str = "No limit"
    cookie_browser: str = "-- none --"
    archive: bool = False


def build_ydl_opts(req: DownloadRequest, *, on_log) -> dict:
    """Pure construction of the yt-dlp options dict from a request.

    Does NOT set progress_hooks / logger — the controller adds those at runtime,
    keeping this function free of threading concerns and unit-testable.
    """
    opts = {
        "format": req.video_format,
        "paths": {"home": req.output_dir},
        "outtmpl": {"default": "%(title)s [%(id)s].%(ext)s"},
        "noplaylist": req.mode != "playlist",
        "quiet": True,
        "no_warnings": False,
        "merge_output_format": "mp4",
        "restrictfilenames": True,
        "max_downloads": MAX_PLAYLIST_DOWNLOADS,
        "postprocessors": [],
    }

    if req.mode == "audio":
        opts["format"] = "bestaudio/best"
        codec = req.audio_codec
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
        if codec not in ("flac", "wav"):
            pp["preferredquality"] = req.audio_quality
        opts["postprocessors"].append(pp)
        del opts["merge_output_format"]

    if req.mode == "playlist":
        opts["outtmpl"]["default"] = "%(playlist_title)s/%(playlist_index)03d - %(title)s [%(id)s].%(ext)s"
        opts["noplaylist"] = False
        rng = req.playlist_range.strip()
        if rng:
            if validation.validate_playlist_range(rng):
                opts["playlist_items"] = rng
            else:
                on_log("⚠ Invalid playlist range. Downloading all items.")

    if req.subs:
        lang = req.subs_lang
        opts["writesubtitles"] = True
        opts["subtitleslangs"] = [lang] if lang != "all" else ["all"]
        if req.subs_auto:
            opts["writeautomaticsub"] = True
        if req.subs_embed:
            opts["postprocessors"].append({"key": "FFmpegEmbedSubtitle"})

    if req.thumb:
        opts["writethumbnail"] = True
    if req.thumb_embed:
        opts["postprocessors"].append({"key": "EmbedThumbnail"})

    if req.meta:
        opts["postprocessors"].append({"key": "FFmpegMetadata"})

    if req.chapters_split:
        opts["postprocessors"].append({
            "key": "FFmpegSplitChapters",
            "force_keyframes": False,
        })

    if req.sb:
        cats = list(req.sb_categories)
        if cats:
            if req.sb_action == "remove":
                opts["postprocessors"].append({"key": "SponsorBlock", "categories": cats})
                opts["postprocessors"].append({
                    "key": "ModifyChapters",
                    "remove_sponsor_segments": cats,
                })
            else:
                opts["postprocessors"].append({"key": "SponsorBlock", "categories": cats})

    if req.rate_limit != "No limit":
        opts["ratelimit"] = parse_rate(req.rate_limit)

    if req.cookie_browser != "-- none --":
        opts["cookiesfrombrowser"] = (req.cookie_browser,)

    if req.archive:
        opts["download_archive"] = os.path.join(req.output_dir, ".ytdlp_archive.txt")

    return opts


class Downloader:
    """Runs a yt-dlp download on a daemon thread; cancellable; reports via callbacks.

    Callbacks (called from the worker thread — the shell marshals to Tk):
      on_status(text, color="gray") · on_log(text) · on_progress(fraction) · on_finished()
    """

    def __init__(self, *, on_status, on_log, on_progress, on_finished):
        self._on_status = on_status
        self._on_log = on_log
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._cancel_flag = threading.Event()
        self._thread = None

    def start(self, req: DownloadRequest) -> None:
        self._cancel_flag.clear()
        opts = build_ydl_opts(req, on_log=self._on_log)
        opts["progress_hooks"] = [self._progress_hook]
        opts["logger"] = GUILogger(self._on_log)

        def _worker():
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([req.url])
                if self._cancel_flag.is_set():
                    self._on_status("Download cancelled.", "orange")
                else:
                    self._on_status("✔ Download complete!", "green")
                    self._on_progress(1.0)
            except yt_dlp.utils.MaxDownloadsReached:
                self._on_status(f"✔ Reached limit ({MAX_PLAYLIST_DOWNLOADS}). Done.", "green")
                self._on_progress(1.0)
            except yt_dlp.utils.DownloadError as e:
                if self._cancel_flag.is_set():
                    self._on_status("Download cancelled.", "orange")
                else:
                    self._on_status(f"Download error: {e}", "red")
                    self._on_log(str(e))
            except Exception as e:
                self._on_status(f"Unexpected error: {e}", "red")
                self._on_log(str(e))
            finally:
                self._on_finished()

        self._thread = threading.Thread(target=_worker, daemon=True)
        self._thread.start()

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
                self._on_progress(dl / total)
            parts = []
            if total > 0:
                parts.append(f"{dl / 1048576:.1f}/{total / 1048576:.1f} MB")
            if speed:
                parts.append(f"{speed / 1048576:.1f} MB/s")
            if eta:
                parts.append(f"ETA {timedelta(seconds=eta)}")
            msg = "Downloading: " + "  •  ".join(parts) if parts else "Downloading…"
            self._on_status(msg)
        elif status == "finished":
            fn = os.path.basename(d.get("filename", ""))
            self._on_log(f"✔ Finished: {fn}")

    def cancel(self) -> None:
        self._cancel_flag.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout=None) -> None:
        if self._thread:
            self._thread.join(timeout)
