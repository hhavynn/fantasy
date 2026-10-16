# Shows: different stories, different videos

The channel now has several shows instead of one template. The football question picks the show; each show has its own opening mechanism, beat order, dominant visual, pacing, music and ending. Work is on `feat/studio-shows`.

## Why the nine Week 6 Shorts felt identical

Checked against the scripts (`episodes/2026-w06-short-*/script.json`) and the source, not just the renders.

1. **Only two stories had their own shape.** `editorial.choose_format` knew `mystery` and `opportunity`. Everything else fell to `template`, and `story.compose` maps each *storyline* to a fixed beat list. Every falling target share with steady snaps became `vanishing_targets`, and every vacated role became `fill_in`.
2. **Fixed sentences, not just fixed beats.** The three `vanishing_targets` Shorts open with "X is still on the field. So why did his targets dry up?" and close with "2 games isn't panic time. Just watch: does his share get back toward N%?" then "Riding it out, or benching him?". The two `fill_in` Shorts open "X's role just jumped. But there's a catch." and close "Would you pick up X, or wait?". Only names and numbers change.
3. **The same scene sequence.** Kincaid and Kelce: `player_card > target_balls > split > volume > lineup > decision`. Fannin is the same minus `target_balls`. Cooper and Wilson: `player_card > presence > teammate_out > teammate_out > status_stamp > lineup > decision`.
4. **One ending card.** `lineup` ("the one number to watch") plus `decision` (two buttons) closed 5 of 9 videos.
5. **The originality gate couldn't see it.** `qc.gate_originality` only fails a hook, scene sequence or ending repeated *three times in a row*. Three near-identical videos made in one batch, interleaved with other formats, passed.
6. **No batch-level judgement.** Every qualifying story was made. Nothing asked whether a third declining tight end added anything.

Evans (dilemma), Strange (breakout), Gadsden (mystery) and Allen (opportunity) already had their own shapes. Those four worked.

## What was added

| Piece | Where | What it does |
|---|---|---|
| Shows | `studio/shows.py` | Three new families, each with its own rule on the facts (`red_flag_case`, `heist_case`, `myth_case`), opening mechanisms, beats and ending. The existing formats are registered as shows too (`detective` for mystery, `contingency` for opportunity). |
| Batch editor | `shows.plan_batch`, called from `cli.editorial_pick` | Gives each story the show that fits it best, never the same show twice in a week (a strong fit beats a weak one, then the editorial total breaks ties). A show used in two of the last three Shorts is skipped. A story whose show is taken by a stronger story is **deferred** with the reason, not forced into a different costume. Writes `shows.md` / `shows.json` next to `editorial.md`. |
| Endings | `shows.py` | Endings are mechanisms: **verdict**, **warning** (the one condition that changes the call), **takeaway** (a rule), **conditional** (the existing fork), **cliffhanger** (what will settle it). The heist picks verdict or cliffhanger from the injury status. |
| Openings | `shows.HOOK_MECHANISM`, `pick_mechanism` | Every opening key is classed by mechanism (warning, problem-first, direct, myth, question, contradiction, number, teaser). A show opens with the first mechanism not used in the last three Shorts. Problem-first reveals the player a beat later. |
| Creative fingerprint | `story.finish` | Every script's `structure` now records family, hook mechanism, ending mechanism, visual device, the question, the situation and the metrics on screen. These go into `history.jsonl` with the episode. |
| Creative QC | `studio/creative.py`, `qc.gate_creative` | **Measured:** fails a Short that shares 3 of 5 dimensions (show, opening mechanism, scene sequence, ending mechanism, beat progression) with any of the last 8 Shorts, uses more than 3 usage metrics, ends without a recommendation/condition/rule, or holds one chart over 9 s. **Judgement:** writes `creative_review.md` with the evidence and five questions for a person. Nothing claims to predict retention; `creative.RETENTION_FIELDS` names the analytics columns to join later. |
| Scenes | `studio/graphics/vector_shows.py` | Premium and broadcast designs for the new shows. The original renderer draws them in the premium look (`render_motion.draw_scene` fallback). Pixel has no designs for them yet, so `auto` uses premium. |
| Cheap previews | `studio/storyboard.py` | Builds a story's script as a given show (no audio, no language model), runs the claims, narration and creative gates, and draws a keyframe storyboard. Rendering happens only after these pass. |
| Experiments | `make-episode --player X --show red_flag` | Writes `<slug>-show-<name>` as a variant: production history isn't rewritten. |
| Music | `render_motion.MOOD` | red flag: tense; heist: bright; myth: drive. |

### The three new shows

| | Red Flag Report | Waiver Wire Heist | Myth vs Reality |
|---|---|---|---|
| Question | Should managers be worried? | Is this a pickup worth making before others notice? | Is the obvious reading wrong? |
| Rule on the facts | target share fell in every recent game; snaps within 5 points of before | a teammate's role vacated, not back; durable if he's on IR or ruled out | snaps up 3+ points, the team's targets higher in every recent game than any earlier one, his share down |
| Opening | warning, problem-first (player revealed second) or direct | the absence as an alert, or direct | the assumption as a question |
| Body | the strongest signal, what it means, what could reverse it | the opportunity, why it exists, the catch | three quick pieces of reality |
| Ending | warning: the condition that changes the call | verdict (durable) or cliffhanger (hinges on this week) | takeaway: a rule worth remembering |
| Visual | **one chart that evolves**: his share against his own baseline, then snaps beneath, then the light week marked, then the line where the flag comes down | alert, a 0-100% meter, the old split, his card dropping into a roster slot | no chart: huge stat tiles that stack, then the rule |
| Rhythm | slow, still camera, cuts only | fast whips, push camera | punchy cuts |
| Music | tense | bright | drive |

Every number is a claim placeholder; the claims, narration, subtitle and visual QA gates run unchanged.

## The nine examples, before and after

`nine-video-audit.md` has the full table. In short:

| Player | Before | After (Week 6 batch editor) |
|---|---|---|
| Braelon Allen | opportunity: number hook, carry grid, fork | unchanged (contingency); already its own shape |
| Mike Evans | dilemma: scale, weigh, tiebreak, tip | unchanged (lives in the uncommitted creative-pass-3 work, see below) |
| Brenton Strange | breakout: radar, lag, share split, lock | unchanged (same) |
| Oronde Gadsden II | mystery: mirror, which, rival, zoom out | unchanged (detective) |
| Dalton Kincaid | template: card > balls > split > volume > lineup > buttons | **Red Flag Report**, rendered |
| Harold Fannin Jr. | template: card > split > volume > lineup > buttons | **Myth vs Reality** (he plays more, the Browns throw more, his share fell), previewed |
| Travis Kelce | template, same as Kincaid | **deferred**: the same story as Kincaid this week, and Kincaid's is stronger (a second signal: air-yard share fell in every recent game too). Revisit next week. |
| Emanuel Wilson | template: card > presence > teammate > teammate > status > lineup > buttons | **Waiver Wire Heist** (Price on IR: a durable window), rendered |
| Darius Cooper | template, same as Wilson | **deferred**: the heist is Wilson's this week, and Cooper's window hinges on DeVonta Smith's practice status. If Smith is ruled out, Cooper qualifies (a cliffhanger heist). |

Seven Shorts, all structurally different, instead of nine where five were two templates.

## Outputs

`fantasy-intel/studio/episodes/experiments/shows-2026-w06/` (git-ignored, local):

| File | What |
|---|---|
| `2026-w06-short-dalton-kincaid-show-red_flag/styles/premium/final.mp4` | **rendered sample 1**, 27.4 s, QC passed (14 gates) |
| `2026-w06-short-emanuel-wilson-show-heist/styles/premium/final.mp4` | **rendered sample 2**, 26.8 s, QC passed (14 gates) |
| `2026-w06-short-harold-fannin-jr-show-myth/` | the third preview: `script.md`, `storyboard.png`, `gates.json` (all pass) |
| `*/script.md`, `*/storyboard.png`, `*/gates.json`, `*/creative_review.md` | each preview's script, storyboard, gate results and review prompts |
| `compare-<player>.png` | before (the template's scenes) over after (the show's storyboard) |
| `compare-dalton-kincaid.mp4`, `compare-emanuel-wilson.mp4` | the original premium video and the new show, side by side (new show's audio) |

The original episodes and their MP4s weren't touched.

## Tests and QC

- `uv run pytest -q`: **111 passed** (95 before; 16 new in `tests/test_shows.py`). The new tests cover:
  - each show's rule
  - the batch editor (the stronger fit wins, no repeats, deferral, the recent-history limit)
  - all four show scripts passing the claims and narration gates, at 75 words or fewer and with no more than 3 metrics
  - each show sharing at most 1 of 5 creative dimensions with the old template for the same story
  - the heist ending following the status
  - every show scene passing visual QA in premium and broadcast
  - the similarity checks
  - the original renderer drawing show scenes
- The cheap previews caught two real script problems before any audio was made: a number word outside a claim ("two games"), and "injured" said outside the status claim. Both were fixed by routing them through claims.
- Both renders pass all 14 styled-render gates: data claims, narration numbers, duration, audio, video, A/V length, subtitles, safe zone, text size, contrast, overlap, caption band, number provenance, number dwell.
- A fix found along the way: the styled-render cache key didn't include the show scene module; now it does.

## Remaining weaknesses

- **Kokoro reads names through the espeak fallback** (Emanuel, Jadarian, Seahawks in the heist), as in the original Wilson Short. Listen before publishing.
- **The red flag's single chart holds for about 20 s.** Each stage changes what's emphasised (the title, the snap strip, the light week, the threshold line), but a viewer may still find it static. That's a judgement call; watch it.
- **Pixel style has no designs for the new shows**, so they always render in premium (or broadcast if chosen).
- **Evans's and Strange's formats (dilemma, breakout) aren't on this branch.** They're uncommitted in the `youtube-studio` worktree, mixed into the same files as the FLAME/PRAISE work that was running there. The batch editor treats them as external shows by name. When that work is committed, they register in `shows.cases` the same way `detective` and `contingency` do.
- **Not built:** Buy-Low/Sell-High and League Winner Watch. There's no verified market data, and calling a league winner needs more than two games. The Strange breakout format already covers the breakout case.

## Three next improvements

1. **Commit the creative-pass-3 formats and register them** (`dilemma` as Start/Sit Showdown, `breakout` as League Winner Watch), so the editor sees all seven shows in one place.
2. **A Buy-Low/Sell-High show labelled as a model-based evaluation:** usage against the player's own baseline and positional peers, with no market claims until verified market data exists.
3. **Retention feedback:** once channel analytics exist, join them to the creative fingerprints (`RETENTION_FIELDS`) and report which shows, openings and endings hold viewers. Measure first; don't tune the editor on guesses.
