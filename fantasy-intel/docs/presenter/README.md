# AI presenter layer (experimental)

An optional, fully local AI host that delivers the opening line (`hook`), or the opening and the closing verdict (`hybrid`), of a finished Short. It sits on top of the existing pipeline:

- `off` is the default, and with it nothing changes.
- `final.mp4` is never modified.
- The narration, claims and timing are reused as-is.

**Verdict: it works end to end, but it isn't production-ready.** It looks like a competent AI avatar, not a person (see "How good it looks").

## Realism pass 2: audit, fixes and A/B (2026-10-15)

Current defaults: `mode = "off"`, `engine_mode = "hq"` and `motion = "library"` (variant D, approved from the montages on 2026-10-15). Nothing reaches a publishable file while `mode` is off; `engine_mode = "musetalk"` and `motion = "procedural"` remain the fallbacks.

### Root causes (measured, not guessed)

| Artifact | Cause | Evidence |
|---|---|---|
| Soft, "beauty-filtered" lower face | MuseTalk generates the face at 256 px, pastes it back about 1.5× upscaled, and re-encodes it lossily | Lower/upper-face Laplacian ratio 0.05 in a generated frame, against 0.55 in the base frame |
| Gap between the front teeth | At 256 px each tooth is a few pixels, and the closed-mouth portrait gives the model nothing to copy, so it invents teeth | Present in all four variants below. A teeth-visible reference portrait (experiment A, `~/models/presenter/teeth/full_teeth_37.png`) changed the tooth shapes but not the gap |
| Rigid "photo with a moving mouth" look | Idle motion was an affine sway of one still image: no cheeks, brows, neck, shoulders or blinks | Head-motion range 1.6–2.6% of face width, all rigid |
| Stutter | 25 fps MuseTalk clips mapped onto 30 fps video by nearest frame, so every fifth frame repeats | `_composite` frame mapping |

### What changed

- **Mouth (`engine_mode = "hq"`, experiment B):** our own MuseTalk loop (`presenter_worker.py lipsync_hq`) that:
  - runs at the video's fps (30), so frames map 1:1 onto the video
  - writes lossless FFV1 and resizes with LANCZOS4
  - restores skin detail: the base frame's high-pass is warped onto the generated face by optical flow and added only where detail was lost, never on the lips, feathered at the edges
- **Motion (`motion = "library"`):** idle clips of the same portrait made once with **Wan2.2-TI2V-5B** (Apache-2.0; see `models.md`) by `studio/presenter_motion.py`. Each clip starts from `host.png`, so identity is anchored to it.
- **Library audit (new, in `presenter.py`):** pass 1 of the library showed the model's failure modes, so every clip is audited (Mediapipe) and only its good stretches are used:
  - **Lid drops:** a run of frames with the eyes under 80% of the clip's median opening that lasts longer than 0.4 s (the upper end of a spontaneous blink) reads as sleepy, and is cut. idle_02 shut its eyes for about 1.6 s; idle_03 for 0.6 s plus a slow reopen.
  - **Gaze:** the iris position across the eye is compared with frame 0 (the portrait, which looks into the lens). Calibrated on idle_01: 0.021 off still reads as eye contact, 0.035 clearly looks away, so the cutoff is 0.025. idle_01 looks away after 0.67 s.
  - Stretches shorter than 1.5 s aren't used. If no clip passes, the host falls back to procedural motion.
  - Library frames also map 1:1 (24 fps source played at 30 fps, so 1.25× speed) instead of repeating frames.
- **Review tooling:**
  - Tagged renders: `apply(..., tag=...)` writes `final_presenter_<mode>-<tag>.mp4` and never writes metadata.
  - `studio/presenter_review.py` builds the side-by-side montages.
  - `realism_metrics()` records measurements per window in `presenter_<mode>.json`.

### A/B on the Allen host Short (hybrid: hook + verdict)

Everything is in `studio/episodes/presenter-review/`:

| Folder | What it is |
|---|---|
| `A_baseline` | the committed baseline (`8f7e69d`) |
| `A_rerun` | the same path re-run to get its measurements |
| `B_motion` | library motion + MuseTalk |
| `C_mouth` | procedural motion + hq mouth |
| `D_combined` | library motion + hq mouth |
| `phone_grid.mp4` | all four side by side at phone size, with audio |
| `face_zoom.mp4` | the host windows only, faces enlarged |

Measurements for the verdict window (s05, 6.1 s):

| | A | B | C | D |
|---|---|---|---|---|
| Lower-face detail vs base frame (1 = as sharp as the base) | 0.09 | 0.12 | 0.25 | **0.32** |
| Head motion range (% of face width) | 2.6 | 3.0 | 3.1 | 3.4 |
| Landmark jitter (‰ of face width per frame²) | 1.37 | 1.28 | 1.99 | 1.65 |
| Identity geometry drift (%) | 4.4 | 6.2 | 5.1 | 6.4 |
| Max face colour jump | 1.08 | 1.18 | 0.97 | 1.20 |
| Teeth gap index | 0.28 | 0.29 | 0.27 | 0.31 |
| Render time, whole video | 118 s | 73 s | 145 s | 79 s |

How to read these:
- **Detail** is the clear win: hq triples it, and stubble is visible again in the zoom.
- **Motion** (library pass 1, in this table) is real but small: only idle_03's first 2 s passed the audit, so the 6.1 s line looped that stretch forward and back. See "Library pass 2" below for the re-render with the fuller library.
- **Jitter and identity drift** rise a little with real motion, which is expected: these are a description, not a pass mark.
- **The teeth gap index** doesn't separate the variants, and neither do your eyes: the gap is in all four. It stays uncalibrated until a render without the gap exists.
- **The loop-seam ratio** (recorded too) only catches jumps, not the velocity reversal of a ping-pong loop, so it isn't a useful gate yet.

All variants pass the existing presenter QC; no gate was loosened. The Gadsden lip-sync gate is untouched.

### Teeth: still unsolved

- **Experiment A (teeth-visible portrait):** didn't remove the gap.
- **Experiment B (inference and blending):** made the mouth area sharper, and the gap with it.
- **Experiment C (LatentSync 1.6, 512 px):**
  - The integration works. `studio/presenter_latentsync.py` patches it to use Mediapipe instead of InsightFace, MPS instead of CUDA, and no decord; tests check that InsightFace is never loaded.
  - It isn't practical on this Mac: at 512 px with guidance it peaked at about 57 GB and swapped at about 7 minutes per denoising step (more than a day for one line). The run was stopped.
  - What would make it practical: guidance 1.0 (half the batch) plus chunked attention, which is a patch to its UNet, or a CUDA machine.

### Performance (M5 Pro, 48 GB)

| Step | Time |
|---|---|
| Wan2.2 library clip (81 frames, 704×1280, 30 steps) | about 20 min to denoise and 8–17 min to decode, once per clip (pass 2: 1153–1285 s and 453–518 s) |
| Decode | each clip in its own process (VAE only), with decoded tiles moved to the CPU, otherwise 48 GB runs out |
| hq lip sync | about 1.2–2× the time of the original MuseTalk path, per the table above |

### Library pass 2 (idle_04–06)

The pass-2 prompts asked for eye contact throughout and one quick blink, plus negatives for closed eyes, squinting and looking away. All three clips pass the audit end to end (3.4 s each):
- gaze stays within 0.02 of the portrait
- idle_06 has one quick blink and a slight head turn

The library now has about 12 usable seconds instead of 2. B and D were re-rendered: the verdict line now uses a pass-2 clip, while the hook still draws idle_03's audited stretch (the pick is a stable hash of episode and line).

Verdict window (s05), after the re-render:

| | B | D |
|---|---|---|
| Lower-face detail vs base | 0.09 | 0.30 |
| Head motion range (% of face width) | 2.6 | 2.7 |
| Landmark jitter | 1.09 | 1.56 |
| Identity geometry drift (%) | 3.4 | 4.7 |

The new clips are calmer than idle_01's sideways drift: there's less raw motion, but it's natural, non-rigid movement that keeps eye contact.

### Recommendation

- **The hq mouth (C or D):** ready to approve. It's a clear, low-risk improvement.
- **Library motion:** with pass 2 the library is usable (about 12 s that pass the audit). D, the hq mouth plus library motion, is the candidate to approve: switch `motion = "library"` after you review the updated montages.
- **Keep `mode = "off"`** until D passes your review. The teeth gap remains the most visible tell.

## Update: the host's own voice

You picked **`am_adam`** (voice 7) from the 8-voice audition (`studio/episodes/presenter-audition/voice_audition.mp4`), set as `[presenter] voice` in `config.toml`.

- **How host versions are built:** they're re-rendered as `<slug>-host`, with the same script, claims, hook and scenes, narrated by `am_adam` and re-timed to it. Then the host is added on top.
- **What stays the same:** the regular Shorts and their fenrir `final.mp4` are untouched.
- **What the invariants check:** the host version's own audio and timing against its re-voiced `final.mp4`.

| Video | Host hook | Host hybrid |
|---|---|---|
| Allen (`2026-w06-short-braelon-allen-host/`) | QC passed, lip-sync correlation 0.56 | QC passed |
| Gadsden (`2026-w06-short-oronde-gadsden-ii-host/`) | withheld | withheld |

**Correction about Gadsden.** I said earlier that his withheld clip "looked in sync". The per-frame data proves otherwise. With both voices, the mouth stays closed through "snaps both weeks" and opens wide in the silent pause after it. The gate was right to withhold it. This line ("Same snaps both weeks. 8 targets, then 1.") is one MuseTalk handles badly, for reasons I haven't pinned down.

## Results on the two publish-ready videos (first pass, fenrir voice)

| Video | Original | Hook | Hybrid |
|---|---|---|---|
| Allen | `final.mp4` (unchanged; hash matches its QC) | `final_presenter_hook.mp4`: QC passed | `final_presenter_hybrid.mp4`: QC passed |
| Gadsden | `final.mp4` (unchanged; hash matches its QC) | withheld: QC failed (lip-sync gate, borderline) | withheld: same opening clip |

All files live in `studio/episodes/2026-w06-short-<slug>/`. The withheld Gadsden renders are kept as `final_presenter_*.rejected.mp4` so you can judge them yourself.

- **Why Gadsden was withheld:** his opening clip's mouth-opening contrast was 0.009 against a 0.01 bar. Frame by frame it looked in sync to me. I didn't relax the gate after seeing the result.
- **What every variant guarantees:**
  - Its audio is a byte-identical stream copy of the original.
  - It has the same frame count (623 and 629), at 1080×1920 / 30 fps.
  - Its captions are redrawn from the same word timings with the renderer's own code.

## What was chosen, and why

| Candidate | Verdict |
|---|---|
| **musetalk-mac** (barnent1) | **Used.** A complete MuseTalk 1.5 port to PyTorch MPS. It replaces mmpose/mmcv with Mediapipe, which avoids the classic Mac build failures. It's a small personal repo (2 commits). |
| musetalk-mlx (xocialize) | Not used. Only the UNet, VAE and Whisper are ported. Face detection, cropping, blending and paste-back would all be ours to build. |

**Problems found and handled:**

- **The published musetalk-mac repo is missing `upstream/musetalk/models/`** (`unet.py`, `vae.py`), so it can't run as cloned. I copied that folder from the official TMElyralab/MuseTalk repo (MIT) and removed its training-only `syncnet.py`.
- **musetalk-mac's idle-clip pipeline** uses Kling via Replicate (paid), LivePortrait (whose InsightFace models are non-commercial) and ElevenLabs (paid). None of that is used. Idle motion is procedural instead (below).
- **FLUX.1-schnell is gated on Hugging Face** (login plus terms acceptance), so the portrait uses **Z-Image-Turbo** (Tongyi-MAI, Apache-2.0, ungated) through `mflux` on MLX.
- **Wav2Lip is banned for this channel.** MuseTalk ships a training-only audio module copied from Wav2Lip (`musetalk/data/audio.py`). The worker checks after every run that it was never imported, and QC fails the variant if it was.

**Licenses** (recorded in each `presenter_<mode>.json`):

| Component | License |
|---|---|
| MuseTalk 1.5 code and weights | MIT; the model card says "even commercially" |
| sd-vae-ft-mse | MIT |
| whisper-tiny | Apache-2.0 |
| BiSeNet face parsing | MIT |
| Mediapipe | Apache-2.0 |
| Z-Image-Turbo | Apache-2.0 |
| mflux | MIT |

There are no paid APIs and no network calls at render time.

## The host

- **Blinks:** synthetic blinks were tried and switched off after review (they looked wrong in motion).
- **Portrait:** `studio/assets/presenter/host.png` (seed 37, prompt and model in `host.json`). A fictional man in his early thirties in a sports-studio set, chosen from four candidates. All four seeds produced nearly the same face, which is good for consistency.
- **Consistency:** every video uses this exact file. Regenerating it would change him, so don't.
- **Not a real person:** he's generated from a generic prompt with no reference image. I can't fully rule out an accidental resemblance to someone.
- **Disclosure:**
  - The variant's `metadata_presenter_<mode>.json` adds "The on-screen host is an AI-generated character, not a real person."
  - An "AI HOST" tag is on screen.
  - When uploading, tick YouTube's altered/synthetic-content box, which applies to realistic synthetic people.

## Architecture

```
finished episode (unchanged)            presenter layer (studio/presenter.py)
  final.mp4, audio/sNN.wav,     ──▶  1. landmarks of host.png (Mediapipe, via worker)
  audio/timings.json,                2. base clip: 6 s of procedural sway, breathing and two synthetic
  work/timeline.json,                   blinks (MuseTalk plays it forward then back, so it loops)
  script.json, facts.json            3. lip sync per window: MuseTalk 1.5 on MPS, driven by the window's
                                        existing segment WAV (studio/presenter_worker.py, run in
                                        ~/models/musetalk-mac/.venv with python -I)
                                     4. composite into final.mp4's frames: the host shot (his own studio
                                        background extended behind him), the scene still playing in a
                                        card above him, captions redrawn under his chin, "AI HOST" tag,
                                        4-frame dissolves in and out
                                     5. presenter QC; pass → final_presenter_<mode>.mp4 + metadata,
                                        fail → *.rejected.mp4, final.mp4 stays the publishable file
```

**Windows** come from `work/timeline.json`:
- `hook`: the first scene's narration line.
- `hybrid`: also the last scene (the verdict or fork).

**Presenter QC** (in `presenter_<mode>.json`):

| Check | What it verifies |
|---|---|
| clip exists | clip is present and has the frames the audio needs |
| not corrupt | frame brightness and variance are in range |
| face tracked | Mediapipe finds his face in at least 95% of frames |
| mouth moves | the mouth region actually changes |
| lip sync tracks audio | landmark mouth opening is higher while he speaks than in pauses (contrast > 0.01) and correlates with loudness (> 0.15). A sanity gate, not SyncNet. |
| no flicker | face brightness never jumps by 6 or more |
| no banned modules | the Wav2Lip-derived module never loaded |
| layout | face inside the safe zone, below the card and above the captions |
| invariants | audio stream byte-identical, same frame count, 1080×1920 |

## How to run

**One-time setup** (already done on this Mac):

```bash
git clone --depth 1 https://github.com/barnent1/musetalk-mac ~/models/musetalk-mac
```

```bash
cd ~/models/musetalk-mac && uv venv --python 3.11 .venv && source .venv/bin/activate && uv pip install "torch>=2.5" torchvision torchaudio "numpy>=1.26,<2.0" mediapipe==0.10.14 diffusers==0.30.2 accelerate==0.28.0 transformers==4.39.2 huggingface_hub==0.30.2 opencv-python==4.9.0.80 soundfile librosa==0.11.0 einops omegaconf gdown "imageio[ffmpeg]" ffmpeg-python && bash download_weights_mac.sh
```

```bash
git clone --depth 1 https://github.com/TMElyralab/MuseTalk ~/models/MuseTalk-official && cp -r ~/models/MuseTalk-official/musetalk/models ~/models/musetalk-mac/upstream/musetalk/ && rm ~/models/musetalk-mac/upstream/musetalk/models/syncnet.py
```

**Add the host to finished Shorts** (from `fantasy-intel/studio`):

```bash
uv run make-presenter 2026-w06-short-braelon-allen --mode hybrid
```

**Or as part of a render:**

```bash
STUDIO_DB=../data/fantasy.duckdb uv run make-episode --week 7 --format short --presenter hook
```

The default stays `[presenter] mode = "off"` in `config.toml`.

## Benchmarks (M5 Pro, 48 GB)

| Step | Time |
|---|---|
| MuseTalk weights download | 3.8 GB in 50 s |
| Portrait generation (Z-Image-Turbo, 832×1216, 9 steps, q8) | about 42 s per image, after a one-time model download |
| Landmarks | 1.6 s, once |
| Base clip (150 frames) | about 10 s, once |
| Lip sync, Allen hook (2.3 s of audio), first run | 51 s, including one-time face detection on the base clip |
| Lip sync, later clips (3.3–6.5 s of audio) | 39–46 s each, about 7–15× slower than real time; roughly 25–30 s is model loading on every call |
| Composite, 21 s video | 6 s (hook), 13 s (hybrid) |
| Full hybrid from scratch | about 1.5–2 min per video |
| Full hybrid, cached rerun | about 13 s |

## How good it looks

**Works:**
- At phone size the upper face (eyes, skin, hair, the studio) is photoreal, and the synthetic blinks read as blinks.
- The layout keeps the stat on screen in a card above him, so the hook still lands.
- The mouth opens on speech and closes in pauses.

**Looks fake:**
- **Mouth:** the lower face is visibly softer than the upper face, because MuseTalk regenerates it at 256 px. A dark gap between his front teeth keeps appearing, because the portrait's mouth is closed and the model invents the teeth. Lip shapes are generic rather than crisp per sound.
- **Body:** he barely moves. The procedural sway and breathing are subtle, with no shoulder movement or gestures, so he reads as unnaturally still.
- **Voice and face:** Kokoro's calm read paired with a neutral face gives the "AI avatar ad" feel. Nothing in his face reacts to what he's saying.
- **The card:** at 56% scale the verdict graphic is smaller than in the original (the fork stays readable, but the original's ending is stronger).

Overall it's comparable to free AI-avatar tools. Fantasy viewers who scroll Shorts will likely recognize it as an AI avatar within a second or two.

## Is it ready?

**Not for regular batches.** Keep `off` as the default.

**For a small test:** if you want one, post Allen's *hook* variant against the original and compare 3-second hold and average view duration. Only real numbers can show whether a recognizable AI host helps or hurts. Tick the synthetic-content disclosure.

**Highest-value next steps, in order:**

1. **A real idle base clip** (a few seconds of natural head movement and breathing) instead of procedural motion. Options: a local image-to-video model with a commercial-safe license, or a short self-recorded or licensed actor clip, which would make it a real person and needs consent.
2. **A portrait with a slight open-mouth smile** showing teeth, to stop MuseTalk inventing them.
3. **Keep MuseTalk loaded** (the repo's server mode) to cut about 25 s per clip.
4. **A proper lip-sync score**, such as an Apache/MIT-licensed SyncNet with its license checked, instead of the landmark sanity gate.
