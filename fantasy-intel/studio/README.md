# studio: YouTube episodes from fantasy-intel usage data

Local pipeline: NFL usage data → script (local Qwen3 via llama.cpp) → Kokoro narration → charts →
FFmpeg video with burned-in captions → thumbnail → private-upload metadata. Plan and rules:
[`../docs/youtube_studio_plan.md`](../docs/youtube_studio_plan.md).

## One-time setup

```bash
brew install ffmpeg llama.cpp
curl -L -o ~/models/Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf \
  https://huggingface.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF/resolve/main/Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf
cd fantasy-intel/studio && uv sync
```

## Each week

```bash
# 1. fresh NFL data (fantasy-intel/backend)
uv run python ../scripts/sync_nfl.py
# 2. the local script model (leave running; ~19 GB, pinned in RAM)
llama-server -m ~/models/Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf --port 8080 -c 12288 -np 1 \
  -b 512 -ub 256 -ngl 99 --load-mode mlock --jinja
# 3. this week's Shorts (fantasy-intel/studio); no LLM needed for Shorts
uv run make-episode --week 7                       # three Shorts, one per story
uv run make-episode --week 7 --player braelon-allen
# optional: the long-form episode (needs the llama-server above)
uv run make-episode --week 7 --format long
```

## Shorts (the main product)

Rendered by `stages/story.py` (storyline + beats) and `stages/render_motion.py` (scenes in
`studio/scenes.py` and `stages/render_motion.py`, audio in `studio/audio_design.py`). Story components
show what the narrator says: a player card with the premise on frame 0, targets as footballs per game,
"getting the ball" vs "on the field" (snap share as dots), team volume, a fantasy lineup slot, a decision.
Try other openings with `--hook` (e.g. `--hook comparison`); variants render to `<slug>-hook-<name>/` and
stay out of the channel history.

One player, one protagonist stat, 30-45 s, 1080x1920. Built on the strategy in
[`../docs/research/shorts-strategy-report.md`](../docs/research/shorts-strategy-report.md):

- **Stories**: the strongest *sustained* moves (every recent game shows it, against the median earlier
  game, so a single injury exit is never a "faller"), one per team, three a week. Lead stats are snap,
  target or backfield share; air-yard share is too noisy to lead.
- **Script**: composed deterministically from data claims, no LLM. The angle comes from the data
  (expansion, unresolved, takeover, contraction) and sets the context line and verdict; hook families
  rotate least-recently-used across the channel so it never looks templated.
- **Injury context**: a riser whose teammate (same position group, a real role before) missed the recent
  games is a *fill-in*, and the Short says so: who missed which weeks, why (official injury report), the
  role he held, and his status this week (IR, game designation or practice participation). The verdict
  follows that status. A teammate back in the latest game with the role intact makes a *held* story; a
  faller whose teammate returned is *displaced*. Missed games come from the usage data itself (the team
  played, he had no snaps); reasons and statuses only from the injury report and roster status.
- **Shot rhythm**: one sentence per beat, each with its own visual: stat (fully drawn on frame 0) ->
  weekly bars (checkmarks on each game the move held) -> teammate timeline (OUT weeks) -> status card ->
  stat compare / volume -> takeaway -> follow. A progress bar and a drifting glow keep every frame moving;
  numbers in captions are highlighted. Changes are computed from the displayed percentages, so the
  on-screen arithmetic always adds up.
  From week 11 a "role first, schedule second" Weeks 15-17 beat joins.
- **CTA**: "Follow for the next usage signal" until the site exists; then "the first link on my channel"
  (links in Shorts descriptions are not clickable).
- `metadata.json` carries `experiment` fields (hook, angle, primary stat, length) for comparing Shorts
  one variable at a time once they are published.

`episodes/<season>-wNN-short/review.md` lists the week's Shorts; each Short's `review.md` shows every
sentence with the data behind each number, the weekly table, and things to check by eye.

Outputs land in `episodes/<season>-wNN-long/`: `final.mp4`, `thumbnail.png`, `metadata.json`
(three title options, description with chapters, private-only settings), `captions.srt`, plus every
intermediate (`snapshot.json`, `facts.json`, `script.json`, `audio/`, `shots/`). Rerunning skips any
stage whose inputs did not change; `--force script,tts` reruns specific stages.

## Flame / praise Shorts

```bash
uv run make-episode --week 6 --flame-praise                  # the latest game's strongest flame and praise
uv run make-episode --week 6 --flame-praise --player bo-nix  # one player's strongest case, if he has one
```

A punchy one-player, one-game Short: roast a historically bad usage or efficiency line (flame) or hype a monster
one (praise). It is now an NFL entertainment series: `stages/showrunner.py` picks a football angle and one of five
formats (roast monologue, courtroom parody, comparison, main character, fan meltdown) per story and recent history,
writes `brief.md`, and `--storyboard` previews it before any render; see
[`../docs/flame-praise-series/README.md`](../docs/flame-praise-series/README.md). `--treatment classic` renders the
first template below. Pacing and sound: each format has a short-form target length and speed, every joke beat
is a cue that a CC0 or original meme-style effect lands on to the word, and QC's `pacing` gate measures dead air,
late payoffs and effect sync in the render; see
[`../docs/flame-praise-series/pacing-and-sound.md`](../docs/flame-praise-series/pacing-and-sound.md).
`--compare-mix` also writes `final_nosfx.mp4`; `--frozen-snapshot` re-makes an earlier week from its archived data.
The meme sounds (`assets/sfx/registry.toml`) are not in git: run `uv run python -m studio.sfx_fetch` once per clone
(without them the series falls back to its procedural sounds). Audition candidates with `python -m studio.sfx_audition`. Trash talk about the stat line, never about the person. Code: `stages/flame.py` (selection, claims,
script) and `studio/flame_scenes.py` (ember / spotlight looks; hook card, a strip of every position game, his
season game by game, a stat receipt), ending on the shared `decision` scene.

How candidates are chosen (deterministic; constants at the top of `stages/flame.py`):

- **The game**: the latest week in the data. Metrics are usage/efficiency only: QB passing air yards, air yards per
  attempt (flame only), passing EPA and yards per attempt (nflverse weekly stats, an allow-listed
  `qb_week_passing` slice of the snapshot with no fantasy columns); WR/TE target share, air yards and targets;
  RB share of the running-back carries and snap share (`player_week_usage`).
- **Volume floors**: a game counts only with 20+ attempts (QB) or 50%+ (WR/TE) / 30%+ (RB) of the snaps, for him
  and for the position's distribution, so an early exit is never roasted. A flame also needs an established role
  in his earlier games (20+ attempts; 15%/12% target share for WR/TE; 45% of RB carries).
- **Against the position**: the game must rank in the extreme 10% of every qualifying game at his position this
  season (at worst 3rd), e.g. "the 8th-lowest of 152 quarterback games".
- **Against himself**: at least two earlier qualifying games; a flame must be below his own norm, a praise above it
  (rates pooled, counts averaged).
- **Strength** = position z-score + half his own z-score (capped at 3) + 0.5 for the season's single most extreme
  game + 0.25 for his own season low/high. The strongest metric leads; others in the extreme third become the
  receipts. The week's picks are the strongest flame and praise from different teams.

Every number is an `fp_` claim that `facts.evaluate_claim` re-derives from the snapshot, so the data-claims,
narration, banned-phrase and originality gates apply unchanged. The full board, with each candidate's numbers,
is written to `episodes/<season>-wNN-short/flame_praise.md`.

## QC and provenance

`make-episode` finishes with QC gates and exits non-zero if any fails (upload is blocked):
data claims re-derived from the snapshot, no number in narration or titles outside a claim, duration
(8-12 min long form), silences over 1.5 s and clipping, black or frozen frames, A/V length, caption
overflow, originality versus `episodes/history.jsonl` (5-gram overlap and recycled players), model and
package licenses, private-only upload metadata, and pipeline integrity (no stale stage output).
Results with evidence: `qc.json`. Rights and reproducibility record: `provenance.json` (git commit,
model checkpoints with licenses and sha256, tool versions, data snapshot hash, every prompt, file hashes).

Read the script and watch the video before publishing: the local model still occasionally writes a
sentence the data does not support, and every upload is a human decision.

Pronunciations: words that went through the espeak-ng fallback are listed in
`audio/timings.json` (`fallback_words`). Fix a bad one in `studio/pronunciations.toml`.

Tests: `uv run --group dev pytest`.
