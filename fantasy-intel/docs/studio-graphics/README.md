# Studio graphic styles

Three designed looks for the Shorts, built on one component library, rendered next to (never instead of) the original video.

| Style | Token file | Idea |
|---|---|---|
| **Premium analytics** (`premium`) | `studio/assets/styles/premium.toml` | Editorial data journalism in the Fantasy Intel website's own design system: the Night theme colours, the accent blue as the one "look here" colour, Barlow Condensed for titles and numbers, IBM Plex Sans for text, and the site's "fi" tile and two-colour wordmark in the header. Real charts (column chart, slope chart, scaled tracks), sentence-case type with a clear hierarchy. |
| **Retro pixel football** (`pixel`) | `studio/assets/styles/pixel.toml` | A 16-bit sports game. Everything is drawn on a 216x384 art grid and scaled x5 with no smoothing: an original 5x7 pixel font, a player sprite in team colours with his number, footballs stacked one per target, 5%-per-segment meters, menu windows and a dialog box for captions. |
| **Modern broadcast** (`broadcast`) | `studio/assets/styles/broadcast.toml` | An original studio-show identity. Navy set with faint team bands, slanted team-colour slabs, a score bug, a lower third, scoreboard-style before/after modules, capitals with broadcast italics, a slab wipe between scenes. |

The original renderer stays the default. Nothing here changes `final.mp4`, the long-form video, the facts, the script, the narration, the audio, captions timing, the metadata, the thumbnail or the presenter.

## Where the comparison videos are

`fantasy-intel/studio/episodes/graphics-review/` (git-ignored, local only). One folder per player:

| File | What |
|---|---|
| `compare_2x2.mp4` | **Start here.** Original, premium, pixel and broadcast side by side at phone size, with the shared audio |
| `original.mp4`, `premium.mp4`, `pixel.mp4`, `broadcast.mp4` | the four full videos (same narration, timing, music and captions) |
| `contact_sheet.png` | one settled frame per scene, per style |
| `frames-<style>/` | the frames the visual QA measured (each scene at 45% and 97%) |
| `qc-<style>.json`, `render-<style>.json` | the styled render's QC gates and render timing, with the token file's hash |
| `dalton-kincaid/motion_sheet.png` | the first frames and mid-transition frames |

Players: Dalton Kincaid (vanishing targets, the main comparison), Travis Kelce and Harold Fannin Jr. (same story type, different data and team colours), and Darius Cooper (a fill-in story with different scenes: snap-share reveal, the injured starter's grid, the injury status).

The source videos stay where they are: the styled renders live in each episode folder at `styles/<style>/`.

## Using a style

```bash
uv run make-episode --week 6 --format short --style premium
```

`--style` takes `original`, `premium`, `pixel`, `broadcast` or `auto` (the configured default since 2026-10-15). Any value other than `original` renders the normal Short as before, then also renders `styles/<style>/final.mp4` with its own `qc.json`. The default lives in `config.toml`:

```toml
[graphics]
style = "auto"               # original | premium | pixel | broadcast | auto
auto_default = "premium"     # every story angle, unless overridden below

[graphics.auto]              # per-angle overrides, e.g. fill_in = "broadcast"
```

`auto` picks by story angle and falls back to `original` when the chosen style has no design for one of the story's scenes. Every scene type has a design in all three styles, the mystery and opportunity formats included, so in practice `auto` uses `original` only for an angle with no preference. Mystery and opportunity have none yet; to style them, add a line each under `[graphics.auto]`, for example `mystery = "premium"` and `opportunity = "broadcast"`.

### Caching

The styled render is its own cached stage, `render-<style>`. Its key is the script, facts, story and narration audio, the style's token file, and the code of the graphics modules. Changing a style re-renders only that style: facts, script, narration and the original render are skipped (checked: a second run logs `unchanged, skipped` for every stage).

### Customising a style

Edit its token file and re-run; the cache notices the change.

| Section | Controls |
|---|---|
| `[palette]` | every colour. Premium and broadcast also take team colours from `studio/motion.py`. Pixel has 14 fixed colours plus two team slots, swapped per team like the 16-bit games did |
| `[type]` | `[weight, px, tracking]` per role: kicker, label, body, title, display, number_xl/l/m/s. `case` is `title` (sentence case, names kept capitalised) or `upper`. Broadcast's `italic` is the shear for display type |
| `[space]` | 8 px grid, left margin, right margin inside the platform's side rail, content top and bottom |
| `[stroke]`, `[radius]` | hairlines, rules, corner radius. Broadcast's `skew` is the slant of its panels |
| `[motion]` | cubic-bezier curves (`enter`, `move`), entrance length, stagger, transition type and length, camera push, punch-in |
| `[caption]` | caption weight, size, case, colours, the dark band behind premium captions |
| `[chart]` | column gap, value and axis sizes, gridlines |

Layouts that depend on structure (where a chart sits, what a scene shows) live in `studio/graphics/vector.py` (premium and broadcast) and `studio/graphics/pixelart.py`.

## Components

`studio/graphics/` holds the library. Each piece reads the player's facts and claims, so it works for any player; nothing is hard-coded to one story.

| Component | Where | Used in |
|---|---|---|
| Design tokens, cubic-bezier easing, tracked and tabular type with cap-height alignment, `Frame` with a draw log | `kit.py` | everything |
| Look (panel shape, case, italics, data colour, team colours) | `vector.Look` | premium, broadcast |
| Chrome: header line (premium); score bug and lower third (broadcast); HUD (pixel) | `vector.chrome`, `pixelart.hud` | every scene |
| Player header: cut-out on a team plate or slab, name, position and team | `vector.scene_player_card` | hook |
| Before and after, labelled with the weeks each number covers | `vector.scene_player_card`, `scene_volume` | hook, volume |
| Column chart on a zero-based axis, values on the bars, reference "pace" line, "since" bracket | `vector.column_chart` | targets per game, team targets |
| Slope chart: two metrics, before and now, on one shared axis | `vector.scene_split` | the twist ("same field time, fewer targets") |
| Scaled track (0 to a round number) with fill and markers | `vector.track` | lineup "what to watch", snap share |
| Teammate grid (out weeks against his weeks) | `scene_teammate_out` | fill-in stories |
| Status card | `scene_status_stamp` | fill-in stories |
| Lineup strip and decision card; question and options | `scene_lineup`, `scene_decision` | takeaway, question |
| Pixel font, player sprite, football sprite (leather, gold for the story player's own, silver, outline), turf, segmented meter, menu window, dialog box | `pixel.py`, `pixelart.py` | pixel |
| Format scenes (mystery and opportunity): see below | `vector_formats.py`, `pixel_formats.py` | all three |
| Captions per style | `vector.caption`, `pixelart.caption` | every frame |
| Transitions: slide and fade (premium), slab wipe (broadcast), stepped palette fade (pixel) | `vector.transition`, `pixelart.transition` | scene changes |

### Format scenes

The editorial formats (`studio/format_scenes.py`) tell one football idea per scene. Each has a design in every style, keyed to the same narration cues as the original, so a number still lands as it is said.

| Scene | Premium and broadcast (`vector_formats.py`) | Pixel (`pixel_formats.py`) |
|---|---|---|
| `mirror` | two games side by side: the snaps land with an equals sign, the targets stay "?" until each is said, then a crossed equals; one square per target on one scale | two score panels, "=" and "≠" tags, one football per target |
| `which` | his picture between two cards (week, targets); "Which one is real?" lands on "real" (broadcast: on a highlight slab) | his sprite, the question in the title bar, a "?" block |
| `rival` | the returning teammate head to head: names, "Back" and the weeks he missed, then snaps and targets as numbers over a bar pair on one axis (zero at the centre line) | two sprites, a stat panel with the winner in gold |
| `zoom_out` | his season as a zero-based column chart; on "zoom" the chart widens from the two games everyone saw to every game. The verdict marks the outlier, boxes the snaps that never moved ("Locked in"), and the call replaces the title (broadcast: a slab) | footballs stacked one per target, the same widening in whole art pixels, an "OUTLIER" tag and a "LOCKED IN" frame |
| `volume_balls` | the team's targets per game as columns with his targets inside each column | two rows of footballs, his in gold |
| `verdict` | steady against swinging, game against game, then the call as sentences | two panels with tags, the call in boxes |
| `giant_number` | the share huge on frame 0, what it measures, then one square per carry filling as the line is said | the share at x7 in a night panel, a field of footballs |
| `carry_grid` | one square per carry: his now (filled) against other backs (outlined); or the old split, the starter's squares neutral and his in the data colour, with both shares and counts | the same as footballs (his in gold) and 5%-per-segment meters |
| `handoff` | two pictures, the starter dimmed with "Out", the injury and the weeks missed, an arrow to the backup | two sprites (the starter greys out), footballs arcing across, a dialog box |
| `report` | the injury report as a dated card: missed, injury, practice status (in the down colour), "As of" and "check the final status before kickoff" | a paper report with a stamped status block |
| `window` | "While he sits" over his picture with the call on a chip; when he is back, the only split seen with him on a 0–100% bar | a window opening on his sprite; two meters |
| `fork` | the call both ways as two branch cards, each with its share on a 0–100% track and the weeks it covers | two menu boxes with a cursor and meters |

Premium never stamps (the call is a sentence in the title type); broadcast puts calls on slabs, and big cards get a gentle slant with their colour bar outside the left edge so it never runs under type. The chrome names the show by format: "The case file" for mystery, "Opportunity watch" for opportunity. Squares and footballs are drawn from the claims' own values (38 of 40 carries is 38 filled and 2 outlined; a test checks it), and a derived number the original drew (the "−10" gap, "WEEK {n+1}", the "5-game sample") is replaced by claim numbers or left out.

### Accuracy rules the components follow

- Every number on screen is a claim's display string (`c.d`) or `numbers.render()` of a snapshot value, exactly as in the original renderer, and every draw call logs where its number came from.
- Every chart axis starts at zero. Meters and tracks show their scale (`0%`, `100%`, or 5% per segment). Before and after labels say which weeks they cover, read from the claim's own source weeks.
- No count-up animations: an in-between number would be a number nobody claimed.
- The original's split scene drew target share on a hidden "40% = full bar" scale next to snap share on a 5%-dot scale. The styles replace it with a slope chart on one shared axis.

## Visual QA

`studio/graphics/vqa.py` runs as QC gates on every styled render, next to the usual data, narration, duration, audio, video, A/V and subtitle gates. It measures the draw log and the captured frames instead of judging by eye.

| Gate | Fails when |
|---|---|
| `vqa_safe_zone` | any text is outside the mobile safe zone (x 70–950, y 230–1540) |
| `vqa_text_size` | cap height is under 20 px for labels, or under 30 px for numbers, titles and captions |
| `vqa_contrast` | WCAG contrast of text against what is under its letters is under 4.5:1 (3:1 for caps of 40 px or more) |
| `vqa_overlap` | two texts in a settled frame overlap |
| `vqa_caption_clear` | scene text reaches into the caption band, where the captions would hide it |
| `vqa_numbers` | a number has no source, or a claim-sourced number doesn't show that claim's display string |
| `vqa_number_dwell` | a claim number is fully readable for less than 0.6 s |
| `vqa_pixel_grid` (pixel) | any 5x5 block of a captured frame is not a single colour, which would mean smoothing |

The gates earned their place on the first renders. They caught red numbers on green turf at equal luminance (contrast 1.0), labels under the right-hand action rail, a long name ("Fannin Jr.") running off the card, an "88%" wider than its cell, close values whose labels collided (14% and 21%), three-line captions running into the UI, and (after a frame review the gates had missed) a stamp sliding under the pixel dialog box, which is why `vqa_caption_clear` exists. Reviewing frames by eye also caught "JR." standing in for a surname and a pixel dissolve that looked like signal corruption; both are fixed, and the surname case has a test. Each was fixed in the design, not by loosening a gate.

The format scenes added their own catches, each fixed in the design: a "8 to Endman" label off the right edge, footballs dropping through the count above them (contrast 1.4), the Chargers' blue at 4.47:1 under white type in the broadcast score bug (team slabs now darken just enough to reach 4.5:1, for every scene), a long name pushing the broadcast lower third past the safe zone (the name now steps down a size), and the pixel caption's "more" marker sitting outside the safe zone.

The subtitle gate also learned two things: it honours `captions.json`'s `case` (the burned-in captions are capitals), and it measures the pixel font.

## Comparison report

Same four Shorts, same narration, music, sound effects, timing and captions. Measured facts first, then judgement, labelled as such.

### Measured

| | Original | Premium | Pixel | Broadcast |
|---|---|---|---|---|
| QC (all 4 players) | passed (Kincaid re-checked) | passed, 14 gates each | passed, 15 gates each | passed, 14 gates each |
| Numbers checked against claims (Kincaid) | not measurable (no draw log) | 86 | 71 | 86 |
| Worst text contrast | not measured | 5.5:1 | 4.7:1 | 3.3:1 (large type on team slabs) |
| Render time per Short (all 4 players) | 15–20 s | 6–8 s | 4–6 s | 13–15 s |
| File size (Kincaid) | 7.8 MB | 2.4 MB | 2.3 MB | 7.3 MB |
| Backgrounds per video | 3–5 different | 1 | 1 | 1 |
| Typefaces and weights | Inter Black almost everywhere | the site's Barlow Condensed and IBM Plex Sans, by role | original 5x7 bitmap font | Inter Black and Bold, italic display |
| New dependencies | — | none | none | none |

Render times are on the M5 Pro, single process. Broadcast is slower because of its camera push (a full-frame resize per frame). Premium has none on purpose, so type and hairlines stay pixel-sharp.

Format Shorts (Week 6, measured on the styled renders; both pass QC in every style):

| | Premium | Pixel | Broadcast |
|---|---|---|---|
| Oronde Gadsden II (mystery): numbers checked, worst contrast, render time, size | 94, 5.7:1, 7 s, 2.1 MB | 94, 5.7:1, 6 s, 2.1 MB | 94, 3.3:1, 17 s, 6.9 MB |
| Braelon Allen (opportunity) | 48, 5.7:1, 9 s, 2.1 MB | 51, 5.3:1, 5 s, 2.2 MB | 48, 3.3:1, 13 s, 6.7 MB |

### Judgement (subjective; watch `compare_2x2.mp4` and decide)

| Criterion | Original | Premium | Pixel | Broadcast |
|---|---|---|---|---|
| Looks designed rather than generated | weakest: changing backgrounds, glints, rotated stamps and bouncing make it read as a template pack | strongest: restraint, hierarchy and real charts read as a data desk's work | strong, and the most distinctive; hard-edged sprites and a real bitmap font avoid the fake "AI pixel" look | strong; reads as a TV package, though the faux italic of Inter is a compromise |
| Readability on a phone | good (big type) | good after the size pass; the quietest, so the smallest labels matter most | very good: chunky type, high contrast panels | very good |
| Data clarity | fair: hidden scales, two encodings for a share | best: zero-based axes, the slope chart makes the twist obvious | good: 5% segments and one football per target are literal and honest | good |
| Entertainment value | energetic but samey | calm; may feel slow for meme-style content | highest novelty; fun without being childish because the data panels stay serious | high energy, familiar |
| Motion quality | lots of bounce and shake | smooth settles, no overshoot; possibly too still for some viewers | stepped, deliberate, period-correct | fast, punchy slab wipes |
| Visual consistency across scenes | low | high | high | high |
| Reusability | per-scene code | shared components | shared structure, own drawing | shared components |
| Production efficiency | baseline | fastest vector style | fastest overall | slowest new style, still under 20 s |

My recommendation, for you to confirm by watching: **premium** for usage deep dives (the `vanishing_targets` stories), **broadcast** for weekly reports and fill-in or injury stories, and **pixel** as an occasional change-up for head-to-head or fun stories rather than the default, since its novelty is its strength. Since 2026-10-15 the channel runs **premium for every story it has a design for**, so the Shorts and the website read as one brand; broadcast and pixel stay available through `--style` or a per-angle override. A story with a scene premium has no design for falls back to original (in Week 6: Mike Evans and Brenton Strange, whose newer scene types aren't on this branch). The weekly `review.md` index lists each Short's styled video and its QC next to `final.mp4`. `final.mp4` is still rendered, and metadata, thumbnail and upload still point at it, so when publishing a styled Short, upload `styles/<style>/final.mp4` by hand.

## Limitations and known issues

- **Long-form charts not covered.** The long-form video's charts (`stages/charts.py`) have no styled versions. Every Shorts scene, including the mystery and opportunity formats, does.
- **Typeface.** Inter is the only licensed face in the repo. Broadcast's italics are a shear, not a drawn italic, and a condensed sports face would suit it better. Adding one (for example an OFL condensed sans) is a small download that needs your OK.
- **Sprites are procedural.** The player sprite is built from shapes at native resolution and outlined; it's on-grid and clean, but a hand-drawn sprite sheet (Aseprite) would have more character.
- **Headshots.** Premium and broadcast show the ESPN headshot when `[shorts] headshots = true`, with the same rights caveat as the original renderer; pixel never uses photos.
- **Presenter.** The AI presenter still composites over the original `final.mp4`. The styled videos have the same timeline, so pointing the presenter at `styles/<style>/final.mp4` is a small, separate change.
- **Pixel captions page.** The dialog box shows two lines; a longer caption card pages to the next two lines as the words are spoken (with a ▶ marker), like an RPG text box.
- **Pre-existing audio nondeterminism (not from this work).** `audio_design.sfx` seeds its noise with `hash(kind)`, and Python randomises string hashes per process, so sound-effect noise differs slightly between runs of the same code. The original's video frames are bit-identical between the old and new code (checked with `framemd5`); only the noise in the effects differs. Fixing it (for example `zlib.crc32`) would change every cached render's audio, so it's left for a separate change.

## Next improvements

1. Choose `auto` preferences for the mystery and opportunity angles (their designs exist; see "Using a style").
2. Add a licensed condensed display face for broadcast (needs your OK for the download).
3. Hand-drawn sprite sheets for pixel (player poses, a short run cycle, helmets), loaded by `pixel.py`.
4. Let the presenter composite over a styled video.
5. Make the sound-effect noise deterministic (see above) in its own change.
6. A thumbnail per style.
