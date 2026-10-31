# AI parody show

Short clips about the week's worst stat lines, cut with the studio's captions, Kokoro voice, sounds and pacing QC, and
ready to post within 48 hours of the game. Everything runs on this Mac for free. No paid service is used.

## The rules

- **No real person, ever.** No face, body, uniform, photo or team mark goes into a generated picture. The stat line is
  the character: a beach chair at the 50, a snail asking for a raise. Prompts come from a fixed bank in
  `stages/parody.py`, and `check_prompt` refuses a name, a team, or a word that points at a person. Headshots are off
  for this show whatever `[shorts] headshots` says. There is no switch for this. TikTok bans AI content that falsely
  shows public figures being bullied, even when labelled, and likeness law is moving the same way.
- **The joke is about the stat line.** The narrator speaks as itself and never quotes anyone. A player whose trend is
  an injury, or anyone near real trouble (`TROUBLE` in `stages/parody.py`), is never picked.
- **Every number is a claim**, drawn in code. Generated pictures hold no text.
- **Labelled everywhere:** an AI PARODY badge on every frame, the title, the description, every caption, and a tick
  for each app's AI setting in the upload checklist.
- **Every upload is a human decision.** A person approves every picture before QC passes. Nothing publishes.

## Each week

```bash
# after the Monday 15:00 PT refresh (Tuesday 08:00 PT for the Monday night game)
cd fantasy-intel/studio
uv run make-episode --week 8 --parody                 # up to three Shorts; QC holds them for your review
uv run make-episode --week 8 --parody --storyboard    # or just the pictures first, no narration or render
# look at each review_sheet.png, then
uv run parody-review --week 8 --slug parody-bo-nix --reject tiny_ball   # make one picture again
uv run parody-review --week 8 --slug parody-bo-nix --approve
uv run make-episode --week 8 --parody                 # QC passes, upload/ is written
```

A render takes about 4 minutes a Short, most of it making three stills. `--player bo-nix` makes one Short. `--trends bundle.json` (from `trends export` in `fantasy-intel/social`) lets what
people are talking about nudge the order; social numbers never reach the script.

## How a Short is made

```
flame.candidates -> parody.pick (trend nudge, trouble filter, entertainment test) -> joke + pictures from the banks
  -> frames (Z-Image-Turbo stills, Apple Vision subject masks) -> tts -> render (parody_motion: parallax, push-ins,
  a bump on the punchline word) -> QC -> your review -> upload bundle
```

| File | Job |
|---|---|
| `stages/parody.py` | Selection, joke and picture banks, prompt rules, script |
| `stages/frames.py` | Local stills through `mflux`, masks, `art/manifest.json` |
| `parody_motion.py` | In-house motion and the AI PARODY badge |
| `parody_qc.py` | The show's gates, the review sheet, `parody-review` |
| `parody_wan.py` | Optional overnight Wan clips |
| `vision.py`, `vision_text.swift`, `vision_mask.swift` | Apple Vision on device |

The joke banks hold three jokes per angle and the show covers two angles (`short_throws`, `rough_day`), both
quarterback flames. Team-level bad weeks are not built yet. Add jokes before the banks run out: the originality gate
blocks a near repeat.

## QC

On top of every series gate:

| Gate | Fails when |
|---|---|
| `ai_label` | OCR on one frame a second of the final video misses AI PARODY on any frame |
| `no_likeness` | A shot isn't generated art, a prompt names someone, or Vision finds a face or body |
| `clip_text` | OCR finds writing in a picture or clip. Strokes such as football laces (`1111/`) don't count |
| `clip_review` | A picture or clip isn't approved, or changed after approval |
| `ai_provenance` | Model, license, prompt or seed is missing, or the model isn't allowed |
| `ai_metadata` | The flag, the disclosure line or "AI Parody" in the title is missing |

A gate that cannot look fails. Vision can't see a soccer ball where a football should be, or a joke that doesn't land:
that is what the review is for.

## Optional: overnight Wan clips

Off unless asked for. `uv run parody-wan --week 8` animates two stills from each of that week's Shorts, hook first,
one clip at a time, and resumes after an interruption (`--slug parody-bo-nix` for one Short). `make-episode --week 8 --parody --hero` swaps finished clips in;
each clip then needs its own approval. A clip that fails leaves the still in place.

Measured on the 48 GB M5 Pro, 2026-10-31, on a handful of scenes:

| 3 s clip | Time | Result |
|---|---|---|
| 480x832, 20 steps (the setting used) | 7-8 min on a good run, 15.5 min under memory pressure | Cleanest |
| 480x832, 12 steps | 6.75 min | Painted numbers warped |
| 384x672, 12 steps | 4.6 min | Streaky smears |

Six clips (three Shorts) take about 45 minutes to 1.5 hours, or 2.5-5 hours with three tries each. Close heavy apps
first: swap was nearly full during the slow run, which took twice as long per step.
