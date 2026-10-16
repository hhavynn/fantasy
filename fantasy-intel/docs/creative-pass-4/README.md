# Creative Pass 4: sounding like a person, cutting like an editor

A focused polish pass on the two benchmark Shorts. It doesn't redesign the system, and it doesn't touch trade content.

| | Gadsden (mystery) | Allen (opportunity) |
|---|---|---|
| Final | `studio/episodes/2026-w06-short-oronde-gadsden-ii/final.mp4` (20.9 s) | `studio/episodes/2026-w06-short-braelon-allen/final.mp4` (20.7 s) |
| Alternate hooks | `...-hook-how/final.mp4`, `...-hook-cliff/final.mp4` | `...-hook-count/final.mp4`, `...-hook-hall_out/final.mp4` |
| Review page | `.../oronde-gadsden-ii/review.md` | `.../braelon-allen/review.md` |
| Sheets (this folder) | `oronde-gadsden-ii-first-3s.jpg`, `-contact-sheet.jpg`, `hook-*-first-3s.jpg` | `braelon-allen-first-3s.jpg`, `-contact-sheet.jpg`, `hook-*-first-3s.jpg` |

The production files are 1080×1920 H.264 at 30 fps, with AAC 48 kHz stereo audio. Loudness is −14.7 and −14.9 LUFS, peaking at −1.3 and −1.5 dBFS. Both pass all 12 QC gates. I left the resolution alone.

## 1. The five biggest weaknesses in pass 3, and what fixed them

These come from measurements on the pass-3 renders and frame-by-frame review.

1. **The narration sounded read, not spoken.**
   - Every gap between lines was exactly 0.25 s.
   - Sentence breaks inside a line had no pause at all ("8 targets. Then 1." ran straight through).
   - The payoff lines were the *fastest* at 4.1–4.4 words/s, so the conclusion was rushed.
   - The phrasing was report-like ("Second clue: another tight end, Charlie Kolar, was back. More snaps than Gadsden, and 4 targets.").
2. **The Gadsden hook split the contradiction across two lines.** The snaps landed at 0.3 s, but the targets didn't arrive until 4.1 s.
3. **Allen's opening held one number for about 4 s.** It named the player in the same breath, so nothing new happened after the first half-second.
4. **The endings restated the setup instead of answering it.** Gadsden's "hold, don't start yet" never answered "which game was real?". Allen's "window" pair said "start him while Hall sits" but showed no evidence for the other branch.
5. **Sound effects were wallpaper.** Pass 3 had 30 and 33 events (19 and 24 of them ticks), plus a whoosh or cut sound on every scene change.

## 2. Voice

**What Kokoro can and can't do (audited in `misaki` and `kokoro`):**

- **Usable controls:** voice choice, speaking speed per line, and punctuation (`.`, `,`, `?`).
- **Emphasis markup:** misaki's `[word](+2)` only changes *lexical stress*, not loudness or pitch. Tested and not used.
- **No emotion control:** there are no SSML-style emotion tags.

**One finding changed the approach.** Splitting a line into short separately-synthesized pieces makes Kokoro stretch each piece's last word. "weeks" came out at 0.55 s and "targets" at 0.70 s. That makes it sound *more* robotic. Planned pauses therefore only go between full clauses.

**What changed:**

- **Lines written for the ear:** contractions, questions where a person would ask them ("So which one's the real Oronde Gadsden?", "With Hall playing?"), and numbers only where the number is the point.
- **A rhythm system:**
  - Each line now carries its own `pause` after it (0.15–0.3 s mid-video, 0.6 s after the final line so the music can close).
  - An in-line pause marker `‖` (0.18 s plus the clause edges) goes only between full clauses.
  - The TTS stage synthesizes clause by clause and stitches the word timestamps, so captions stay in sync.
- **Pace:**
  - Speed is set per line: about 1.1–1.16 for explanation, about 1.08 for the payoff.
  - Measured rates now vary by role: 2.5–2.8 words/s on the hooks, 3.0–3.2 through the explanations, 3.1 on the verdicts, instead of the payoff being the fastest.
- **Peaks:** clip peaks are normalized, because Kokoro occasionally overshoots on a stressed word.

**The honest limit:** Kokoro-82M is a read-speech model. Better writing and timing take it from "an AI reading a report" to "a calm narrator talking to you". It can't produce real excitement, skepticism or comic timing. The next real step up is your own voice (the OpenVoice V2 path is already built). Any other paid or neural TTS would need your approval, plus a side-by-side test to justify it. I didn't add one.

**I could not listen to the audio.** Rates, gaps, loudness and peaks are measured. How it actually sounds needs your ears.

## 3. Hooks: three candidates each, scored

Scores are 1–5 and are my editorial judgment from the frame grids (`hook-*-first-3s.jpg`). They aren't audience data.

**Gadsden**

| Hook | Line | Clarity | Curiosity | Visual | Natural | Accuracy | Muted | Total |
|---|---|---|---|---|---|---|---|---|
| **`collide` (chosen)** | "Same snaps both weeks. 8 targets, then 1." | 5 | 5 | 5 | 4 | 5 | 5 | **29** |
| `cliff` | "Oronde Gadsden's targets just fell off a cliff. His snaps didn't move." | 4 | 4 | 5 | 3 | 4 | 5 | 25 |
| `how` | "How do you play the exact same snaps and go from 8 targets to 1?" | 4 | 4 | 3 | 4 | 5 | 4 | 24 |

- **`collide`:** 39 = 39 lands by 0.35 s, and "TARGETS ? ?" is teased at 0.9 s. The 8 and the 1 land as they're spoken, so the contradiction is complete by about 2.3 s.
- **`cliff`:** shows the whole contradiction fastest (about 0.8 s), but the voice never says the numbers, and "fell off a cliff" is a stock phrase. It's also slightly loose, since the 8 was the outlier.
- **`how`:** the slowest to land; the numbers arrive at about 2.4 s.

**Allen**

| Hook | Line | Clarity | Curiosity | Visual | Natural | Accuracy | Muted | Total |
|---|---|---|---|---|---|---|---|---|
| **`share` (chosen)** | "95% of the backfield?" | 5 | 5 | 5 | 5 | 5 | 5 | **30** |
| `hall_out` | "Breece Hall's out, and Braelon Allen's getting basically every carry." | 5 | 3 | 4 | 5 | 5 | 4 | 26 |
| `count` | "38 of 40 running back carries. One guy." | 4 | 4 | 3 | 4 | 5 | 4 | 24 |

- **`share`:** 95% sits on frame 0 with its denominator on screen ("OF THE JETS' RUNNING BACK CARRIES · WEEKS 4–5 · 38 OF 40"). Who it is comes in the next line, at 2.3 s.
- **`hall_out`:** clear, but it gives away the answer before raising the question.
- **`count`:** the voice says "38 of 40" while the screen leads with 95%, which splits attention.

**How hooks rotate:** a format always opens with its strongest hook unless that hook opened one of the last two Shorts (`formats.pick_hook`). Rotation happens only when repetition would show, never at random.

## 4. Final scripts and storyboards

### Gadsden: "Which one's the real Oronde Gadsden?" (20.9 s)

| Time | Scene (in) | Said | On screen |
|---|---|---|---|
| 0.0–3.3 | mirror | Same snaps both weeks. 8 targets, then 1. | Face, name and team strip. 39 and 39 slam into "=". "TARGETS ? ?" teased, then 8 (white, 8 footballs) and 1 (red, 1 football), and "≠" stamps. |
| 3.3–5.8 | which (zoom punch) | So which one's the real Oronde Gadsden? | His face, large, between "WEEK 4 · 8 TARGETS" and "WEEK 5 · 1 TARGET". A gold "?" lands on "real". |
| 5.8–10.2 | rival (cut) | Part of it? Charlie Kolar was back, out-snapping him and drawing 4 targets. | Two faces. "BACK · MISSED WEEKS 3 AND 4" on "back". 39 vs 51 snaps, then 1 vs 4 targets. |
| 10.2–14.4 | zoom-out (cut) | But zoom out. Before that 8-target game? 2, 2, 3. | The camera pulls back from Weeks 4–5 to the whole season. Weeks 1–3 (2, 2, 3) appear as they're said, over snap counts 10, 30, 39, 39, 39. |
| 14.4–20.9 | verdict | That 8 was the outlier. His snaps are locked in. The targets aren't. Don't chase the big week. | "OUTLIER" stamps over the gold 8. "LOCKED IN" boxes 39 · 39 · 39. "DON'T CHASE THE SPIKE" headline, a "5-GAME SAMPLE" note, and the wordmark. |

**The answer, and why it holds.** The question "is his opportunity disappearing?" was the wrong one. His targets were 2, 2, 3, 8, 1. The 8 is the exception, and the 1 is close to his normal. His snaps have been exactly 39 for three weeks (55–58%). The takeaway is qualified: don't chase the spike. The "5-game sample" note stays on screen.

Kolar's return is shown side by side with Gadsden and never claimed as the cause. The team-volume clue (Chargers targets 31 → 21) is still in the data and was cut for time.

### Allen: "95% of the backfield?" (20.7 s)

| Time | Scene (in) | Said | On screen |
|---|---|---|---|
| 0.0–2.3 | giant number | 95% of the backfield? | "95%" at full size on frame 0. "OF THE JETS' RUNNING BACK CARRIES", chip "WEEKS 4–5 · 38 OF 40". 38 footballs and 2 outlines fill in. No name yet. |
| 2.3–4.8 | carry grid (whip) | That's Braelon Allen, with Breece Hall out. | Face, name and team. 40 large footballs, 38 his. "38 OF 40". |
| 4.8–8.6 | old split (rewind) | With Hall playing? Allen got 27% of the carries. | 70 footballs: Hall's 51 in silver first, then Allen's 19 in gold as "27%" is said. Faces with 73% / 27% and the counts. |
| 8.6–15.1 | report (cut) | Hall's missed Weeks 4 and 5 with a quadricep injury, and as of Oct 15, he was not practicing. | A report sheet. The injury line appears as it's said and the date underlines. "DID NOT PRACTICE" stamps on "not practicing". |
| 15.1–20.7 | fork (whip) | If Hall sits again, Allen's an easy start. If he's back, expect the old split. | "WEEK 6 · AT PATRIOTS". Branch 1 on "sits": "START ALLEN · 95% OF RB CARRIES · WEEKS 4–5". Branch 2 on "back": "THE OLD SPLIT · ALLEN HAD 27% · WEEKS 1–3". Wordmark. |

**The answer.** It's conditional, and both branches carry their evidence.

**Status timing:**
- Hall's status comes from the Week 6 practice report in data synced Oct 15 (he was Out with a quadricep injury in Weeks 4 and 5).
- The video expires at kickoff on Sun Oct 18, 1:00 pm ET. QC fails it after that, and `review.md` lists the publish-by time.
- Re-check the final Friday report before posting.

## 5. Editing, visuals and audio

**Pacing.** Each line's pause is chosen for its role. The video ends 0.6 s after the last word, never on a held graphic.

| | Gadsden | Allen |
|---|---|---|
| Scene lengths | 3.3 / 2.5 / 4.4 / 4.2 / 6.5 s | 2.3 / 2.5 / 3.8 / 6.5 / 5.6 s |

The longest scenes keep changing as they're spoken: the zoom-out reveals the season and then stamps it, and the report reveals each line in turn.

**Transitions fit the moment:**

| Transition | Where | Why |
|---|---|---|
| Zoom punch | into "which one's real?" | a question |
| Hard cut | to evidence | |
| Whip | to the reveal and the call | |
| Rewind | to the old split | going back in time |
| None | inside a continuing thought | |

The two videos now share no transition sequence (Gadsden: zoom/cut/cut/none; Allen: whip/rewind/cut/whip).

**One focal point at a time:**
- The face is the focus at the question.
- The single season row is the focus at the zoom-out.
- Big text is reserved for key facts ("OUTLIER", "LOCKED IN", "START ALLEN"), and the narration's full sentences appear only in the captions.

**The endings are the payoff:** a verdict in Gadsden's case, a two-branch call in Allen's, and the Fantasy Intelligence wordmark only after it. Neither ends on a question card.

**New scenes:** `which`, `zoom_out` (the camera literally zooms out over his season) and `fork`. They're built on the existing renderer, with no new framework or dependencies.

**Sound:**
- 11 and 5 events, down from 30 and 33.
- Transition sounds are now opt-in per edit. Ticks mark only spoken numbers, stamps mark verdicts, a rise marks the zoom-out, and one impact opens Allen's video.
- The music is unchanged (procedural, a −28 dB bed ducked further under speech, a different mood per format) and still enough for energy.

## 6. Pipeline changes

**Script (`stages/story.py`, `stages/formats.py`):**
- `beat(..., pause=)` and the `‖` in-line pause.
- Rewritten mystery and opportunity scripts.
- `pick_hook` (strongest unless just used).
- `outlier_game`.
- The one-clue rule: a returning teammate beats one week of team volume.

**Voice (`stages/tts.py`):** clause-by-clause synthesis with planned silences, stitched word timestamps, and peak normalization.

**Facts (`stages/facts.py`, `numbers.py`):**
- per-game season detail (targets and snaps every week) for the zoom-out
- a dated short status claim (`status_brief`)

**Scenes and render (`format_scenes.py`, `render_motion.py`):**
- the three new scenes
- row-by-row reveals on the report sheet
- a hook that holds the player back
- labeled 73/27 shares
- the zoom transition, and opt-in transition sounds
- the format, editorial and scene code is hashed into the stage cache keys, so editing it always re-renders

**Editorial checks (`editorial.py`).** Eight pre-render questions with reasons (why care, surprise, timely, evidence, consequence, fits ~20 s, beyond a ranking, fresh angle). A script over 70 words, weak evidence, no consequence or a stale angle is rejected before rendering.

**Variation fingerprint.** Each Short records its opening, transitions, rhythm and ending in history. QC fails a third identical ending in a row, on top of the existing hook, scene-sequence and opening-sentence checks.

**Review page:** the checklist was added. Tests: 53 passing.

## 7. Honest before/after

**Better:**
- The first two seconds now carry the whole premise.
- The stories now *answer* their question: "the 8 was the outlier" is a real insight that pass 3 missed.
- The pacing breathes, the sound effects mean something, and the two videos look, sound and cut differently.

**Still machine-made:**
- **The voice:** good diction but no feeling. That's the main tell left.
- **The visuals** are clean motion graphics, but still clearly generated: no human face, no handwriting, no camera.
- **Variety:** two formats. A second mystery would follow the same shape as the first (the gates will flag it).

Viewers who know fantasy Shorts will still read these as polished infographic videos rather than a person's channel.

## 8. Remaining limitations

- I could not listen, so voice naturalness is untested by ear.
- **Headshots** are unlicensed ESPN photos with team logos on the uniforms (`headshots = false` turns them off).
- **Small samples:** two-game windows and a five-game season. The scripts and screen say so where it matters.
- **Gadsden's data runs through Week 5.** After Week 6 is played, the story needs a re-check.

## 9. Ready for real-world tests?

**Yes, for a small, careful first test,** after you listen to both with sound on.

- Post **Allen first, and before Sun Oct 18, 1:00 pm ET** (re-check Hall's Friday status), then **Gadsden**.
- Post the chosen hooks first. The alternates exist for A/B tests once there's baseline data.
- For a first read, track 3-second hold, average view duration and completion against each other. Only real analytics can show whether these work.
- **If the voice is what loses viewers,** recording your own voice (the OpenVoice V2 path is already built) is the highest-impact next step.
