"""The flame / praise series' comedic sound palette: meme-style effects for punchlines, reveals and payoffs.

Two sources, both cleared for monetized use on YouTube, TikTok and Instagram (assets/sfx/manifest.json is the
machine-readable record, with sha256, source, license, attribution and platforms for every file):

  recorded  CC0 recordings in assets/sfx/: Kenney "Impact Sounds" and "Interface Sounds" (kenney.nl, CC0 1.0) and
            two BigSoundBank crowd recordings (bigsoundbank.com, CC0). No attribution required; credited anyway.
  original  effects synthesized here, in the styles short-form editors use (a distorted bass "boom", a ringing
            metal-pipe clang, a cartoon bonk and boing, a slide whistle, a "wah wah" trombone, a cinematic hit, a
            choir "aah", an air horn). The viral originals (the Vine boom, the metal pipe clip) have no verifiable
            source or license, so none of them is copied: these are new sounds in the same comedic idiom.

Every effect has a semantic purpose (the beat it serves: punchline, stat_reveal, roast_impact, verdict, ...), a tone
(flame, praise or either), a class (impact, cartoon, sting, crowd) and an `attack`: the seconds from the start of
the file to the moment the ear hears the hit, so the hit, not the file, lands on the word.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import soundfile as sf

from .core import ASSETS

SR = 48000
SFX_DIR = ASSETS / "sfx"
SYNC_OFFSET_S = 0.0           # global nudge for every cue (negative = earlier); see README, "Sound design"

# Recorded files: (file, source, url, license, author)
RECORDED = {
    "kenney_impactMetal_heavy_001.wav": ("Kenney Impact Sounds 1.0", "https://kenney.nl/assets/impact-sounds"),
    "kenney_impactMetal_heavy_003.wav": ("Kenney Impact Sounds 1.0", "https://kenney.nl/assets/impact-sounds"),
    "kenney_impactWood_heavy_000.wav": ("Kenney Impact Sounds 1.0", "https://kenney.nl/assets/impact-sounds"),
    "kenney_impactWood_heavy_002.wav": ("Kenney Impact Sounds 1.0", "https://kenney.nl/assets/impact-sounds"),
    "kenney_impactPlate_heavy_001.wav": ("Kenney Impact Sounds 1.0", "https://kenney.nl/assets/impact-sounds"),
    "kenney_impactPlate_heavy_003.wav": ("Kenney Impact Sounds 1.0", "https://kenney.nl/assets/impact-sounds"),
    "kenney_impactPunch_heavy_002.wav": ("Kenney Impact Sounds 1.0", "https://kenney.nl/assets/impact-sounds"),
    "kenney_impactPunch_heavy_004.wav": ("Kenney Impact Sounds 1.0", "https://kenney.nl/assets/impact-sounds"),
    "kenney_bong_001.wav": ("Kenney Interface Sounds 1.0", "https://kenney.nl/assets/interface-sounds"),
    "kenney_error_005.wav": ("Kenney Interface Sounds 1.0", "https://kenney.nl/assets/interface-sounds"),
    "kenney_error_006.wav": ("Kenney Interface Sounds 1.0", "https://kenney.nl/assets/interface-sounds"),
    "kenney_toggle_001.wav": ("Kenney Interface Sounds 1.0", "https://kenney.nl/assets/interface-sounds"),
    "kenney_scratch_004.wav": ("Kenney Interface Sounds 1.0", "https://kenney.nl/assets/interface-sounds"),
    "bsb_0236_teens_cheer.wav": ("BigSoundBank #0236 'Shouts and Applauses of Teens #1' (Joseph Sardin)",
                                 "https://bigsoundbank.com/shouts-and-applauses-of-teens-1-s0236.html"),
    "bsb_2479_applause.wav": ("BigSoundBank #2479 'Applause Concert Bar #1' (Joseph Sardin)",
                              "https://bigsoundbank.com/applause-concert-bar-1-s2479.html"),
}
PLATFORMS = ["youtube", "youtube_shorts", "tiktok", "instagram_reels", "facebook", "x"]


# ------------------------------------------------------------------------------------------ building blocks

def _t(seconds: float) -> np.ndarray:
    return np.arange(int(seconds * SR)) / SR


def _env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    return np.clip(t / max(attack, 1e-4), 0, 1) * np.exp(-t / max(decay, 1e-4))


def _glide(f0: float, f1: float, seconds: float, curve: float = 3.0) -> np.ndarray:
    """Instantaneous frequency gliding from f0 to f1 (exponential-ish)."""
    k = np.linspace(0, 1, int(seconds * SR)) ** (1 / curve)
    return f0 * (f1 / f0) ** k


def _osc(freq: np.ndarray, shape: str = "sine") -> np.ndarray:
    ph = 2 * np.pi * np.cumsum(freq) / SR
    if shape == "saw":
        return 2 * ((ph / (2 * np.pi)) % 1.0) - 1
    if shape == "square":
        return np.sign(np.sin(ph))
    return np.sin(ph)


def _spectral(x: np.ndarray, gain_fn) -> np.ndarray:
    """Filter by shaping the spectrum (gain_fn(freqs) -> gains): formants and tone controls without scipy."""
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return np.fft.irfft(spec * gain_fn(f), len(x))


def _lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    return _spectral(x, lambda f: 1 / np.sqrt(1 + (f / cutoff) ** 4))


def _room(x: np.ndarray, seconds: float = 0.5, wet: float = 0.25, seed: int = 1) -> np.ndarray:
    """A short, dark room tail (exponentially decaying noise impulse) so hits don't sound dry and synthetic."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    ir = rng.standard_normal(n) * np.exp(-np.arange(n) / (SR * seconds / 5))
    ir = _lowpass(ir, 3500)
    ir /= np.sqrt(np.sum(ir ** 2)) + 1e-9
    m = len(x) + n
    tail = np.fft.irfft(np.fft.rfft(x, m) * np.fft.rfft(ir, m), m)      # FFT convolution, same length as out
    out = np.pad(x, (0, n)) + wet * tail
    return out


def _fade_end(x: np.ndarray, seconds: float = 0.12) -> np.ndarray:
    n = min(len(x), int(seconds * SR))
    x = np.array(x, dtype=np.float64)
    x[-n:] *= np.linspace(1, 0, n) ** 2
    return x


def _norm(x: np.ndarray, peak: float = 0.89) -> np.ndarray:
    return (x / (np.max(np.abs(x)) + 1e-9) * peak).astype(np.float32)


def _mixdown(layers: list[tuple[np.ndarray, float, float]]) -> np.ndarray:
    """[(signal, start seconds, gain)] -> one buffer."""
    n = max(int(at * SR) + len(sig) for sig, at, _ in layers)
    out = np.zeros(n, np.float32)
    for sig, at, g in layers:
        i = int(at * SR)
        out[i:i + len(sig)] += g * sig
    return out


@lru_cache(maxsize=None)
def recorded(name: str) -> np.ndarray:
    x, sr = sf.read(SFX_DIR / name, dtype="float32")
    if x.ndim > 1:
        x = x.mean(1)
    assert sr == SR, f"{name}: expected {SR} Hz"
    return x


def _pitch(x: np.ndarray, semitones: float) -> np.ndarray:
    """Resample-based pitch shift (duration changes with it, like a tape): fine for short hits."""
    if abs(semitones) < 1e-3:
        return x
    ratio = 2 ** (semitones / 12)
    idx = np.arange(0, len(x) - 1, ratio)
    return np.interp(idx, np.arange(len(x)), x).astype(np.float32)


# ------------------------------------------------------------------------------------------ the effects

def boom() -> np.ndarray:
    """A distorted bass drop with a tail, in the style of the 'Vine boom' reveal (an original sound). Saturated so
    its harmonics still read on a phone speaker."""
    t = _t(1.0)
    f = 48 + 95 * np.exp(-t / 0.05)
    body = _osc(f) * _env(len(t), 0.002, 0.33)
    click = _lowpass(np.random.default_rng(2).standard_normal(len(t)), 2500) * _env(len(t), 0.0005, 0.012) * 0.6
    x = _fade_end(np.tanh(3.2 * (body + click)), 0.35)
    x = _room(x, 0.7, 0.35)
    punch = recorded("kenney_impactPunch_heavy_002.wav")
    return _norm(_mixdown([(x, 0, 1.0), (punch, 0, 0.45)]))


def pipe_clang() -> np.ndarray:
    """A metal pipe hitting a concrete floor: a recorded metal strike, a ringing modal tail (free bar partials) and
    a second, smaller bounce. An original take on the 'metal pipe' joke sound."""
    rng = np.random.default_rng(7)
    t = _t(2.0)
    ring = np.zeros(len(t))
    for ratio, amp, dec in ((1.0, 1.0, 1.4), (2.756, 0.75, 0.95), (5.404, 0.5, 0.6), (8.933, 0.35, 0.4),
                            (13.34, 0.22, 0.26)):
        f = 610 * ratio * (1 + 0.0015 * rng.standard_normal())
        ring += amp * np.sin(2 * np.pi * f * t + rng.random() * 6) * np.exp(-t / dec)
    ring *= np.clip(t / 0.002, 0, 1)
    ring = _fade_end(ring, 0.4)
    strike = recorded("kenney_impactMetal_heavy_001.wav")
    bounce = _pitch(recorded("kenney_impactMetal_heavy_003.wav"), 2)
    x = _mixdown([(strike, 0, 1.0), (0.42 * ring, 0.004, 1.0), (bounce, 0.21, 0.55), (0.18 * _fade_end(ring[: int(0.9 * SR)], 0.3), 0.214, 1.0)])
    return _norm(_room(x, 0.6, 0.3))


def bonk() -> np.ndarray:
    """A cartoon bonk: a hollow wooden knock with a pitch that drops."""
    t = _t(0.35)
    tone = _osc(_glide(520, 180, 0.35, 1.5)) * _env(len(t), 0.001, 0.07)
    wood = recorded("kenney_impactWood_heavy_002.wav")
    bong = recorded("kenney_bong_001.wav")
    return _norm(_mixdown([(np.tanh(2 * tone), 0, 0.8), (wood, 0, 0.7), (bong, 0, 0.6)]))


def boing() -> np.ndarray:
    """A cartoon spring: a fast vibrato that dies away over a rising tone."""
    t = _t(0.75)
    depth = 140 * np.exp(-t / 0.22)
    f = 210 + 120 * (t / 0.75) + depth * np.sin(2 * np.pi * 24 * t)
    x = (_osc(f) + 0.3 * _osc(2 * f)) * _env(len(t), 0.003, 0.32)
    return _norm(np.tanh(1.5 * x))


def slide_whistle(direction: str = "down") -> np.ndarray:
    """A slide whistle: a breathy sine sliding down (it fell short) or up."""
    dur = 0.75 if direction == "down" else 0.5
    f = _glide(1900, 480, dur, 1.6) if direction == "down" else _glide(520, 1900, dur, 0.8)
    t = _t(dur)
    f = f * (1 + 0.012 * np.sin(2 * np.pi * 6.5 * t))
    breath = _spectral(np.random.default_rng(3).standard_normal(len(t)), lambda fr: np.exp(-((fr - 2500) / 1500) ** 2))
    x = _osc(f) + 0.012 * breath / (np.std(breath) + 1e-9)
    env = np.clip(t / 0.03, 0, 1) * np.clip((dur - t) / 0.08, 0, 1)
    return _norm(x * env)


def _svf_lowpass(x: np.ndarray, cutoff: np.ndarray, q: float = 2.2) -> np.ndarray:
    """A resonant state-variable low-pass whose cutoff moves per sample: the 'wah' of a plunger mute."""
    y = np.empty_like(x)
    low = band = 0.0
    damp = 1 / q
    for i, v in enumerate(x):
        f = 2 * np.sin(np.pi * min(cutoff[i], SR / 6) / SR)
        high = v - low - damp * band
        band += f * high
        low += f * band
        y[i] = low
    return y


def sad_trombone() -> np.ndarray:
    """'Wah wah wah waaah': four descending semitones on a muted brass voice, each one a plunger 'wah', the last
    held with vibrato."""
    notes = [(62, 0.34), (61, 0.34), (60, 0.34), (59, 1.2)]
    parts, at = [], 0.0
    for i, (m, dur) in enumerate(notes):
        t = _t(dur)
        f0 = 440 * 2 ** ((m - 69) / 12)
        vib = 1 + (0.02 * np.sin(2 * np.pi * 5.0 * t) * np.clip((t - 0.25) / 0.3, 0, 1) if i == 3 else 0 * t)
        saw = _osc(np.full(len(t), f0) * vib, "saw")
        opening = np.sin(np.pi * np.clip(t / min(dur, 0.34), 0, 1)) if i < 3 else \
            np.clip(t / 0.15, 0, 1) * (0.65 + 0.35 * np.sin(2 * np.pi * 2.5 * t))
        x = _svf_lowpass(saw, 350 + 1500 * opening, 2.4)
        env = np.clip(t / 0.03, 0, 1) * np.clip((dur - t) / 0.06, 0, 1)
        parts.append((np.tanh(1.6 * x) * env, at, 1.0))
        at += dur + 0.03
    return _norm(_room(_mixdown(parts), 0.4, 0.2))


def record_scratch() -> np.ndarray:
    from .series_audio import sfx as base
    return _norm(_mixdown([(base("scratch"), 0, 1.0), (recorded("kenney_scratch_004.wav"), 0, 0.6)]))


def cinematic_hit() -> np.ndarray:
    """A trailer hit: a sub drop, a dark noise burst and a punch, with a long tail."""
    t = _t(1.6)
    sub = _osc(_glide(90, 32, 1.6, 1.2)) * _env(len(t), 0.004, 0.6)
    burst = _lowpass(np.random.default_rng(4).standard_normal(len(t)), 1800) * _env(len(t), 0.002, 0.08) * 0.5
    x = _fade_end(np.tanh(2.4 * (sub + burst)), 0.5)
    return _norm(_room(_mixdown([(x, 0, 1.0), (recorded("kenney_impactPunch_heavy_004.wav"), 0, 0.6)]), 1.2, 0.4))


def choir() -> np.ndarray:
    """A heavenly 'aah': a detuned major chord shaped by the formants of the vowel, swelling in and out."""
    t = _t(1.9)
    x = np.zeros(len(t))
    for m in (72, 76, 79, 84):
        for cents in (-9, 0, 8):
            x += _osc(np.full(len(t), 440 * 2 ** ((m - 69 + cents / 100) / 12)) * (1 + 0.004 * np.sin(2 * np.pi * 5 * t)),
                      "saw")
    aah = lambda f: (np.exp(-((f - 800) / 180) ** 2) + 0.7 * np.exp(-((f - 1150) / 220) ** 2)
                     + 0.25 * np.exp(-((f - 2900) / 400) ** 2))
    x = _spectral(x, aah)
    env = np.clip(t / 0.35, 0, 1) * np.clip((1.9 - t) / 0.5, 0, 1)
    return _norm(_room(x * env, 1.4, 0.5))


def air_horn() -> np.ndarray:
    """Three air-horn blasts (short, short, long): detuned saws, a pitch scoop and hard saturation."""
    blasts = []
    for at, dur in ((0.0, 0.22), (0.27, 0.16), (0.48, 0.62)):
        t = _t(dur)
        f = 466 * (1 - 0.06 * np.exp(-t / 0.03))
        x = sum(_osc(f * (1 + d), "saw") for d in (-0.012, 0, 0.009)) + 0.6 * _osc(f * 1.5, "saw")
        env = np.clip(t / 0.01, 0, 1) * np.clip((dur - t) / 0.03, 0, 1)
        blasts.append((np.tanh(2.5 * _lowpass(x, 4200)) * env, at, 1.0))
    return _norm(_mixdown(blasts))


def gavel(knocks: int = 2) -> np.ndarray:
    """A real wooden knock (twice, the second harder) over a low bench thud."""
    wood = recorded("kenney_impactWood_heavy_000.wav")
    t = _t(0.4)
    thud = _osc(_glide(140, 70, 0.4, 1.3)) * _env(len(t), 0.002, 0.09)
    clack = _spectral(np.random.default_rng(9).standard_normal(int(0.06 * SR)),
                      lambda f: np.exp(-((f - 2200) / 1200) ** 2)) * _env(int(0.06 * SR), 0.0005, 0.008)
    clack = clack / (np.max(np.abs(clack)) + 1e-9)          # the bright edge of the knock, for phone speakers
    layers = [(wood, 0.0, 0.7), (clack, 0.0, 0.45)] if knocks == 2 else []
    at = 0.17 if knocks == 2 else 0.0
    layers += [(wood, at, 1.0), (clack, at, 0.6), (np.tanh(2 * thud), at, 0.7)]
    return _norm(_room(_mixdown(layers), 0.5, 0.25))


def evidence_stamp() -> np.ndarray:
    return _norm(_mixdown([(recorded("kenney_impactPlate_heavy_001.wav"), 0, 0.9),
                           (recorded("kenney_impactPunch_heavy_004.wav"), 0, 0.7)]))


def crowd(name: str, seconds: float) -> np.ndarray:
    x = recorded(name)[: int(seconds * SR)].copy()
    fade = int(0.7 * SR)
    x[-fade:] *= np.linspace(1, 0, fade)
    return _norm(x, 0.8)


# purpose = the beats it serves; tone = flame / praise / any; class = how the mix treats it (ducking, masking)
PALETTE = {
    "boom": {"make": boom, "purpose": ("stat_reveal", "roast_impact", "punchline", "hype_peak"), "tone": "any",
             "class": "impact", "attack": 0.0, "gain": -3, "variants": (0, -1, 1)},
    "pipe_clang": {"make": pipe_clang, "purpose": ("roast_impact", "final_payoff", "punchline"), "tone": "flame",
                   "class": "impact", "attack": 0.0, "gain": -5, "variants": (0, -1, 1)},
    "bonk": {"make": bonk, "purpose": ("punchline", "roast_impact"), "tone": "any", "class": "cartoon",
             "attack": 0.0, "gain": -4, "variants": (0, 2, -2)},
    "boing": {"make": boing, "purpose": ("punchline", "joke_setup"), "tone": "any", "class": "cartoon",
              "attack": 0.0, "gain": -6, "variants": (0, 2, -2)},
    "slide_down": {"make": lambda: slide_whistle("down"), "purpose": ("stat_reveal", "punchline"), "tone": "flame",
                   "class": "cartoon", "attack": 0.7, "gain": -9, "variants": (0, -1, 1)},
    "slide_up": {"make": lambda: slide_whistle("up"), "purpose": ("stat_reveal", "hype_peak"), "tone": "praise",
                 "class": "cartoon", "attack": 0.45, "gain": -9, "variants": (0, 1)},
    "sad_trombone": {"make": sad_trombone, "purpose": ("final_payoff", "dramatic_reversal"), "tone": "flame",
                     "class": "sting", "attack": 0.0, "gain": -8, "variants": (0, -1)},
    "record_scratch": {"make": record_scratch, "purpose": ("dramatic_reversal", "punchline", "awkward_pause"),
                       "tone": "any", "class": "sting", "attack": 0.0, "gain": -6, "variants": (0, 1)},
    "error_ding": {"make": lambda: _norm(recorded("kenney_error_005.wav")), "purpose": ("ui", "punchline"),
                   "tone": "flame", "class": "sting", "attack": 0.0, "gain": -6, "variants": (0, -2)},
    "toggle_click": {"make": lambda: _norm(recorded("kenney_toggle_001.wav")), "purpose": ("ui",), "tone": "any",
                     "class": "ui", "attack": 0.0, "gain": -8, "variants": (0,)},
    "gavel": {"make": gavel, "purpose": ("verdict", "joke_setup"), "tone": "flame", "class": "impact",
              "attack": 0.17, "gain": -2, "variants": (0, -1)},
    "gavel_single": {"make": lambda: gavel(1), "purpose": ("joke_setup", "verdict"), "tone": "flame",  # kenney_impactWood_heavy_000
                     "class": "impact", "attack": 0.0, "gain": -4, "variants": (0, 1)},
    "evidence_stamp": {"make": evidence_stamp, "purpose": ("exhibit", "stat_reveal"), "tone": "any",
                       "class": "impact", "attack": 0.0, "gain": -6, "variants": (0, -1, 1, 2)},
    "cinematic_hit": {"make": cinematic_hit, "purpose": ("hype_peak", "stat_reveal"), "tone": "praise",
                      "class": "impact", "attack": 0.0, "gain": -3, "variants": (0, -1)},
    "choir": {"make": choir, "purpose": ("hype_peak", "final_payoff"), "tone": "praise", "class": "crowd",
              "attack": 0.5, "gain": -10, "variants": (0, 2)},
    "crowd_cheer": {"make": lambda: crowd("bsb_0236_teens_cheer.wav", 3.0), "purpose": ("final_payoff", "hype_peak"),
                    "tone": "praise", "class": "crowd", "attack": 0.1, "gain": -9, "variants": (0,)},
    "applause": {"make": lambda: crowd("bsb_2479_applause.wav", 3.0), "purpose": ("final_payoff",), "tone": "praise",
                 "class": "crowd", "attack": 0.15, "gain": -10, "variants": (0,)},
    "air_horn": {"make": air_horn, "purpose": ("hype_peak", "final_payoff"), "tone": "praise", "class": "sting",
                 "attack": 0.0, "gain": -9, "variants": (0, 1)},
    # not a sound: the music cuts out at the cue (handled by the mix), for the deadpan beat
    "music_cut": {"make": None, "purpose": ("awkward_pause", "punchline"), "tone": "any", "class": "cut",
                  "attack": 0.0, "gain": 0, "variants": (0,)},
}
# Each format's sound personality, in order of preference. Courtroom never sounds like the deadpan monologue.
# ------------------------------------------------------------------------------------------ the meme registry
# Real soundboard clips (assets/sfx/registry.toml), the series' default: the reactions young NFL fans recognise.
# The audio lives in assets/sfx/meme/ (gitignored, `python -m studio.sfx_fetch`); a missing file falls back to the
# entry's procedural `fallback`, and the cue says so.
MEME_DIR = SFX_DIR / "meme"
REGISTRY_PATH = SFX_DIR / "registry.toml"


def _registry() -> dict:
    import tomllib
    return tomllib.loads(REGISTRY_PATH.read_text()) if REGISTRY_PATH.exists() else {}


REGISTRY = _registry()


def meme_file(sid: str) -> Path:
    return MEME_DIR / REGISTRY[sid]["source"].rsplit("/", 1)[-1]


def _decode(path: Path) -> np.ndarray:
    import subprocess
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).copy()


def active_rms(x: np.ndarray) -> float:
    """RMS over the sounding part only (10 ms blocks within 30 dB of the loudest), so silence doesn't dilute it."""
    x = x if x.ndim == 1 else x.mean(1)
    blk = x[: len(x) // 480 * 480].reshape(-1, 480)
    if not len(blk):
        return 1e-9
    r = np.sqrt((blk ** 2).mean(1))
    keep = r > r.max() * 10 ** (-30 / 20)
    return float(np.sqrt((r[keep] ** 2).mean())) if keep.any() else 1e-9


def _meme(sid: str) -> np.ndarray:
    """A registry clip, trimmed and faded, normalised to unit active RMS: the mix levels it against the narration."""
    e = REGISTRY[sid]
    a, b = e["trim"]
    x = _decode(meme_file(sid))[int(a * SR):int(b * SR)]
    n = min(len(x), int(0.04 * SR))
    x[-n:] *= np.linspace(1, 0, n)
    return (x / active_rms(x)).astype(np.float32)


for _sid, _e in REGISTRY.items():
    PALETTE[_sid] = {"make": (lambda s=_sid: _meme(s)), "purpose": tuple(_e["purpose"]), "tone": _e["tone"],
                     "class": _e["class"], "attack": _e["hit"], "gain": 0, "level_db": _e["level_db"],
                     "variants": (0,), "contexts": tuple(_e["contexts"]), "registry": True,
                     "fallback": _e.get("fallback")}
# Not sounds: `music_stop` silences the music from the cue to the end (the hard stop); `music_cut` for a beat.
PALETTE["music_stop"] = {"make": None, "purpose": ("hard_stop",), "tone": "any", "class": "stop", "attack": 0.0,
                         "gain": 0, "variants": (0,)}


def available(sid: str) -> bool:
    return not PALETTE[sid].get("registry") or meme_file(sid).exists()


# Each format's sound personality, in order of preference: real reactions first, a few diegetic foley sounds where
# the scene calls for them (a courtroom gavel, a phone toggle), and the hard stop.
FORMAT_SFX = {
    "roast_monologue": ("toggle_click", "crickets", "metal_pipe", "bruh", "fahhh", "music_stop"),
    "courtroom": ("gavel_single", "gavel", "fahhh", "emotional_damage", "get_out", "among_us", "music_stop"),
    "comparison": ("bruh", "fahhh", "get_out", "vine_boom", "mj_heehee", "anime_wow", "shocked", "yippee",
                   "music_stop"),
    "main_character": ("undertaker", "anime_wow", "heavenly", "shocked", "rizz", "crowd_cheer", "music_stop"),
    "fan_meltdown": ("anime_wow", "shocked", "mlg_airhorn", "yippee", "vine_boom", "crowd_cheer", "music_stop"),
}


@lru_cache(maxsize=None)
def render(sfx_id: str, variant: float = 0) -> np.ndarray:
    spec = PALETTE[sfx_id]
    if spec["make"] is None:
        return np.zeros(1, np.float32)
    x = spec["make"]()
    if spec.get("registry"):                   # unit-RMS: the mix sets the level against the narration
        return _pitch(x, variant).astype(np.float32)
    return _fade_end(_pitch(x, variant), 0.03).astype(np.float32)


def select(cues: list[dict], fmt: str, kind: str, recent: list[list[str]], seed: str) -> list[dict]:
    """Choose a sound for every cue. A cue asks for a moment (event) and, for meme sounds, a context (the joke it
    is: stat_ruins_it, throw_out, arrival...); the sound must fit the event, the format's personality and the tone.
    The context match matters most, then the builder's preference, then not repeating this video or the last two.
    A cue may also ask for nothing (`"none"`). A missing meme file falls back to its procedural stand-in.
    Deterministic, so the cue sheet is reproducible."""
    used_recently = {s for r in recent[-2:] for s in r}
    used: list[str] = []
    out = []
    for i, cue in enumerate(cues):
        if cue.get("context") == "none":
            out.append({**cue, "sfx": None, "why": "this beat is funnier with nothing"})
            continue
        allowed = FORMAT_SFX.get(fmt, tuple(PALETTE))
        want = cue.get("prefer") or ()
        cands = [s for s in allowed if cue["event"] in PALETTE[s]["purpose"]
                 and PALETTE[s]["tone"] in ("any", kind)]
        if not cands:
            out.append({**cue, "sfx": None, "why": "no sound fits this beat in this format"})
            continue

        def score(s):
            sc = -allowed.index(s) * 0.4
            sc += 5 if cue.get("context") and cue["context"] in PALETTE[s].get("contexts", ()) else 0
            sc += 3 if s in want else 0
            sc -= 4 if s in used and not cue.get("repeatable") else 0
            sc -= 1.5 if s in used_recently else 0
            return sc
        best = max(cands, key=lambda s: (score(s), -allowed.index(s)))
        note = ""
        if not available(best):
            fb = PALETTE[best].get("fallback")
            note = f"; {best} file missing (run python -m studio.sfx_fetch), using {fb}"
            best = fb if fb in PALETTE else None
            if best is None:
                out.append({**cue, "sfx": None, "why": "meme file missing and no fallback" + note})
                continue
        variants = PALETTE[best]["variants"]
        h = int(hashlib.sha256(f"{seed}:{i}:{best}".encode()).hexdigest()[:8], 16)
        used.append(best)
        out.append({**cue, "sfx": best, "variant": variants[h % len(variants)],
                    "gain": PALETTE[best]["gain"] + cue.get("gain_adjust", 0),
                    "fallback_used": bool(note),
                    "why": f"{cue['event']}" + (f" / {cue['context']}" if cue.get("context") else "") + f" in {fmt}; "
                    + ("recently used, but the best fit" if best in used_recently else "not used in the last two videos")
                    + note})
    return out


def manifest() -> dict:
    """assets/sfx/manifest.json: every recorded file and every synthesized effect, with its rights."""
    files = []
    for name, (source, url) in RECORDED.items():
        data = (SFX_DIR / name).read_bytes()
        files.append({"file": name, "sha256": hashlib.sha256(data).hexdigest(), "source": source, "url": url,
                      "license": "CC0-1.0", "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                      "commercial_use": True, "modification": True, "attribution_required": False,
                      "attribution": "Kenney (www.kenney.nl)" if name.startswith("kenney") else
                      "Joseph Sardin - BigSoundBank.com", "platforms": PLATFORMS,
                      "processing": "converted to 48 kHz mono 16-bit WAV; trimmed, layered and pitched in code"})
    synth = [{"id": k, "license": "original work generated by studio/sfx_palette.py (owned by the channel)",
              "commercial_use": True, "platforms": PLATFORMS,
              "uses_recorded": sorted({n for n in RECORDED if n.split(".")[0] in _source_of(k)})}
             for k, v in PALETTE.items() if v["make"] is not None]
    return {"note": "No clip copied from social media. Viral originals (Vine boom, metal pipe) are not used: no "
                    "verifiable license.", "recorded": files, "effects": synth}


def _source_of(sfx_id: str) -> str:
    import inspect
    fn = PALETTE[sfx_id]["make"]
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):
        return ""


def write_manifest() -> Path:
    path = SFX_DIR / "manifest.json"
    path.write_text(json.dumps(manifest(), indent=2) + "\n")
    return path
