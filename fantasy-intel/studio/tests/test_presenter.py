from __future__ import annotations

import numpy as np
import soundfile as sf

from studio import presenter as P
from studio.core import Config, Episode, write_json

LM = {"face_box": [262, 274, 611, 707],
      "mouth": {"upper": [439, 583], "lower": [439, 584], "left": [361, 576], "right": [515, 572]},
      "eyes": {}}


def _episode(tmp_path, monkeypatch, engine="/nonexistent/musetalk"):
    monkeypatch.setenv("STUDIO_EPISODES", str(tmp_path / "episodes"))
    cfg = Config.load()
    cfg.raw["presenter"] = {"mode": "off", "engine": engine, "cache": str(tmp_path / "cache")}
    ep = Episode(2026, 6, "short", cfg, log=lambda *_: None, slug="someone")
    ep.dir.mkdir(parents=True)
    (ep.dir / "work").mkdir()
    write_json(ep.path("work/timeline.json"), {"scenes": [
        {"segment": "s01", "_start": 0.0, "_dur": 2.0, "type": "giant_number"},
        {"segment": "s02", "_start": 2.0, "_dur": 3.0, "type": "carry_grid"},
        {"segment": "s03", "_start": 5.0, "_dur": 4.0, "type": "fork"}]})
    ep.path("final.mp4").write_bytes(b"original")
    return ep


def test_off_does_nothing(tmp_path, monkeypatch):
    ep = _episode(tmp_path, monkeypatch)
    assert P.apply(ep, "off") is None
    assert not list(ep.dir.glob("*presenter*"))


def test_failure_is_safe_and_recorded(tmp_path, monkeypatch):
    ep = _episode(tmp_path, monkeypatch)
    assert P.apply(ep, "hook") is None                      # MuseTalk isn't at /nonexistent: no variant
    assert ep.path("final.mp4").read_bytes() == b"original"
    assert not ep.path("final_presenter_hook.mp4").exists()
    rec = (ep.dir / "presenter_hook.json").read_text()
    assert '"passed": false' in rec and "error" in rec


def test_windows_follow_the_mode(tmp_path, monkeypatch):
    ep = _episode(tmp_path, monkeypatch)
    assert [w["segment"] for w in P.windows(ep, "hook")] == ["s01"]
    assert [w["segment"] for w in P.windows(ep, "hybrid")] == ["s01", "s03"]


def test_host_layout_clears_the_card_and_captions():
    lay = P.layout_qc(LM)
    assert lay["face_in_frame"]["pass"] and lay["no_caption_collision"]["pass"]
    tall = {**LM, "face_box": [262, 100, 611, 1200]}             # a face that would run into the captions
    assert not P.layout_qc(tall)["face_in_frame"]["pass"]


def _clip_and_audio(tmp_path, mouth_open):
    n = len(mouth_open)
    rng = np.random.default_rng(0)
    clip = (rng.random((n, 760, 700, 3)) * 60 + 90).astype(np.uint8)
    audio = np.concatenate([np.sin(np.arange(640) / 3) * (0.5 if o else 0.0) for o in mouth_open]).astype(np.float32)
    path = tmp_path / "a.wav"
    sf.write(path, audio, 16000)
    return clip, path


def test_lipsync_gate_passes_a_mouth_that_follows_speech_and_fails_a_frozen_one(tmp_path):
    speech = [0, 0, 0, 0] + [1, 1, 0, 1, 1, 1, 0, 1] * 4 + [0] * 8
    clip, audio = _clip_and_audio(tmp_path, speech)
    good = P.clip_qc(clip, audio, LM, len(speech) - 1, {"open": [0.04 * s for s in speech], "frames_without_face": 0})
    assert good["lipsync_tracks_audio"]["pass"]
    frozen = P.clip_qc(clip, audio, LM, len(speech) - 1, {"open": [0.0] * len(speech), "frames_without_face": 0})
    assert not frozen["lipsync_tracks_audio"]["pass"]
    lost = P.clip_qc(clip, audio, LM, len(speech) - 1, {"open": [None] * len(speech), "frames_without_face": 44})
    assert not lost["face_tracked"]["pass"]
