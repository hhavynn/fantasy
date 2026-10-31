# Soccer parody implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Selected parody and parallel-universe ideas render as labelled cartoons, with the content rules enforced by QC.

**Architecture:** PR 5 of 5, stacked on the soccer pack. See `docs/superpowers/specs/2026-10-30-soccer-shorts-account-design.md`.

**Tech Stack:** Python 3.11, Pillow, FFmpeg, Kokoro, pytest.

## Global Constraints

- No face-swaps, cloned player voices or depictions of real people. The cast is a closed list of fictional archetypes.
- Every parody says aloud and shows on screen that it is invented. No invented line is attributed to a real person.
- No fiction format for a story that is mainly an injury or a serious harm.
- Real numbers are sourced claims.

---

### Task 1: Cast and scenes

**Files:** Create `studio/soccer/toons.py`. Modify `scenes.py`. Test `tests/test_soccer_pack.py`.

- [ ] Test every stock scene draws. Draw the cast, sets, bubble, split panels, fact card and label from code.

### Task 2: Scripts

**Files:** Create `studio/soccer/parody.py`. Modify `script.py`, `cli.py`.

- [ ] Test templates follow the story, the real number comes from the package, and hand-written beats pass the same gates.

### Task 3: Rules gate

**Files:** Modify `studio/soccer/qc.py`, `render.py`, and `social_intel/sports/soccer/ideas.py`, `topics.py`.

- [ ] Test the gate fails on each rule break, for every real name in the story, ignoring accents.
- [ ] Block fiction formats on injury and serious stories in idea generation and QC, by the story's leading topics.

### Task 4: Live check

- [ ] Render one parody and one what-if from real trends with the real voice. QC must pass. Inspect frames by eye.
