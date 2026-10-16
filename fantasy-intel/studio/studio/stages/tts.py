"""Stage 4: Kokoro-82M narration, one WAV per segment, plus word timings for captions.

Pronunciation order: studio/pronunciations.toml (inline misaki phonemes) > misaki's lexicon > misaki's
espeak-ng fallback. Most player names are outside the lexicon, and without the fallback they would be
silent. espeak-ng is GPL-3.0; we run it locally and never distribute it, and the GPL does not reach the
audio it outputs. Every word that went through the fallback is listed in audio/timings.json
(`fallback_words`) for review; a bad one gets a pronunciations.toml entry.
"""

from __future__ import annotations

import re
import tomllib

import numpy as np
import soundfile as sf

from ..core import STUDIO_ROOT, Episode, read_json, run_stage, write_json

VERSION = 5
PRONUNCIATIONS = STUDIO_ROOT / "studio" / "pronunciations.toml"


def load_pronunciations() -> dict[str, str]:
    if not PRONUNCIATIONS.exists():
        return {}
    return tomllib.loads(PRONUNCIATIONS.read_text()).get("words", {})


def apply_pronunciations(text: str, lexicon: dict[str, str]) -> str:
    """Kokoro inline phonemes: `[Word](/phonemes/)`. Longest entries first so names beat their parts."""
    for word in sorted(lexicon, key=len, reverse=True):
        text = re.sub(rf"(?<![\w\[]){re.escape(word)}(?![\w\]])", f"[{word}](/{lexicon[word]}/)", text)
    return text


def make_pipeline(cfg: dict):
    from kokoro import KPipeline
    return KPipeline(lang_code="a", repo_id=cfg["repo"])


def lexicon_misses(text: str) -> list[str]:
    """Words misaki cannot phonemize from its own lexicon (they go to the espeak-ng fallback)."""
    from misaki import en
    g2p = en.G2P(trf=False, british=False, fallback=None, unk="")
    _, tokens = g2p(text)
    return sorted({t.text for t in tokens if re.search(r"[A-Za-z]", t.text) and not t.phonemes})


def synthesize(pipe, text: str, cfg: dict, speed: float | None = None) -> tuple[np.ndarray, list[dict], list[str]]:
    sr = cfg["sample_rate"]
    chunks, words, unknown, offset = [], [], [], 0.0
    for result in pipe(text, voice=cfg["voice"], speed=speed or cfg["speed"], split_pattern=r"\n+"):
        audio = result.audio.detach().cpu().numpy() if hasattr(result.audio, "detach") else np.asarray(result.audio)
        for tok in result.tokens or []:
            if re.search(r"[A-Za-z]", tok.text) and not tok.phonemes:
                unknown.append(tok.text)          # silent even with the fallback
            if tok.start_ts is not None and tok.end_ts is not None and tok.text.strip():
                words.append({"text": tok.text, "start": round(offset + tok.start_ts, 3),
                              "end": round(offset + tok.end_ts, 3), "space": bool(tok.whitespace)})
        chunks.append(audio)
        offset += len(audio) / sr
    audio = np.concatenate(chunks).astype(np.float32)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak > 0.95:                       # Kokoro occasionally overshoots full scale on a stressed word
        audio *= 0.95 / peak
    if cfg.get("trim") and words:
        # Kokoro pads each clip with ~0.15 s of silence and lets the last word ring out; trim to the speech
        # (with a short breath either side) so beats run together without dead air.
        start = max(min(w["start"] for w in words) - 0.05, 0.0)
        end = min(max(w["end"] for w in words) + 0.08, len(audio) / sr)
        audio = audio[int(start * sr):int(end * sr)]
        fade = int(0.01 * sr)
        audio[:fade] *= np.linspace(0, 1, fade)
        audio[-fade:] *= np.linspace(1, 0, fade)
        words = [{**w, "start": round(w["start"] - start, 3), "end": round(w["end"] - start, 3)} for w in words]
    tail = np.zeros(int(cfg["segment_tail_silence"] * sr), dtype=np.float32)
    return np.concatenate([audio, tail]).astype(np.float32), words, unknown


def _run(ep: Episode) -> None:
    script = read_json(ep.path("script.json"))
    cfg = tts_config(ep)
    lexicon = load_pronunciations()
    pipe = make_pipeline(cfg)
    out_dir = ep.path("audio")
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.wav"):        # a shorter script must not leave earlier segments behind for QC
        old.unlink()                         # and provenance to pick up
    timings, unknown = {"sample_rate": cfg["sample_rate"], "voice": cfg["voice"], "segments": []}, {}
    fallback_words: set[str] = set()
    for seg in script["narration_segments"]:
        fallback_words |= set(lexicon_misses(seg["text_spoken"])) - set(lexicon)
        tail = seg.get("pause", cfg["segment_tail_silence"])
        parts = seg.get("spoken_parts") or [seg["text_spoken"]]
        audio, words, missing = None, [], []
        for i, part in enumerate(parts):
            # each part trimmed to its speech, then the planned silence: within a line, part_pause; after it,
            # the line's own pause
            gap = seg.get("part_pause", 0.3) if i < len(parts) - 1 else tail
            a, w, m = synthesize(pipe, apply_pronunciations(part, lexicon), {**cfg, "segment_tail_silence": gap},
                                 seg.get("speed"))
            off = 0.0 if audio is None else len(audio) / cfg["sample_rate"]
            words += [{**x, "start": round(x["start"] + off, 3), "end": round(x["end"] + off, 3)} for x in w]
            missing += m
            audio = a if audio is None else np.concatenate([audio, a])
        if missing:
            unknown[seg["id"]] = sorted(set(missing))
            continue
        wav = out_dir / f"{seg['id']}.wav"
        sf.write(wav, audio, cfg["sample_rate"], subtype="PCM_16")
        duration = len(audio) / cfg["sample_rate"]
        if cfg.get("clone"):
            # Re-voice in the enrolled speaker's timbre; the converter keeps the timing, so the word
            # timestamps still hold. Kept at the converter's rate; the renderer resamples.
            from ..voice import convert
            kokoro_wav = out_dir / f"{seg['id']}.kokoro.wav"
            wav.rename(kokoro_wav)
            sr_out = convert(kokoro_wav, wav, cfg["voice"], cfg["clone"])
            kokoro_wav.unlink()
            duration = sf.info(wav).frames / sr_out
        timings["segments"].append({"id": seg["id"], "duration": round(duration, 3), "words": words})
    if unknown:
        write_json(ep.path("tts_unknown_words.json"), unknown)
        raise RuntimeError(
            "words Kokoro could not pronounce at all (they would be silent): "
            f"{sorted({w for ws in unknown.values() for w in ws})}. Add them to studio/pronunciations.toml.")
    timings["fallback_words"] = sorted(fallback_words)
    write_json(out_dir / "timings.json", timings)
    total = sum(s["duration"] for s in timings["segments"])
    ep.log(f"  tts: {len(timings['segments'])} segments, {total:.1f}s of narration; "
           f"{len(fallback_words)} words via espeak-ng fallback")


def tts_config(ep: Episode) -> dict:
    cfg = dict(ep.cfg["tts"])
    if ep.fmt == "short":
        sh = ep.cfg["shorts"]
        cfg.update(segment_tail_silence=sh["segment_tail_silence"], voice=sh.get("voice", cfg["voice"]),
                   trim=sh.get("trim_silence", False), clone=sh.get("voice_clone") or None)
    return cfg


def run(ep: Episode) -> None:
    pron = PRONUNCIATIONS.read_text() if PRONUNCIATIONS.exists() else ""
    cfg = tts_config(ep)
    if cfg.get("clone"):
        from ..voice import consent
        rec = consent(cfg["clone"])
        if rec is None:
            raise RuntimeError(f"voice_clone '{cfg['clone']}' is not enrolled; run enroll-voice first")
        cfg = {**cfg, "clone_embedding": rec["embedding_sha256"]}     # re-render when the voice changes
    run_stage(ep, "tts", VERSION, {"tts": cfg, "pronunciations": pron},
              ["script.json"], ["audio"], lambda: _run(ep))
