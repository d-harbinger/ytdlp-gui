# TEACHING.md — ytdlp-gui

Per-project audit of AI-codegen slop, taught with **this repo's own code** as the
examples, then triaged into a cleanup plan. Part of the `/mnt/Projects` library cleanup.

Each finding: **Tell** (what it looks like) · **Why** (why an AI does it) ·
**Detect** (a signal you can run) · **Fix** (the surgical undo).

---

## Snapshot — 2026-05-28

- **What it is:** a CustomTkinter GUI wrapping `yt-dlp` for video downloads; also fetches/parses YouTube transcripts.
- **`ytdlp_gui.py`** — single file, **1907 LOC**, 71 top-level defs/classes.
- git: clean tree, privacy infra present. **Runs?** Not verified — GUI, audited in a display-less VM.

**ytdlp-gui's slop profile — the opposite of pomo.** pomo looked clean by eye but was
slop-heavy (duplication). ytdlp-gui looks slop-heavy by *grep* (23 `except`, 8 `pass`)
but is mostly **clean** there — its real problem is **structural**: one class does
everything. Counts don't catch architecture.

**Strengths (credit where due):** real input validation — `_validate_url` enforces an
`ALLOWED_SCHEMES` allow-list and there's a `YTDLP_GUI_YOUTUBE_ONLY` env gate. Good
hygiene for a tool that shells out to `yt-dlp`. Error handling is mostly *narrow* and
*logged*, not swallowed.

---

## Findings (ranked by leverage)

### 1. God-class: `YtDlpGUI` does everything  *(CRITICAL — structural)*

- **Tell:** `class YtDlpGUI` spans **lines 372→1907 (~1535 LOC, 48 methods)**. One object builds the UI, orchestrates the `yt-dlp` subprocess, runs the download thread + cancellation, reads/writes config, fetches/parses transcripts, and drives logging. You can't understand, test, or change one responsibility without loading all of them.
- **Why:** an AI grows a GUI class method-by-method as features are requested ("add a transcript tab", "add config persistence") and never steps back to split responsibilities — each addition is locally reasonable, the sum is a monolith.
- **Detect:** a single class > ~500 LOC or > ~15 methods; `awk 'NR>=372' ytdlp_gui.py | grep -cE '^    def '` → 48.
- **Fix:** extract cohesive modules and leave `ytdlp_gui.py` as the UI shell that wires them — `downloader.py` (yt-dlp subprocess + threading + cancel), `transcript.py` (the json3 caption fetch/parse, already module-level functions — easy to lift), `config.py` (`_read_config`/`_write_config`/`_save_config_key`/`_detect_scale`). **This is a real refactor — it needs a plan, not an in-place hack.** Don't slop it.

### 2. Single-file monolith — helpers colocated with the god-class

- **Tell:** validation, config I/O, transcript fetch/parse, DPI-scale detection all live module-level in the same 1907-line file as the class.
- **Why:** new helpers get appended to the open file rather than placed in a module.
- **Detect:** unrelated concerns sharing one file; `grep -nE '^(class|def) ' ytdlp_gui.py` shows downloader + transcript + config + UI in one place.
- **Fix:** the *low-risk first step* toward #1 — move the already-standalone module-level helpers into files (`transcript.py`, `config.py`) and import them, without touching the class internals. Do this before the harder class split.

### 3. Cross-tool overlap with the `transcript` project  *(flag — your call)*

- **Tell:** ytdlp-gui embeds transcript extraction (`_fetch_transcript_via_yt_dlp`, `_parse_json3_captions`, `_TranscriptSnippet`) while you maintain a *separate* `transcript` project whose whole purpose is YouTube transcripts.
- **Why:** features get built where they're convenient, not where they belong; capability drifts across tools.
- **Detect:** two projects that do the same job. (Checked: no shared symbol names, so this is *conceptual* overlap, not copy-pasted code.)
- **Fix:** decide whether transcript logic should live in one place both tools consume, or whether the two are genuinely separate products. A consolidation *strategy* question — not a bug, and not mine to decide.

### 4. Two cosmetic broad `except Exception`  *(MINOR)*

- **Tell:** `ytdlp_gui.py:263` (around `urlparse`) and `:392` (window-icon load) catch bare `Exception`.
- **Why:** reflexive broad catch.
- **Detect:** `grep -nE 'except Exception' ytdlp_gui.py` — then read each; most others here capture `as e` and log.
- **Fix:** narrow to `ValueError` (263) and `tk.TclError`/`OSError` (392). Trivial, optional — both already behave correctly (263 returns a message, 392 is cosmetic).

---

## Triage / cleanup plan

| # | Finding | Action | Status |
|---|---------|--------|--------|
| 1 | God-class `YtDlpGUI` | Extract `downloader`/`transcript`/`config` modules; UI shell wires them | open — **needs a refactor plan**, not a quick edit |
| 2 | Monolith helpers | Low-risk first step: lift module-level helpers into files | open |
| 3 | `transcript` overlap | Decide consolidation strategy across the two tools | open — your call |
| 4 | 2 cosmetic broad excepts | Narrow types | open — trivial, optional |

**Unlike pomo, there is no safe one-shot cleanup here.** The headline (#1) is a genuine
refactor of a 1535-line class — high blast radius, must be planned and verified, not
slopped in place. #4 is the only truly safe-now edit. Recommend: plan #1+#2 as a proper
refactor (own spec/plan) when you choose to invest; do #4 anytime.

---

## Verification gate

No "fixed/working" claim until ytdlp-gui launches host-side (GUI; not runnable in the
audit VM) and a real download + a transcript fetch are exercised. `py_compile` is
necessary but nowhere near sufficient for a refactor of this size.
