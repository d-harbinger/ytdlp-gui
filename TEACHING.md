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
| 1 | God-class `YtDlpGUI` | Extracted `config`/`validation`/`transcript`/`downloader` modules; thin shell wires them via callbacks | **done** 2026-05-28 — 5 phases; class 1535→928 LOC; static/headless checks green; **host-verified** (user ran the GUI clean) |
| 2 | Monolith helpers | Lifted in phases 1–2 (pure helpers → their own modules) | **done** — see #1 |
| 3 | `transcript` overlap | **Resolved:** transcript was folded into ytdlp-gui (canonical home); standalone is dead and archived at `_archive/transcript/` | done 2026-05-28 |
| 4 | 2 cosmetic broad excepts | Narrowed (urlparse→`ValueError` @263, icon→`tk.TclError` @392) | done 2026-05-28 |

**Unlike pomo, there is no safe one-shot cleanup here.** The headline (#1) is a genuine
refactor of a 1535-line class — high blast radius, must be planned and verified, not
slopped in place. #4 is the only truly safe-now edit. Recommend: plan #1+#2 as a proper
refactor (own spec/plan) when you choose to invest; do #4 anytime.

**Status 2026-05-28:** #3 + #4 done. **#1 + #2 are now executed** (was: open → designed →
executed). The spec at `docs/superpowers/specs/2026-05-28-ytdlp-gui-god-class-refactor-design.md`
was carried out in 5 phases; the *what + why* is taught in the section below. All
VM-possible checks are green (py_compile, import smoke, pyflakes, 32 pure-function tests),
and the **host-side verification gate passed** — the user ran the GUI on 2026-05-28 and the
tested paths worked cleanly. Refactor complete.

---

## What I did — the god-class refactor, and why (2026-05-28)

Finding #1 went *open → designed → **executed*** on 2026-05-28: a written spec
(`docs/superpowers/specs/2026-05-28-ytdlp-gui-god-class-refactor-design.md`) split `YtDlpGUI`
into `config` / `validation` / `transcript` / `downloader` modules behind a thin UI shell,
then the spec was carried out in five phases. What follows is each decision and **why**, so
the reasoning is reviewable, not just the result.

### The shape, before and after

```
BEFORE · one file, one god-class

  ytdlp_gui.py ──────────────────────────────────────── 1907 LOC
  ┌───────────────────────────────────────────────────────────┐
  │  class YtDlpGUI(ctk.CTk)          ~1535 LOC · 48 methods    │
  │  ░░░░░░░░░░░░ everything tangled together ░░░░░░░░░░░░░░░░  │
  │   • builds the entire UI                                   │
  │   • yt-dlp download: thread + cancel + progress hook       │
  │   • transcripts: fetch / parse / format  (one 270-LOC fn)  │
  │   • config read/write + DPI-scale detection               │
  │   • URL + playlist-range validation                       │
  │   • yt-dlp logging adapter                                │
  │                                                           │
  │   glue that made it inseparable:                          │
  │     · self.after(0, …) smeared through both workers       │
  │     · self._cancel_flag + self._download_thread  SHARED    │
  │     · ~20 self.*_var.get() read inline by the workers      │
  └───────────────────────────────────────────────────────────┘
  + standalone helpers jammed in at module level

  change one responsibility → load all of them. untestable without a display.


AFTER · thin shell wiring cohesive modules

  ytdlp_gui.py ── thin UI shell ─────────────────────── 1038 LOC
  ┌───────────────────────────────────────────────────────────┐
  │  class YtDlpGUI         928 LOC · 36 methods               │
  │   • builds UI  • gathers widgets → Request  • wires/marshal│
  └────────┬──────────────────────────────────────▲───────────┘
   Request │ (frozen dataclass —                   │ callbacks:
   flows   │  no widget crosses below)             │ on_status/on_log/
   DOWN    ▼                                       │ on_progress/on_finished
  ┌────────┴───────────────┐     ┌─────────────────┴──────────┐
  │ downloader.py     241  │     │ transcript.py        693   │
  │  Downloader (controller)│     │  TranscriptExtractor (ctrl)│
  │  build_ydl_opts        │     │  fetch / parse / format    │
  │  GUILogger, parse_rate │     │  resolve_videos/apply_range│
  └───────────┬────────────┘     └──────────────┬─────────────┘
              └────────────┬────────────────────┘
                           ▼  (shared · pure · tkinter-free)
            ┌──────────────────────┐  ┌──────────────────────┐
            │ validation.py     45 │  │ config.py         65 │
            └──────────────────────┘  └──────────────────────┘

  tests/ ── 32 plain-assert tests (no pytest) + _harness.py
  the shell is the ONLY thing that imports tkinter.
```

| Metric | Before | After |
|---|---|---|
| Files | 1 | 5 (+ tests) |
| `YtDlpGUI` class | 1535 LOC / 48 methods | 928 LOC / 36 methods |
| Largest method | `_start_transcript_extraction` ~270 LOC | moved into `TranscriptExtractor` |
| Touches tkinter | everything | shell only |
| Headless-testable logic | ~none | validators, config, opts-building, all formatters |
| Automated tests | 0 | 32 |
| Static analysis | — | pyflakes clean (found + fixed a latent bug) |

### The real problem wasn't size — it was coupling
A 1535-line class is the symptom. The disease is that three concerns are tangled so you
can't move one without dragging the others:

1. **UI-thread marshaling.** Tkinter may only be touched from its own main thread, so every
   background worker pushes updates through `self.after(0, lambda: …)` (the download worker at
   `ytdlp_gui.py:1307–1330`; the transcript worker throughout `:1551–1799`). That one pattern
   is *why* the worker logic can't simply be lifted out — it reaches back into the widget
   object on every status line.
2. **Direct widget reads.** `_build_ydl_opts` (`:1160`) and `_start_transcript_extraction`
   (`:1536`) read ~20 `self.*_var.get()` values inline. The "settings" live scattered across
   widgets, gathered nowhere.
3. **Shared mutable cancel state.** One `self._cancel_flag` and one `self._download_thread`
   (`:379–380`) are shared by both paths and by `_on_close` / the cancel button.

**Takeaway:** when a class is "too big to split," find the *cross-cutting coupling* first.
The line count is downstream of it.

### The move: callbacks + request objects (dependency inversion)
The fix inverts who-depends-on-whom. Rather than worker code reaching into the UI, the
extracted controllers take four plain callbacks (`on_status`, `on_log`, `on_progress`,
`on_finished`) and *call* them. The **shell** supplies those callbacks and is the only place
that knows about `self.after(0, …)`.

- **Why callbacks beat a UI reference.** If a controller held `self.ui` and called
  `self.ui.set_status(…)`, it would still depend on the UI's method names — you'd have moved
  the file, not broken the coupling. Callbacks point the dependency *inward* (UI → controller),
  so the controller compiles and tests with no tkinter at all.
- **Why request dataclasses.** Packing the 20 widget reads into a frozen `DownloadRequest` /
  `TranscriptRequest` turns "read a live widget" into "read a field." That's what makes
  `build_ydl_opts(req)` a **pure function**: same input → same dict, unit-testable headless.
  The widget→request gather stays in the shell; everything below the shell sees data, not GUI.

**Principle:** keep the framework (tkinter) at the *edge*. Core logic must not import the
thing that's hard to test.

### Smaller calls, and the reasoning behind each
- **A `validation.py`, even though the audit named only three modules.** Both validators are
  needed by *both* controllers, and a controller must never import its sibling. A dependency
  shared by two peers belongs in a third, neutral module — not smuggled into one of them.
  Honest cohesion beats a tidy module count.
- **No shared base class for the two controllers.** They share ~10 lines of thread/cancel
  boilerplate. A base class would couple two otherwise-independent modules to save those 10
  lines. Instead they expose *identical method names* (`start`/`cancel`/`is_running`/`join`)
  and the shell drives "the active one" by duck typing. **DRY isn't free — it trades
  duplication for coupling, and here the coupling costs more.**
- **Drop the leading underscore when a function becomes a module's public API.** `_read_config`
  means "private to this file." Once it lives in `config.py` and is imported elsewhere, the
  underscore lies — rename to `config.read_config`.
- **Phase the work safe → risky.** Pure, zero-behavior modules first (`config`, `validation`);
  the threaded stateful controllers last. Each phase compiles and tests on its own, so a break
  surfaces in the cheap phase instead of hiding under the risky one. **Order changes by blast
  radius.**

### What I deliberately did *not* do
No behavior changes, no new features, no "while I'm here" cleanup of unrelated code, and **no
execution** — you asked for the design, so I stopped at the spec and the approval gate. Scope
discipline is itself the lesson: a refactor that also "improves" things is two changes wearing
one diff, and you can't tell which one broke the download.

### Executing it: test-first, and the bug verification caught
The five phases ran safe→risky. Each extracted module got **plain `assert`-based tests written
first** (RED), then the code was moved to make them pass (GREEN) — no pytest dependency added
(`tests/_harness.py` is a ~30-line runner; the project had no test framework and didn't need a
new one). 32 tests now cover the pure surface: validators, config round-trip, json3 parsing,
the formatters, `apply_range`, `parse_rate`, and `build_ydl_opts(req)`.

The payoff showed up immediately: running **pyflakes** as a static gate flagged a *pre-existing*
crash in `_fetch_info` (untouched by the refactor) — `except Exception as e:` followed by a
**deferred** `self.after(0, lambda: …{e}…)`. Python clears the exception name `e` when the
`except` block exits, so the lambda would `NameError` whenever a fetch failed: the error
handler itself was broken. Fixed by capturing `err = str(e)` before the lambda. **Lesson:**
"compiles + imports" would never have caught this — it lives in a method body that only runs on
the error path. A static name-checker (pyflakes) and real verification are how you find the bug
the happy path hides.

### Why the verification gate is non-negotiable here
`py_compile` proves the files *parse*. It says nothing about whether a download still runs. A
GUI/threading refactor can compile perfectly and still deadlock, or silently drop every status
update, the first time it's actually run. The only honest proof is a real download
(video/audio/playlist) and a real transcript fetch (single + playlist), host-side — the gate
below.

---

## Verification gate

No "fixed/working" claim until ytdlp-gui launches host-side (GUI; not runnable in the
audit VM) and a real download + a transcript fetch are exercised. `py_compile` is
necessary but nowhere near sufficient for a refactor of this size.
