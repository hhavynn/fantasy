"""The soccer pack: script shape, content rules and a real render with synthetic audio."""

from __future__ import annotations

import json
import subprocess

import numpy as np
import pytest
import soundfile as sf

from studio.core import Config, Episode, read_json, write_json
from studio.soccer import qc, render, scenes as S, script as SP
from studio.soccer.cli import SoccerConfig, main, pick
from studio.soccer.explainers import EXPLAINERS, for_text
from studio.stages import qc as Q


@pytest.mark.parametrize("name", sorted(EXPLAINERS))
def test_every_explainer_passes_the_narration_and_source_gates(name):
    s = SP.build(name)
    assert Q.gate_narration(s).passed, Q.gate_narration(s).failures
    assert qc.gate_sources(s).passed and qc.gate_content(s).passed
    assert s["narration_segments"][-1]["section"] == "cta"


@pytest.mark.parametrize("name", sorted(EXPLAINERS))
def test_every_scene_draws(name):
    s = SP.build(name)
    for shot in s["shots"]:
        for t in (0.0, 0.5, 3.0):
            assert S.draw(shot, t, 3.0, S.Ctx()).size == (1080, 1920)


def test_a_trend_changes_the_hook_not_the_rule():
    plain, trend = SP.build("offside"), SP.build("offside", {"cluster": {"label": "Kylian Mbappé: refereeing"}})
    assert trend["narration_segments"][0]["text_display"].startswith("Everyone's arguing about Kylian Mbappé.")
    assert plain["narration_segments"][1:] == trend["narration_segments"][1:]


def test_a_changed_claim_fails_the_source_gate():
    s = SP.build("cards")
    s["claims"] = [{**s["claims"][0], "value": 3}]
    assert not qc.gate_sources(s).passed


@pytest.mark.parametrize("bad", [{"type": "photo"}, {"type": "stamp", "text": "x", "crest": "a.png"}])
def test_content_gate_blocks_anything_but_drawn_scenes(bad):
    s = SP.build("var")
    s["shots"][1] = bad
    assert not qc.gate_content(s).passed


def test_trend_text_picks_an_explainer():
    assert for_text("Another VAR row at Anfield") == "var" and for_text("Offside again!") == "offside"
    assert for_text("A quiet transfer window") is None
    assert for_text("Álvaro Morata and various other transfer news") is None
    assert pick({"idea": {"working_title": "Every part of the refereeing row explained"},
                 "cluster": {"topics": ["refereeing"], "top_posts": [{"text": "Red card for the keeper"}]}}) == "cards"


def test_soccer_episodes_stay_apart_from_nfl(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_EPISODES", str(tmp_path))
    cfg = Config.load()
    assert SoccerConfig(cfg.raw, cfg.path).episodes_dir == tmp_path / "soccer"
    assert cfg.episodes_dir == tmp_path


def test_list_and_unmatched_package(capsys, tmp_path):
    assert main(["--list"]) == 0 and "offside" in capsys.readouterr().out
    pkg = tmp_path / "p.json"
    pkg.write_text(json.dumps({"idea": {"working_title": "quiet"}, "cluster": {}}))
    assert main(["--package", str(pkg)]) == 2


def test_metadata_gate_still_requires_nfl_attribution_by_default(tmp_path):
    from PIL import Image
    thumb = tmp_path / "t.png"
    Image.new("RGB", (1280, 720)).save(thumb)
    meta = {"privacy_status": "private", "self_declared_made_for_kids": False, "contains_synthetic_media": True,
            "description": "Narrated with an AI voice. IFAB Laws of the Game #shorts", "title": "t", "tags": []}
    assert not Q.gate_metadata(meta, thumb, "short").passed
    assert Q.gate_metadata(meta, thumb, "short", qc.ATTRIBUTION).passed


def test_full_stage_chain_with_synthetic_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_EPISODES", str(tmp_path))
    base = Config.load()
    raw = {**base.raw, "shorts": {**base.raw["shorts"], "voice_clone": ""}, "video": {**base.raw["video"], "fps": 6}}
    cfg = SoccerConfig(raw, base.path)
    ep = Episode(2026, 44, "short", cfg, slug="cards", log=lambda m: None)
    ep.dir.mkdir(parents=True)
    SP.run(ep, "cards", None)
    s = read_json(ep.path("script.json"))
    ep.path("audio").mkdir()
    segments = []
    for seg in s["narration_segments"]:
        words = seg["text_spoken"].split()
        dur = 0.3 * len(words) + 0.2
        wav = np.sin(np.linspace(0, 900, int(24000 * dur))).astype(np.float32) * 0.3
        sf.write(ep.path(f"audio/{seg['id']}.wav"), wav, 24000, subtype="PCM_16")
        segments.append({"id": seg["id"], "duration": dur, "words": [
            {"text": w, "start": i * 0.3, "end": (i + 1) * 0.3, "space": True} for i, w in enumerate(words)]})
    write_json(ep.path("audio/timings.json"), {"sample_rate": 24000, "segments": segments, "fallback_words": []})
    render.run(ep)
    assert ep.path("final.mp4").exists() and ep.path("thumbnail.png").exists()
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(ep.path("final.mp4"))], capture_output=True, text=True).stdout
    assert out.strip() == "1080,1920"
    meta = read_json(ep.path("metadata.json"))
    assert meta["privacy_status"] == "private" and qc.ATTRIBUTION in meta["description"] and "nfl" not in json.dumps(meta).lower()


def test_studio_imports_leave_torch_able_to_load():
    import importlib.util

    import studio.stages.facts  # noqa: F401
    importlib.util.find_spec("pandas")  # raised before: torch makes this exact call while loading


def test_no_scene_goes_dark_before_its_words_arrive():
    for name in EXPLAINERS:
        for shot in SP.build(name)["shots"]:
            mean = np.asarray(S.draw(shot, 0.0, 3.0, S.Ctx()).convert("L")).mean()
            assert mean > 20, shot["type"]
