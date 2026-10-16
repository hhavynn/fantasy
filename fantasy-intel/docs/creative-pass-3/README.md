# Creative Pass 3: from a template to two formats

Two hero Shorts, built by a new editorial layer that picks a story shape from the data. Videos, scripts and
review pages are in `fantasy-intel/studio/episodes/` (git-ignored, reproducible). This folder holds the contact
sheets and this write-up.

| | Hero A: mystery | Hero B: opportunity |
|---|---|---|
| Video | `episodes/2026-w06-short-oronde-gadsden-ii/final.mp4` (22.0 s) | `episodes/2026-w06-short-braelon-allen/final.mp4` (24.3 s) |
| Alternate opening | `episodes/2026-w06-short-oronde-gadsden-ii-hook-question/final.mp4` | `episodes/2026-w06-short-braelon-allen-hook-count/final.mp4` |
| Review page (claims, storyboard, scores) | `.../oronde-gadsden-ii/review.md` | `.../braelon-allen/review.md` |
| Sheets | `oronde-gadsden-ii-first-3s.jpg`, `oronde-gadsden-ii-contact-sheet.jpg` | `braelon-allen-first-3s.jpg`, `braelon-allen-contact-sheet.jpg` |

Nothing here predicts views. Only real audience analytics can show whether these work.

## 1. Audit of the seven earlier renders

| # | Player | Storyline | Scene sequence | Opening | Ending |
|---|---|---|---|---|---|
| 1 | Fannin | vanishing_targets | card, split, volume, lineup, decision | "...is still on the field. So why did his targets dry up?" | "Riding it out, or benching him?" |
| 2 | Kelce | vanishing_targets | card, balls, split, volume, lineup, decision | *same line* | *same* |
| 3 | Allen | fill_in | card, compare, teammate×2, status, lineup, decision | "...role just jumped. But there's a catch." | "Would you pick up Allen, or wait?" |
| 4 | Gadsden | unresolved | hook, presence, bars, twist, statement, decision | "...is playing way more. His targets? All over the place." | "Is Gadsden on your roster?" |
| 5 | Cooper | fill_in | *identical to 3* | *same line as 3* | *same as 3* |
| 6 | Wilson | fill_in | *identical to 3* | *same line as 3* | *same as 3* |
| 7 | Kincaid | vanishing_targets | *identical to 2* | *same line as 1* | *same as 1* |

What repeated across the seven:

- **Scenes:** 6 of 7 open on the same player card. All 7 end on a mock lineup plus a two-button question.
- **Look:** one transition everywhere (the push-up), one camera move (a slow push-in), one music track (100 BPM, A minor), one sound set.
- **Narration:** "role just jumped", "there's a catch", "still on the field", "watch whether", "pick him up, or wait?". Most numbers were read out as "X to Y".

Root causes in the code:

1. **One storyline, one fixed beat list.** `story.compose` mapped each storyline to a fixed sequence, so three fill-ins could only ever be the same video.
2. **Hooks per storyline, not per story.** One to three hook lines each, rotated least-recently-used. With one fill-in hook, every fill-in opened identically.
3. **A shared ending.** The `lineup` + `decision` beats were appended to almost every storyline.
4. **The renderer had one grammar.** Every beat got its own scene with the same transition, camera, music and SFX. Nothing let a story change any of them.
5. **Selection had no editorial judgment.** It ranked by the size of the usage move, with no notion of hook, actionability or repetition.
6. **Numbers from different windows sat side by side.** Gadsden's old "8% to 15%" was a three-game mean against a two-game mean, while the brief's "26% to 5%" is Week 4 against Week 5. Neither was a like-for-like comparison.

## 2. What changed

### Editorial layer (`studio/editorial.py`, `studio/stages/formats.py`)

- **Formats chosen from the data.**
  - `mystery`: two consecutive games with the same playing time (within 4 snap-share points) where one game had at least 3× the other's targets, plus at least one clue the data can show.
  - `opportunity`: a running back whose teammate's absence opened the backfield (vacated, not returned, backfield share up at least 30 pts).
  - Everything else falls back to the earlier engine (`template`).
- **Each format owns its whole shape:**
  - opening and hook variants
  - beat order and voice
  - scene types and transitions (mystery: hard cuts; opportunity: whip pans and a "rewind")
  - camera (mystery: still with snap-ins; opportunity: drifting)
  - music mood (mystery: 84 BPM in D minor with a ticking clock and no backbeat; opportunity: 108 BPM, F–C–G–Am)
  - ending
- **The editorial board** scores every qualifying story 0–5 on the eight criteria, with a written reason for each (see `episodes/2026-w06-short/editorial.md`).
  - Gates: a total of at least 24/40, relevance and evidence of at least 2 each, and either a real hook (≥4) or a recognizable player. "Recognizable" means a first-round pick or a 5th-year+ veteran: a public, inspectable proxy.
  - Run on this week, the board picked Allen (39/40) and Gadsden (33/40) itself, ahead of 30+ template stories.
  - It rejected Cooper, Wilson and others as "no clear reason to care: a usage move without a twist, on a player few will know".
  - The weekly command now renders the board's top picks rather than the biggest usage moves.
- **Repetition checks in QC (originality gate):**
  - the same scene sequence 3 times in a row fails
  - the same opening sentence, with only names and numbers swapped, in 2 of the last 4 fails (the old fill-ins would all fail this)
  - the same hook family 3 times in a row still fails
  - the editorial distinctiveness score drops when a format runs 3 times in a row or a player repeats within 6 Shorts

### Data layer (`studio/stages/facts.py`, `snapshot.py`)

New claim kinds, each re-derived from the snapshot by QC:

- `usage_sum`
- `team_week_sum`
- `pooled_share`: his carries over the team's RB carries in the same weeks, so the shares match the counts shown
- `team_max_targets`, `players_targeted`
- `absent_together`
- `venue`
- `data_as_of`: the sync time, shown on screen

Every new detail is per game or summed over named weeks. None is a mean presented as a game. Per-game snaps, catches, air yards and RB carries came into the snapshot.

**Teammate back.** A same-position teammate who returned in the latest game after missing the game before it. This is reported side by side with the player, never as a cause.

### Rendering (`studio/format_scenes.py`, `render_motion.py`, `audio_design.py`)

- **Nine new scenes:**
  - mirror (numbers colliding)
  - footballs-as-targets
  - a face-to-face rival
  - a stamped verdict board
  - a giant number on frame 0
  - a carry grid (old split vs. new)
  - a handoff (the starter's carries arcing to the backup)
  - a dated injury-report sheet
  - an open window with the old split
- **Charts:** only the last scene uses a bar, because the old split is genuinely a proportion.
- **Per-scene control:** transition (`cut`, `whip`, `rewind`, `none`, `push`), camera (`still`, `drift`, `push`), and reveal cues keyed to the spoken word (via the TTS word timestamps).
- **Sound:** two new SFX (stamp, cut) and three music moods.
- **On-screen numbers are claims.** They're listed per beat, so the `data_claims` QC gate re-derives them along with the narration.

### QC and review

- **Timeliness gate:** a live injury status fails QC after kickoff, and synced data older than 7 days fails.
- **Each review page now includes:**
  - a timestamped storyboard
  - the hook variants
  - the editorial score breakdown
  - a "publish before kickoff" line
  - a source for every on-screen number

## 3. Hero A: Oronde Gadsden (mystery)

**Checking the brief's numbers first.** The 40% → 56% snap share is a mean of Weeks 1–3 against a mean of Weeks 4–5. The 26% → 5% target share is two single games. Those windows aren't comparable, so the script compares like with like:

| Week | Snaps | Snap share | Targets | Target share | Chargers targets | Catches |
|---|---|---|---|---|---|---|
| 4 (vs SEA) | **39** | 55% | **8** | 26% | 31 | 5 |
| 5 (vs DEN) | **39** | 58% | **1** | 5% | 21 | 0 |

**Context the data can show:**

- **Kolar:** Charlie Kolar (TE) missed Weeks 3–4 and was back in Week 5. He played 51 snaps (76%) and drew 4 targets.
- **Spread:** Quentin Johnston and Ladd McConkey also missed Week 5. Nine Chargers were targeted and nobody had more than 4. This is in the facts but not in the script, to keep it under 22 s.

None of this is claimed as the cause. Snap share counts run plays too, and route data isn't available in-season.

**Script (60 words, 22.0 s) and storyboard:**

| Time | Scene (in) | Said | On screen |
|---|---|---|---|
| 0.0–4.1 | mirror (cut) | Oronde Gadsden played the exact same snaps in back-to-back weeks. | Name, face and team from frame 0. "39" and "39" fly in and collide into "=" by 0.35 s. "TARGETS ? ?" from 0.9 s. |
| 4.1–6.0 | mirror, continued | 8 targets. Then 1. | The "?"s become 8 (white, 8 footballs) and 1 (red, 1 football). "≠" stamps. |
| 6.0–8.4 | Exhibit A (cut) | First clue: the Chargers threw way less. | 31 footballs, then 21 with 10 empty outlines and "−10". His targets in gold. |
| 8.4–14.4 | Exhibit B (cut) | Second clue: another tight end, Charlie Kolar, was back. More snaps than Gadsden, and 4 targets. | Two faces. "BACK · MISSED WEEKS 3 AND 4" lands on "back". 39 vs 51 snaps, then 1 vs 4 targets. |
| 14.4–17.8 | verdict board (cut) | But even with Kolar back, Gadsden's snaps didn't budge. | "SNAPS 39 · 39" stamped STEADY on "budge". "TARGETS 8 · 1" stamped SWINGING. |
| 17.8–22.0 | verdict call | Don't start him off one big week. Don't cut him off one bad one. | "HOLD HIM." / "DON'T START HIM YET." Fantasy Intelligence wordmark. |

**Opening variants:**

- `same_snaps` (used): the snaps collide first, then the targets.
- `question`: "Same snaps, back-to-back weeks. So why did Oronde Gadsden go from 8 targets to 1?" Rendered as the `-hook-question` file.
- `rhythm`: "39 snaps, 8 targets. 39 snaps, 1. Same tight end."

**Takeaway and its support:** "hold, don't start yet" rests on steady snaps (39, 39) and volatile targets (8, 1). It doesn't promise a breakout.

## 4. Hero B: Braelon Allen (opportunity)

**What the percentage measures:** designed rushes by him ÷ designed rushes by all Jets running backs (the repo's metric contract; scrambles excluded).

| Window | Allen | Hall | All Jets RB carries | Allen's share (pooled) |
|---|---|---|---|---|
| Weeks 1–3 (with Hall) | 19 | 51 | 70 | 27% (Hall 73%) |
| Weeks 4–5 (Hall out) | 38 | — | 40 | 95% |

**Pooled shares replaced per-game averages.** The old render showed 96% (the mean of 100% and 92%) beside "38 of 40", which is 95%. The screen now uses pooled shares everywhere, so the shares and the counts agree.

**Injury source:** the official report listed Hall Out with a quadricep injury in Weeks 4 and 5. Week 6 (the latest practice status in the data, synced Oct 15): did not participate.

**Expiry:** the Jets play at New England on Sun Oct 18, 13:00 ET. The video says "as of Oct 15", and QC fails it after kickoff. Re-check the final report before publishing.

**Script (67 words, 24.3 s) and storyboard:**

| Time | Scene (in) | Said | On screen |
|---|---|---|---|
| 0.0–3.9 | giant number (cut) | Braelon Allen just got almost every Jets running back carry. | "95%" slams in on frame 0 with a shake. "OF THE JETS' RUNNING BACK CARRIES", chip "WEEKS 4–5 · 38 OF 40", face beside the name. 38 footballs and 2 outlines fill while the line is spoken. |
| 3.9–6.2 | carry grid (whip) | 38 of 40 since Hall went out. | 40 large footballs, 2 empty. "38 OF 40". |
| 6.2–11.1 | old split (rewind) | Before that? This was Breece Hall's backfield. 51 carries to 19. | 70 footballs: 51 silver (Hall), then 19 gold (Allen) as "19" is said. Both faces with counts and names. |
| 11.1–13.6 | handoff (whip) | Then a quadricep injury kept Hall out. | Hall's face stamped OUT. "QUADRICEP INJURY · MISSED WEEKS 4 AND 5". His carries arc across to Allen. |
| 13.6–18.8 | report (whip) | As of Oct 15, Hall was not practicing, per the Week 6 injury report. | A report sheet slides up. The date underlines as it's said, then "DID NOT PRACTICE" stamps. |
| 18.8–21.1 | window (whip) | So while Hall sits, Allen's a starter. | "WHILE HALL SITS". Allen framed in an open window. "START ALLEN" stamp. "NEXT: AT PATRIOTS". |
| 21.1–24.3 | window, continued | When Hall's back, the only split we've seen is this one. | "WHEN HALL IS BACK". Both faces over a 73% / 27% bar. Wordmark. |

**Opening variants:**

- `giant` (used)
- `count`: "38 of 40. That's how many Jets running back carries went to Braelon Allen." Rendered as `-hook-count`.
- `changed_hands`: "The Jets backfield just changed hands. And it might not be for long."

**Takeaway and its support:** "start him while Hall sits" rests on 95% of RB carries with Hall out. "When Hall's back, the only split we've seen is 73/27" is the evidence the data has. It doesn't forecast.

## 5. Before and after

`before-pass2-gadsden-fannin.jpg` is the previous system. Gadsden and Fannin show the same card, the same chart vocabulary and the same lineup-plus-buttons ending.

| | Before | After |
|---|---|---|
| First second | A player card fading in, with the premise number arriving at 0.45 s | Mystery: 39 = 39 colliding by 0.35 s, with "?" targets teased. Opportunity: "95%" at full size on frame 0. |
| Shape | The same 6–7 beats for every story of a type | Clue-driven investigation vs. a timeline of the role changing hands |
| Look and sound | One background family, push transitions, one music bed | Case file / hard cuts / still camera / tense score vs. team field / whips / drift / bright score |
| Numbers | Read aloud as "X to Y". Means compared with single games. | The narration interprets and the screen carries the exact, like-for-like numbers. Shares match their counts. |
| Ending | Mock lineup plus "PICK UP X? ADD / WAIT" | A verdict the evidence supports: "HOLD HIM. DON'T START HIM YET." / "START ALLEN", plus the honest caveat |
| Same template twice? | Yes: Cooper and Wilson are the same video | No shared scene type, 0.0 narration 5-gram overlap, different palette, pacing, transitions and music |

Honest limits on the after: these are two formats, not a creative engine. A second opportunity story would follow the same shape, just as a second mystery would. The repetition gates will flag that, and more formats are the fix.

## 6. Export validation (ffprobe on the encoded files)

| | Codec | Size | fps | Audio | Duration | Loudness |
|---|---|---|---|---|---|---|
| Gadsden | H.264 High, yuv420p | 1080×1920 | 30 | AAC LC 48 kHz stereo, 192 kb/s | 22.0 s | −14.8 LUFS, peak −1.0 dBFS |
| Allen | H.264 High, yuv420p | 1080×1920 | 30 | AAC LC 48 kHz stereo, 192 kb/s | 24.3 s | −14.6 LUFS, peak −1.3 dBFS |

**About the 512×910 copies:**

- The production files have always been 1080×1920. Every earlier `final.mp4` on disk probes at that size too.
- The 512×910 / 30 fps copies you reviewed don't match anything the renderer writes. They match the preview transcode applied to files sent through the app.
- Use the paths above, or copy the files directly, for full quality.

**No low-res preview mode:** a full-resolution render takes about 6–10 s per Short, so a preview mode wouldn't save time.

**QC:** all 12 gates pass on all four renders, including the new timeliness gate and the data-claims gate, which re-derives every spoken and on-screen number.

**Safe zones:**

- All content sits between x 70–950 and y 230–1300.
- Captions sit at y 1325–1655 in the band shared by TikTok, Reels and Shorts, clear of the right-hand action rail.
- The story reads with sound off: captions plus on-screen numbers carry every claim.

**Checked by machine, not by ear:**

- caption timing, from the word timestamps
- loudness
- duration
- clipping
- pronunciations: the overrides are written for Oronde, Gadsden, Kolar, Braelon, Breece, Allen and quadricep, and no espeak fallback words remain

I could not listen to the audio. Please watch both with sound once.

## 7. Open concerns

- **Headshots** are ESPN photos used at your direction. They aren't licensed, and they show team logos on the uniforms. `headshots = false` brings back the jersey graphic.
- **Injury report styling:** the sheet is a styled restatement of the official report's data with no NFL marks. It's labeled with the sync date.
- **Allen, Week 4:** the Jets had only about 32 offensive snaps in Week 4 (Allen played 30 of them, 94%). The carry counts are real, but it was a short game. Small samples throughout: two-game windows.
- **Kolar's reason** for missing Weeks 3–4 isn't stated, because it isn't in the injury data for those weeks.
- **Time sensitivity:** Allen's video expires at kickoff on Oct 18, 1:00 pm ET, and QC enforces that.

## 8. Making the next one

First sync fresh data and the injury report (from `fantasy-intel/backend`), then render (from `fantasy-intel/studio`):

```bash
uv run python ../scripts/sync_nfl.py
```

```bash
STUDIO_DB=../data/fantasy.duckdb uv run make-episode --week 7 --format short
```

The weekly run scores every story (`episodes/2026-w07-short/editorial.md`) and renders the best that pass. The format is automatic:

- **Mystery** when two consecutive games share a snap share (±4 pts) and the targets swing 3×+, with a clue present: the team threw ≥20% less, or a same-position teammate came back with at least as many snaps.
- **Opportunity** when a running back's teammate is out and his backfield share rose 30+ pts.

To make one player's video: `--player <slug>`. To try another opening: `--hook <name>` (variants are listed in each review.md, e.g. `question`/`rhythm` or `count`/`changed_hands`). Read `review.md`, watch with sound, and publish before the kickoff time it lists.

**Natural next formats:** start/sit dilemma, breakout discovery, and a receiver version of opportunity, each with its own scenes, so the repetition gates have real alternatives to rotate through.
