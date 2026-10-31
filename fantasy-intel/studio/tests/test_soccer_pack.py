"""The soccer pack: script shape, content rules and a real render with synthetic audio."""

from __future__ import annotations

import json
import subprocess

import numpy as np
import pytest
import soundfile as sf

from studio.core import Config, Episode, read_json, write_json
from studio.soccer import parody, qc, render, scenes as S, script as SP
from studio.soccer.cli import SoccerConfig, choose, main, pick, slug_for
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
            "description": "Narrated with an AI voice. Source: IFAB Laws of the Game #shorts", "title": "t", "tags": []}
    assert not Q.gate_metadata(meta, thumb, "short").passed
    assert Q.gate_metadata(meta, thumb, "short", qc.ATTRIBUTION).passed


def test_full_stage_chain_with_synthetic_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_EPISODES", str(tmp_path))
    base = Config.load()
    raw = {**base.raw, "shorts": {**base.raw["shorts"], "voice_clone": ""}, "video": {**base.raw["video"], "fps": 6}}
    cfg = SoccerConfig(raw, base.path)
    ep = Episode(2026, 44, "short", cfg, slug="cards", log=lambda m: None)
    ep.dir.mkdir(parents=True)
    SP.run(ep, {"explainer": "cards"}, None)
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


# ---------------------------------------------------------------- parody and what-if

def package(topics=("award",), fmt="parody", label="Harry Kane & Kylian Mbappé: award", stats=None):
    stats = [{"label": "Harry Kane goals this season", "value": 11, "source": "FPL bootstrap-static",
              "as_of": "2026-10-31"}] if stats is None else stats
    return {"idea": {"format": fmt, "working_title": "What really happened with Harry Kane (parody)",
                     "source_links": ["https://example.test/story"]},
            "cluster": {"label": label, "topics": list(topics), "enrichment": {"stats": stats}, "top_posts": []}}


def built(fmt="parody", **kw):
    p = package(fmt=fmt, **kw)
    return SP.build_parody(fmt, p), p


def meta_for(s):
    return {"description": render.FICTION_NOTICE, "altered_content": True, "contains_synthetic_media": True}


@pytest.mark.parametrize("topics,want", [(("award",), "heist"), (("transfer",), "nature"), (("manager",), "press"),
                                         (("form",), "press")])
def test_template_follows_the_story(topics, want):
    assert parody.template_for(package(topics)) == want


@pytest.mark.parametrize("fmt,topics", [("parody", ("award",)), ("parody", ("transfer",)), ("parody", ("manager",)),
                                        ("parallel_universe", ("transfer",))])
def test_stock_scripts_pass_every_rule_gate(fmt, topics):
    s, p = built(fmt, topics=topics)
    for gate in (Q.gate_narration(s), qc.gate_content(s), qc.gate_sources(s, p), qc.gate_parody(s, meta_for(s))):
        assert gate.passed, gate.failures
    for shot in s["shots"]:
        assert S.draw(shot, 1.0, 3.0, S.Ctx()).size == (1080, 1920)


def test_the_real_number_comes_from_the_package():
    s, p = built()
    assert s["claims"][0]["value"] == 11 and "{{c001}}" in s["narration_segments"][-2]["text_template"]
    p["cluster"]["enrichment"]["stats"][0]["value"] = 12
    assert not qc.gate_sources(s, p).passed


def test_no_stat_means_no_reality_number():
    s, p = built(stats=[])
    assert s["claims"] == [] and qc.gate_sources(s, p).passed and Q.gate_narration(s).passed


@pytest.mark.parametrize("break_it", [
    lambda s: s["narration_segments"][0].update(text_spoken="Hello."),
    lambda s: s["shots"][1].pop("badge"),
    lambda s: s["shots"][1]["cast"].append({"who": "kane", "x": 1}),
    lambda s: s["shots"][1].update(say={"who": "boss", "text": "I AM MBAPPE"}),
    lambda s: s["narration_segments"][1].update(text_display="Harry Kane told reporters it was fine."),
    lambda s: s["narration_segments"][1].update(text_display="It was fine, said Mbappé."),
    lambda s: s["narration_segments"][1].update(text_display="Mbappe said it was fine."),
])
def test_parody_gate_blocks_each_rule_break(break_it):
    s, _ = built()
    assert qc.gate_parody(s, meta_for(s)).passed     # the break below is what fails it, nothing left over
    break_it(s)
    assert not qc.gate_parody(s, meta_for(s)).passed


def test_a_script_never_changes_the_shared_templates():
    a, _ = built()
    a["shots"][1]["cast"].append({"who": "kane", "x": 1})
    b, _ = built()
    assert all(m["who"] != "kane" for m in b["shots"][1]["cast"])


def test_every_real_name_in_the_story_is_protected():
    p = package(label="Harry Kane & Kylian Mbappé: award")
    p["cluster"]["entity_names"] = {"x": "Lamine Yamal"}
    assert parody.real_names(p) == ["Harry Kane", "Kylian Mbappé", "Lamine Yamal"]


@pytest.mark.parametrize("topic", ["injury", "serious"])
def test_parody_gate_refuses_injury_and_serious_stories(topic):
    s, _ = built(topics=("transfer", topic))
    failures = qc.gate_parody(s, meta_for(s)).failures
    assert any("no fiction format" in f for f in failures)


def test_one_stray_mention_does_not_block_a_story():
    s, _ = built(topics=("award", "transfer", "serious"))
    assert qc.gate_parody(s, meta_for(s)).passed


def test_parody_gate_requires_the_disclosures():
    s, _ = built()
    assert not qc.gate_parody(s, {**meta_for(s), "description": "nothing"}).passed
    assert not qc.gate_parody(s, {**meta_for(s), "altered_content": False}).passed


def test_a_hand_written_script_still_passes_through_the_gates():
    p = package()
    good = [["The trophy files a complaint against gravity.", {"type": "toon", "set": "vault", "cast": [{"who": "trophy", "x": 540}]}]]
    s = SP.build_parody("parody", p, good)
    assert qc.gate_parody(s, meta_for(s)).passed and s["narration_segments"][1]["text_display"].startswith("The trophy")
    bad = [["Mbappé said the trophy was his.", {"type": "toon", "set": "vault", "cast": [{"who": "trophy", "x": 540}]}]]
    assert not qc.gate_parody(SP.build_parody("parody", p, bad), meta_for(s)).passed


def test_choose_follows_the_ideas_format():
    assert choose(None, package(fmt="parody"), None) == {"format": "parody", "custom": None}
    assert choose(None, package(fmt="parallel_universe"), None)["format"] == "parallel_universe"
    assert choose("var", None, None) == {"explainer": "var"}
    assert slug_for({"format": "parallel_universe"}, package()) == "parallel-universe-harry-kane"


def test_parody_metadata_declares_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_EPISODES", str(tmp_path))
    base = Config.load()
    ep = Episode(2026, 44, "short", SoccerConfig(base.raw, base.path), slug="p", log=lambda m: None)
    ep.dir.mkdir(parents=True)
    write_json(ep.path("script.json"), built()[0])
    render.metadata(ep)
    meta = read_json(ep.path("metadata.json"))
    assert render.FICTION_NOTICE in meta["description"] and meta["altered_content"] and meta["privacy_status"] == "private"
