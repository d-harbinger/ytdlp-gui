# ytdlp-gui God-Class Refactor — Design

**Date:** 2026-05-28
**Source of truth:** `TEACHING.md` findings #1 (god-class) + #2 (single-file monolith)
**Scope chosen:** Full split, executed in safe phases.

## Problem

`ytdlp_gui.py` is a single 1907-LOC file. `class YtDlpGUI` spans ~1535 LOC / 48
methods and does everything: builds the UI, runs the yt-dlp subprocess + download
thread + cancellation, reads/writes config, fetches/parses/formats transcripts,
and drives logging. You cannot understand, test, or change one responsibility
without loading all of them.

The mechanical coupling that makes this hard to split:

1. **UI-thread marshaling.** Every worker thread pushes UI updates through
   `self.after(0, lambda: ...)`. Tkinter knowledge is smeared across the
   download and transcript workers.
2. **Direct widget reads.** Both `_build_ydl_opts` and
   `_start_transcript_extraction` read ~20 `self.*_var.get()` widget variables
   inline.
3. **Shared mutable cancel state.** One `self._cancel_flag` (`threading.Event`)
   and one `self._download_thread` are shared by both the download and
   transcript paths and by `_on_close` / the cancel button.

## Goal

Leave `ytdlp_gui.py` as a thin UI shell that wires together cohesive,
independently-testable modules. No behavior change.

## Decoupling approach

**Callback controllers + request dataclasses.**

- Controllers expose `start(req)` / `cancel()` / `is_running()` / `join(timeout)`
  and take four plain callbacks at construction: `on_status(text, color="gray")`,
  `on_log(text)`, `on_progress(fraction)`, `on_finished()`.
- Controllers call those callbacks **directly from their worker thread**. They
  know nothing about tkinter. The **shell's** callback implementations perform
  the `self.after(0, ...)` marshaling onto the Tk main thread. All tkinter stays
  in the shell.
- The shell reads every widget var once and packs them into a frozen
  `DownloadRequest` / `TranscriptRequest`. No module below the shell ever
  touches a widget. The shell also keeps the UI-only constants
  (`VIDEO_PRESET_FORMATS`, `AUDIO_CODECS`, dropdown lists, etc.) and the
  `_resolve_format_string` mapping, passing the **already-resolved** yt-dlp
  format expression in the request — so `downloader.py` needs no UI constants.

Alternatives considered and rejected:

- **Controller holds a UI reference** (`self.ui.set_status(...)`): less rewiring
  but recouples controllers to the UI's method surface; marshaling still needed.
- **`queue.Queue` + `after(100,...)` polling**: maximally decoupled but
  introduces a polling loop the current code does not have. Overkill.

## Module layout

Four new modules plus the slimmed shell.

| File | Contents | Imports |
|------|----------|---------|
| `config.py` | `read_config`, `write_config`, `save_config_key`, `detect_scale`; `CONFIG_DIR`/`CONFIG_FILE`, `SCALE_OPTIONS`. No `customtkinter` import — stays pure; the shell calls `detect_scale()` then applies `ctk.set_widget_scaling` / `set_window_scaling`. | stdlib only |
| `validation.py` | `validate_url`, `validate_playlist_range`; `ALLOWED_SCHEMES`, `YOUTUBE_HOSTS`, `YOUTUBE_ONLY`, `_PLAYLIST_RANGE_RE`. Shared pure validators — neither controller may import the other, so these get a neutral home. | stdlib only |
| `transcript.py` | `TranscriptSnippet`, `parse_json3_captions`, `fetch_transcript_via_yt_dlp`, `is_transcript_rate_limit_error`, the `TRANSCRIPT_*` tuning constants + `TRANSCRIPT_YT_DLP_PLAYER_CLIENTS`; formatters (`fmt_ts`, `safe_filename`, `yaml_str`, `build_body_flat`, `build_body_paragraphs`, `format_single_transcript`, `format_playlist_transcripts`); `resolve_videos`, `apply_range`, `write_transcripts`, `MAX_TRANSCRIPT_VIDEOS`; **`TranscriptExtractor` controller**. | `validation` |
| `downloader.py` | `GUILogger`, `parse_rate`, `build_ydl_opts(req)`, `MAX_PLAYLIST_DOWNLOADS`, **`Downloader` controller**. | `validation` |
| `ytdlp_gui.py` | Thin UI shell: widget construction, var→request gathering, `after`-wrapped callbacks, controller wiring, `__main__`. `_native_askdirectory` stays here (tkinter `filedialog`). | all of the above |

No shared base class for the two controllers: they expose identical method names,
so the shell drives whichever is active by duck typing. ~10 lines of thread/cancel
boilerplate duplicated is clearer than coupling two otherwise-independent modules.

## Interfaces

```python
# downloader.py
@dataclass(frozen=True)
class DownloadRequest:
    url: str
    output_dir: str
    mode: str                 # "video" | "audio" | "playlist"
    video_format: str         # already-resolved yt-dlp format expression (shell maps UI selection)
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
    sb_categories: list[str]
    rate_limit: str           # "No limit" | "1M" | ...
    cookie_browser: str       # "-- none --" | "firefox" | ...
    archive: bool

def build_ydl_opts(req: DownloadRequest, *, on_log) -> dict: ...
    # pure dict construction; on_log used only for the "invalid range" warning

class Downloader:
    def __init__(self, *, on_status, on_log, on_progress, on_finished): ...
    def start(self, req: DownloadRequest) -> None     # spawns daemon thread
    def cancel(self) -> None                          # owns its threading.Event
    def is_running(self) -> bool
    def join(self, timeout=None) -> None

# transcript.py
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
    def __init__(self, *, on_status, on_log, on_progress, on_finished): ...
    def start(self, req: TranscriptRequest) -> None
    def cancel(self) -> None
    def is_running(self) -> bool
    def join(self, timeout=None) -> None
```

## Data flow

1. User clicks Download. Shell validates URL (`validation.validate_url`),
   resolves output dir, clears log, disables/enables buttons, sets
   `active_controller`.
2. Shell gathers widget vars → `DownloadRequest` or `TranscriptRequest`.
3. Shell calls `active_controller.start(req)`.
4. Controller worker thread runs; calls `on_status` / `on_log` / `on_progress`
   as it goes. Shell's implementations wrap each in `self.after(0, ...)`.
5. On completion/error the controller calls `on_finished`; the shell's
   `on_finished` re-enables buttons and clears `active_controller`.
6. Cancel button / window close → `active_controller.cancel()` (and `join`
   with timeout on close).

## Naming

Functions that become a module's public API drop the leading underscore
(`_read_config` → `config.read_config`, `_parse_json3_captions` →
`transcript.parse_json3_captions`, etc.). Genuinely-internal helpers keep the
underscore. Mechanical change, correct convention for a real refactor.

## Error handling

Preserve existing behavior exactly: narrow logged excepts, the transcript
circuit-breaker / adaptive-pacing / yt-dlp-fallback logic, `MaxDownloadsReached`
and `DownloadError` handling, the two already-narrowed cosmetic excepts. This
refactor moves code; it does not change error semantics.

## Testing

Pure functions are extracted first and unit-tested headless (no display, no
network) via TDD:

- `validation.validate_url`, `validation.validate_playlist_range`
- `transcript.parse_json3_captions`, `apply_range`, `fmt_ts`, `safe_filename`,
  `yaml_str`, `build_body_flat`, `build_body_paragraphs`,
  `format_single_transcript`, `format_playlist_transcripts`
- `downloader.parse_rate`, `downloader.build_ydl_opts(req)` → assert the opts
  dict for representative requests (video / audio / playlist / subs / SponsorBlock
  / rate-limit / cookies / archive)

Controllers (threaded, network-bound) are not unit-tested; they are covered by
the host-side manual gate.

## Verification gate

Per phase: `python -m py_compile` on every module + an import smoke test +
the relevant pure-function unit tests.

Final gate — run host-side by the user (the GUI is not runnable in the
display-less audit VM): real **download** (video, audio, playlist) **and**
real **transcript fetch** (single video + playlist). No "fixed/working" claim
until this passes. `py_compile` is necessary but nowhere near sufficient.

## Phased execution (safe → risky)

1. **`config.py` + `validation.py`** — pure, zero behavioral risk. Shell imports
   them; remove the moved module-level defs. Verify: compile + import + validator
   tests.
2. **`transcript.py` pure functions** — snippet/parse/fetch/formatters/resolve/
   apply_range/constants. Shell delegates. Verify: compile + import + formatter
   tests.
3. **`downloader.py` + `Downloader` controller** — `build_ydl_opts`, `GUILogger`,
   `parse_rate`, controller. Wire the download path. Verify: compile + import +
   `build_ydl_opts` tests + host-side download.
4. **`TranscriptExtractor` controller** — the big worker moves into
   `transcript.py`. Wire the transcript path. Verify: compile + import +
   host-side transcript (single + playlist).
5. **Shell cleanup** — remove dead wrappers, finalize the thin shell, confirm
   `_on_close` / cancel target the active controller. Final full host-side pass.

## Out of scope

- Cross-tool consolidation with the (now-archived) `transcript` project —
  TEACHING.md finding #3, already resolved.
- Any change to download/transcript behavior, UI layout, or features.
