"""The channel owner's own voice: enrollment, consent record and conversion.

Kokoro still performs the narration (timing, prosody, word timestamps); OpenVoice V2's tone-colour converter
(MIT code and weights, pinned commit) then re-voices it in an enrolled speaker's timbre. Duration is
preserved, so captions and animation stay in sync. Output is watermarked by OpenVoice (wavmark) as synthetic.

Rights: only a voice whose speaker consented can be used, and the plan allows only the channel owner's own.
Enrollment therefore requires the speaker's explicit declaration and writes voices/<name>/consent.json with
the hashes of the recordings and of the resulting embedding; QC refuses a cloned narration without a
matching, non-test consent record. Recordings and embeddings stay local (git-ignored).

    uv run enroll-voice --name havyn --i-am-the-speaker voices/havyn/raw/*.wav
"""

from __future__ import annotations

import argparse
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
import soundfile as sf

from .core import STUDIO_ROOT, sha256_file, utcnow, write_json, read_json

VOICES = STUDIO_ROOT / "voices"
CONVERTER_REPO = "myshell-ai/OpenVoiceV2"
OPENVOICE_COMMIT = "74a1d147b17a8c3092dd5430504bd83ef6c7eb23"
MIN_SECONDS, GOOD_SECONDS = 30.0, 90.0
DECLARATION = ("I am the person speaking in these recordings, I own them, and I consent to their use to create "
               "a synthetic version of my voice for narrating this channel's videos.")
# A neutral passage Kokoro reads to fingerprint its own voice (the conversion's source timbre).
KOKORO_REFERENCE = ("Here's what the usage data says this week. His snap share climbed, the targets followed, "
                    "and the role looks real. Two games is a small sample, so we'll keep watching. Some weeks "
                    "the box score lies, and the playing time tells you the truth before anyone else notices.")


@lru_cache(maxsize=1)
def converter():
    from huggingface_hub import hf_hub_download
    from openvoice.api import ToneColorConverter
    ck = hf_hub_download(CONVERTER_REPO, "converter/checkpoint.pth")
    cfg = hf_hub_download(CONVERTER_REPO, "converter/config.json")
    conv = ToneColorConverter(cfg, device="cpu")          # watermarking is on by default
    conv.load_ckpt(ck)
    conv._paths = {"checkpoint": ck, "config": cfg}
    return conv


def weights_info() -> dict:
    conv = converter()
    return {"repo": CONVERTER_REPO, "code_commit": OPENVOICE_COMMIT, "license": "MIT (code and weights)",
            "checkpoint_sha256": sha256_file(Path(conv._paths["checkpoint"])),
            "watermark": conv.watermark_model is not None}


def voice_dir(name: str) -> Path:
    return VOICES / name


def check_recordings(paths: list[Path]) -> tuple[list[dict], list[str]]:
    """Per-file stats and problems: too short overall, clipped, too quiet."""
    stats, problems = [], []
    total = 0.0
    for p in paths:
        x, sr = sf.read(p, always_2d=True)
        mono = x.mean(axis=1)
        dur = len(mono) / sr
        peak = float(np.max(np.abs(mono))) if len(mono) else 0.0
        rms = float(np.sqrt(np.mean(mono ** 2))) if len(mono) else 0.0
        total += dur
        stats.append({"file": p.name, "sha256": sha256_file(p), "seconds": round(dur, 2), "sample_rate": sr,
                      "peak": round(peak, 3), "rms_dbfs": round(20 * np.log10(rms + 1e-12), 1)})
        if peak >= 0.99:
            problems.append(f"{p.name}: clipped (peak {peak:.2f}); record a little quieter")
        if rms < 0.005:
            problems.append(f"{p.name}: very quiet; move closer to the microphone")
    if total < MIN_SECONDS:
        problems.append(f"only {total:.0f}s of speech; record at least {MIN_SECONDS:.0f}s (ideally {GOOD_SECONDS:.0f}s+)")
    return stats, problems


def enroll(name: str, paths: list[Path], declared: bool, test: bool = False) -> Path:
    """Build voices/<name>/se.pt from the recordings and write the consent record."""
    if not declared and not test:
        raise SystemExit("Enrollment needs the speaker's own declaration: pass --i-am-the-speaker.\n" + DECLARATION)
    stats, problems = check_recordings(paths)
    if problems and not test:
        raise SystemExit("Recordings need another take:\n- " + "\n- ".join(problems))
    import torch
    d = voice_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    se = converter().extract_se([str(p) for p in paths])
    torch.save(se.cpu(), d / "se.pt")
    write_json(d / "consent.json", {
        "speaker": name, "test": test,
        "declaration": None if test else DECLARATION, "declared_at": None if test else utcnow(),
        "note": "STAND-IN for pipeline testing only; QC blocks publishing with it." if test else
                "Channel owner's own voice, recorded and declared by the speaker.",
        "recordings": stats, "embedding_sha256": sha256_file(d / "se.pt"), "converter": weights_info(),
    })
    return d


def consent(name: str) -> dict | None:
    path = voice_dir(name) / "consent.json"
    return read_json(path) if path.exists() else None


def consent_problems(name: str) -> list[str]:
    """Why a cloned narration with this voice may not be published (empty list = fine)."""
    rec = consent(name)
    if rec is None:
        return [f"voice '{name}' has no consent record; run enroll-voice"]
    out = []
    if rec.get("test"):
        out.append(f"voice '{name}' is a test stand-in, not the channel owner's consented voice")
    if not rec.get("declaration"):
        out.append(f"voice '{name}' has no speaker declaration")
    se = voice_dir(name) / "se.pt"
    if not se.exists() or sha256_file(se) != rec.get("embedding_sha256"):
        out.append(f"voice '{name}' embedding does not match its consent record")
    return out


@lru_cache(maxsize=8)
def kokoro_source_se(kokoro_voice: str, speed: float = 1.1):
    """The conversion's source timbre: Kokoro's voice (or blend) fingerprinted from a fixed passage, cached."""
    import re
    import torch
    cache = VOICES / "_kokoro" / f"{re.sub(r'[^\w.]+', '_', kokoro_voice)}.pt"
    if cache.exists():
        return torch.load(cache)
    from kokoro import KPipeline
    from .stages.tts import kokoro_voice as voice_arg
    pipe = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
    audio = np.concatenate([r.audio.numpy() for r in pipe(KOKORO_REFERENCE, voice=voice_arg(pipe, kokoro_voice),
                                                          speed=speed)])
    cache.parent.mkdir(parents=True, exist_ok=True)
    wav = cache.with_suffix(".wav")
    sf.write(wav, audio, 24000)
    se = converter().extract_se([str(wav)])
    torch.save(se.cpu(), cache)
    return se


def convert(wav_in: Path, wav_out: Path, kokoro_voice: str, target: str, tau: float = 0.3) -> int:
    """Re-voice a Kokoro clip in the enrolled speaker's timbre. Returns the output sample rate."""
    import torch
    conv = converter()
    tgt = torch.load(voice_dir(target) / "se.pt")
    audio = conv.convert(str(wav_in), kokoro_source_se(kokoro_voice), tgt, tau=tau, message="usage-report")
    sr = conv.hps.data.sampling_rate
    sf.write(wav_out, audio, sr, subtype="PCM_16")
    return sr


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="enroll-voice", description="Enroll the channel owner's own voice.")
    ap.add_argument("--name", required=True)
    ap.add_argument("--i-am-the-speaker", action="store_true", help=DECLARATION)
    ap.add_argument("--test", action="store_true", help="a stand-in voice for pipeline testing; never publishable")
    ap.add_argument("recordings", nargs="+", type=Path)
    a = ap.parse_args(argv)
    d = enroll(a.name, a.recordings, a.i_am_the_speaker, a.test)
    rec = read_json(d / "consent.json")
    secs = sum(r["seconds"] for r in rec["recordings"])
    print(f"enrolled '{a.name}' from {len(rec['recordings'])} recording(s), {secs:.0f}s -> {d}")
    if secs < GOOD_SECONDS and not a.test:
        print(f"tip: {GOOD_SECONDS:.0f}s+ of clean speech gives a steadier voice")
    return 0


if __name__ == "__main__":
    sys.exit(main())
