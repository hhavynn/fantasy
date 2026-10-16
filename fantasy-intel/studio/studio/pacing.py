"""Pacing and comedy-timing audit for a rendered Short: does it drag, and do the jokes land on time?

Everything here is measured from the episode's own files (script.json, audio/timings.json, work/timeline.json and
final.mp4 through FFmpeg), never estimated from intent:

  duration_s, words, wpm         the render's length and the narration's density
  first_word_s                   when the narrator starts talking
  first_hook_s                   when the first sentence is complete (the premise has been said)
  first_reveal_s                 when the first number is said
  first_payoff_s                 when the first payoff hits: the first meme cue on a punchline / reveal / impact /
                                 hype / verdict; for a Short without cues, its first number (basis says which)
  longest_voice_gap_s            the longest stretch between two spoken words (dead air, as the viewer hears it)
  longest_silence_s              the longest near-silent stretch in the final mix (-35 dB, music included)
  visual_changes                 FFmpeg scene-change detections (cuts, reveals, big motion), and their mean spacing
  static_pct                     share of the video with a frozen frame (FFmpeg freezedetect)
  sfx_events, sfx                the meme cues in the mix, and how far each hit is from its word (sync_error_s)
  sfx_over_speech                cues whose hit lands while the narrator is mid-word (can mask the line)

`audit()` turns the numbers into format-aware warnings and failures (the limits are each format's `pacing` in
stages/showrunner.FORMATS); those are heuristics for review, not predictions of retention.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

SILENCES = ("music_cut", "music_stop")        # cues that take sound away: placed and listed, never "heard"
PAYOFF_EVENTS = ("punchline", "stat_reveal", "roast_impact", "hype_peak", "verdict", "final_payoff", "exhibit")
SYNC_TOLERANCE_S = 0.06


def _ff(args: list[str]) -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args], capture_output=True, text=True).stderr


def _duration(video: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True).stdout
    return float(out.strip())


def _tokens(ep_dir: Path) -> list[dict]:
    """Every display token with absolute start/end and its claim, exactly as the renderer aligned them."""
    from .stages.render_motion import align
    script = json.loads((ep_dir / "script.json").read_text())
    timings = {s["id"]: s for s in json.loads((ep_dir / "audio/timings.json").read_text())["segments"]}
    claims = {c["id"]: c for c in script["claims"]}
    out, off = [], 0.0
    for seg in script["narration_segments"]:
        tm = timings[seg["id"]]
        for tok in align(seg, tm["words"], claims):
            out.append({**tok, "start": off + tok["start"], "end": off + tok["end"], "segment": seg["id"]})
        off += tm["duration"]
    return out


def measure(ep_dir: Path) -> dict:
    ep_dir = Path(ep_dir)
    video = ep_dir / "final.mp4"
    script = json.loads((ep_dir / "script.json").read_text())
    timeline = json.loads((ep_dir / "work/timeline.json").read_text())
    toks = _tokens(ep_dir)
    dur = _duration(video)
    words = sum(len(s["text_spoken"].split()) for s in script["narration_segments"])
    first_sentence = next((t["end"] for t in toks if re.search(r"[.?!]$", t["text"])), toks[-1]["end"])
    numbers = {c["id"] for c in script["claims"] if c["format"] in ("int", "dec1", "pct", "pp", "ordinal")}
    claims = [t for t in toks if t["claim"] in numbers]           # a number said, not a team name or a week
    gaps = [(b["start"] - a["end"], a["end"]) for a, b in zip(toks, toks[1:])]
    longest_gap = max(gaps, default=(0.0, 0.0))

    from .series_audio import parse_meme
    from . import sfx_palette as P
    cues = [(at, *parse_meme(kind), g) for at, kind, g in timeline["sound_events"] if parse_meme(kind)]
    hits = []
    for at, sid, var, event, gain in cues:
        hit = at + P.PALETTE[sid]["attack"] - P.SYNC_OFFSET_S
        hits.append({"time_s": round(hit, 3), "sfx": sid, "variant": var, "event": event, "gain_db": gain})
    # sync: each hit against its anchor, re-resolved from the timings (independently of the renderer's placement)
    from .series_scenes import anchor_time
    planned = [c for seg in script["narration_segments"] for c in seg["scene"].get("cues", []) if c.get("sfx")]
    offsets = {}
    off = 0.0
    timings = {s["id"]: s for s in json.loads((ep_dir / "audio/timings.json").read_text())["segments"]}
    for seg in script["narration_segments"]:
        seg_toks = [dict(t, start=t["start"] - off, end=t["end"] - off) for t in toks if t["segment"] == seg["id"]]
        for c in seg["scene"].get("cues", []):
            if c.get("sfx"):
                a = anchor_time(c, seg_toks)
                if a is not None:
                    offsets.setdefault((c["sfx"], c["event"]), []).append(off + a)
        off += timings[seg["id"]]["duration"]
    for h in hits:
        want = offsets.get((h["sfx"], h["event"]), [])
        if want:
            best = min(want, key=lambda w: abs(w - h["time_s"]))
            h["anchor_s"] = round(best, 3)
            h["sync_error_s"] = round(h["time_s"] - best, 3)
        h["over_speech"] = any(t["start"] + 0.02 < h["time_s"] < t["end"] - 0.02 for t in toks)

    heard = heard_onsets(ep_dir, hits)
    for h, onset in zip(hits, heard):
        if onset is not None:                 # where the effect is heard in the rendered audio
            # a registry clip's hit IS its loud onset (measured the same way); a procedural one's hit is `attack`
            # after its start
            spec = P.PALETTE[h["sfx"]]
            h["heard_s"] = round(onset + (0 if spec.get("registry") else spec["attack"]), 3)
            h["heard_error_s"] = round(h["heard_s"] - h.get("anchor_s", h["time_s"]), 3)
    payoff_hits = [h["time_s"] for h in hits if h["event"] in PAYOFF_EVENTS]
    # the first payoff: a sound on a payoff beat, or the first number said (a stat lands on its own), whichever is
    # earlier: fewer sounds must not read as a later joke
    first_number = claims[0]["start"] if claims else None
    options = [(t, "a meme sound on a payoff beat") for t in payoff_hits]
    if first_number is not None:
        options.append((first_number, "the first number said"))
    first_payoff, basis = min(options) if options else (first_sentence, "the first sentence")

    sil = _ff(["-i", str(video), "-vn", "-af", "silencedetect=n=-35dB:d=0.25", "-f", "null", "-"])
    silences = [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", sil)]
    scn = _ff(["-i", str(video), "-an", "-vf", "select='gt(scene,0.1)',showinfo", "-f", "null", "-"])
    changes = [float(x) for x in re.findall(r"pts_time:([\d.]+)", scn)]
    frz = _ff(["-i", str(video), "-an", "-vf", "freezedetect=n=0.002:d=0.5", "-f", "null", "-"])
    frozen = sum(float(x) for x in re.findall(r"freeze_duration: ([\d.]+)", frz))
    starts = re.findall(r"freeze_start: ([\d.]+)", frz)
    if len(starts) > len(re.findall(r"freeze_end", frz)):          # frozen through the end
        frozen += dur - float(starts[-1])
    events = sorted(set([round(x, 2) for x in changes] + [h["time_s"] for h in hits]))
    spacing = [b - a for a, b in zip(events, events[1:])]
    return {
        "episode": ep_dir.name, "duration_s": round(dur, 2), "words": words, "wpm": round(words / dur * 60),
        "first_word_s": round(toks[0]["start"], 2), "first_hook_s": round(first_sentence, 2),
        "first_reveal_s": round(claims[0]["start"], 2) if claims else None,
        "first_payoff_s": round(first_payoff, 2), "first_payoff_basis": basis,
        "longest_voice_gap_s": round(longest_gap[0], 2), "longest_voice_gap_at_s": round(longest_gap[1], 2),
        "longest_silence_s": round(max(silences, default=0.0), 2),
        "visual_changes": len(changes), "scenes": len(timeline["scenes"]),
        "mean_event_spacing_s": round(sum(spacing) / len(spacing), 2) if spacing else None,
        "static_pct": round(100 * frozen / dur, 1),
        "sfx_events": sum(1 for h in hits if h["sfx"] not in SILENCES), "sfx": hits,
        "max_sync_error_s": max((abs(h.get("sync_error_s", 0)) for h in hits), default=0.0),
        # swells (crowd, choir) fade in: they have no single hit to time, so they are listed but not scored here
        "max_heard_error_s": max((abs(h["heard_error_s"]) for h in hits if "heard_error_s" in h
                                  and P.PALETTE[h["sfx"]]["class"] not in ("crowd", "bed")), default=None),
        "mp4_audio_lag_s": mp4_lag(ep_dir),
        "sfx_over_speech": [h["sfx"] for h in hits if h["over_speech"] and h["sfx"] not in SILENCES],
        "silences": [h["time_s"] for h in hits if h["sfx"] in SILENCES],
    }


def _audio(video: Path) -> "np.ndarray":
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def _wav(path: Path) -> "np.ndarray":
    import soundfile as sf
    x, sr = sf.read(path, dtype="float32")
    assert sr == 48000
    return x.mean(1) if x.ndim > 1 else x


def mp4_lag(ep_dir: Path) -> float | None:
    """Seconds between the rendered MP4's audio and the mix it was encoded from (cross-correlated envelopes)."""
    import numpy as np
    if not (ep_dir / "work/mix.wav").exists():
        return None
    a, b = _audio(ep_dir / "final.mp4"), _wav(ep_dir / "work/mix.wav")
    hop = 240                                                       # 5 ms
    env = lambda x: np.abs(x[: len(x) // hop * hop]).reshape(-1, hop).mean(1)
    ea, eb = env(a), env(b)
    n = min(len(ea), len(eb))
    ea, eb = ea[:n] - ea[:n].mean(), eb[:n] - eb[:n].mean()
    lags = range(-40, 41)
    best = max(lags, key=lambda k: float(np.dot(ea[max(0, k):n + min(0, k)], eb[max(0, -k):n - max(0, k)])))
    return round(best * hop / 48000, 3)


def heard_onsets(ep_dir: Path, hits: list[dict]) -> list[float | None]:
    """Find each effect in the rendered audio: final.mp4 minus final_nosfx.mp4 (same voice and music, no meme
    effects) leaves the effects alone; the onset is the first point near the planned hit where that residue rises
    above a third of its local peak. None without the comparison mix."""
    import numpy as np
    nosfx = ep_dir / "work/mix_nosfx.wav"
    if not nosfx.exists() or not hits:
        return [None] * len(hits)
    # the two mixes before loudness normalisation: loudnorm's look-ahead moves the gain around a loud hit, which
    # would smear the residue; timing is untouched by gain (mp4_audio_lag_s checks the MP4 carries this mix)
    a, b = _wav(ep_dir / "work/mix.wav"), _wav(nosfx)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    g = float(np.dot(a, b) / (np.dot(b, b) + 1e-9))                 # the two were loudness-normalised separately
    res = np.abs(a - g * b)
    w = int(0.005 * 48000)
    env = np.convolve(res, np.ones(w) / w, mode="same")
    out = []
    from . import sfx_palette as P
    for h in hits:
        if h["sfx"] in SILENCES:
            out.append(None)
            continue
        spec = P.PALETTE[h["sfx"]]
        start = h["time_s"] - (0 if spec.get("registry") else spec["attack"])   # where the loud part should begin
        i0, i1 = max(0, int((start - 0.25) * 48000)), min(n, int((start + 0.35) * 48000))
        seg = env[i0:i1]
        if len(seg) == 0 or seg.max() <= 1e-4:
            out.append(None)
            continue
        idx = int(np.argmax(seg > seg.max() / 3))
        out.append((i0 + idx) / 48000)
    return out


DEFAULT_LIMITS = {"hook_s": 3.0, "payoff_s": 4.0, "gap_s": 0.7}


def audit(m: dict, structure: dict | None, prior_sfx: list[list[str]] | None = None) -> dict:
    """Format-aware findings. `fail` blocks (QC); `warn` asks for a look; both say what was measured."""
    st = structure or {}
    lim = {**DEFAULT_LIMITS, **(st.get("pacing") or {})}
    lo, hi = st.get("duration_range") or (15, 45)
    fail, warn = [], []
    if m["first_word_s"] > 0.5:
        fail.append(f"narration starts at {m['first_word_s']}s (dead air before the hook)")
    if m["first_hook_s"] > lim["hook_s"] + 1.5:
        fail.append(f"the first sentence ends at {m['first_hook_s']}s (format target {lim['hook_s']}s)")
    elif m["first_hook_s"] > lim["hook_s"]:
        warn.append(f"the first sentence ends at {m['first_hook_s']}s (target {lim['hook_s']}s)")
    if m["first_payoff_s"] > lim["payoff_s"] + 2.5:
        fail.append(f"first payoff at {m['first_payoff_s']}s (target {lim['payoff_s']}s): the joke arrives late")
    elif m["first_payoff_s"] > lim["payoff_s"]:
        warn.append(f"first payoff at {m['first_payoff_s']}s (target {lim['payoff_s']}s)")
    if m["longest_voice_gap_s"] > lim["gap_s"] + 0.5:
        fail.append(f"{m['longest_voice_gap_s']}s without a word at {m['longest_voice_gap_at_s']}s")
    elif m["longest_voice_gap_s"] > lim["gap_s"]:
        warn.append(f"{m['longest_voice_gap_s']}s without a word at {m['longest_voice_gap_at_s']}s")
    if m["duration_s"] > hi + 4:
        fail.append(f"{m['duration_s']}s against the format's {lo}-{hi}s")
    elif m["duration_s"] > hi:
        warn.append(f"{m['duration_s']}s, over the format's {hi}s")
    if m["max_sync_error_s"] > SYNC_TOLERANCE_S:
        fail.append(f"a sound effect is placed {m['max_sync_error_s']}s off its word")
    if m.get("mp4_audio_lag_s") not in (None, 0.0) and abs(m["mp4_audio_lag_s"]) > 0.02:
        fail.append(f"the MP4's audio is {m['mp4_audio_lag_s']}s off the mix it was made from")
    if m.get("max_heard_error_s") is not None and m["max_heard_error_s"] > 0.08:
        warn.append(f"in the rendered audio an effect is heard {m['max_heard_error_s']}s off its word")
    if m["static_pct"] > 35:
        warn.append(f"{m['static_pct']}% of the video is a frozen frame")
    if m["sfx_events"] > 6:
        warn.append(f"{m['sfx_events']} meme effects: soundboard territory")
    if m["sfx_over_speech"]:
        warn.append(f"effects landing mid-word (check the line is still clear): {m['sfx_over_speech']}")
    mine = sorted(h["sfx"] for h in m["sfx"])
    if prior_sfx and mine and any(sorted(p) == mine for p in prior_sfx[-2:]):
        fail.append(f"the same sound set as a recent series video: {mine}")
    return {"pass": not fail, "fail": fail, "warn": warn}


def markdown(m: dict, findings: dict) -> str:
    lines = [f"# Pacing: {m['episode']}", "",
             f"{m['duration_s']} s, {m['words']} words ({m['wpm']} wpm). First word {m['first_word_s']} s, first "
             f"sentence done {m['first_hook_s']} s, first number {m['first_reveal_s']} s, first payoff "
             f"{m['first_payoff_s']} s ({m['first_payoff_basis']}).",
             f"Longest gap between words {m['longest_voice_gap_s']} s (at {m['longest_voice_gap_at_s']} s); longest "
             f"near-silence in the mix {m['longest_silence_s']} s. {m['visual_changes']} visual changes across "
             f"{m['scenes']} scenes (mean spacing of changes and hits {m['mean_event_spacing_s']} s); frozen "
             f"{m['static_pct']}%.", "",
             f"**{'PASS' if findings['pass'] else 'FAIL'}**" + "".join(f"\n- fail: {x}" for x in findings["fail"])
             + "".join(f"\n- warn: {x}" for x in findings["warn"]), "",
             "## Sound-effect cue sheet", "", "| hit (s) | effect | variant | beat | anchor (s) | sync error (s) | gain dB |",
             "|---|---|---|---|---|---|---|"]
    lines[-2] = "| hit (s) | effect | variant | beat | anchor (s) | placed error (s) | heard in the mix (s) | gain dB |"
    lines[-1] = "|---|---|---|---|---|---|---|---|"
    lines += [f"| {h['time_s']} | {h['sfx']} | {h['variant']:+g} | {h['event']} | {h.get('anchor_s', '—')} | "
              f"{h.get('sync_error_s', '—')} | {h.get('heard_s', '—')} | {h['gain_db']} |" for h in m["sfx"]]
    return "\n".join(lines) + "\n"
