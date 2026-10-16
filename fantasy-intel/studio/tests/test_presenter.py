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


def test_same_voice_puts_the_host_straight_on_the_original(tmp_path, monkeypatch):
    ep = _episode(tmp_path, monkeypatch)
    ep.cfg.raw["presenter"]["voice"] = ep.cfg["shorts"]["voice"]
    calls = []
    monkeypatch.setattr(P, "apply", lambda e, m: calls.append((e.id, m)))
    P.present(ep, "hook")
    assert calls == [(ep.id, "hook")]                      # no re-voiced -host episode needed
    assert P.present(ep, "off") is None


def test_engine_settings_change_the_cache_key(tmp_path, monkeypatch):
    ep = _episode(tmp_path, monkeypatch)
    st = P.settings(ep.cfg)
    base = P.engine_params({**st, "engine_mode": "musetalk"})
    hq = P.engine_params({**st, "engine_mode": "hq"})
    hq_more = P.engine_params({**st, "engine_mode": "hq", "detail": 0.5})
    assert base["fps"] == 25 and hq["fps"] == st["fps"]
    assert len({str(base), str(hq), str(hq_more)}) == 3          # any change re-renders (keys differ)


def test_missing_motion_library_falls_back_to_procedural(tmp_path, monkeypatch):
    ep = _episode(tmp_path, monkeypatch)
    st = {**P.settings(ep.cfg), "motion": "library", "library": tmp_path / "no-library"}
    assert P.library_clips(st) == []
    called = {}
    monkeypatch.setattr(P, "landmarks", lambda s, image=None: called.setdefault("lm", LM))
    monkeypatch.setattr(P, "base_frames", lambda s, lm, fps=25: called.setdefault("procedural", tmp_path))
    frames, lm = P.base_for(st, {"segment": "s01"}, ep.id)
    assert "procedural" in called and lm == LM


def test_eye_audit_keeps_blinks_and_cuts_slow_closes():
    blink = [0.16] * 20 + [0.08, 0.03, 0.03, 0.09] + [0.16] * 20         # 4 frames at 30 fps: a blink
    assert P.eye_spans(blink, 30) == [(0, 44)]
    sleepy = [0.16] * 48 + [0.05] * 20 + [0.16] * 13                     # 0.8 s with the eyes shut
    assert P.eye_spans(sleepy, 24) == [(0, 47), (69, 81)]
    assert P.eye_spans([None, None], 24) == []
    drift = [0.50] * 10 + [0.47, 0.46, 0.44, 0.44] + [0.50] * 10          # looks off to the side, then back
    assert P.eye_spans([0.16] * 24, 24, drift) == [(0, 10), (14, 24)]
    shut = [0.16] * 10 + [0.03] * 4 + [0.16] * 10                        # gaze isn't judged with the lids down
    assert P.eye_spans(shut, 24, [0.50] * 10 + [0.40] * 4 + [0.50] * 10) == [(0, 24)]


def test_library_motion_uses_only_audited_stretches(tmp_path, monkeypatch):
    ep = _episode(tmp_path, monkeypatch)
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "idle_01.mp4").write_bytes(b"x")
    (lib / "idle_02.mp4").write_bytes(b"y")
    st = {**P.settings(ep.cfg), "motion": "library", "library": lib}
    audits = {"idle_01.mp4": [(0.0, 2.0), (3.1, 3.4)], "idle_02.mp4": [(0.0, 1.0)]}   # too short: skipped
    monkeypatch.setattr(P, "library_audit", lambda s, c: {"usable_s": audits[c.name]})
    picked = []
    monkeypatch.setattr(P, "motion_frames", lambda s, c, fps, start, seconds: picked.append(
        (c.name, start, seconds)) or tmp_path)
    monkeypatch.setattr(P, "landmarks", lambda s, image=None: LM)
    for seg in ("s01", "s02", "s03", "s04"):
        P.base_for(st, {"segment": seg}, ep.id)
    assert {p[0] for p in picked} == {"idle_01.mp4"}
    assert all(0.0 <= start and start + seconds <= 2.0 for _, start, seconds in picked)


def test_realism_metrics_see_shake_and_a_tooth_gap(tmp_path):
    from PIL import Image
    base = tmp_path / "base"
    base.mkdir()
    rng = np.random.default_rng(1)
    for i in range(3):
        Image.fromarray((rng.random((760, 700, 3)) * 40 + 100).astype(np.uint8)).save(base / f"{i:08d}.png")
    clip = (rng.random((30, 760, 700, 3)) * 40 + 100).astype(np.uint8)
    ids = [33, 133, 362, 263, 1, 152, 105, 334, 61, 291, 13, 14]
    steady = [[[300 + j, 400 + j] for j in range(12)] for _ in range(30)]
    shaky = [[[300 + j + (4 if f % 2 else 0), 400 + j] for j in range(12)] for f in range(30)]
    calm = P.realism_metrics(clip, {"points": steady, "point_ids": ids, "open": [0.0] * 30}, LM, base, 30)
    shake = P.realism_metrics(clip, {"points": shaky, "point_ids": ids, "open": [0.0] * 30}, LM, base, 30)
    assert shake["landmark_jitter"] > calm["landmark_jitter"] + 1
    # an open mouth with bright teeth and a dark slit in the middle scores higher than even teeth
    def mouth_clip(gap):
        c = np.full((10, 760, 700, 3), 120, np.uint8)
        c[:, 583:600, 400:480] = 235
        if gap:
            c[:, 583:600, 438:442] = 20
        return c
    pts = [[[361, 576], [400, 576], [470, 576], [515, 576], [439, 520], [439, 700], [400, 400], [470, 400],
            [361, 590], [515, 590], [439, 583], [439, 620]]] * 10
    m = {"points": pts, "point_ids": ids, "open": [0.06] * 10}
    assert P.realism_metrics(mouth_clip(True), m, LM, base, 30)["teeth_gap_index"] > \
        P.realism_metrics(mouth_clip(False), m, LM, base, 30)["teeth_gap_index"] + 0.3


def test_latentsync_patch_is_idempotent_and_drops_insightface(tmp_path):
    import pytest
    from studio import presenter_latentsync as LS
    repo = tmp_path / "LatentSync"
    for rel, old, _ in LS.PATCHES:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text((f.read_text() if f.exists() else "") + "\n" + old + "\n")
    (repo / "latentsync/utils/face_detector.py").write_text("from insightface.app import FaceAnalysis\n")
    first = LS.patch(repo)
    assert first and LS.patch(repo) == []                          # second run changes nothing
    assert "insightface" not in (repo / "latentsync/utils/face_detector.py").read_text().split('"""')[2]
    (repo / "latentsync/utils/util.py").write_text("something else entirely\n")
    with pytest.raises(SystemExit):                                # upstream changed: stop, don't guess
        LS.patch(repo)
