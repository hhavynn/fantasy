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
# 3. the episode (fantasy-intel/studio)
uv run make-episode --week 7 --format long
```

Outputs land in `episodes/<season>-wNN-long/`: `final.mp4`, `thumbnail.png`, `metadata.json`
(three title options, description with chapters, private-only settings), `captions.srt`, plus every
intermediate (`snapshot.json`, `facts.json`, `script.json`, `audio/`, `shots/`). Rerunning skips any
stage whose inputs did not change; `--force script,tts` reruns specific stages.

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
