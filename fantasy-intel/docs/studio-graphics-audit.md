# Studio graphics audit

What the Shorts renderer draws today, why it reads as templated, and what to keep. Every finding was checked against the source on `feat/youtube-studio` (8f7e69d, the latest studio commit; `main` has no studio code) and against frames pulled from the rendered `final.mp4` of the Dalton Kincaid, Travis Kelce, Harold Fannin Jr. and Darius Cooper Week 6 Shorts.

Paths are relative to `fantasy-intel/studio/`.

## How a Short is drawn

| Step | Where | How it works |
|---|---|---|
| Story to scenes | `stages/story.py` `compose()` | Each narration beat carries a scene spec (`{"type": "split", "role": "twist", ...}`). Scene types come from a fixed menu per story angle. |
| Timeline | `stages/render_motion.py` `plan()` (split out in this branch from `_run`) | Kokoro word timestamps are aligned to display tokens (`align`), each claim keeps its start time, captions are cut into 2–5 word cards (`phrases`). |
| Scenes | `render_motion.py` (`scene_*`), `scenes.py` (story components), `format_scenes.py` (mystery/opportunity formats) | Each scene is a Python function drawing with Pillow onto a 1080x1920 frame, for every frame. Animation is `progress()` + easing, keyed to `Ctx.at(claim)` and `Ctx.at(word=...)`. |
| Look | `motion.py` | Constants (`INK`, `GOLD`, `UP`, `DOWN`), `TEAM_COLORS`, three backgrounds (`field_strip`, `team_backdrop`, `studio_backdrop`), cached text layers with blurred drop shadows, the jersey and headshot "portrait" badge, captions. |
| Player cards | `scenes.scene_player_card`, `motion.portrait`, `motion.jersey` | Team-colour gradient card with a secondary-colour outline, ESPN headshot inside a disc with a number pill, premise strip, a glint sweep. |
| Stats to graphics | scene functions | Numbers are always `c.d(claim_id)` (the claim's display string) or `numbers.render()` of a snapshot value. Charts are hand-drawn rectangles. |
| Team colours | `motion.TEAM_COLORS`, `team_colors()`, `vivid()` | Primary and secondary hex per team; `vivid()` swaps in gold when the primary is too dark. |
| Backgrounds | `motion.background()`, `format_scenes.background()` | Picked by scene type: turf, team stripes, studio grid, case file, alert. |
| Captions | `motion.caption_frame` | Inter Black 66 px, all caps, 9 px dark stroke, the spoken word in gold, numbers and emphasised words in the story colour, centred at y=1490. |
| Transitions | `render_motion._run` | Push with a 3-sample blur, whip, rewind, zoom, cut with a white flash; a whoosh on most scene changes. |
| A/V sync | `render_motion.align`, `audio_design.mix` | Word-level: a number lands on the frame its word starts; sound events come from the same times. |
| Presenter | `presenter.py` | Composites a separately rendered host over windows of the finished `final.mp4`; it never touches the scene code. |
| Consistency guard | none | No shared tokens, no layout grid, no visual test. QC checks captions, black and frozen frames, A/V length; nothing checks layout. |

## Why it reads as generated

| # | Finding | Evidence | Effect |
|---|---|---|---|
| 1 | **Every scene changes the whole world.** The background is chosen by scene type, so one 25 s Short cycles turf → studio grid → studio → team stripes → studio → studio. | `render_motion.SCENES` (`"hook": ("field", ...)`, `"volume": ("team", ...)` and so on), `scenes.STORY_SCENES` | The video looks like six templates stitched together, not one designed piece. This is the strongest "assembled" tell. |
| 2 | **Backgrounds compete with the data.** Turf carries giant yard numbers and hash marks; the team backdrop has 90 px diagonal stripes plus a secondary-colour slash across the lower third. | `motion.field_strip` (yard numbers at alpha 22 behind charts), `motion.team_backdrop` (`polygon ... secondary + (90,)`) | In the Kincaid "Bills targets per game" frame a red slash runs behind the caption and the bars; the turf numbers sit behind the dot grid. |
| 3 | **One voice for all type.** Almost everything is Inter Black in capitals: titles, labels, numbers, week ticks, chips, captions. | `motion.put_text` defaults to `weight="black"`, scene calls rarely pass another weight | No hierarchy: the eye can't tell the headline from the axis label except by size. All-caps black is the default "AI explainer" look. |
| 4 | **No spacing system.** Positions are hand-picked per scene (330, 640, 860, 1110, 1330...). Some scenes are centred, some left-aligned, and margins vary (70, 90, 170). | `scene_big_compare`, `scene_twist`, `scenes.scene_split`, `scenes.scene_lineup` | Elements jump position between cuts; nothing lines up across scenes. |
| 5 | **Decoration standing in for design.** A glint sweeps the player card; every text layer has a 10 px blurred drop shadow; stamps are rotated 4–6°; option buttons pulse; held elements wobble. | `scenes.sweep_band`, `motion.text_layer(shadow=True)`, `stamp.rotate(...)`, `scene_decision` `pulse`, `motion.wobble` | These are the tells of template packs. Blurred shadows on a flat dark background also soften the type. |
| 6 | **Everything bounces.** `back_out` (overshoot 1.6) is the entrance for headlines, numbers, chips, stamps and buttons alike. | `motion.back_out` used in nearly every scene | Primary reveals and secondary labels move the same way, so nothing feels more important. |
| 7 | **A hidden, inconsistent scale.** In the split scene the target-share bar is drawn with "40% of targets = full bar" while snap share in the same frame is 20 dots of 5%. Neither scale is shown. | `scenes.scene_split`: `w = max(64, 520 * frac / 0.4 * grow)` | Two encodings of the same unit (a share) in one frame, one unlabelled. 21% fills half its track, which reads as "about half". Not wrong data, but a misleading picture. |
| 8 | **Filler.** The lineup scene draws six grey placeholder bars for the other slots; the decision scene is two buttons on an empty set. | `scenes.scene_lineup`, `render_motion.scene_decision` | Large areas carry no information; the eye scans for meaning and finds none. |
| 9 | **The whole frame is resampled every frame.** A 3.5% push-in plus punch-ins resizes 1080x1920 with bilinear filtering on every frame. | `render_motion._camera` | Text and hairlines are never pixel-aligned, so small type shimmers slightly. |
| 10 | **Two unrelated palettes.** The long-form charts use `visual.py` (`INK #e9eef4`, `CAUTION #f2bd52`); Shorts use `motion.py` (`INK (240,244,248)`, `GOLD (255,205,64)`); `format_scenes.py` adds teal, red, paper and silver. | `visual.py`, `motion.py`, `format_scenes.py` | No channel identity across formats. |
| 11 | **Contrast and safe zone are unchecked.** Team colours are used raw for cards and backdrops; `vivid()` only swaps very dark primaries. Nothing measures text against what is behind it, or text against the platform UI zone. | `motion.vivid`, no layout QA in `stages/qc.py` | The new visual QA found real cases in first drafts of the new styles (red numbers on green turf have equal luminance; labels under the right-hand action rail), so the old renderer is likely to have some too. |
| 12 | **The caption gate measures mixed case; captions are burned in capitals.** | `motion.caption_frame` uppercases; `qc.gate_subtitles` wraps `cue["text"]` as written | A latent gap: capitals in Inter Black are wider. Re-measured in capitals, none of the 18 existing Week 6 Shorts overflow today, so nothing is broken yet. The styled renders record `"case"` in `captions.json` and the gate now honours it. |

Things that were **not** problems: frame 0 always shows the player and the stakes; numbers land on the frame their word is spoken; transitions are short (0.2 s); captions fit; nothing fabricates a number.

## What to preserve

- **The data path.** Every on-screen number is a claim display string (`c.d`) or `numbers.render()` of a snapshot value, and QC re-derives claims from the snapshot. The new styles use exactly the same path, and the new visual QA checks it per drawn number.
- **Word-level timing.** `align` / `Ctx.at` / `phrases` are the best part of the renderer. The styles reuse `plan()`, so a styled video has the identical timeline, captions and audio.
- **Stage caching and provenance**, the music and sound design (`audio_design.py`), captions as data (`captions.json`), the AI disclosure in metadata, the presenter layer.
- **Story-driven scene choice.** The scene menu per angle (`story.compose`) is sound; the problem is how scenes are drawn, not which ones.

## Recommendations, and what this branch did

| Recommendation | Done here |
|---|---|
| One background per style for the whole video; data never sits on texture | Yes. Premium: one dark set with a single soft key light. Broadcast: one deep navy set with faint team bands. Pixel: turf frames the screen, but data always sits on menu panels. |
| Design tokens (colour, type, spacing, stroke, radius, motion, captions, charts) in config, shared by every scene | Yes: `assets/styles/{premium,pixel,broadcast}.toml`, loaded by `studio/graphics/kit.py`. |
| A type hierarchy: kicker, label, title, display, three number sizes, with weights and tracking | Yes (`[type]` in each token file); cap-height alignment in `kit.text_layer`. |
| Easing by role: entrances settle without overshoot; reserve emphasis for the reveal | Yes: cubic-bezier curves per style (`kit.bezier`); no `back_out` in the new styles. |
| Charts with honest, visible scales: zero-based axes, slope chart for before/now, a shared 0–100% scale for meters | Yes: `vector.column_chart`, the slope chart in `vector.scene_split`, `vector.track`, 5%-per-segment pixel meters. |
| Remove filler (placeholder bars) and decoration (glints, blurred shadows, rotated stamps, wobble, pulsing) | Yes in the new styles. |
| Measure layout: safe zone, text size, contrast, overlap, number provenance, number dwell time | Yes: `studio/graphics/vqa.py`, run as QC gates on every styled render. |
| Keep the original renderer as the default and as a fallback | Yes: `final.mp4` is unchanged; styles render to `styles/<style>/final.mp4`. |
