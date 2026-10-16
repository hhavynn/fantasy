# Flame / Praise: short-form pacing + punchline-aware meme sound

Second pass on the series (`docs/flame-praise-series/README.md` is the first). Every number below is measured from the
rendered files by `studio/pacing.py`, not estimated.

## 1. Why the four videos felt slow (measured)

- **Slow delivery.** 109–132 words per minute. The series lines asked Kokoro for 0.96–1.04x, *slower* than the
  channel's 1.12x default.
- **Dead air from splitting.** Every `‖` split the line into separately synthesized parts. Kokoro pads each part, so
  a "breath" cost 0.33 s and a beat 0.68 s; "Exhibit A: 4 sacks." alone took 3.3 s.
- **Late payoffs.** The first number arrived at 9.2 s (Bo comparison), 8.6 s (Cam), 8.1 s (Puka) and 5.9 s (monologue).
  Puka spent its first 8 s on a title card; Cam spent 7 s calling the case.
- **Static frames.** The Bo comparison and the monologue had no scene changes at all (FFmpeg scene detection), and
  70–80% of their frames were frozen.
- **Nothing on the jokes.** The punchlines had no sound at all, just a music bed (or silence in the monologue) and a
  few generic ticks and whooshes.

## 2. What changed

- **Rewrites**: 39–47 words instead of 55–70. The tease comes first (Bo), the number on frame 0 (Puka), "Order!" on a
  gavel (Cam). The monologue keeps one escalation, not two.
- **Delivery**: per-format Kokoro speed of 1.16–1.28x. `‖` no longer splits the line (Kokoro breathes on the
  punctuation); only the comic beat `⏸` splits, with 0.28 s added. Beat gaps are 0.08 s. The last beat leaves exactly
  enough room for its payoff sound to finish (0.45–1.1 s by effect class), then cuts.
- **Picture**: hard cuts between beats; the Puka number fills frame 0; court stamps land on the spoken number;
  typing runs at 60–70 characters per second; the ruler visibly shrinks as "smaller ruler" lands.
- **Targets** (`showrunner.FORMATS`): monologue 11–16 s, comparison 14–19, cinematic 13–19, courtroom 17–23,
  meltdown 10–17. QC's duration gate follows them.

## 3. Sound effects: sources and licenses

Machine-readable record: `studio/assets/sfx/manifest.json` (file, sha256, source URL, license, attribution,
platforms, processing). License files from the packs are kept beside the sounds.

| Source | Files used | License |
|---|---|---|
| Kenney "Impact Sounds" 1.0 (kenney.nl) | metal heavy ×2, wood heavy ×2, plate heavy ×2, punch heavy ×2 | CC0 1.0: commercial use, modification, no attribution needed |
| Kenney "Interface Sounds" 1.0 (kenney.nl) | bong, error ×2, toggle, scratch | CC0 1.0 |
| BigSoundBank #0236 "Shouts and Applauses of Teens #1", #2479 "Applause Concert Bar #1" (Joseph Sardin) | crowd cheer, applause | CC0 (public domain) |
| Original, synthesized in `studio/sfx_palette.py` | boom, pipe ring, bonk tone, boing, slide whistle, "wah wah" trombone, cinematic hit, choir, air horn, gavel body | the channel's own work |

**Not used:** the viral originals (the actual Vine boom, the "metal pipe falling" clip, a MyInstants sad trombone).
Their origin and license can't be verified, and some reportedly come from paid libraries. TikTok's in-app sound
rights don't carry over to YouTube or Reels, so they aren't treated as cleared. The palette recreates the
*comedic idiom* instead: a distorted bass drop with a room tail, a recorded metal strike plus a modal pipe ring and
a second bounce, a plunger-muted brass "wah wah wah waaah", and so on.

Palette (18 effects, each with a purpose, a tone, a class and an `attack` = the seconds from file start to the hit):
boom, pipe_clang, bonk, boing, slide_down, slide_up, sad_trombone, record_scratch, error_ding, toggle_click, gavel,
gavel_single, evidence_stamp, cinematic_hit, choir, crowd_cheer, applause, air_horn, plus `music_cut` (a deadpan
stop). Each format has its own sound personality (`FORMAT_SFX`): courtroom never gets a choir, and the monologue
never gets an air horn.

## 4. The punchline-aware engine

1. **Script**: every builder marks its comedic beats as cues: `joke_setup`, `punchline`, `stat_reveal`,
   `roast_impact`, `exhibit`, `verdict`, `hype_peak`, `final_payoff`, `ui`, `awkward_pause`. Each cue is anchored to a
   spoken word (its start or end) or to a number as it is said.
2. **Selection** (`sfx_palette.select`, at script time, stored in `script.json`): the candidates are effects whose
   purpose matches the beat, within the format's palette and tone. Scoring prefers the builder's intent, avoids
   repeats within the video (unless a cue is `repeatable`, like exhibit stamps, which rotate pitch variants) and
   avoids the last two series videos' sounds (`history.jsonl` records `structure.sfx`). It is deterministic.
3. **Placement** (`series_scenes.sounds`): hit time = the anchor from Kokoro's word timestamps − the effect's
   attack + `SYNC_OFFSET_S` (a global nudge, 0 by default). A cue whose anchor is missing is dropped, never moved.
   The camera punch-in uses the same anchor.
4. **Mix** (`series_audio.mix`): the music ducks 10 dB from 80 ms before each hit, `music_cut` stops the bed, and
   crowd/choir effects duck under the voice so they never mask a word. Peaks are kept safe, and FFmpeg loudnorm still
   targets −14 LUFS.

## 5. Pacing audit (QC gate `pacing`, series only)

`studio/pacing.py` measures:
- when the first word is spoken, when the first sentence ends, when the first number is said, and when the first
  payoff lands;
- the longest gap between words and the longest near-silence in the mix;
- FFmpeg scene changes and frozen-frame share;
- the cue sheet, with each effect's placement error and where it is **heard in the mix** (final mix minus the
  no-SFX mix, before loudness normalization);
- MP4 audio lag against its mix, effects landing mid-word, and the same sound set as a recent video.

It fails a Short on dead air, a very late hook or payoff, a misplaced effect, an MP4 out of sync with its mix, or a
repeated sound set. Softer problems are warnings. Results go to `pacing.json` and `pacing.md`.

## 6. Before / after (measured)

**Bo Nix comparison**

| Measure | Original | New |
|---|---|---|
| Duration (s) | 31.9 | 19.17 |
| Words | 70 | 47 |
| Words per minute | 132 | 147 |
| First sentence done (s) | 3.48 | 2.5 |
| First number said (s) | 9.2 | 5.92 |
| First payoff (s) | 9.2 | 4.41 |
| Longest gap between words (s) | 0.88 | 0.56 |
| Longest near-silence in the mix (s) | 0.52 | 0.42 |
| Major visual changes | 0 | 6 |
| Frozen frame (%) | 77.8 | 58.3 |
| Meme sound effects | 0 | 4 |

**Cam Ward courtroom**

| Measure | Original | New |
|---|---|---|
| Duration (s) | 31.37 | 17.73 |
| Words | 58 | 39 |
| Words per minute | 111 | 132 |
| First sentence done (s) | 1.23 | 0.35 |
| First number said (s) | 8.59 | 3.08 |
| First payoff (s) | 8.59 | 3.08 |
| Longest gap between words (s) | 0.88 | 0.58 |
| Longest near-silence in the mix (s) | 0.66 | 0.62 |
| Major visual changes | 10 | 13 |
| Frozen frame (%) | 80.4 | 60.3 |
| Meme sound effects | 0 | 6 |

**Puka Nacua cinematic**

| Measure | Original | New |
|---|---|---|
| Duration (s) | 30.27 | 18.5 |
| Words | 55 | 40 |
| Words per minute | 109 | 130 |
| First sentence done (s) | 0.97 | 1.0 |
| First number said (s) | 8.14 | 0.05 |
| First payoff (s) | 8.14 | 0.05 |
| Longest gap between words (s) | 0.88 | 0.58 |
| Longest near-silence in the mix (s) | 0.37 | 0.71 |
| Major visual changes | 15 | 16 |
| Frozen frame (%) | 15.0 | 0.0 |
| Meme sound effects | 0 | 4 |

**Bo Nix monologue**

| Measure | Original | New |
|---|---|---|
| Duration (s) | 25.37 | 15.93 |
| Words | 55 | 39 |
| Words per minute | 130 | 147 |
| First sentence done (s) | 3.88 | 2.52 |
| First number said (s) | 5.92 | 3.63 |
| First payoff (s) | 5.92 | 2.52 |
| Longest gap between words (s) | 0.93 | 0.61 |
| Longest near-silence in the mix (s) | 1.3 | 0.95 |
| Major visual changes | 0 | 6 |
| Frozen frame (%) | 69.8 | 63.6 |
| Meme sound effects | 0 | 3 |
Every meme effect was found in the rendered mix within 1–10 ms of its word, except the error ding (40 ms). The
choir is a swell with no single onset, so it is listed but not scored. MP4 audio lag against its mix is 0 ms in all
four. All four pass every QC gate. Loudness is −13.3 to −15.4 LUFS, with a true peak of −1.3 to −1.4 dBFS.

Cue sheets (in each episode's `pacing.md`):
- **Bo comparison**: boing on "ruler" (4.41 s), slide whistle landing on "4.3", boom on "shorter", trombone after
  "home".
- **Cam courtroom**: gavel on "Order!" (0.05 s), three evidence stamps on 4, 3rd and 3 (pitch-rotated), boom on
  "home", double gavel on "guilty".
- **Puka cinematic**: cinematic hit on frame 0 ("24"), boom on "18", choir after "more", crowd cheer after "did not".
- **Bo monologue**: toggle click on "airplane", error ding after "mode", metal pipe after "back".

## 7. Outputs

`studio/episodes/`, each with `final.mp4`, `final_nosfx.mp4` (the same picture without meme effects),
`script.json` (with its cue sheet), `pacing.md`/`pacing.json`, `qc.json`, `brief.md` and `review.md`:

- `2026-w06-short-flame-bo-nix-fast/`
- `2026-w06-short-flame-cam-ward-fast/`
- `2026-w06-short-praise-puka-nacua-fast/`
- `2026-w06-short-flame-bo-nix-fast-monologue/`
- `2026-w06-short-flame-bo-nix-fast-newvoice/`: the comparison in the owner's new voice (blend + enrolled clone,
  18.2 s, QC passed).
- `2026-w07-short-flame-bo-nix-seahawks/`: the Broncos–Seahawks game now that nflverse has published Week 6. Only 20
  of his 107 passing yards came in the air (87 after the catch); aDOT 2.96. It is blocked by the existing
  originality rule because Bo Nix was featured the week before. That is an editorial call for the owner.

The Week 6 remakes reuse the archived Week 6 snapshot (`--frozen-snapshot`), because the database now holds context
through Week 6 only.

## 8. Remaining weaknesses

- I can't listen. Sound quality was checked with spectrograms, levels, onset timing and click checks, not ears. The
  synthesized boom, pipe and trombone are recognizable in shape, but they are not the viral originals.
- No crowd gasp yet. The two CC0 crowd recordings are a teen cheer and applause.
- The comparison, courtroom and monologue still show 58–64% frozen frames. The cuts help, but the scenes inside a
  beat are mostly still. The comparison's first payoff is 4.4 s against a 4 s target, and it runs 19.2 s against a
  19 s ceiling.
- Puka's longest near-silence went *up* (0.37 → 0.71 s). That is the deliberate music drop before "18"; judge it by
  ear.
- Courtroom uses six effects. That is intended (rapid-fire evidence) but sits at the edge of soundboard territory.
- No retention data exists. Shorter and faster is measured; whether it is *better* is not.

## 9. Next improvements

1. Listen to each video against its `final_nosfx.mp4`, then tune gains and swap any effect that doesn't land (one
   line in `FORMAT_SFX`/`PALETTE`).
2. Add a CC0 crowd gasp/"ooh" and a short laugh-track sting (BigSoundBank or Freesound CC0), each recorded in the
   manifest.
3. Add motion inside beats (number count-ups, a moving ball flight) to cut the frozen share below 40%.
4. Once anything is published, compare retention per format and per effect before tuning further.
