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
