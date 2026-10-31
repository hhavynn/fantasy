"""The AI parody show: who it picks, what its prompts may say, what it says aloud, and how its stills move."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from conftest import make_snapshot
from studio import parody_motion as PM
from studio import parody_qc as PQ
from studio import parody_wan as PW
from studio import vision as V
from studio.core import Config, Episode, read_json, sha256_file, write_json
from studio.stages import export, flame, frames, metadata, parody, qc
from studio.stages import showrunner as SR
from studio.stages.story import finish
from test_series import qb_snapshot


def facts_for(**kw):
    snap = qb_snapshot(**kw)
    c = flame.pick(snap, only="alpha-passer")[0]
    f = flame.build_facts(snap, {"gsis_id": c["gsis_id"], "name": c["name"], "role": c["kind"]})
    return snap, f, {cl["id"]: cl for cl in f["claims"]}


def composed(angle="short_throws", recent=(), **kw):
    snap, f, claims = facts_for(**kw)
    p = f["players"][0]
    p["angles"] = [a for a in p["angles"] if a["id"] == angle] + [a for a in p["angles"] if a["id"] == "stat_outlier"]
    brief = parody.brief(p, list(recent))
    return snap, f, claims, brief, *parody.compose(f, claims, brief)


def scripted(**kw) -> dict:
    snap, f, claims, brief, beats, titles = composed(**kw)
    return {"snap": snap, "facts": f, "claims": claims, "brief": brief, "beats": beats, "titles": titles}


# -------------------------------------------------------------------------------------------- selection

def cand(name, strength, team="PHI", gid=None):
    return {"kind": "flame", "name": name, "gsis_id": gid or name, "team": team, "strength": strength, "position": "QB",
            "slug": f"flame-{name.lower()}"}


def trend(players, total=80, topics=("bust",), label="", teams=()):
    return {"entities": {"players": list(players), "teams": list(teams)}, "topics": list(topics), "label": label,
            "score": {"total": total}}


def test_a_trending_player_outranks_a_slightly_stronger_game():
    out = parody.rank([cand("Quiet", 4.0), cand("Loud", 3.5, team="DAL")], [trend(["Loud"])])
    assert [c["name"] for c in out] == ["Loud", "Quiet"]


def test_without_trend_data_the_flame_order_stands():
    out = parody.rank([cand("Quiet", 4.0), cand("Loud", 3.5, team="DAL")], [])
    assert [c["name"] for c in out] == ["Quiet", "Loud"]


@pytest.mark.parametrize("t", [trend(["Hurt"], topics=("injury",)), trend(["Hurt"], label="Hurt arrested after game"),
                               trend(["Hurt"], label="family death statement")])
def test_a_person_in_real_trouble_is_never_parodied(t):
    assert parody.rank([cand("Hurt", 9.0)], [t]) == []


def test_a_team_in_real_trouble_is_never_parodied():
    assert parody.rank([cand("Hurt", 9.0, team="DAL")], [trend([], topics=("injury",), teams=["DAL"], label="plane crash")]) == []


def test_only_flames_are_candidates():
    snap = qb_snapshot()
    picks = parody.pick(snap, [], count=5, only="alpha-passer")
    assert picks and all(c["kind"] == "flame" for c in picks)


def test_one_short_per_team():
    out = parody.limit([cand("A", 5), cand("B", 4), cand("C", 3, team="DAL")], 3)
    assert [c["name"] for c in out] == ["A", "C"]


def test_post_by_is_forty_eight_hours_after_kickoff():
    snap = make_snapshot()
    for g in snap["tables"]["nfl_schedules"]:
        g["gametime"] = "13:00"
    assert parody.post_by(snap, "PHI", 5) == "2026-10-07T13:00:00-04:00"


def test_post_by_is_empty_on_an_unknown_game():
    assert parody.post_by(make_snapshot(), "PHI", 99) is None


# ------------------------------------------------------------------------------------------------ prompts

def test_every_prompt_in_the_bank_passes_the_likeness_rules():
    names = ["Alpha Passer"]
    assert parody.ART
    for key in parody.ART:
        assert parody.check_prompt(parody.prompt(key), names) == [], key


@pytest.mark.parametrize("bad", ["Alpha Passer on a field", "a passer named Passer", "a Cowboys helmet", "an NFL logo",
                                 "a quarterback throwing", "a man in a jersey", "a crowd of fans", "the number 12"])
def test_a_prompt_with_a_person_a_team_or_a_mark_is_refused(bad):
    assert parody.check_prompt(bad, ["Alpha Passer"])


def test_every_ball_in_a_prompt_is_an_american_football():
    for key in parody.ART:
        text = parody.prompt(key)
        assert "football" not in text.replace("American football", ""), key


def test_no_picture_asks_for_an_object_that_carries_numbers():
    for key, text in parody.ART.items():
        assert not any(w in text.lower() for w in ("ruler", "tape measure", "clock", "sign", "newspaper")), key


def test_every_prompt_asks_for_no_people_and_no_text():
    for key in parody.ART:
        p = parody.prompt(key).lower()
        assert "no people" in p and "no text" in p and "no numbers" in p and "no logos" in p


# ----------------------------------------------------------------------------------------------- script

@pytest.mark.parametrize("kw", [{"angle": "short_throws"}, {"angle": "rough_day", "sacks": 5, "ints": 3, "latest_yds": 90}])
def test_every_number_is_a_claim_and_the_script_passes_the_narration_gate(kw, episode):
    s = scripted(**kw)
    beats, titles, _ = finish(s["beats"], s["facts"]["players"][0], {"column": "x"}, "flame", "ai_parody", None,
                              {"tm": ""}, titles=s["titles"])
    flame._write_script(episode, s["facts"], s["claims"], beats, titles, {}, "test")
    gate = qc.gate_narration(read_json(episode.path("script.json")))
    assert gate.passed, gate


def test_every_shot_is_ai_art_with_a_hard_cut():
    s = scripted()
    for i, b in enumerate(s["beats"]):
        assert b["scene"]["type"] == "ai_still" and b["scene"]["art"] in parody.ART
        assert b["scene"]["transition"] == ("none" if i == 0 else "cut")


def test_the_narrator_never_puts_words_in_a_players_mouth():
    s = scripted()
    for b in s["beats"]:
        assert '"' not in b["text_template"] and "“" not in b["text_template"]
        assert not any(w in b["text_template"].split() for w in ("I", "I'm", "my", "said", "says"))


def test_the_title_says_it_is_an_ai_parody():
    assert all("AI Parody" in t for t in scripted()["titles"])


def test_it_never_ends_on_a_fantasy_question():
    for kw in ({"angle": "short_throws"}, {"angle": "rough_day", "sacks": 5, "ints": 3, "latest_yds": 90}):
        for joke in parody.JOKES[kw["angle"]]:
            assert not SR.fantasy_ending(joke["punch"]), joke["id"]


def test_the_script_reaches_the_formats_minimum_length():
    s = scripted()
    assert SR.estimate_seconds(s["beats"], s["claims"], "ai_parody") >= SR.min_seconds("ai_parody")


def test_a_joke_rests_until_the_others_have_run():
    first = scripted()["brief"]["joke"]
    second = scripted(recent=[{"joke": first}])["brief"]["joke"]
    assert first != second


def test_a_game_with_no_broad_angle_has_no_brief():
    snap, f, claims = facts_for()
    p = f["players"][0]
    p["angles"] = [a for a in p["angles"] if a["id"] == "stat_outlier"]
    assert parody.brief(p, [])["entertainment_test"]["pass"] is False


def test_the_show_is_never_chosen_by_the_flame_praise_series():
    assert SR.FORMATS["ai_parody"]["kinds"] == ()
    snap, f, claims = facts_for()
    brief = SR.choose(f["players"][0], [])
    assert brief["format"] != "ai_parody" and all(o["format"] != "ai_parody" for o in brief["options"])


# ------------------------------------------------------------------------------------------------ motion

@pytest.fixture
def still(tmp_path):
    arr = np.random.default_rng(1).integers(0, 255, (640, 360, 3), dtype=np.uint8)
    arr[:, :, 1] = np.linspace(0, 255, 640, dtype=np.uint8)[:, None]
    path = tmp_path / "chair.png"
    Image.fromarray(arr).resize((720, 1280)).save(path)
    mask = tmp_path / "chair.mask.png"
    m = np.zeros((1280, 720), np.uint8)
    m[700:1100, 200:520] = 255
    Image.fromarray(m).save(mask)
    return path, mask


def diff(a, b) -> float:
    return float(np.abs(np.asarray(a, np.float32) - np.asarray(b, np.float32)).mean())


def test_a_frame_fills_the_short(still):
    assert PM.frame(still[0], None, 0.5, 3.0, "push").size == (1080, 1920)


def test_a_still_never_sits_still(still):
    for move in PM.MOVES:
        assert diff(PM.frame(still[0], None, 0.0, 3.0, move), PM.frame(still[0], None, 3.0, 3.0, move)) > 1, move


def test_the_same_inputs_give_the_same_frame(still):
    assert diff(PM.frame(*still, 1.0, 3.0, "drift_left"), PM.frame(*still, 1.0, 3.0, "drift_left")) == 0


def test_the_subject_moves_against_its_background(still):
    plain = diff(PM.frame(still[0], None, 2.0, 3.0, "drift_left"), PM.frame(still[0], None, 0.0, 3.0, "drift_left"))
    layered = PM.frame(*still, 2.0, 3.0, "drift_left")
    assert plain > 0 and diff(layered, PM.frame(still[0], None, 2.0, 3.0, "drift_left")) > 0.5


def test_a_hit_bumps_the_subject_then_settles(still):
    before = PM.frame(*still, 0.9, 3.0, "push", hit=1.0)
    at = PM.frame(*still, 1.02, 3.0, "push", hit=1.0)
    later = PM.frame(*still, 2.5, 3.0, "push", hit=1.0)
    nohit = PM.frame(*still, 2.5, 3.0, "push")
    assert diff(before, at) > 1
    assert diff(later, nohit) < 0.5


class Ctx:
    def __init__(self, tmp, claims=None):
        self.episode_dir, self.claims, self.tokens = tmp, claims or {}, []
        self.clips = {}

    def d(self, cid):
        return self.claims[cid]["display"]

    def at(self, claim=None, word=None, default=0.0):
        return default


def badge_red(img) -> float:
    x0, y0, x1, y1 = PM.BADGE_BOX
    box = np.asarray(img.crop((x0, y0, x1, y1)), np.float32)
    return float(((box[..., 0] > 170) & (box[..., 1] < 80) & (box[..., 2] < 80)).mean())


def test_the_ai_parody_badge_is_on_every_frame_of_a_scene(still, tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    Image.open(still[0]).save(art / "chair.png")
    c = Ctx(tmp_path)
    for t in (0.0, 0.03, 1.0, 2.9):
        img = Image.new("RGB", (1080, 1920))
        PM.scene_ai(img, t, 3.0, {"type": "ai_still", "art": "chair", "move": "push", "stats": []}, c)
        assert badge_red(img) > 0.3, t


def test_a_stat_appears_when_it_is_said(still, tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    Image.open(still[0]).save(art / "chair.png")
    c = Ctx(tmp_path, {"c1": {"display": "4.3"}})
    c.at = lambda claim=None, word=None, default=0.0: 1.0
    spec = {"type": "ai_still", "art": "chair", "move": "push", "stats": [{"claim": "c1", "label": "YARDS"}]}
    early, late = Image.new("RGB", (1080, 1920)), Image.new("RGB", (1080, 1920))
    PM.scene_ai(early, 0.5, 3.0, spec, c)
    PM.scene_ai(late, 1.5, 3.0, spec, c)
    assert diff(early, late) > 3


def test_the_scene_is_registered_and_paints_its_own_frame():
    from studio import flame_scenes as FPS
    assert {"ai_still", "ai_hero"} <= set(FPS.SCENES)
    assert FPS.backdrop_for({"type": "ai_still", "style": "ai_parody", "fp": "flame"}) == "none"
    assert FPS.punches({"type": "ai_still"}, None, 3.0) == []


# ------------------------------------------------------------------------------------------- frames and QC

@pytest.fixture
def clean_vision(monkeypatch):
    monkeypatch.setattr(V, "read_text", lambda paths: {str(p): [] for p in paths})
    monkeypatch.setattr(V, "people", lambda path: {"faces": [], "humans": []})
    monkeypatch.setattr(V, "subject_mask", lambda path, out: False)


def fake_still(text, seed, out):
    Image.new("RGB", (832, 1472), (seed % 200 + 20, 90, 60)).save(out)


@pytest.fixture
def made(episode, clean_vision):
    """A parody Short up to its pictures: facts, brief, script and stills, no audio or render."""
    episode = Episode(2026, 6, "short", episode.cfg, log=lambda *_: None, slug="parody-alpha-passer")
    episode.dir.mkdir(parents=True)
    s = scripted()
    p = s["facts"]["players"][0]
    beats, titles, _ = finish(s["beats"], p, {"column": "x"}, "flame", "ai_parody", None, {"tm": ""}, titles=s["titles"])
    structure = {"series": "ai_parody", "format_family": "ai_parody", "story_angle": s["brief"]["angle"], "angle": "flame"}
    flame._write_script(episode, s["facts"], s["claims"], beats, titles, structure, "test")
    write_json(episode.path("facts.json"), s["facts"])
    write_json(episode.path("story.json"), {"order": 80})
    write_json(episode.path("brief.json"), s["brief"])
    frames.run(episode, make=fake_still)
    return episode


def approve(ep):
    PQ.approve(ep, by="tester")


def test_each_picture_is_made_once_with_its_prompt_and_seed_recorded(made):
    m = read_json(made.path("art/manifest.json"))
    keys = {s["art"] for s in read_json(made.path("script.json"))["shots"]}
    assert set(m) == keys
    for key, e in m.items():
        assert made.path(f"art/{key}.png").exists()
        assert e["prompt"] == parody.prompt(key) and e["model"] == "Tongyi-MAI/Z-Image-Turbo" and e["license"] == "Apache-2.0"
        assert isinstance(e["seed"], int) and len(e["sha256"]) == 64


def test_a_rerun_does_not_remake_a_picture(made):
    calls = []
    made.force = {"frames"}
    frames.run(made, make=lambda *a: calls.append(a))
    assert calls == []


def test_a_rejected_picture_comes_back_with_a_new_seed_and_the_others_stay(made):
    m = read_json(made.path("art/manifest.json"))
    key, other = sorted(m)[:2]
    PQ.reject(made, key)
    frames.run(made, make=fake_still)
    after = read_json(made.path("art/manifest.json"))
    assert after[key]["seed"] != m[key]["seed"] and after[other] == m[other]


def test_a_changed_prompt_remakes_the_picture(made, monkeypatch):
    key = read_json(made.path("script.json"))["shots"][0]["art"]
    calls = []
    monkeypatch.setitem(parody.ART, key, "A different empty stadium")
    frames.run(made, make=lambda t, s, o: (calls.append(o.stem), fake_still(t, s, o)))
    assert calls == [key]


def test_a_remade_picture_replaces_the_old_file_even_if_the_generator_refuses_to_overwrite(made):
    """mflux writes chair_1.png rather than replace chair.png."""
    def polite(text, seed, out):
        target = out if not out.exists() else out.with_stem(out.stem + "_1")
        Image.new("RGB", (832, 1472), (9, 200, 9)).save(target)

    key = sorted(read_json(made.path("art/manifest.json")))[0]
    old = made.path(f"art/{key}.png").read_bytes()
    PQ.reject(made, key)
    frames.run(made, make=polite)
    assert made.path(f"art/{key}.png").read_bytes() != old
    assert read_json(made.path("art/manifest.json"))[key]["sha256"] == sha256_file(made.path(f"art/{key}.png"))


def test_a_generator_that_writes_nothing_stops_the_stage(made):
    key = sorted(read_json(made.path("art/manifest.json")))[0]
    PQ.reject(made, key)
    with pytest.raises(RuntimeError, match="no picture"):
        frames.run(made, make=lambda t, s, o: None)


def test_a_prompt_that_names_someone_stops_the_stage(made, monkeypatch):
    monkeypatch.setitem(parody.ART, "chair", "Alpha Passer in a beach chair")
    monkeypatch.setitem(parody.ART, "ruler", "Alpha Passer in a beach chair")
    made.force = {"frames"}
    (made.path("art/manifest.json")).unlink()
    with pytest.raises(RuntimeError, match="prompt"):
        frames.run(made, make=fake_still)


def gate(made, fn):
    return fn(made, read_json(made.path("script.json")), read_json(made.path("facts.json")))


def test_likeness_gate_passes_clean_pictures(made):
    assert gate(made, PQ.gate_no_likeness).passed


def test_a_face_in_a_picture_fails_the_likeness_gate(made, monkeypatch):
    monkeypatch.setattr(V, "people", lambda path: {"faces": [[0, 0, 1, 1, 0.9]], "humans": []})
    g = gate(made, PQ.gate_no_likeness)
    assert not g.passed and "face" in " ".join(g.failures)


def test_a_human_in_a_picture_fails_the_likeness_gate(made, monkeypatch):
    monkeypatch.setattr(V, "people", lambda path: {"faces": [], "humans": [[0, 0, 1, 1, 0.9]]})
    assert not gate(made, PQ.gate_no_likeness).passed


def test_a_shot_that_is_not_ai_art_fails_the_likeness_gate(made):
    script = read_json(made.path("script.json"))
    script["shots"][0]["type"] = "fp_hook"
    assert not PQ.gate_no_likeness(made, script, read_json(made.path("facts.json"))).passed


def test_a_prompt_changed_on_disk_fails_the_likeness_gate(made):
    m = read_json(made.path("art/manifest.json"))
    key = sorted(m)[0]
    write_json(made.path("art/manifest.json"), {**m, key: {**m[key], "prompt": "Alpha Passer on a field"}})
    assert not gate(made, PQ.gate_no_likeness).passed


def test_a_likeness_gate_that_cannot_look_fails(made, monkeypatch):
    def boom(path):
        raise OSError("no swift")
    monkeypatch.setattr(V, "people", boom)
    assert not gate(made, PQ.gate_no_likeness).passed


def test_text_in_a_picture_fails_the_text_gate(made, monkeypatch):
    assert gate(made, PQ.gate_clip_text).passed
    monkeypatch.setattr(V, "read_text", lambda paths: {str(p): ["50"] for p in paths})
    g = gate(made, PQ.gate_clip_text)
    assert not g.passed and "50" in " ".join(g.failures)


@pytest.mark.parametrize("mark", ["|", "1111/", "ll|l", "- -", "HHH"])
def test_a_stray_mark_is_not_text(made, monkeypatch, mark):
    monkeypatch.setattr(V, "read_text", lambda paths: {str(p): [mark] for p in paths})
    assert gate(made, PQ.gate_clip_text).passed


@pytest.mark.parametrize("line", ["50", "BROWNS", "Exit 12", "7"])
def test_writing_is_text(line):
    assert PQ.is_text(line)


def test_nothing_ships_until_a_person_approves_every_picture(made):
    g = gate(made, PQ.gate_clip_review)
    assert not g.passed and "not approved" in " ".join(g.failures)
    approve(made)
    assert gate(made, PQ.gate_clip_review).passed


def test_approval_is_tied_to_the_file(made):
    approve(made)
    key = sorted(read_json(made.path("art/manifest.json")))[0]
    Image.new("RGB", (832, 1472), (1, 2, 3)).save(made.path(f"art/{key}.png"))
    g = gate(made, PQ.gate_clip_review)
    assert not g.passed and key in " ".join(g.failures)


def test_a_rejected_picture_loses_its_approval(made):
    approve(made)
    key = sorted(read_json(made.path("art/manifest.json")))[0]
    PQ.reject(made, key)
    assert not gate(made, PQ.gate_clip_review).passed


def test_provenance_gate_needs_model_prompt_seed_and_hash(made):
    assert gate(made, PQ.gate_provenance).passed
    m = read_json(made.path("art/manifest.json"))
    key = sorted(m)[0]
    write_json(made.path("art/manifest.json"), {**m, key: {**m[key], "model": "some/other-model"}})
    assert not gate(made, PQ.gate_provenance).passed
    del m[key]["seed"]
    write_json(made.path("art/manifest.json"), m)
    assert not gate(made, PQ.gate_provenance).passed


def label_video(ep, labels):
    import subprocess
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=108x192:r=30:d=3", "-pix_fmt", "yuv420p", str(ep.path("final.mp4"))], check=True)


def test_the_label_gate_reads_every_sampled_frame(made, monkeypatch):
    label_video(made, None)
    monkeypatch.setattr(V, "read_text", lambda paths: {str(p): ["AI PARODY", "18"] for p in paths})
    g = gate(made, PQ.gate_ai_label)
    assert g.passed and g.details["frames"] >= 3


def test_the_label_is_read_from_where_the_badge_belongs(made):
    frame = made.path("frame.png")
    img = Image.new("RGB", (1080, 1920), (0, 90, 0))
    img.save(frame)
    crop = PQ.badge_crop(frame)
    with Image.open(crop) as c:
        assert c.width >= PM.BADGE_BOX[2] - PM.BADGE_BOX[0] and c.height < 400


def test_the_label_gate_ignores_the_gap_between_the_words(made, monkeypatch):
    label_video(made, None)
    monkeypatch.setattr(V, "read_text", lambda paths: {str(p): ["AIPARODY"] for p in paths})
    assert gate(made, PQ.gate_ai_label).passed


def test_one_frame_without_the_label_fails_the_label_gate(made, monkeypatch):
    label_video(made, None)

    def missing(paths):
        out = {str(p): ["AI PARODY"] for p in paths}
        out[str(sorted(paths)[-1])] = ["18"]
        return out
    monkeypatch.setattr(V, "read_text", missing)
    assert not gate(made, PQ.gate_ai_label).passed


def test_the_metadata_gate_needs_the_flag_the_note_and_the_title(made):
    good = {"ai_parody": True, "title": "x (AI Parody)", "description": SR.FORMATS["ai_parody"]["note"]}
    assert PQ.gate_ai_metadata(good).passed
    assert not PQ.gate_ai_metadata({**good, "ai_parody": False}).passed
    assert not PQ.gate_ai_metadata({**good, "title": "x"}).passed
    assert not PQ.gate_ai_metadata({**good, "description": "x"}).passed


# ------------------------------------------------------------------------------- the shared gates

def hist(eid, players, series):
    return {"episode_id": eid, "week": 5, "players": players, "narration": ["a different line entirely here today"],
            "structure": {"series": series, "hook": "old"}}


def st(series="ai_parody"):
    return {"series": series, "hook": "h", "_week": 6, "_order": 1}


def test_a_flame_and_a_parody_of_one_player_can_share_a_week():
    assert qc.gate_originality("e", ["x words of narration here now"], ["QB00"], [hist("f", ["QB00"], "flame_praise")], st()).passed


def test_the_same_player_cannot_be_parodied_twice():
    assert not qc.gate_originality("e", ["x words of narration here now"], ["QB00"], [hist("f", ["QB00"], "ai_parody")], st()).passed


def test_the_flame_series_still_refuses_a_recycled_player():
    assert not qc.gate_originality("e", ["x words of narration here now"], ["QB00"], [hist("f", ["QB00"], "flame_praise")],
                                   st("flame_praise")).passed


def series_script(shots, last="Somebody open a window."):
    return {"episode_id": "e", "structure": {"series": "ai_parody", "format_family": "ai_parody"},
            "shots": shots, "narration_segments": [{"text_display": last}]}


def test_the_series_gate_treats_ai_shots_as_labelled(tmp_path):
    write_json(tmp_path / "b.json", {"format": "ai_parody", "entertainment_test": {"pass": True, "why": ""}})
    assert qc.gate_series(series_script([{"type": "ai_still"}]), tmp_path / "b.json", []).passed
    assert not qc.gate_series(series_script([{"type": "fp_hook"}]), tmp_path / "b.json", []).passed


def test_a_show_may_run_week_after_week(tmp_path):
    write_json(tmp_path / "b.json", {"format": "ai_parody", "entertainment_test": {"pass": True, "why": ""}})
    rows = [{"episode_id": f"p{i}", "structure": {"series": "ai_parody", "format_family": "ai_parody"}} for i in range(3)]
    assert qc.gate_series(series_script([{"type": "ai_still"}]), tmp_path / "b.json", rows).passed


def test_the_licenses_gate_knows_the_local_models(episode):
    assert qc.gate_licenses(episode.cfg, {"kokoro"}).passed
    episode.cfg.raw["parody"] = {**episode.cfg.raw["parody"], "model_license": "CC-BY-NC"}
    assert not qc.gate_licenses(episode.cfg, {"kokoro"}).passed


def test_the_integrity_gate_covers_the_pictures(made):
    for name in ("facts", "script", "tts", "render", "thumbnail", "metadata"):
        write_json(made.path(f".stages/{name}.json"), {"inputs": {}, "outputs": {}})
    assert qc.gate_integrity(made).passed
    key = sorted(read_json(made.path("art/manifest.json")))[0]
    Image.new("RGB", (8, 8)).save(made.path(f"art/{key}.png"))
    assert not qc.gate_integrity(made).passed


def test_no_headshot_is_fetched_for_this_show(monkeypatch):
    from studio import headshots
    cfg = Config.load()
    monkeypatch.setattr(headshots, "fetch", lambda _id: pytest.fail("fetched a headshot"))
    assert headshots.for_player(cfg, {"espn_id": 1}, {"series": "ai_parody"}) is None
    assert headshots.record(cfg, {"espn_id": 1, "name": "x"}, {"series": "ai_parody"}) is None


def test_the_metadata_stage_flags_the_show(made):
    s = read_json(made.path("script.json"))
    keys = [x["id"] for x in s["narration_segments"]]
    write_json(made.path("audio/timings.json"), {"segments": [{"id": k, "duration": 3.0} for k in keys]})
    s["structure"].update(story_angle=read_json(made.path("brief.json"))["angle"])
    write_json(made.path("script.json"), s)
    metadata.run(made)
    meta = read_json(made.path("metadata.json"))
    assert meta["ai_parody"] is True and SR.FORMATS["ai_parody"]["note"] in meta["description"]
    assert "#aiparody" in meta["description"] and meta["contains_synthetic_media"] is True


def test_the_ai_parody_line_survives_every_caption():
    desc = ("Alpha vs the Cowboys: the story.\nEvery stat is from nflverse game data; the jokes are jokes.\n"
            f"{SR.FORMATS['ai_parody']['note']}\n\nNarrated with an AI voice.\nData: nflverse (CC BY 4.0): x.\n\n#shorts #nfl")
    story, fixed = export.parts(desc)
    assert SR.FORMATS["ai_parody"]["note"] in fixed
    text = export.caption("t", story[:1], fixed, ["#nfl"], 2200)
    assert "AI parody" in text and "Narrated with an AI voice" in text


def test_the_review_sheet_shows_every_picture(made):
    sheet = PQ.sheet(made)
    assert sheet.exists() and Image.open(sheet).width > 400


# ------------------------------------------------------------------------------------------ overnight Wan

def test_hero_clips_are_off_by_default_even_when_a_clip_exists(made):
    fake_clip(made, sorted(read_json(made.path("art/manifest.json")))[0])
    made.force = {"script"}
    parody.script_run(made)
    assert {s["type"] for s in read_json(made.path("script.json"))["shots"]} == {"ai_still"}


def fake_clip(ep, key):
    ep.path("clips").mkdir(exist_ok=True)
    ep.path(f"clips/{key}.mp4").write_bytes(b"clip")
    write_json(ep.path(f"clips/{key}.json"), {"model": "Wan-AI/Wan2.2-TI2V-5B-Diffusers", "license": "Apache-2.0",
                                              "prompt": "p", "seed": 1})


def test_hero_swaps_only_the_beats_with_a_finished_clip(made):
    shots = read_json(made.path("script.json"))["shots"]
    key = shots[1]["art"]
    fake_clip(made, key)
    parody.script_run(made, hero=True)
    after = read_json(made.path("script.json"))["shots"]
    assert [s["type"] for s in after if s["art"] == key] == ["ai_hero"]
    assert next(s for s in after if s["art"] == key)["clip"] == f"clips/{key}.mp4"
    assert all(s["type"] == "ai_still" for s in after if s["art"] != key)


def test_a_clip_needs_its_own_approval_and_provenance(made):
    key = read_json(made.path("script.json"))["shots"][1]["art"]
    fake_clip(made, key)
    parody.script_run(made, hero=True)
    approve(made)
    made.path(f"clips/{key}.mp4").write_bytes(b"changed")
    assert not gate(made, PQ.gate_clip_review).passed
    write_json(made.path(f"clips/{key}.json"), {"prompt": "p"})
    assert not gate(made, PQ.gate_provenance).passed


def test_the_clip_frames_go_through_the_likeness_gate(made, monkeypatch):
    key = read_json(made.path("script.json"))["shots"][1]["art"]
    fake_clip(made, key)
    parody.script_run(made, hero=True)
    monkeypatch.setattr(PQ, "_samples", lambda clip, out: [made.path("art/manifest.json")])
    seen = []
    monkeypatch.setattr(V, "people", lambda path: seen.append(path.name) or {"faces": [], "humans": []})
    gate(made, PQ.gate_no_likeness)
    assert "manifest.json" in seen


def test_the_queue_takes_distinct_pictures_hook_first(made):
    script = read_json(made.path("script.json"))
    keys = PW.hero_keys(script, 2)
    assert len(set(keys)) == 2 and keys[0] == script["shots"][0]["art"]
    assert len(PW.hero_keys(script, 1)) == 1


def test_the_queue_can_take_one_short(made):
    other = made.dir.with_name("2026-w06-short-parody-other")
    other.mkdir()
    for rel in ("script.json", "art/manifest.json"):
        write_json(other / rel, read_json(made.path(rel)))
    for key in read_json(made.path("art/manifest.json")):
        Image.new("RGB", (8, 8)).save(other / "art" / f"{key}.png")
    both = {j["episode"] for j in PW.jobs(made.cfg, 2026, 6)}
    one = {j["episode"] for j in PW.jobs(made.cfg, 2026, 6, slug="parody-other")}
    assert len(both) == 2 and one == {other.name}


def test_the_queue_skips_a_finished_clip_and_asks_for_the_measured_settings(made):
    made.cfg.raw["parody"] = {**made.cfg.raw["parody"], "hero_clips": 2}
    todo = PW.jobs(made.cfg, 2026, 6)
    assert todo and (todo[0]["size"], todo[0]["frames"], todo[0]["steps"]) == ([480, 832], 73, 20)
    assert "no people" in todo[0]["prompt"]
    fake_clip(made, todo[0]["key"])
    assert len(PW.jobs(made.cfg, 2026, 6)) == len(todo) - 1


def test_a_failed_clip_leaves_the_short_alone(made, capsys):
    job = PW.jobs(made.cfg, 2026, 6)[0]

    class R:
        returncode = 1
    assert PW.run_job(made.cfg, job, run=lambda *a, **k: R()) is False


def test_an_interrupted_night_resumes_at_the_decode(made):
    job = PW.jobs(made.cfg, 2026, 6)[0]
    made.path("clips").mkdir(exist_ok=True)
    Path(job["latents"]).write_bytes(b"x")
    calls = []

    class R:
        returncode = 0
    PW.run_job(made.cfg, job, run=lambda cmd, **k: calls.append(cmd[3]) or R())
    assert calls == ["--decode"]


def test_the_wan_worker_runs_in_its_own_environment(made):
    job = PW.jobs(made.cfg, 2026, 6)[0]
    cmd = PW.worker(made.cfg, "denoise", job)
    assert cmd[0].endswith("wan/.venv/bin/python") and cmd[2].endswith("parody_wan.py") and cmd[3] == "--denoise"


def test_the_wan_file_really_starts_the_way_the_worker_starts_it():
    """studio/numbers.py would shadow the standard library if the script's folder went on the path."""
    import subprocess
    import sys
    cmd = [sys.executable, *PW.worker({"parody": {"wan_python": "x"}}, "denoise", {})[1:3], "--help"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-400:]


def test_the_wan_file_can_start_without_the_studio_installed():
    import ast
    tree = ast.parse(Path(PW.__file__).read_text())
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert all(getattr(n, "level", 0) == 0 for n in top)
    names = {a.name.split(".")[0] for n in top for a in n.names}
    assert not names & {"torch", "diffusers", "studio"}


def test_a_quarterback_under_the_season_attempts_floor_has_no_short_throws_angle():
    snap = qb_snapshot()
    rows = snap["tables"]["qb_week_passing"]
    snap["tables"]["qb_week_passing"] = [r for r in rows if not (r["gsis_id"] == "QB00" and r["week"] in (1, 2))]
    p = flame.build_facts(snap, {"gsis_id": "QB00", "name": "Alpha Passer", "role": "flame"})["players"][0]
    assert "short_throws" not in [a["id"] for a in p["angles"]]
