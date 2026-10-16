# Presenter models: what is installed, why, and under what license

Every model the presenter layer uses runs locally. Each one is listed here, with its license and cost, before it is downloaded.

| Model | Role | License (code / weights) | Disk | Peak memory | Status |
|---|---|---|---|---|---|
| Z-Image-Turbo (Tongyi-MAI) via mflux | the host portrait (once) and the teeth-visible derivative (once) | Apache-2.0 / Apache-2.0; mflux MIT | about 24 GB (HF cache) | about 16 GB at q8 | installed |
| MuseTalk 1.5 via musetalk-mac | lip sync | MIT / MIT ("even commercially") | 3.8 GB | about 6 GB | installed |
| sd-vae-ft-mse, whisper-tiny, BiSeNet, Mediapipe | parts of MuseTalk | MIT, Apache-2.0, MIT, Apache-2.0 | included above | | installed |
| **Wan2.2-TI2V-5B (Wan-AI), diffusers** | **natural idle motion: a few reusable 4–5 s clips of the same portrait with real head, face, blink and shoulder movement, generated once** | **Apache-2.0 / Apache-2.0, not gated** | **34.2 GB** (transformer 20 GB fp32, UMT5 text encoder 11.4 GB, VAE 2.8 GB) | about 25–30 GB in bf16 on MPS with CPU offload; decode in a separate VAE-only process | installed; library pass 1 made (3 clips) |
| **LatentSync 1.6 (ByteDance)**, `latentsync_unet.pt` + `whisper/tiny.pt` only | **experiment C: 512 px lip sync (MuseTalk works at 256), to test whether real teeth and lower-face detail survive** | **Apache-2.0 code / OpenRAIL++ weights (commercial use allowed under use-based restrictions; our AI disclosure satisfies the transparency spirit)** | **5.15 GB** (SD VAE already installed) | about 18 GB per the authors; **about 57 GB measured on MPS** (512 px, guidance 1.5), swapping | installed, experiment only; impractical on 48 GB as is |

**LatentSync's face detection.** As shipped, inference uses InsightFace (`FaceAnalysis`, buffalo models, non-commercial) only to find three alignment points: the eyebrow centers and the nose. The experiment replaces that with Mediapipe face mesh (Apache-2.0). No InsightFace code or weights are installed. Its other auxiliary checkpoints (SyncNet, i3d, VGG and so on) are training and evaluation tools, and aren't downloaded.

## Why Wan2.2-TI2V-5B

The procedural base clip (affine sway on one still) can't produce non-rigid movement: cheeks, brows, neck and shoulders moving independently, or real blinks. That is the main reason the host reads as "a photo with a moving mouth".

| Candidate | Rejected because |
|---|---|
| LivePortrait | needs InsightFace detection and landmark weights, which are non-commercial |
| Hallo, EchoMimic | InsightFace, plus CUDA-centric |
| SadTalker | 3DMM look; GFPGAN and facexlib licensing to check |
| LTX-Video | its open-weights license limits commercial use by revenue |
| HunyuanVideo-I2V | territory and user-count restrictions |
| CogVideoX-5B-I2V | custom license |

Wan2.2-TI2V-5B is Apache-2.0, supports image-to-video from a single frame, and the 5B size fits in 48 GB of unified memory. It is slow on a Mac (tens of minutes per clip), which is acceptable for a motion library generated once and reused across videos.

## Not used (and why)

| Item | Reason |
|---|---|
| FLUX.1-schnell | gated on Hugging Face; FLUX dev, fill and kontext are non-commercial |
| LivePortrait / InsightFace | non-commercial face models |
| Wav2Lip and anything derived from it | banned for this channel |
| Kling, Replicate, ElevenLabs | paid APIs |
