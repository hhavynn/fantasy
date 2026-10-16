# Visual overhaul: from animated infographic to NFL edit

The meme-sound demos sounded like TikTok edits but looked like slideshows. This pass rebuilds the picture for
three formats (comparison, courtroom, main character) without changing a word of the scripts, the claims, or the
sound cues.

## Why the five meme demos were boring

The visual audit (`studio/visual_audit.py`) on the five latest renders, sampled at phone size with captions masked:

| Video | Compositions | Longest unchanged | Player (share of frame height) | Picture reacts to sounds |
|---|---|---|---|---|
| Bo Nix vs Seahawks (comparison) | 1 | 18.2 s (the whole video) | 0.06 | no (ratio 0.0–0.3) |
| Bo Nix vs Chargers (comparison) | 2 | — | 0.06 | no |
| Cam Ward (courtroom) | 4 | 7.2 s | 0.02 | only the gavels |
| Puka Nacua (main character) | 2 | — | 0.11 | weakly |
| Bo Nix (monologue) | 2 | 13.0 s | 0.05 | — |

- **One canvas per video.** The comparison was a single turf diagram that gained labels; the courtroom was
  paper documents swapped on one wood wall. Nothing a viewer would call a new shot.
- **No player.** The player was a 150 px circle in the corner, about 6% of the frame height. A Short about a
  person where the person is a thumbnail.
- **Sounds with no picture.** FAHHH and GET OUT fired over a frame that did not move.
- **Seahawks and Chargers were the same video.** Every Seahawks frame matched a Chargers frame (repeat 1.0).

## What is new

- `studio/edit_kit.py` holds the primitives:
  - **Cutout:** the player's transparent headshot as a big sticker with a white outline, drop shadow and
    team-colour rim. It can be cropped into a close-up, mirrored, or bottom-anchored so the flat edge stays off
    frame.
  - **Backdrops:** hype (team two-tone, light rays, halftone), roast, court and versus, each with moving grain.
  - **Hit effects:** slam (overshoot landing), sticker, comic burst, shake, flash.
  - **Freeze:** the hard stop drains the colour.
  - **`hits()`:** reads the beat's sound-cue anchors, so the picture hits on the same frame as the sound.
- `studio/edit_scenes.py` holds three visual languages:
  - **Comparison (`vx_`), a confrontation:**
    - a ruler measuring him snaps into a close-up as it shrinks to a stub;
    - two throw lanes on the same yard scale, his ball plopping at 3;
    - a passing-yards bar with only a sliver in the air, where he crashes in on FAHHH;
    - a scoreboard;
    - the empty deep field, where the bored safety is thrown out on GET OUT, then the colour drains.
  - **Courtroom (`cx_`), escalating parody:**
    - the defendant in the dock under a gavel slam;
    - a mugshot polaroid with a camera flash;
    - a conspiracy board with red string and a red circle on FAHHH;
    - an evidence avalanche that buries him;
    - the gavel winds up, then a cracked screen, GUILTY, jail bars and CASE CLOSED. The PARODY badge is on every
      scene.
  - **Main character (`hx_`), a hype edit:**
    - a giant bust over a giant outlined number that snaps to a close-up on his name;
    - a VS split against the opponent's whole offense;
    - stat slams that re-frame him each time (close / mirrored / mid);
    - one gold dot among 412 that opens into his face;
    - a poster.
- **Wiring:**
  - The builders in `stages/showrunner.py` swap scene types only. Text, claims and cues are unchanged, plus
    three facts the new scenes draw (the score's team names, his targets in the VS, the board's rank).
  - The edit scenes register in `flame_scenes.SCENES`, keep their cue sounds, and paint their own background.
  - The QC parody gate accepts `cx_` scenes.
- **Rule kept:** every number drawn is still a claim's display string. The ruler has ticks only; the dot grid has
  dots, not digits.

## After (same scripts, same sounds)

| Demo | Compositions | Longest unchanged | Player | Sync ratio at each sound (3+ = picture reacts) |
|---|---|---|---|---|
| Bo Nix comparison | 1 → 10 | 18.2 → 4.0 s | 0.06 → 0.43 | BRUH 46, FAHHH 73, GET OUT 7.7 |
| Cam Ward courtroom | 4 → 11 | 7.2 → 2.8 s | 0.02 → 0.28 | gavel 74, FAHHH 67, EMOTIONAL DAMAGE 60, gavel 11 |
| Puka Nacua main character | 2 → 13 | 8.6 → 2.6 s | 0.11 → 0.44 | Undertaker 40, Anime Wow 46, heavenly 35 |

Before, these sync ratios were all at or below 0.6, except the courtroom gavels. Near-duplicate frames against the
old version: 0.0–0.13. The player share is declared by each scene (`edit_scenes.HERO`), not measured from pixels. Treat it as the
designer's estimate.

## What the metrics do not say

- **Empty share** barely moved (0.73 → 0.61 for Bo). The dark roast backdrops read as flat at phone size. The
  frames are fuller than that number suggests, but the backgrounds are plainer than a real edit's footage.
- **Sync ratios** are frame-difference spikes. A shake on every hit scores well whether or not it's funny.

## Known limits

- **One photo per player.** Every shot is the same smiling ESPN headshot, re-cropped. Bo Nix grinning through his
  own roast undercuts it. Several expressions (more Commons photos, cut out) would fix it. See "Rights".
- **Captions collide with the player.** `M.CAPTION_Y` (1490/1920, about 77%) sits where the bottom-anchored
  player's chin and mouth are, and research puts the safe zone at middle to upper-middle. It's a global setting
  shared with every Short, so it's flagged rather than changed here.
- **Not yet given the new look:** the roast monologue, the fan meltdown, and the comparison's target-hog branch
  (`cmp_tally`) still use the old scenes.
- **Rendering cost:** about 45 ms a frame, versus about 25 ms before.

## Rights

The cutouts are ESPN headshots, cached locally and used at the channel owner's direction behind
`[shorts] headshots = true` (see `studio/headshots.py`). They are **not** cleared for commercial redistribution.
With the switch off, every scene falls back to the jersey graphic. Wikimedia Commons has CC-licensed photos of all
three players (for example `File:Bo Nix (cropped).jpg`, `File:Cam Ward 2024.jpg`, `File:Nacua 731 (cropped).jpg`).
Cutting them out needs a background-removal model, a large download that is waiting for approval. No
AI-generated faces are used.

## Monologue redesign (proposal, not built)

The deadpan monologue should stay still, but not empty:
- **0 s:** a phone lock screen fills the frame, with Bo as the contact photo and "AIRPLANE MODE" as the
  notification.
- **The stat:** the airplane-mode toggle flips and the whole screen goes grey.
- **"20 of 107 in the air":** a delivery-tracker UI with a parcel that moved 20 yards.
- **The closing line:** the phone screen cracks, then a hard cut to black.

That's one prop per joke, with the player as the contact photo.

## Seahawks vs Chargers

These are the same video with different numbers. Keep Seahawks (the more recent game, with the more extreme
air-yards stat), and skip Chargers or give it a different concept (a courtroom or a monologue). Both versions fail
the originality gate because the same player was featured back-to-back. That's an editorial call, left to the
owner.

## Reproduce

```bash
cd fantasy-intel/studio
uv run make-episode --week 7 --flame-praise --player bo-nix --treatment comparison --variant seahawks-visual --frozen-snapshot --compare-mix
uv run make-episode --week 6 --flame-praise --player cam-ward --treatment courtroom --variant visual --frozen-snapshot --compare-mix
uv run make-episode --week 6 --flame-praise --player puka-nacua --treatment main_character --variant visual --frozen-snapshot --compare-mix
```

`python -c "from studio import visual_audit as V; print(V.measure(Path('episodes/<id>')))"` audits a render, and
`V.contact_sheet(...)` builds a 12-frame phone-size sheet.
