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

from pathlib import Path

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


def write_nosfx(ep) -> Path:
    """final_nosfx.mp4: the same picture with every meme cue left out of the mix, for an A/B listen."""
    import json
    import subprocess

    import soundfile as sf
    script = json.loads(ep.path("script.json").read_text())
    tl = json.loads(ep.path("work/timeline.json").read_text())
    voice, sr = [], None
    for seg in script["narration_segments"]:
        data, sr = sf.read(ep.path(f"audio/{seg['id']}.wav"), dtype="float32")
        voice.append(data)
    mixed = mix(A.upsample(np.concatenate(voice), sr), [tuple(e) for e in tl["sound_events"]], script["structure"],
                tl["scenes"], ep.cfg["shorts"], with_meme=False)
    wav = ep.path("work/mix_nosfx.wav")
    sf.write(wav, mixed, SR, subtype="PCM_16")
    out = ep.path("final_nosfx.mp4")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(ep.path("final.mp4")), "-i", str(wav),
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac",
                    "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", str(out)], check=True)
    return out


def drop_windows(scenes: list[dict]) -> list[tuple[float, float]]:
    """Silence the bed from just before a `music_drop` scene until its first words have landed."""
    out = []
    for sc in scenes:
        if sc.get("music_drop"):
            first = next((t["end"] for t in sc.get("_tokens", []) if t.get("text")), 0.6)
            out.append((max(0.0, sc["_start"] - 0.35), sc["_start"] + min(first + 0.25, sc["_dur"])))
    return out


def _ramp_gate(n: int, windows: list[tuple[float, float]], floor: float, ramp_s: float = 0.04) -> np.ndarray:
    """1.0 everywhere except the windows, where it ramps down to `floor` (and back up after)."""
    gain = np.ones(n, np.float32)
    ramp = max(1, int(ramp_s * SR))
    for a, b in windows:
        i, j = max(0, int(a * SR)), min(n, int(b * SR))
        if j <= i:
            continue
        gain[i:j] = np.minimum(gain[i:j], floor)
        lo = max(0, i - ramp)
        gain[lo:i] = np.minimum(gain[lo:i], np.linspace(1, floor, i - lo))
        hi = min(n, j + ramp)
        gain[j:hi] = np.minimum(gain[j:hi], np.linspace(floor, 1, hi - j))
    return gain


def parse_meme(kind: str) -> tuple[str, float, str] | None:
    """'meme:boom:-1:stat_reveal' -> ('boom', -1.0, 'stat_reveal')."""
    if not kind.startswith("meme:"):
        return None
    _, sfx_id, variant, event = kind.split(":", 3)
    return sfx_id, float(variant), event


def mix(voice: np.ndarray, events: list[tuple[float, str, float]], structure: dict, scenes: list[dict],
        shorts_cfg: dict, with_meme: bool = True) -> np.ndarray:
    """Voice + the format's music (or none) + its sounds. Meme cues (sfx_palette) duck the music for the hit, a
    `music_cut` cue silences it for a beat, long crowd/choir effects duck under the voice so they never mask a word,
    and `with_meme=False` leaves every meme cue out (the comparison mix)."""
    from . import sfx_palette as P
    n = len(voice)
    v = voice / (np.max(np.abs(voice)) + 1e-9) * A.db(-3)
    speech = A.envelope(v)
    speech = np.pad(speech, (0, max(0, n - len(speech))), mode="edge")[:n]
    talking = np.clip(speech / (np.percentile(speech, 90) + 1e-9), 0, 1)
    memes = [(at, *parse_meme(kind), g) for at, kind, g in events if parse_meme(kind)]
    plain = [(at, kind, g) for at, kind, g in events if not parse_meme(kind)]
    # the comparison mix (with_meme=False) keeps every edit (music stops, dips for the hits) and loses only the
    # sounds, so the two differ by the effects alone
    sounding = memes if with_meme else []
    bed = np.zeros(n, np.float32)
    mood = structure.get("music_mood")
    if structure.get("music", True) and mood and mood != "none" and shorts_cfg.get("music", True):
        m = A.music(n / SR, mood=mood)
        bed = np.pad(m, (0, max(0, n - len(m))))[:n] * A.db(structure.get("music_db", shorts_cfg.get("music_db", -26)))
        bed = bed * (1 - 0.55 * talking)
        bed *= _ramp_gate(n, drop_windows(scenes), 0.0)
        hits = [(at - 0.08, at + min(0.6, len(P.render(sid, var)) / SR)) for at, sid, var, _, _ in memes
                if P.PALETTE[sid]["class"] in ("impact", "cartoon", "sting", "reaction")]
        bed *= _ramp_gate(n, hits, A.db(-10), 0.03)                  # make room for the hit
        cuts = [(at, at + 0.9) for at, sid, _, _, _ in memes if sid == "music_cut"]
        cuts += [(at, n / SR + 1) for at, sid, _, _, _ in memes if sid == "music_stop"]   # the hard stop: for good
        bed *= _ramp_gate(n, cuts, 0.0, 0.01)                         # the music just stops
    fx = np.zeros(n, np.float32)
    for at, kind, gain_db in plain:
        sig = sfx(kind) * A.db(shorts_cfg.get("sfx_db", -12) + gain_db)
        i = int(at * SR)
        if 0 <= i < n:
            j = min(n, i + len(sig))
            fx[i:j] += sig[: j - i]
    long_fx = np.zeros(n, np.float32)
    ref = P.active_rms(v)                                              # the narration's loudness, for meme levels
    for at, sid, var, _, gain_db in sounding:
        if sid in ("music_cut", "music_stop"):
            continue
        spec = P.PALETTE[sid]
        sig = P.render(sid, var) * A.db(gain_db)
        if spec.get("registry"):                # a reaction sits `level_db` above (or below) the voice
            sig = sig * ref * A.db(spec["level_db"])
        i = int(at * SR)
        if i < 0:                               # starts before the video does: drop its head, keep the hit in place
            sig, i = sig[-i:], 0
        if 0 <= i < n:
            j = min(n, i + len(sig))
            (long_fx if P.PALETTE[sid]["class"] in ("crowd", "bed") else fx)[i:j] += sig[: j - i]
    long_fx *= 1 - 0.7 * talking                                       # a crowd never talks over the narrator
    out = v + bed + fx + long_fx
    fade_in, fade_out = int(0.015 * SR), int(0.12 * SR)
    out[:fade_in] *= np.linspace(0, 1, fade_in)
    out[-fade_out:] *= np.linspace(1, 0, fade_out)
    peak = np.max(np.abs(out))
    if peak > A.db(-1):
        out = out / peak * A.db(-1)
    return np.stack([out, out], axis=1).astype(np.float32)
