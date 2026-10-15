# YouTube studio for fantasy-intel: plan + Phase 0 findings

## Context

You want a faceless, voiceover-plus-charts YouTube channel built on fantasy-intel's NFL usage data. Its job is to drive traffic to the site and later earn sponsor/ad money. Budget: about 5 hrs/week, with about 12 NFL weeks left (the last regular-season games are on 2027-01-10 per `nfl_schedules`). Getting to a first upload fast matters more than a perfect pipeline.

`docs/research/ai-youtube-report.md` doesn't exist. I read the copy you attached (`~/Downloads/deep-research-report (1).md`) and will commit it to `fantasy-intel/docs/research/ai-youtube-report.md` in Milestone 1. Its core conclusion supports this design: YouTube demonetizes **interchangeable templated content**, not AI content as such. So the originality gate and your human publish review are core features, not polish.

**Feasibility verdict: yes.** Everything runs locally for $0. The real constraints aren't technical: (1) there's no public site yet for the call to action, (2) the YouTube API audit lock (below), and (3) routes and first-read data don't exist in-season.

---

## Phase 0 findings

### Data: what we have (all in `fantasy-intel/data/fantasy.duckdb`, 2026 season, weeks 1–5)

| Need | Status | Source |
|---|---|---|
| Snap share | ✅ ~400 player-games/week | `player_week_usage.offense_pct` (nflverse snap counts) |
| Target share, air yards, air-yard share, WOPR, aDOT | ✅ | `player_week_usage` (built from pbp) |
| RB role: backfield rush share, team rush share, RZ/GL carries, RZ targets | ✅ | `player_week_usage` |
| Role trend (recent vs earlier, UP/FLAT/DOWN) | ✅ needs ≥4 games | `app/analytics/trends.py`, `player_scores` |
| Offensive context: PROE, neutral pass rate, sack and pressure rate | ✅ | `team_context`, `nfl_team_week` |
| QB per game, head coach | ✅ | `nfl_schedules.home_qb_name` / `away_qb_name`, `*_coach` |
| ROS and Weeks 15–17 schedule | ✅ all 18 weeks | `nfl_schedules` |
| Opponent strength (pass/rush EPA allowed, RB/TE target share allowed, ranks) | ✅ season to date | `defense_context` (plus the no-lookahead logic in `app/analytics/schedule_intelligence.py`) |
| Implied team totals | ✅ | `team_game_lines` / `team_context.next_implied_total` |

### Data: what's missing

- **Routes run / route participation / TPRR: 0 rows.** nflverse doesn't publish participation data in-season, and no external provider file has been imported (`external_usage_imports` = 0). **Decision (yours): dropped for v1.** Scripts must never say "routes".
- **First-read share: unusable.** The FTN `read_thrown` code semantics are unverified (nflreadr#325), and `docs/nfl_metrics_verified_contract.md` §3 already bans them from any score. Dropped for v1, and the same goes for FTN designed and checkdown targets.
- **Play-caller:** not in the data (head coach ≠ play-caller). v1 uses head coach + PROE/neutral pass rate. A hand-maintained `playcallers.csv` with a source URL per row is optional later.
- **Public site: doesn't exist** (the app is local-only and holds private ESPN league data). **Decision (yours): site not ready, assess feasibility only.** The CTA URL becomes a config value. v1 renders a channel CTA ("subscribe; the tool is coming") and swaps in the URL when the site ships. Site screen recordings are an optional shot type, off by default (recording localhost would capture league data).
- **Data freshness:** the DB runs through week 5. Each episode needs `scripts/sync_nfl.py` first (nflverse lags games by about a day). ESPN sync is **not** needed: videos use NFL tables only, and the snapshot step allow-lists tables so no league or manager data can leak into a video.

**Data licensing:** most nflverse data is CC-BY 4.0, so every description carries an attribution line. FTN data is CC-BY-SA 4.0, which is another reason to leave it out of v1. In M1 I'll confirm the per-file license for snap counts (PFR-sourced) against the nflreadr data docs.

### Machine

| | |
|---|---|
| OS | macOS 26.6.2, arm64 |
| CPU | Apple M5 Pro, 18 cores |
| GPU | M5 Pro integrated, 20 cores, Metal 4 (no CUDA) |
| Memory | 48 GB unified (shared by CPU and GPU) |
| Disk | 335 GB free |
| ffmpeg | **not installed** → `brew install ffmpeg` |
| llama.cpp | **not installed** → `brew install llama.cpp` (Metal build) |
| Python | system 3.9.6 (too old); `uv` is present, so the studio uses uv-managed 3.12 like the backend |

**What can realistically run locally:** all of it. With 48 GB unified memory:
- **Qwen3-30B-A3B-Instruct-2507, Q4_K_M GGUF (~18.6 GB)** in `llama-server`. It's MoE with ~3B active parameters, so it's fast on Apple Silicon: a full script in about a minute. Fallback: Qwen3-14B Q4_K_M (~9 GB). Both Apache-2.0. `llama-server` supports `response_format: {type: "json_schema", schema: …}`, which I verified in the server README. **No paid alternative is needed; cost is $0.**
- **Kokoro-82M**: runs on CPU or MPS, faster than real time.
- FFmpeg/Pillow/matplotlib: trivial. Video encode can use VideoToolbox hardware H.264.
- Not viable here: CUDA-only video diffusion (not needed and not wanted).

### License audit (code / weights / bundled deps checked separately)

| Component | Code | Weights | Bundled deps | Verdict |
|---|---|---|---|---|
| llama.cpp | MIT | — | — | ✅ |
| Qwen3 (30B-A3B-2507 / 14B) | — | Apache-2.0 | GGUF conversions inherit the weight license; record the HF repo + sha256 | ✅ |
| Kokoro-82M | Apache-2.0 | Apache-2.0. Model card: trained only on permissive/non-copyrighted audio, production use welcomed | `misaki` G2P is Apache-2.0. **espeak-ng (GPL-3.0) is an optional fallback.** v1 runs `fallback=None` plus a pronunciation dictionary for player names, so GPL code isn't in the loop. Even if enabled, GPL covers distributing the software, not the audio it outputs | ✅ |
| OpenVoice V2 | MIT | MIT, commercial use stated | MeloTTS base (MIT) | ✅ **only with your own recorded voice**; not in v1 |
| FFmpeg (Homebrew) | LGPL/GPL build (x264) | — | — | ✅ for internal use (we don't redistribute the binary); build config recorded in provenance |
| Pillow / matplotlib / Playwright | HPND / PSF-style / Apache-2.0 | — | Chromium (BSD) | ✅ |
| **Fonts** | — | — | **Apple SF fonts are licensed for Apple-platform UI, not burned-in video** | Bundle **Inter (OFL-1.1)** in the repo; never use system fonts |
| F5-TTS, XTTS-v2/Coqui, AudioCraft/MusicGen, Wav2Lip | — | — | — | ❌ **banned.** A license gate hard-fails if any of these packages or checkpoints appear in the env or config |
| Music | — | — | — | None in v1 |

### ⚠️ YouTube API audit lock (affects Milestone 4)

Per Google's `videos` reference, **uploads via `videos.insert` from unverified API projects created after 2020-07-28 are locked private, and changing `privacyStatus` can't unlock them; only a project compliance audit can.** That means you could *not* publish an API-uploaded video yourself until the audit passes. Plan:
- In M4, build the private-only uploader as specified **and** submit the Cloud project for the API compliance audit right away (it takes weeks; start early).
- Until the audit passes, `make-episode` also emits an **upload bundle** (`final.mp4`, `thumbnail.png`, `metadata.json` with copy-paste title/description/tags) for a manual private upload in YouTube Studio. That keeps the first upload off the critical path.
- The `videos.insert` quota cost is disputed across sources (100 vs 1,600 units); check the live quota table in M4.

---

## Video formats

All numbers come from a deterministic **facts packet**. The LLM never chooses players or computes stats; it writes prose around facts Python already selected.

**(a) Weekly long-form, "Usage Risers & Fallers, Week N" (8–12 min, 1920×1080)**
1. Cold open (~20 s): the week's biggest role change, as one big stat card.
2. What we measure (~20 s, rotated phrasing): snap share, target share, air-yard share, backfield share. "No fantasy points, no projections."
3. **5 risers** (~70 s each): a last-2-games vs prior-games usage chart, a team context card (QB, PROE/neutral pass rate), and a ROS + **Weeks 15–17** opponent strip (defense EPA-allowed ranks).
4. **4 fallers**, same structure.
5. Playoff-schedule watch (~60 s): the best and worst Weeks 15–17 strips among the featured players.
6. CTA (~15 s): the channel CTA now, the site URL later.

Selection rules (Python): minimum snaps/targets, ≥4 games for trend, deltas on snap share / target share / air-yard share / backfield share, and a position mix. Players featured in the last 2 episodes are de-prioritized.

**(b) Shorts, "one player, one usage stat" (30–45 s, 1080×1920)**
A hook stat card ("Target share: 14% → 29%"), a week-by-week bar chart, one context line (QB/PROE or Weeks 15–17 strip), then the CTA. These come from the same facts packet, so each long-form week feeds 3–5 Shorts.

---

## Pipeline architecture

New uv project **`fantasy-intel/studio/`**, separate from the backend so torch/kokoro never touch the API's deps. It reads the backend DuckDB **read-only** and reuses query logic from `backend/app/analytics/` (`trends.py`, `schedule_intelligence.py`, `context.py`) by importing it, not re-deriving it.

```
studio/
  pyproject.toml            # duckdb, matplotlib, pillow, kokoro, soundfile, jsonschema, httpx, google-api-python-client
  config.toml               # db path, cta_url (empty for now), model endpoint, thresholds, banned-package list
  assets/fonts/Inter-*.ttf  # OFL, with license file
  studio/
    cli.py                  # make-episode --week N --format long|short [--force STAGE]
    stages/  snapshot.py  facts.py  script.py  tts.py  charts.py  assemble.py  thumbnail.py  metadata.py  qc.py  provenance.py  upload.py
    llm/     base.py (ScriptLLM protocol)  llamacpp.py (OpenAI-compatible, json_schema)  fake.py (tests)
    schema/  script.schema.json
    pronunciations.toml     # player-name → phoneme overrides for Kokoro
  tests/
episodes/<episode_id>/      # git-ignored outputs
episodes/history.jsonl      # committed: per-episode players, narration hash, shingles (for the similarity gate)
```

**Stages** (each reads versioned inputs and writes outputs plus a `.stage.json` with its input hash; skipped when the hash is unchanged):

1. **snapshot** → `snapshot.json`: allow-listed NFL tables only, canonical-sorted JSON, sha256.
2. **facts** → `facts.json`: selected players with every number pre-formatted, each with a stable `claim_id` and source (`table.column`, gsis_id, weeks).
3. **script** → `script.json` from the swappable LLM. Fields: `episode_id, title_candidates[3], claims[], narration_segments[], shots[], expected_duration_seconds, made_for_kids=false, synthetic_voice=true, cta`. **Narration references numbers only as `{{c12}}` placeholders**, which are filled from facts deterministically. Schema validation failure → up to 2 re-prompts → fail.
4. **tts** → `audio/seg_NN.wav` (Kokoro, 24 kHz) plus word timestamps (Kokoro's English pipeline returns token timings, so no Whisper is needed for captions).
5. **charts** → `shots/shot_NN.png` (matplotlib, Inter, a dark palette matching the dashboard), then `shot_NN.mp4` with motion (FFmpeg zoompan/pan/fade) sized to each segment's audio. Every shot moves, so the frozen-frame gate stays meaningful.
6. **assemble** → `final.mp4`: concat, loudness-normalized (`loudnorm` −14 LUFS), burned-in captions. Uses the libass `subtitles` filter if the brew ffmpeg build has it, otherwise Pillow-rendered caption PNG overlays (same burn-in result; checked in M2).
7. **thumbnail** → `thumbnail.png` 1280×720 (Pillow: big stat + player name, no photos or logos).
8. **metadata** → `metadata.json`: the 3 titles, description (with "Narrated with an AI voice.", nflverse attribution and the CTA), tags, `privacyStatus: private`, `selfDeclaredMadeForKids: false`, `containsSyntheticMedia: true`.
9. **qc** → `qc.json` (M3). Any failure blocks upload.
10. **provenance** → `provenance.json` (M3).
11. **upload** (M4) → `upload.json`.

**QC gates (M3)**
- *Data claims:* every placeholder resolves to a claim, and every claim value re-reads equal from `snapshot.json`. The final narration is scanned for **any numeral or spelled-out number not produced by a placeholder** → fail. A banned-phrase list also fails the episode ("fantasy points", "points per game", "PPG", "projected/projection", "routes", "first read").
- *Duration:* long 480–720 s, short 30–45 s.
- *Audio:* `silencedetect` (> 1.5 s), peak ≥ −0.1 dBFS (clipping), empty audio.
- *Video:* `blackdetect`, `freezedetect` (> 6 s), resolution/fps/codec via ffprobe.
- *A/V length:* streams within 0.25 s.
- *Subtitles:* each caption measured with Pillow at burn size; must fit 2 lines inside the title-safe area.
- *Similarity:* word 5-gram shingle Jaccard of the narration (CTA/intro boilerplate excluded) vs every `history.jsonl` entry, plus featured-player overlap. Over threshold → fail.
- *License gate:* the installed packages and checkpoint list are checked against an allowlist and the banned list.

**Provenance (M3):** git commit + dirty flag; studio and backend package versions; model names, HF repos, file sha256s and licenses (Qwen GGUF, Kokoro weights, voice id); `ffmpeg -version` + buildconf; data snapshot hash + nflverse sync run id; full prompts (system + user) and LLM params; sha256 of every output file.

**Upload (M4):** OAuth installed-app flow, token stored in `~/.config/fantasy-studio/` (never in the repo), resumable `videos.insert` with `privacyStatus` hard-coded to `private` (a test asserts no code path can set anything else), and `thumbnails.set`. Then poll `videos.list?part=processingDetails,status` until processing succeeds or fails. No publish and no schedule (`publishAt`) anywhere.

---

## Milestones (stop after each, show the result, commit)

Branch: **`feat/youtube-studio`** off `main`, in a new worktree `.worktrees/youtube-studio`, matching the repo's practice. Your current `feat/sleeper-foundation` checkout and its modified `.claude/launch.json` stay untouched.

1. **Plan + Phase 0** (this). Commit: this plan as `fantasy-intel/docs/youtube_studio_plan.md` + the research report.
2. **One long-form episode rendered locally end to end, no upload.** `brew install ffmpeg llama.cpp`, download the Qwen GGUF and Kokoro, then build stages 1–8. Run `sync_nfl.py`, then `make-episode --week 6 --format long`. Deliverable: `final.mp4`, thumbnail, 3 titles, metadata. Unit tests for facts selection, placeholder fill, schema and idempotency (FakeLLM, no network).
3. **QC gates + provenance.** Plus tests that feed deliberately broken inputs (a wrong number, a silent gap, a black frame, overflowing captions, a near-duplicate script) and assert each one fails.
4. **Private upload via the API.** You create the Google Cloud project and OAuth client (I can't create accounts or enter credentials) and submit the audit. Also builds the manual upload bundle.
5. **Shorts format.** Vertical charts plus `--format short`, 3–5 Shorts per week from the same facts.

**Realistic timeline:** M2 is the big one (~1–2 sessions of build plus your ~1 hr of review). A first private render around week 7 is realistic. Ongoing cost to you: about 30–45 min/week to sync, review and publish.

## Verification

- `cd fantasy-intel/studio && uv run pytest` (stages with FakeLLM, QC negative cases, the private-only assertion).
- Backend tests stay green (`cd backend && uv run pytest`); the studio only reads the DB.
- M2: watch `episodes/<id>/final.mp4`. ffprobe shows 1920×1080 with an audio stream and a duration of 8–12 min. Rerunning `make-episode` with no changes skips every stage.
- M3: `qc.json` all-pass on the real episode; each injected defect fails its gate.
- M4: the video appears as **Private** in YouTube Studio and `upload.json` shows `processingStatus: succeeded`.

## Risks you should know

- **YPP "inauthentic content":** a weekly templated show is exactly what YouTube scrutinizes. Mitigations: the facts change every week, the similarity gate, rotating structure and phrasing, your review, and (optionally later) your own voice via OpenVoice V2 for a recognizable host identity.
- **No site yet** means early videos build the audience but drive no traffic. The CTA URL is one config line once the site ships.
- **Small samples early on:** the role trend needs ≥4 games, so week-6 trends rest on 5 games. Scripts state the sample ("over his last 2 games").
