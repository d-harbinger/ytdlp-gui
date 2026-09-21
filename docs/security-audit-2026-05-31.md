# ytdlp-gui — security audit, 2026-05-31

Security pass for the workspace audit campaign. Scope for a desktop GUI that
drives `yt-dlp`: command injection (the classic failure mode for download
front-ends), URL handling (scheme/SSRF), output-path traversal, post-download
command execution, dangerous Python sinks, and dependency freshness.

**Result: clean — no findings, no code changes.** The app is written
defensively; this pass confirmed the injection and traversal surfaces are
already closed.

## Threat model

A local desktop app (customtkinter) that takes a user-supplied URL and
download options and fetches media via `yt-dlp`. Adversary inputs: the URL,
the playlist range, the chosen output directory, and any field that could
reach a shell or the filesystem. Because it runs on the user's own machine,
"writing to a folder the user picked" is not a threat; "a crafted URL/title
escaping into a shell or out of the intended directory" is.

## What was checked — all sound

- **No shell / command injection.** `yt-dlp` is driven through its **Python
  API** (`yt_dlp.YoutubeDL(opts)` in `downloader.py` and `transcript.py`), not
  a shell-invoked binary. There is no `os.system`, no `shell=True`, no
  string-built command anywhere.
- **The few `subprocess` calls are safe.** `ytdlp_gui.py` shells out only to
  native folder pickers (`zenity`/`kdialog`) and a folder opener
  (`xdg-open`/`gio`), always with **list arguments** (no `shell=True`). The
  picker even carries an inline `SECURITY: title must remain hardcoded — never
  pass user input (CWE-78)` guard. The opener passes the user's own validated
  directory as a single argv element — not shell-interpreted.
- **URL validation.** `validation.py` enforces an `http(s)` scheme allowlist
  (blocks `file://`, `javascript:`, etc.) and an optional YouTube-domain
  restriction (`YTDLP_GUI_YOUTUBE_ONLY`). Playlist ranges are regex-validated
  (`^[\d,\-:\s]+$`).
- **No output-path traversal.** `outtmpl` is a **fixed** template
  (`"%(title)s [%(id)s].%(ext)s"`), never user-supplied, and
  `restrictfilenames: True` sanitizes titles into safe filenames — a malicious
  video title cannot inject path separators. `paths.home` is the user's own
  chosen download directory.
- **No post-download command execution.** No `--exec` / `exec_cmd`. Every
  configured postprocessor is a built-in `yt-dlp`/FFmpeg one
  (ExtractAudio, EmbedThumbnail, EmbedSubtitle, Metadata, SplitChapters,
  SponsorBlock, ModifyChapters) — none run a user-supplied command.
- **No dangerous Python sinks.** No `eval`, no `pickle` load, no
  `yaml.load`. The one `__import__` resolves a fixed module name
  (`youtube_transcript_api`), not user input.
- **Dependencies current.** `requirements.txt` pins `yt-dlp[default]
  >=2025.11.12` (recent; yt-dlp ships frequent security fixes),
  `customtkinter>=5.2.0`, `youtube-transcript-api>=1.0.0`.

## Recommendation (optional, not a finding)

Run `pip-audit` against the resolved environment periodically — the `>=` pins
float forward, so freshness depends on the install. Not run in this pass
(no resolved environment captured); the declared versions are current.

## Net

No vulnerabilities found. Command-injection, SSRF/scheme, path-traversal, and
post-download-exec surfaces are all closed by design (API invocation,
input validation, fixed templates, list-arg subprocess). No changes made.

## Addendum, 2026-09-21 — the update path

`updater.py` introduced the application's first subprocess sinks and its first
network requests to hosts the application chooses. Reviewed against the same
threat model:

- **Two subprocess calls, both list-argument, neither through a shell.**
  `[sys.executable, "-m", "pip", "install", …]` with a fixed tuple of
  requirement names, and `[<deno>, "upgrade"]`. No value from a URL, from media
  metadata or from the settings file reaches either argument list. The deno
  path comes from a `PATH` lookup — the same lookup yt-dlp performs for every
  YouTube download, so it grants nothing the download path had not already.
- **The upgrades are fenced by ownership.** pip runs only inside a virtual
  environment; `deno upgrade` runs only when this account can write both the
  binary and its directory. Neither can touch a system interpreter or a
  distribution-packaged deno, and neither ever runs unprompted.
- **Two fixed HTTPS requests,** to `pypi.org` and `dl.deno.land`, with default
  certificate verification, an eight-second timeout and a bounded read on the
  deno pointer. The responses are compared as version numbers and displayed;
  nothing from them is executed or used to build a path or a command. A hostile
  response could at worst show a false "update available", and pressing the
  button would then install whatever the real index serves.
- **Not covered:** artifact authenticity. pip and `deno upgrade` trust their
  origin over TLS; nothing is hash-pinned.

The recommendation above was carried out the same day: `pip-audit` against the
resolved environment, after an upgrade through the button's own code path,
reported no known vulnerabilities, and yt-dlp's thirteen published advisories
all carry fix versions at or below 2026.7.4, under the declared floor.
