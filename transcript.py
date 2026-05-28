"""Transcript extraction: caption parsing, the yt-dlp fallback fetcher,
output formatters, video resolution, and disk writing.

Contains the pure helpers (parsing/formatting/resolution) plus the threaded,
cancellable TranscriptExtractor controller. Nothing here imports the UI — the
controller talks back through callbacks, and write_transcripts takes on_log.
"""
import json
import os
import random
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

import validation

# Rate-limit exception classes from youtube-transcript-api 1.x. Imported
# defensively: older/newer versions may shuffle the public surface, so we fall
# back to message-string matching in is_transcript_rate_limit_error if any of
# these are missing.
_TRANSCRIPT_RATE_LIMIT_EXC: tuple = ()
for _name in ("IpBlocked", "RequestBlocked", "TooManyRequests", "YouTubeRequestFailed"):
    try:
        _TRANSCRIPT_RATE_LIMIT_EXC += (getattr(__import__("youtube_transcript_api", fromlist=[_name]), _name),)
    except (ImportError, AttributeError):
        pass


def is_transcript_rate_limit_error(exc: BaseException) -> bool:
    if _TRANSCRIPT_RATE_LIMIT_EXC and isinstance(exc, _TRANSCRIPT_RATE_LIMIT_EXC):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in (
        "too many requests", "429", "ip blocked", "ip-blocked",
        "ipblocked", "request blocked", "requestblocked", "blocked by youtube",
        "youtube is blocking", "youtuberequestfailed",
    ))


# Transcript-loop pacing + retry tuning. Designed for "let it finish in one go
# even on a 121-video playlist" rather than "fastest possible".
TRANSCRIPT_BASE_DELAY = 1.5         # seconds between successive video fetches
TRANSCRIPT_BASE_JITTER = 0.6        # +random[0, jitter] on each delay
TRANSCRIPT_MAX_RETRIES = 4          # per-video retries on rate-limit errors
TRANSCRIPT_BACKOFF_BASE = 8.0       # first backoff (s); doubles each retry
TRANSCRIPT_BACKOFF_CAP = 240.0      # max backoff (s) per retry
TRANSCRIPT_THROTTLE_FLOOR = 5.0     # once throttled, raise base delay to ≥ this
# Circuit breaker: if N consecutive videos each fully exhaust their retries with
# rate-limit errors AND the yt-dlp fallback also fails, the network is hard-blocked
# at every endpoint we know how to reach. Abort fast.
# 1 = abort after the very first fully-failed video — that's already proof.
TRANSCRIPT_HARD_BLOCK_THRESHOLD = 1

MAX_TRANSCRIPT_VIDEOS = 500


@dataclass
class TranscriptSnippet:
    """Minimal mirror of youtube_transcript_api's FetchedTranscriptSnippet.
    The formatters iterate over snippets reading .start and .text; keeping the
    same attribute shape lets yt-dlp-sourced data flow through the existing
    format pipeline unchanged.
    """
    text: str
    start: float
    duration: float


def parse_json3_captions(raw: str) -> list[TranscriptSnippet]:
    """Parse YouTube's json3 caption format into our snippet shape.

    json3 events look like: {"tStartMs": 1234, "dDurationMs": 5678,
    "segs": [{"utf8": "Hello"}, {"utf8": " world"}]}. Some events have no
    `segs` (caption track formatting markers) — skip those.
    """
    data = json.loads(raw)
    snippets: list[TranscriptSnippet] = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text:
            continue
        start_ms = ev.get("tStartMs", 0) or 0
        dur_ms = ev.get("dDurationMs", 0) or 0
        snippets.append(TranscriptSnippet(
            text=text,
            start=start_ms / 1000.0,
            duration=dur_ms / 1000.0,
        ))
    return snippets


# yt-dlp player clients used for transcript extraction. Order matters: yt-dlp
# tries them left to right and uses the first one that returns playable info.
# Empirically validated 2026-05-01 against an IP that had been bot-checked by the
# default mix (web / web_safari): `web_embedded` (the iframe-embed client used for
# video embeds on third-party sites) routes through a less-policed endpoint and
# works without cookies or a YouTube account. `tv` and `android_vr` are kept as
# additional fallbacks for the rare cases where `web_embedded` itself is restricted.
TRANSCRIPT_YT_DLP_PLAYER_CLIENTS = ["tv", "android_vr", "web_embedded"]


def fetch_transcript_via_yt_dlp(vid: str, lang: str,
                                cookies_browser: str | None = None) -> list[TranscriptSnippet]:
    """Fallback transcript fetcher that goes through yt-dlp instead of
    youtube-transcript-api. Uses a hand-picked player-client list that avoids the
    bot-checked `web` / `web_safari` clients — works without cookies or a YouTube
    account on most networks.

    `cookies_browser` (firefox/chrome/etc.) is optional: pass it when the user is
    logged into YouTube and wants extra cred for borderline cases. Won't help users
    without a YouTube account; the player-client tweak is what actually breaks
    through the bot-check.

    Raises RuntimeError on any failure (no captions found, yt-dlp blocked too,
    parse failure, etc.) — the caller decides whether to count it as
    rate-limit-shaped for circuit-breaker purposes.
    """
    url = f"https://www.youtube.com/watch?v={vid}"
    # "auto" means "any English-ish track we can find". Listing variants explicitly
    # is more reliable than yt-dlp's own auto-selection here.
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

    snippets = parse_json3_captions(raw)
    if not snippets:
        raise RuntimeError("yt-dlp captions were empty")
    return snippets


# ── Formatters ─────────────────────────────────────────────────────────────────
def fmt_ts(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip().strip(".")
    cleaned = re.sub(r'\s+', " ", cleaned)
    return cleaned


def yaml_str(text: str) -> str:
    """Escape a string for use inside a YAML double-quoted scalar."""
    return (
        text.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r", " ")
            .replace("\n", " ")
    )


def build_body_flat(data, include_ts: bool) -> str:
    if include_ts:
        return "\n".join(f"[{fmt_ts(s.start)}] {s.text}" for s in data)
    return " ".join(s.text for s in data)


def build_body_paragraphs(data, include_ts: bool) -> str:
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
                txt = f"**[{fmt_ts(para_start)}]** {txt}"
            paragraphs.append(txt)
            current, last_flush_ts = [], s.start
    if current:
        txt = " ".join(current)
        if include_ts:
            txt = f"**[{fmt_ts(para_start)}]** {txt}"
        paragraphs.append(txt)
    return "\n\n".join(paragraphs)


def format_single_transcript(fmt: str, include_ts: bool,
                             video_id: str, video_title: str, data) -> str:
    if fmt == "plain":
        return build_body_flat(data, include_ts)

    body = build_body_paragraphs(data, include_ts)
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
        f'title: "{yaml_str(video_title)}"\n'
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


def format_playlist_transcripts(fmt: str, include_ts: bool,
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
        lines.append(f'title: "{yaml_str(playlist_title)}"\n')
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
            lines.append(build_body_flat(v["data"], include_ts) + "\n")
        else:
            lines.append(build_body_paragraphs(v["data"], include_ts) + "\n\n---\n")

    return "".join(lines)


def resolve_videos(url: str) -> tuple[str, list[tuple[str, str]]]:
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


def apply_range(entries: list[tuple[str, str]], rng: str) -> list[tuple[str, str]]:
    """Apply yt-dlp-style range spec ('1-10', '1,3,5', '5-') to entries.

    Silently skips unparseable parts (including slice syntax like '1:5' which the
    shared validator permits for yt-dlp playlist mode but we don't support when
    driving youtube-transcript-api ourselves).
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


def write_transcripts(videos: list[dict], collection_title: str,
                      target: Path, fmt: str, ext: str, include_ts: bool,
                      per_file: bool, is_collection: bool, *, on_log) -> int:
    """Write transcripts to disk. Returns count of files written.

    `on_log` is called with a human-readable line for each file/dir written.
    """
    if not is_collection:
        v = videos[0]
        if v["data"] is None:
            raise RuntimeError(f"No transcript available: {v['error']}")
        content = format_single_transcript(fmt, include_ts, v["id"], v["title"], v["data"])
        safe = safe_filename(v["title"])[:80] or v["id"]
        out_path = target / f"{safe} [{v['id']}]{ext}"
        out_path.write_text(content, encoding="utf-8")
        on_log(f"📄 Wrote: {out_path.name}")
        return 1

    # Collection (playlist or multi-video resolution)
    if per_file:
        subdir_name = safe_filename(collection_title)[:80] or "playlist"
        subdir = target / subdir_name
        subdir.mkdir(parents=True, exist_ok=True)
        written = 0
        pad = len(str(len(videos)))
        for i, v in enumerate(videos, 1):
            if v["data"] is None:
                continue
            content = format_single_transcript(fmt, include_ts, v["id"], v["title"], v["data"])
            safe = safe_filename(v["title"])[:80] or v["id"]
            out_path = subdir / f"{str(i).zfill(pad)} - {safe} [{v['id']}]{ext}"
            out_path.write_text(content, encoding="utf-8")
            written += 1
        on_log(f"📁 Wrote {written} file(s) to: {subdir.name}/")
        return written

    # Concatenated single file
    content = format_playlist_transcripts(fmt, include_ts, collection_title, videos)
    safe = safe_filename(collection_title)[:80] or "playlist"
    out_path = target / f"{safe}_transcripts{ext}"
    out_path.write_text(content, encoding="utf-8")
    on_log(f"📄 Wrote: {out_path.name}")
    return 1


# ── Controller ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TranscriptRequest:
    url: str
    output_dir: str
    fmt: str                  # "plain" | "markdown" | "obsidian"
    include_timestamps: bool
    per_file: bool
    lang: str
    playlist_range: str
    cookie_browser: str | None


class TranscriptExtractor:
    """Runs transcript extraction on a daemon thread; cancellable; reports via callbacks.

    Same callback shape as downloader.Downloader so the UI shell drives either
    uniformly. Tkinter-free; the shell marshals callbacks onto the Tk main thread.
    """

    def __init__(self, *, on_status, on_log, on_progress, on_finished):
        self._on_status = on_status
        self._on_log = on_log
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._cancel_flag = threading.Event()
        self._thread = None

    def start(self, req: "TranscriptRequest") -> None:
        if req.playlist_range and not validation.validate_playlist_range(req.playlist_range):
            self._on_status("Invalid playlist range. Use formats like 1-10 or 1,3,5.", "red")
            self._on_finished()
            return

        self._cancel_flag.clear()
        fmt = req.fmt
        include_ts = req.include_timestamps
        per_file = req.per_file
        lang = req.lang
        rng = req.playlist_range
        url = req.url
        output_dir = req.output_dir
        cookie_browser = req.cookie_browser
        ext = ".md" if fmt in ("markdown", "obsidian") else ".txt"

        def _worker():
            try:
                self._on_status("Resolving URL…")
                self._on_log(f"→ Resolving: {url}")

                collection_title, all_entries = resolve_videos(url)
                if not all_entries:
                    raise RuntimeError("No videos found for this URL.")

                # Apply playlist range filter (if any) for multi-video URLs.
                entries = apply_range(all_entries, rng) if (len(all_entries) > 1 and rng) else all_entries

                if len(entries) > MAX_TRANSCRIPT_VIDEOS:
                    self._on_log(f"⚠ Capping at {MAX_TRANSCRIPT_VIDEOS} videos (was {len(entries)}).")
                    entries = entries[:MAX_TRANSCRIPT_VIDEOS]

                is_collection = len(entries) > 1
                total = len(entries)
                self._on_log(
                    f"✓ {total} video(s) to process.  Format: {fmt}.  Lang: {lang}.  Per-file: {per_file}"
                )

                ytt = YouTubeTranscriptApi()
                videos: list[dict] = []
                # Adaptive base pacing: starts low, ratchets up the first time YouTube
                # rate-limits us so the rest of the run stays under the threshold.
                adaptive_delay = TRANSCRIPT_BASE_DELAY
                # Circuit-breaker counter: incremented when both the API AND the yt-dlp
                # fallback fail for a video with rate-limit-shaped errors. Reset on any
                # success or non-rate-limit failure.
                consecutive_rl_failures = 0
                hard_blocked = False
                # Once the yt-dlp fallback proves successful, skip the API for all
                # remaining videos — the API is clearly blocked on this network.
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
                        self._on_status("Cancelled.", "orange")
                        return

                    short = (vtitle[:60] + "…") if len(vtitle) > 61 else vtitle

                    # Pace requests (skip before the first one). Small jitter avoids
                    # robotic timing patterns YouTube can flag on.
                    if i > 1:
                        if _interruptible_sleep(adaptive_delay + random.uniform(0, TRANSCRIPT_BASE_JITTER)):
                            self._on_status("Cancelled.", "orange")
                            return

                    self._on_status(f"[{i}/{total}] {short}")
                    self._on_progress(i / total)

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
                                self._on_log(f"✔ [{short}]")
                                consecutive_rl_failures = 0
                                break
                            except Exception as ex:
                                is_rl = is_transcript_rate_limit_error(ex)
                                if is_rl and attempt < TRANSCRIPT_MAX_RETRIES:
                                    # Bump base delay so the rest of the run is gentler.
                                    adaptive_delay = max(adaptive_delay, TRANSCRIPT_THROTTLE_FLOOR)
                                    wait = min(
                                        TRANSCRIPT_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 3.0),
                                        TRANSCRIPT_BACKOFF_CAP,
                                    )
                                    self._on_log(
                                        f"⏳ [{short}] rate-limited; waiting {wait:.0f}s "
                                        f"before retry {attempt + 1}/{TRANSCRIPT_MAX_RETRIES}…"
                                    )
                                    self._on_status(
                                        f"[{i}/{total}] rate-limited, waiting {wait:.0f}s…", "orange"
                                    )
                                    if _interruptible_sleep(wait):
                                        self._on_status("Cancelled.", "orange")
                                        return
                                    continue

                                entry["error"] = (str(ex).splitlines()[0] or "no transcript")[:200]
                                if is_rl:
                                    api_exhausted_with_rl = True
                                else:
                                    # Non-rate-limit failure (genuinely missing transcript,
                                    # etc.) — proves the IP isn't hard-blocked, so reset the
                                    # breaker and don't try yt-dlp (it can't conjure captions
                                    # that don't exist).
                                    consecutive_rl_failures = 0
                                    self._on_log(f"✖ [{short}] {entry['error']}")
                                break

                    # ── Fallback path: yt-dlp ──
                    # Triggered when sticky mode is on (API previously proven blocked this
                    # run) OR when the API just exhausted retries with rate-limit errors.
                    if entry["data"] is None and (yt_dlp_sticky or api_exhausted_with_rl) and not self._cancel_flag.is_set():
                        if api_exhausted_with_rl and not yt_dlp_sticky:
                            self._on_log(f"↪ [{short}] API blocked, trying yt-dlp fallback…")
                            self._on_status(f"[{i}/{total}] yt-dlp fallback…", "orange")
                        try:
                            entry["data"] = fetch_transcript_via_yt_dlp(vid, lang, cookie_browser)
                            entry["error"] = None
                            consecutive_rl_failures = 0
                            if not yt_dlp_sticky:
                                yt_dlp_sticky = True
                                self._on_log(
                                    f"✔ [{short}] (via yt-dlp) — switching all "
                                    "remaining videos to yt-dlp."
                                )
                            else:
                                self._on_log(f"✔ [{short}] (yt-dlp)")
                        except Exception as ex:
                            yt_err = (str(ex).splitlines()[0] or "yt-dlp failed")[:200]
                            yt_is_rl = is_transcript_rate_limit_error(ex)
                            if api_exhausted_with_rl:
                                # Both paths failed, both rate-limit-shaped = network truly
                                # blocked at every endpoint.
                                entry["error"] = f"API blocked + yt-dlp failed: {yt_err}"
                                consecutive_rl_failures += 1
                                self._on_log(f"✖ [{short}] {entry['error']}")
                            else:
                                # Sticky mode (API already known blocked); only yt-dlp failed.
                                # Treat rate-limit-shaped failures as breaker fuel; treat real
                                # "no captions" as a per-video miss and reset the counter.
                                entry["error"] = f"yt-dlp failed: {yt_err}"
                                if yt_is_rl:
                                    consecutive_rl_failures += 1
                                else:
                                    consecutive_rl_failures = 0
                                self._on_log(f"✖ [{short}] {entry['error']}")

                    videos.append(entry)

                    # Circuit breaker: bail out the moment we have proof the network is
                    # blocked at every endpoint. Continuing would burn ~2 min per video.
                    if consecutive_rl_failures >= TRANSCRIPT_HARD_BLOCK_THRESHOLD:
                        hard_blocked = True
                        remaining = total - i
                        # Differentiate: "Sign in to confirm" means YouTube accepted the
                        # request but demanded auth — fixable with cookies-from-browser.
                        # Pure 429s/IpBlocked mean the IP itself is rejected.
                        last_err = (entry.get("error") or "").lower()
                        is_bot_check = (
                            "sign in to confirm" in last_err
                            or "not a bot" in last_err
                            or "use --cookies" in last_err
                        )
                        self._on_log("━" * 60)
                        if is_bot_check:
                            # The player-client tweak usually bypasses this without auth.
                            # Reaching here means even those clients got bot-checked.
                            self._on_log(
                                f"🛑 YouTube bot-check refused every player client. "
                                f"Aborting with {remaining} video(s) unprocessed."
                            )
                            self._on_log(
                                "   Try: (1) switch your VPN exit node, "
                                "(2) wait several hours for the IP reputation to "
                                "decay, or (3) if you have a YouTube account, set "
                                "'Cookies from:' to your logged-in browser."
                            )
                        else:
                            self._on_log(
                                f"🛑 Network appears hard-blocked by YouTube. "
                                f"Aborting with {remaining} video(s) unprocessed."
                            )
                            self._on_log(
                                "   Next steps: switch your VPN exit node or "
                                "wait several hours."
                            )
                        break

                # Write to disk
                target = Path(output_dir)
                target.mkdir(parents=True, exist_ok=True)
                written = write_transcripts(
                    videos, collection_title, target, fmt, ext, include_ts, per_file, is_collection,
                    on_log=self._on_log,
                )

                ok = sum(1 for v in videos if v["data"] is not None)
                self._on_progress(1.0)
                if hard_blocked:
                    self._on_status(
                        f"🛑 Aborted at {len(videos)}/{total} — see log "
                        f"for fix. Wrote {written} file(s).", "red"
                    )
                else:
                    self._on_status(
                        f"✔ {ok}/{total} extracted  ·  wrote {written} file(s)", "green"
                    )
            except Exception as e:
                msg = str(e)
                self._on_status(f"Transcript error: {msg}", "red")
                self._on_log(f"✖ {msg}")
            finally:
                self._on_finished()

        self._thread = threading.Thread(target=_worker, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel_flag.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout=None) -> None:
        if self._thread:
            self._thread.join(timeout)
