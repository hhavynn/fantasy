"""Original audio for Shorts: a procedural music bed, sound effects, and the final mix.

Everything here is synthesized from oscillators and noise by this code, so the channel owns it outright: no
samples, loops, AI music models or library tracks, and nothing to license for TikTok, Reels or Shorts.

  music   100 BPM, A minor (Am-F-C-G): soft kick, clap, hats, sine bass, a quiet pad. Kept low and ducked
          under the voice so it adds energy without competing.
  sfx     whoosh (scene changes), impact (big reveals), tick (chart bars), rise (into the question).
  mix     voice + ducked music + sfx, short fades at both ends (no pops), peak-safe; loudness is normalised
          afterwards by FFmpeg loudnorm.
"""

from __future__ import annotations

import numpy as np

SR = 48000
BPM = 100
ROOTS = [57, 53, 48, 55]                 # A3, F3, C3, G3 (MIDI) - one chord per bar
CHORDS = [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]]


def midi_hz(n: float) -> float:
    return 440.0 * 2 ** ((n - 69) / 12)


def _env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    a = np.clip(t / max(attack, 1e-4), 0, 1)
    return a * np.exp(-t / max(decay, 1e-4))


def _noise(n: int, rng) -> np.ndarray:
    return rng.standard_normal(n).astype(np.float32)


def _highpass(x: np.ndarray, k: float = 0.97) -> np.ndarray:
    y = np.empty_like(x)
    prev_x = prev_y = 0.0
    for i, v in enumerate(x):              # short buffers only (hits), so a Python loop is fine
        prev_y = k * (prev_y + v - prev_x)
        prev_x = v
        y[i] = prev_y
    return y


def _lowpass_sweep(x: np.ndarray, f0: float, f1: float) -> np.ndarray:
    """One-pole low-pass whose cutoff glides from f0 to f1 (Hz) across the buffer."""
    cut = np.geomspace(f0, f1, len(x))
    a = 1 - np.exp(-2 * np.pi * cut / SR)
    y = np.empty_like(x)
    s = 0.0
    for i in range(len(x)):
        s += a[i] * (x[i] - s)
        y[i] = s
    return y


def kick(rng) -> np.ndarray:
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    freq = 45 + 75 * np.exp(-t / 0.04)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    return (np.sin(phase) * _env(n, 0.002, 0.16)).astype(np.float32)


def clap(rng) -> np.ndarray:
    n = int(0.25 * SR)
    body = _highpass(_noise(n, rng), 0.9) * _env(n, 0.001, 0.07)
    return (body * 0.6).astype(np.float32)


def hat(rng) -> np.ndarray:
    n = int(0.06 * SR)
    return (_highpass(_noise(n, rng), 0.6) * _env(n, 0.001, 0.015) * 0.35).astype(np.float32)


def bass_note(hz: float, dur: float) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    tone = np.sin(2 * np.pi * hz * t) + 0.25 * np.sin(4 * np.pi * hz * t)
    return (np.tanh(1.4 * tone) * _env(n, 0.005, dur * 0.7)).astype(np.float32)


def pad_chord(notes: list[int], dur: float) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros(n, np.float32)
    for m in notes:
        for detune in (-0.08, 0.08):
            hz = midi_hz(m + 12 + detune)
            for h in range(1, 5):                     # a soft, band-limited saw
                out += (np.sin(2 * np.pi * hz * h * t) / h).astype(np.float32)
    swell = np.clip(t / 0.6, 0, 1) * np.clip((dur - t) / 0.5, 0, 1)
    return out * swell / (len(notes) * 2 * 2.1)


def music(duration: float, seed: int = 7) -> np.ndarray:
    """A loopable minimal beat, as long as `duration`."""
    rng = np.random.default_rng(seed)
    n = int((duration + 1.0) * SR)
    out = np.zeros(n, np.float32)
    beat = 60 / BPM
    k, c, h = kick(rng), clap(rng), hat(rng)

    def add(sig, at, gain):
        i = int(at * SR)
        if i >= n:
            return
        j = min(n, i + len(sig))
        out[i:j] += sig[: j - i] * gain

    bar_len = 4 * beat
    bars = int(np.ceil(duration / bar_len)) + 1
    for b in range(bars):
        t0 = b * bar_len
        ch = b % 4
        add(pad_chord(CHORDS[ch], bar_len), t0, 0.35)
        for i in range(8):                            # eighth notes
            at = t0 + i * beat / 2
            add(h, at + (0.018 if i % 2 else 0), 0.5 if i % 2 else 0.8)
            add(bass_note(midi_hz(ROOTS[ch] - 12), beat / 2 * 0.9), at, 0.32 if i % 2 == 0 else 0.18)
        add(k, t0, 1.0)
        add(k, t0 + 2 * beat, 1.0)
        add(k, t0 + 3.5 * beat, 0.6)
        add(c, t0 + beat, 0.7)
        add(c, t0 + 3 * beat, 0.7)
    out = out[: int(duration * SR)]
    return out / (np.max(np.abs(out)) + 1e-9)


def sfx(kind: str, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed + hash(kind) % 1000)
    if kind == "whoosh":
        n = int(0.32 * SR)
        x = _lowpass_sweep(_noise(n, rng), 250, 7000)
        t = np.arange(n) / n
        return (x * np.sin(np.pi * t) ** 1.5 * 1.6).astype(np.float32)
    if kind == "impact":
        n = int(0.6 * SR)
        t = np.arange(n) / SR
        boom = np.sin(2 * np.pi * (50 + 40 * np.exp(-t / 0.05)) * t) * np.exp(-t / 0.22)
        click = _highpass(_noise(n, rng), 0.8) * np.exp(-t / 0.01) * 0.5
        return (np.tanh(1.6 * (boom + click))).astype(np.float32)
    if kind == "tick":
        n = int(0.06 * SR)
        t = np.arange(n) / SR
        return (np.sin(2 * np.pi * 1700 * t) * np.exp(-t / 0.012) * 0.7).astype(np.float32)
    if kind == "rise":
        n = int(0.5 * SR)
        x = _lowpass_sweep(_noise(n, rng), 400, 9000)
        return (x * np.linspace(0, 1, n) ** 2 * 1.2).astype(np.float32)
    raise ValueError(kind)


def upsample(x: np.ndarray, sr_in: int) -> np.ndarray:
    """Band-limited resample to 48 kHz via the FFT (exact for integer ratios like 24k -> 48k)."""
    if sr_in == SR:
        return x.astype(np.float32)
    n_out = int(round(len(x) * SR / sr_in))
    spec = np.fft.rfft(x)
    out = np.fft.irfft(spec, n_out) * (n_out / len(x))
    return out.astype(np.float32)


def envelope(x: np.ndarray, window: float = 0.12) -> np.ndarray:
    w = int(window * SR)
    kernel = np.ones(w, np.float32) / w
    return np.convolve(np.abs(x), kernel, mode="same")


def db(v: float) -> float:
    return 10 ** (v / 20)


def mix(voice: np.ndarray, events: list[tuple[float, str, float]], use_music: bool, music_db: float,
        sfx_db: float) -> np.ndarray:
    """Voice (48 kHz mono) + ducked music + sound effects -> stereo float32, peak-safe."""
    n = len(voice)
    v = voice / (np.max(np.abs(voice)) + 1e-9) * db(-3)
    bed = np.zeros(n, np.float32)
    if use_music:
        bed = music(n / SR)[:n] * db(music_db)
        speech = envelope(v)
        duck = 1 - 0.55 * np.clip(speech / (np.percentile(speech, 90) + 1e-9), 0, 1)
        bed = bed * duck
    fx = np.zeros(n, np.float32)
    for at, kind, gain_db in events:
        sig = sfx(kind) * db(sfx_db + gain_db)
        i = int(at * SR)
        if 0 <= i < n:
            j = min(n, i + len(sig))
            fx[i:j] += sig[: j - i]
    out = v + bed + fx
    fade_in, fade_out = int(0.015 * SR), int(0.25 * SR)
    out[:fade_in] *= np.linspace(0, 1, fade_in)
    out[-fade_out:] *= np.linspace(1, 0, fade_out)
    peak = np.max(np.abs(out))
    if peak > db(-1):
        out = out / peak * db(-1)
    return np.stack([out, out], axis=1).astype(np.float32)
