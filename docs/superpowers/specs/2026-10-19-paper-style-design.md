# Paper style for Shorts — design

Date: 2026-10-19 · Status: approved (Havyn)

## Why
Reference: a Gradient FC TikTok (Lamine Yamal season in numbers). What makes it work: one continuous surface
instead of a slideshow of cards, everything building on element by element, hand-drawn marker callouts on the
key number, background-removed player stickers, page-stack transitions and calm pacing (no flashes or whips).
Our Shorts read as separate full-frame cards with cuts and whips.

## What
A fourth graphic style, `paper`, rendered with `--style paper` into `styles/paper/final.mp4` next to the
original. The story, narration, music, sound effects, timing and captions are unchanged, so an A/B against
`premium` compares pictures only.

Look: dark paper. The premium Night palette, the site's faces, plus a static paper grain (seeded noise and
fibres, soft vignette). Marker colour: the site's caution amber `#F2BD52`. Handwritten notes in Caveat (OFL).

## Design (approach A: a new look on the existing vector scene library)
- `assets/styles/paper.toml`: tokens (premium plus `marker`, a `hand` face, longer stagger, `page` transition).
- `studio/graphics/paper.py`: primitives, each pure and deterministic:
  - `grain(base)`: the paper texture over the backdrop (cached).
  - `sticker(img, border, tilt)`: cut-out with a white outline from a dilated alpha, a drop shadow and a tilt.
  - `rays(team)`: faint team-colour sunburst for the player card.
  - `circle(F, box, k, color)`: a hand-drawn loop around a box, stroked on as k goes 0→1, ends overlapping.
  - `note(F, text, x, y, k, color)` and `arrow(...)`: handwritten note and curved arrow (text is always a
    claim-backed label the scene already shows; logged through Frame.text with its source).
  - `page(prev, cur, k)`: the old page slides off down and to the side with a ~3° turn and a shadow,
    uncovering the new page beneath.
- `vector.py`: `Look.paper`; `backdrop` adds grain; `transition` uses `page`; scenes add callouts only
  `if L.paper`: player card (sticker on rays, circle the new value), target balls (circle the latest lit
  column, mini sticker), split (circle the recent value), volume, lineup (circle the "now" mark), teammate out
  (circle the Out cells), status stamp (underline), decision (sticker beside the question). Format and show
  scenes get the look (grain, transitions, stagger) without bespoke callouts.
- Numbers pop and get circled; they never count up (in-between values would have no claim behind them).
- Registered in `graphics.STYLES`, `render.STYLES`, CLI choices; render cache keys include `paper.py`.

## Tests
Existing style-parametrized tests cover paper once it is in `STYLES` (every scene inside the safe zone, sourced
numbers, reveal timing). New: callouts stay above the caption zone; sticker outline and grain are deterministic;
the page transition is the previous frame at k=0 and the new one at k=1. Then render this week's Shorts in
paper and compare contact sheets against premium.


## PR #147 acceptance follow-up — 2026-10-25

Current main was merged into the existing `h/paper-style` branch without conflicts or rewritten history.
The final PR remains confined to the eleven Studio/design/font files; main's backend/frontend changes are
not part of its PR diff. Caveat and its OFL license remain together.

Design reconciliation:
- Target-ball scenes now place a miniature outlined player sticker in the top-right margin, aligned above
  the highlighted last column. It enters with that column's marker, outside the chart and title.
- Decision scenes now use the same small sticker above the question's right edge. Questions leaving fewer
  than 80 pixels of free header height intentionally omit it rather than collide with text or captions.
- Caveat notes intentionally call sourced `Frame.text` directly, instead of adding the proposed `note`
  wrapper: this already provides the intended typography, animation and claim provenance.
- The existing before/after direction arrows are retained. The curved amber arrow primitive remains
  available but is not added to scenes already carrying a loop, to keep the treatment restrained. These
  are explicit implementation deviations, not a claim of literal compliance with every proposed primitive.

Follow-up changes add the two mini stickers, constrain the new Broadcast big-compare change chip to the
right rail, and delay Paper pen starts until an explicit relevant claim token in player-card, split,
volume, lineup, teammate-out and status scenes. Scenes without that token keep their existing fallback.
Grid coverage now checks the latest six week/value associations using distinct values.

Verification (local Studio Python 3.12 environment; no new data, model or API calls):
- `python -m pytest tests/test_graphics.py tests/test_shorts.py tests/test_qc.py tests/test_stage_fixes.py -q`
  from Studio: 122 passed in 4.82 seconds (61 graphics tests). New sticker, chip and timing cases were
  observed failing before their fixes.
- `python fantasy-intel/scripts/agent_check.py affected --base origin/main`: static safety passes; this
  runner does not select Studio tests, so the explicit Studio command above supplies that coverage.
- `git diff --check`: clean. Final SHA CI remains the acceptance gate; no local platform suite was run.
- Frozen week-7 Allen, Bernard and Coleman episodes: Paper and Premium static VQA passes at frame zero,
  mid-scene and settled samples. Broadcast also passes the new big-compare regression after the chip fix.
- A 563-frame / 18.75-second Bernard Paper video passes all 14 existing styled QC gates, including audio,
  video, subtitle, safe-zone, contrast, overlap, sourced-number and dwell checks. Outputs remain local under
  `/tmp/fantasy-paper-147-acceptance/`; no generated episode, frame or video is tracked.
- Agent visual inspection covered Paper/Premium contact sheets for all three frozen stories, a synthetic
  nine-game grid and target-ball scene, and actual video frame zero/page-turn samples. Outlines and small
  stickers are clear, highlighted values readable at phone scale, captions stay above page turns, and the
  page exposes the next scene without a black flash. Owner motion/aesthetic sign-off remains outstanding.
- Independent diff-scoped review verified all six timing fixes and found no remaining actionable issues.

Verified baseline limitations:
- Coleman now passes `data_claims` on the frozen snapshot and uses “an ankle injury”. The historical grammar
  failure is not reproducible; no facts/story source was changed.
- Broadcast Bernard surname contrast (1.93:1) and Coleman decision text reaching x=984 (safe right=950)
  both reproduce with `origin/main`'s renderer. They are not new Paper regressions.
- Allen's archived original QC records repeated-story originality/creative failures. Styled QC does not
  evaluate those gates; it is not publishing approval.
- Facts, snapshot, story, script and narration inputs are unchanged, and caption cues match the archived
  original exactly. Same-process plans produce identical mixes/timelines/cues. Separate-process mixes and
  encoded audio are not byte-identical because pre-existing `audio_design.sfx` seeds with Python's salted
  string hash. This visual PR leaves that shared audio behavior unchanged and makes no cross-process
  byte-identity claim.

The original output path and style selection remain unchanged. Paper still writes
`styles/paper/final.mp4`; publication and merging require owner review.
