# Real NFL photography, B-roll and story-aware media editing (editorial_media)

An optional, experimental treatment for the faceless Shorts: the same narration, numbers and sounds, re-edited
with real photos of the players, contextual B-roll and the studio's own graphics used as interruptions or
insets. It moves off the single ESPN face card toward edited sports journalism: full-screen action photography,
different pictures through the story, minimal readable subtitles. Branch `feat/editorial-media`, built on
`feat/flame-praise-visuals` (PR #76).

- **Source research:** [research.md](research.md): every option rated; what is free, what is paid, and what is
  actually cleared today
- **Catalog with licences and credits:** [media-manifest.md](media-manifest.md)
- **Storyboards** (with each demo's QC): [Puka Nacua](storyboards/1-puka-nacua-praise.md),
  [Bo Nix](storyboards/2-bo-nix-flame.md), [Braelon Allen](storyboards/3-braelon-allen-analysis.md)

## 1. Audit: what existed

| Area | Before this branch |
|---|---|
| Player pictures | `headshots.py`: one ESPN headshot per player (owner override, `[shorts] headshots`). `edit_kit.cutout` turns it into a sticker. `motion.player_art` falls back to a drawn jersey. No other photo source, no catalog, no licence record beyond the headshot's provenance line |
| Renderer | `stages/render_motion.py` draws every frame in Pillow and pipes raw RGB into one FFmpeg encode. Scenes are `fn(img, t, dur, spec, ctx)` registered in `SCENES`; each beat is one scene on its own clock with word-anchored cues. Transitions are push, whip, rewind, zoom and cut. Captions are 2–3-word cards at a fixed y |
| External stills or clips | None. Everything is drawn from code (`motion.py`: "no photos, logos, footage") |
| Flame/praise | `edit_scenes.py` (vx_/cx_/hx_ edit looks), `flame_scenes.py`, `series_audio.py` (meme sounds placed on words), `showrunner.py` (formats) |
| QC | `stages/qc.py` (facts, pacing, rights). `visual_audit.py` (compositions, holds, player share, sync) |
| Tools | FFmpeg 9 with zoompan, xfade, crop, gblur, overlay. Pillow 12, NumPy, SciPy, torch. No OpenCV, rembg or mediapipe. Apple Vision is on the machine via `swift` |

**What was reused, unchanged:** the whole per-frame pipeline idea, `render_motion.align/phrases/Ctx/draw_scene`
(graphic shots are drawn by the original scene functions on the beat's own clock), `edit_kit`
(slam/shake/flash/freeze/cutout/backdrop/hits), `visual_audit` (measure and contact sheets), and the episode's
existing `work/mix.wav`, so every meme sound still lands on its word. No existing module changed except
`cli.py` (an opt-in flag), `config.toml` (an `[editorial_media] mode = "off"` section) and `.gitignore`.

## 2. Architecture

```
assets/media/manifest.json      tracked: metadata, licence, attribution, subject box, review status
assets/media/cache/             git-ignored files (restore: python -m studio.media_sources restore)
studio/media_library.py         Entry schema, licence table, check_use (identity, team, season, injury, stock,
                                licence, review), labels, attribution
studio/media_sources.py         Commons / Openverse / Pexels / Pixabay / local / ESPN adapters, Apple Vision
                                subject boxes (vision_subject.swift), approve, report, restore
studio/shot_planner.py          clauses -> intents -> media, treatments, variety, gaps
studio/media_scenes.py          photo / broll / graphic / inset / presenter / missing shots, crops, motion,
                                depth, grades, overlays, labels, credits, captions, clip reader
studio/media_qc.py              plan + output checks; preview_pass vs publish_pass
studio/stages/editorial_media.py  prepare (copy) -> storyboard -> render -> provenance -> QC -> audit
```

**Catalog types:** `action_photo`, `catch_photo`, `rush_photo`, `celebration_photo`, `sideline_photo`,
`practice_photo`, `team_photo`, `licensed_video`, `context_broll`, `source_evidence`, `headshot_fallback`, plus
`interview_still`. Each entry records the player id, name, team, season and date, type, cache path, size or
video specs, the subject box and face, what it shows, its context, source, licence, attribution and review.

**Integrity checks (`check_use`), applied to every shot:**

| Check | Result |
|---|---|
| Wrong uniform for current analysis | Blocked. Puka's BYU photo and Braelon's high-school photo are in the catalog and are never picked |
| Generic injury imagery presented as a player's injury | Blocked unless labelled ILLUSTRATIVE. Injury lines show the availability report itself (source evidence); a photo of the player is allowed only as identification |
| Stock under a line about a specific player | Must carry ILLUSTRATIVE (drawn on screen) |
| A teammate photo | Must be that teammate's gsis id |
| Non-commercial licence; ESPN image outside `headshot_fallback`; ESPN with the switch off | Blocked |
| Unreviewed file | Warning in preview, block for publishing |
| Old season | Labelled "FILE PHOTO 2023"; team photos are always "FILE PHOTO <year>", never "this game" |

## 3. How shot selection works

`shot_planner.plan` cuts each narration beat into clauses at sentence ends, and at commas when both halves can
hold a shot or name different people ("That's Braelon Allen, | with Breece Hall out"). Each clause gets an
intent from its words, claims and sound cues:

| Intent | Example | First choice |
|---|---|---|
| dominance / identity | "Puka Nacua against the Bills." | his best cleared photo in his current uniform |
| stat_hook | "24 targets." (the opener) | photo + the number slammed in the frame's empty zone |
| stat_run | "17 catches, 138 yards, 1 touchdown." | a hard cut and a new photo on every number (hype) |
| comparison | "Average NFL throw: 7.9 yards. His? 3." | the beat's own graphic (the ruler lanes), or an inset over his photo |
| data_story | "Allen got 27% of the carries." | the graphic |
| teammate_absence | "with Breece Hall out." | a verified photo of that teammate |
| injury_status | "Hall's missed Weeks 4 and 5 …" | the availability report full-screen, then as a card over Hall's photo |
| team_result | "The Broncos won anyway, 17 to 14." | a team photo labelled FILE PHOTO + the score from the claims |
| punchline | "Grab a smaller ruler." | B-roll matching the joke word (a tape measure, ILLUSTRATIVE), snap-zoomed on the sound hit; otherwise the beat's gag graphic |
| verdict | "If Hall sits again, Allen's an easy start." | the decision graphic |

There is no fixed sequence:

- **Treatments** weight photos against graphics, shot length, motion, grade and transitions differently:
  `commentary`, `player_hype`, `roast`, `fantasy_analysis`, `injury_opportunity`, `trade_debate`. They are chosen
  from the story angle and rotated off the last two episodes'.
- **Ties** are broken by a seed from the episode id.
- **Photos are spent sparingly:** least used first, and a re-used photo gets a new framing (medium, tight, wide).
- **A recent episode's plan shape** is re-drawn.
- **Every pick says why.** When the media wanted isn't available, it's listed as a gap: wanted vs used.
- **Presenter cutaways** are a slot: only on an opinion line, at most one per treatment that allows it, and only
  when a pre-rendered host clip exists for that beat. The demos have none; the media carries them.

## 4. The treatment

- **Subject-aware crops:** a 9:16 window from the subject box and face (Apple Vision at ingest, corrected by
  hand in review). The face sits about a third of the way down, with headroom. A burned-in TV lower third is
  avoided (`subject.avoid`).
- **Small sources:** a source that would upscale beyond 2.6× becomes a sharp card over a blurred fill of itself,
  not mush.
- **Motion and depth:** slow push, pull or drift per shot; a snap zoom on punchlines. The background softens
  away from the subject; vignette, edge shading for legibility and a grade per treatment.
- **Captions** sit at the top or the bottom, whichever the player covers less (his face weighs 4×). They are
  one line where possible and hidden while the same number is slammed big on screen.
- **Overlays:** stat slams (a claim's display string and the word spoken after it) and scores.
- **Labels and credits:** a small "Photo: Tennessee Titans / CC BY 4.0" credit on every photo, with FILE PHOTO,
  ILLUSTRATIVE or FALLBACK labels. The credit lines are also in the storyboard and `media_provenance.json`,
  ready for the description.
- **Graphics stay:** the beat's original scene as a full-screen interruption, or a 58% card over the player's
  blurred photo (inset).
- **Sound hits:** every hit punches and flashes the picture; a hard stop freezes it (roast) or holds it (praise).
- **Missing media** renders as a striped MEDIA NEEDED slate. QC blocks publishing a video with one.

## 5. Workflow

```bash
cd fantasy-intel/studio
# 1. storyboard first (no render): storyboard.md, storyboard.png (one real frame per shot + notes), plan QC
python -m studio.stages.editorial_media --source episodes/<episode> --preview
# 2. render the experiment into <episode>-editorial (the source folder is only read)
python -m studio.stages.editorial_media --source episodes/<episode> [--treatment roast]
# or, for every Short a run makes:
make-episode --week 7 --flame-praise --editorial-media preview|render
# media
python -m studio.media_sources commons-search "Bo Nix"
python -m studio.media_sources add-commons "File:Bo Nix scramble.png" --id nix-scramble-2025 --type action_photo \
    --player 00-0039732 --name "Bo Nix" --team DEN --season 2025 --relevance scramble,game
python -m studio.media_sources add-local ~/Pictures/rams.jpg --id rams-owner-1 --type team_photo --license owner \
    --team LA --season 2026
python -m studio.media_sources approve nix-scramble-2025 --by owner
```

Run Python with `PYTHONPATH=.` from `fantasy-intel/studio` when using the shared `.venv`: its installed
`studio` package points at another worktree.

## 6. Demos (local only, `studio/episodes/editorial-media-demos/`, never published)

Each folder holds `final.mp4`, `storyboard.md/.png`, `media_qc.md`, `media_provenance.json`,
`visual_audit.json`, `before_after_contact_sheet.jpg` and `source.json` (the original's path and hashes). The
originals are untouched.

| Demo | Source (only read) | Shots | Pictures | Compositions before → after | Longest hold | Player share | Empty frame |
|---|---|---|---|---|---|---|---|
| 1 Puka Nacua praise, `player_hype` | `2026-w06-short-praise-puka-nacua-visual` | 9 | 3 distinct real-Puka pictures (camp 2023, interview 2024, ESPN headshot fallback) + Rams team photo | 13 → 14 | 2.6 → 2.4 s | 0.44 → 0.58 | 0.44 → 0.35 |
| 2 Bo Nix flame, `roast` | `2026-w07-short-flame-bo-nix-seahawks-visual` | 7 | 2 real 2025 game frames + Broncos team photo + tape-measure B-roll | 10 → 13 | 4.0 → 2.8 s | 0.43 → 0.44 | 0.61 → 0.48 |
| 3 Braelon Allen analysis, `injury_opportunity` | `2026-w06-short-braelon-allen`, re-voiced in the current voice (`3-braelon-allen-source-current-voice`) | 8 | Braelon: ESPN fallback only. Breece Hall: 2 verified photos | 5 → 7 | 4.8 → 2.6 s | 0.00 → 0.15 | 0.67 → 0.56 |

Voice: Puka and Nix reuse their source episodes' narration, already in the current Shorts voice (am_puck 0.7 +
af_heart 0.3 at 1.25×, re-voiced as the owner's enrolled voice). Braelon's only source episode predates that
voice (am_adam), so it was re-voiced into a new folder with today's config: same script and claims, nothing
switched. The original episode is untouched.

- **Puka:** the stat hooks all stay (24 targets, the entire Bills offense 18, 17/138/1 as three hard cuts, 412
  games, the shootout). Gap: no 2025–26 Rams game photo of him exists on a free source.
- **Bo Nix:** real Broncos game photos. The ruler comparison (`vx_lanes`), the air-yards gag and the deep-safety
  payoff stay as interruptions, with the meme sounds (bruh, fahhh, get out) on the same words. The visual
  punchline is "Grab a smaller ruler" over a literal tape measure, labelled ILLUSTRATIVE, snapping in on "ruler".
- **Braelon:** no cleared photo of him as a Jet exists, so the storyboard is transparent about it: the labelled
  ESPN fallback, plus the gap "wanted: action photo of Braelon Allen (NYJ, current season)". His beat about
  Breece Hall uses verified Hall photos, and the injury beat shows the injury report (evidence) rather than any
  injury footage.

QC: all three pass as experiments. **None is publishable**, by design: every Commons file is still `pending`
human review. Remaining warnings: the two ESPN headshot fallbacks.

## 7. Coverage gaps and next steps

1. **Player game photos are the bottleneck.** The fix is a paid editorial licence that covers monetised Shorts
   (Imagn or AP; see research.md), or the owner's own photos.
2. **Braelon Allen:** nothing in a Jets uniform is cleared anywhere free. Puka: nothing from a 2025–26 game. The
   small Nix stills (scramble, dropback) render as cards.
3. **Video B-roll:** the clip path (`licensed_video`, `ClipReader`) is built and tested with a synthetic clip,
   but no real clip is in the catalog. Pexels/Pixabay keys would add stadium, crowd and object B-roll.
4. **Review:** approve or reject each pending entry (rights and subject box), then re-run QC.
5. **Presenter:** the cutaway slot exists, but no host clip aligned to an editorial beat has been rendered (the
   front-teeth gap is unsolved, so it stays off).
