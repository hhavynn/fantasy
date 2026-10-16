"""Audio for the flame / praise series: the shared music bed and sounds (studio/audio_design.py), plus a few
sounds and moods of its own, and the silences comedy needs.

Everything is synthesized here (oscillators and noise), like audio_design: nothing to license anywhere.

  sounds  gavel (two wooden knocks), scratch (a record scratch into a dead stop), boom (a sub drop for a reveal),
          type (one typewriter key), swell (a crowd-like roar of filtered noise)
  moods   court (slow, minor, a ticking clock), hype (a driving minor groove), frantic (fast and bright);
          a format may also have no music at all (the deadpan monologue lives on silence)
  drops   a scene marked `music_drop` cuts the music out just before it starts and brings it back after its
          first words land: the beat of silence before a punchline or a reveal
"""

from __future__ import annotations

import numpy as np

from . import audio_design as A

SR = A.SR

# Series-only moods, built on the bed's existing patterns (tense: clock and pulse; full: kick, clap, hats).
MOODS = {
    "court": {"bpm": 72, "roots": [45, 41, 43, 40], "chords": [[45, 48, 52], [41, 45, 48], [43, 47, 50], [40, 44, 47]],
              "pattern": "tense"},
    "hype": {"bpm": 124, "roots": [50, 46, 41, 48], "chords": [[50, 53, 57], [46, 50, 53], [41, 45, 48], [48, 52, 55]],
             "pattern": "full"},
    "frantic": {"bpm": 146, "roots": [55, 52, 48, 50], "chords": [[55, 59, 62], [52, 55, 59], [48, 52, 55], [50, 54, 57]],
                "pattern": "full"},
}
for _name, _mood in MOODS.items():          # the bed renders any mood it is given by name
    A.MOODS.setdefault(_name, _mood)


def sfx(kind: str, seed: int = 5) -> np.ndarray:
    rng = np.random.default_rng(seed + sum(map(ord, kind)))
    if kind == "gavel":                      # two wooden knocks, the second louder
        n = int(0.55 * SR)
        t = np.arange(n) / SR
        out = np.zeros(n, np.float32)
        for at, g in ((0.0, 0.6), (0.17, 1.0)):
            i = int(at * SR)
            tt = t[: n - i]
            knock = np.sin(2 * np.pi * (180 + 90 * np.exp(-tt / 0.01)) * tt) * np.exp(-tt / 0.045)
            click = A._highpass(rng.standard_normal(n - i).astype(np.float32), 0.6) * np.exp(-tt / 0.004) * 0.6
            out[i:] += g * (knock + click)
        return np.tanh(1.4 * out).astype(np.float32)
    if kind == "scratch":                    # a record scratch: a noisy pitch sweep down, then nothing
        n = int(0.38 * SR)
        t = np.arange(n) / SR
        f = 900 * np.exp(-t / 0.09) + 120
        tone = np.sin(2 * np.pi * np.cumsum(f) / SR)
        grit = A._lowpass_sweep(rng.standard_normal(n).astype(np.float32), 3000, 600)
        env = np.clip(t / 0.01, 0, 1) * np.exp(-t / 0.14)
        return (np.tanh(2.0 * (tone * 0.6 + grit * 0.8)) * env).astype(np.float32)
    if kind == "boom":                       # a sub drop under a reveal
        n = int(1.1 * SR)
        t = np.arange(n) / SR
        sub = np.sin(2 * np.pi * (38 + 70 * np.exp(-t / 0.08)) * t) * np.exp(-t / 0.45)
        return np.tanh(1.8 * sub).astype(np.float32)
    if kind == "type":                       # one typewriter key
        n = int(0.07 * SR)
        t = np.arange(n) / SR
        click = A._highpass(rng.standard_normal(n).astype(np.float32), 0.5) * np.exp(-t / 0.006)
        body = np.sin(2 * np.pi * 420 * t) * np.exp(-t / 0.015) * 0.4
        return (np.tanh(2 * (click + body)) * 0.8).astype(np.float32)
    if kind == "swell":                      # a crowd-like roar: band-limited noise swelling up and away
        n = int(1.6 * SR)
        x = A._lowpass_sweep(rng.standard_normal(n).astype(np.float32), 500, 2400)
        t = np.linspace(0, 1, n)
        return (x * np.sin(np.pi * t) ** 1.2 * 0.9).astype(np.float32)
    return A.sfx(kind)


def drop_windows(scenes: list[dict]) -> list[tuple[float, float]]:
    """Silence the bed from just before a `music_drop` scene until its first words have landed."""
    out = []
    for sc in scenes:
        if sc.get("music_drop"):
            first = next((t["end"] for t in sc.get("_tokens", []) if t.get("text")), 0.6)
            out.append((max(0.0, sc["_start"] - 0.35), sc["_start"] + min(first + 0.25, sc["_dur"])))
    return out


def mix(voice: np.ndarray, events: list[tuple[float, str, float]], structure: dict, scenes: list[dict],
        shorts_cfg: dict) -> np.ndarray:
    """Like audio_design.mix, with the format's mood (or no music), its music level, and its drops."""
    n = len(voice)
    v = voice / (np.max(np.abs(voice)) + 1e-9) * A.db(-3)
    bed = np.zeros(n, np.float32)
    mood = structure.get("music_mood")
    if structure.get("music", True) and mood and mood != "none" and shorts_cfg.get("music", True):
        m = A.music(n / SR, mood=mood)
        bed = np.pad(m, (0, max(0, n - len(m))))[:n] * A.db(structure.get("music_db", shorts_cfg.get("music_db", -26)))
        speech = A.envelope(v)
        duck = 1 - 0.55 * np.clip(speech / (np.percentile(speech, 90) + 1e-9), 0, 1)
        bed = bed * np.pad(duck, (0, max(0, n - len(duck))), mode="edge")[:n]
        gain = np.ones(n, np.float32)
        ramp = int(0.04 * SR)
        for a, b in drop_windows(scenes):
            i, j = int(a * SR), min(n, int(b * SR))
            if j <= i:
                continue
            gain[i:j] = 0.0
            gain[max(0, i - ramp):i] = np.minimum(gain[max(0, i - ramp):i], np.linspace(1, 0, i - max(0, i - ramp)))
            k = min(n, j + ramp)
            gain[j:k] = np.minimum(gain[j:k], np.linspace(0, 1, k - j))
        bed = bed * gain
    fx = np.zeros(n, np.float32)
    for at, kind, gain_db in events:
        sig = sfx(kind) * A.db(shorts_cfg.get("sfx_db", -12) + gain_db)
        i = int(at * SR)
        if 0 <= i < n:
            j = min(n, i + len(sig))
            fx[i:j] += sig[: j - i]
    out = v + bed + fx
    fade_in, fade_out = int(0.015 * SR), int(0.2 * SR)
    out[:fade_in] *= np.linspace(0, 1, fade_in)
    out[-fade_out:] *= np.linspace(1, 0, fade_out)
    peak = np.max(np.abs(out))
    if peak > A.db(-1):
        out = out / peak * A.db(-1)
    return np.stack([out, out], axis=1).astype(np.float32)
