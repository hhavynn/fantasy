# Soccer pack implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `make-soccer` renders a QC-passing "every X explained" Short, reusing the NFL voice, audio and technical gates.

**Architecture:** PR 4 of 5, stacked on the stats PR. New code sits in `studio/soccer/`; NFL files do not move. See
`docs/superpowers/specs/2026-10-30-soccer-shorts-account-design.md`.

**Tech Stack:** Python 3.11, Pillow, FFmpeg, Kokoro, pytest.

## Global Constraints

- NFL output stays identical. Shared code changes only through default-preserving parameters.
- Scenes are code-drawn only: no photos, crests, kits or footage. Every upload stays private and human-approved.
- Numbers in narration are claims with a stated source.

---

### Task 1: Explainer library and script

**Files:** Create `studio/soccer/explainers.py`, `script.py`. Test `tests/test_soccer_pack.py`.

- [ ] Test every explainer passes the shared narration gate and a trend only changes the hook.
- [ ] Write offside, cards and VAR from the IFAB Laws of the Game.

### Task 2: Scenes, render, thumbnail and metadata

**Files:** Create `studio/soccer/scenes.py`, `render.py`. Test `tests/test_soccer_pack.py`.

- [ ] Test every scene draws and never goes dark, and a synthetic-audio render gives a 1080x1920 private Short.
- [ ] Reuse `align`, `phrases`, `caption_frame` and `audio_design.mix`.

### Task 3: QC, CLI and trend handoff

**Files:** Create `studio/soccer/qc.py`, `cli.py`. Modify `studio/stages/qc.py`, `pyproject.toml`, `README.md`, and
`social_intel/cli.py`, `sports/soccer/adapter.py`.

- [ ] Add `attribution` to `gate_metadata` and `stages` to `gate_integrity`, keeping NFL defaults.
- [ ] Add `content_rules` and `claim_sources`, and write history only after QC passes.
- [ ] Make `trends --sport soccer select` print the `make-soccer` command.

### Task 4: Live check

- [ ] Render all three explainers and one real trend package with the real voice. QC must pass.
- [ ] Inspect frames by eye.
