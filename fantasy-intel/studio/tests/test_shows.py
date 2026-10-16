"""Shows: which story fits which show, the batch editor, the scripts' factual gates, the scenes, and the
creative similarity checks."""

from __future__ import annotations

import pytest

from conftest import _rows, make_snapshot
from studio import creative, shows
from studio.graphics import vector, vqa
from studio.stages import qc, story
from studio.stages.facts import ALL, build_facts
from studio.stages.render_motion import Ctx

INJ = [{"gsis_id": "00-STAR", "team": "PHI", "week": w, "report_status": "Out", "report_primary_injury": "Hamstring",
        "practice_status": "Did Not Participate In Practice"} for w in (4, 5)]


def _facts(snap, gsis):
    f = build_facts(snap, ALL, set(), only=gsis)
    return f, f["players"][0], {c["id"]: c for c in f["claims"]}


def _red_flag_snap():
    """A tight end whose target share fell in both recent games while his snaps held, in a light last week."""
    snap = make_snapshot()
    snap["tables"]["player_week_usage"] = [r for r in snap["tables"]["player_week_usage"] if r["gsis_id"] != "00-FALLER"]
    snap["tables"]["player_week_usage"] += _rows("00-FALLER", "DAL", "TE", [0.70, 0.66, 0.67, 0.65, 0.66],
                                                 [0.21, 0.29, 0.12, 0.07, 0.11])
    return snap


def _myth_snap():
    """Snaps up, the team throwing more in each recent game, yet his share down."""
    snap = make_snapshot()
    rows = [r for r in snap["tables"]["player_week_usage"] if r["gsis_id"] != "00-FALLER"]
    mine = _rows("00-FALLER", "DAL", "TE", [0.80, 0.82, 0.82, 0.90, 0.92], [0.20, 0.22, 0.21, 0.12, 0.11])
    for r in mine:                                   # a busier passing game in Weeks 4 and 5
        r["targets"] = round(r["target_share"] * (25 if r["week"] <= 3 else 40))
    other = _rows("00-OTHER", "DAL", "WR", [0.9] * 5, [0.3] * 5)
    for r in other:
        r["targets"] = 20 if r["week"] <= 3 else 35
    snap["tables"]["player_week_usage"] = rows + mine + other
    snap["tables"]["nfl_players"].append({"gsis_id": "00-OTHER", "display_name": "Other Receiver", "position": "WR"})
    snap["tables"]["nfl_team_week"] = [{"team": "DAL", "week": w, "team_targets": 25 + w if w <= 3 else 40 + w}
                                       for w in range(1, 6)]
    return snap


def _heist_snap(ir: bool):
    snap = make_snapshot()
    snaps = [None if w in (4, 5) else 0.95 for w in range(1, 6)]
    snap["tables"]["player_week_usage"] += _rows("00-STAR", "PHI", "WR", snaps, [None if w in (4, 5) else 0.30
                                                                                  for w in range(1, 6)])
    snap["tables"]["nfl_players"].append({"gsis_id": "00-STAR", "display_name": "Star Receiver", "position": "WR"})
    snap["tables"]["nfl_injuries"] += INJ
    if ir:
        snap["tables"]["nfl_rosters_weekly"] += [{"gsis_id": "00-STAR", "team": "PHI", "week": 6, "status": "RES"}]
    else:
        snap["tables"]["nfl_injuries"].append({"gsis_id": "00-STAR", "team": "PHI", "week": 6, "report_status": None,
                                               "report_primary_injury": "Hamstring",
                                               "practice_status": "Did Not Participate In Practice"})
    return snap


def _script(f, claims, show, used=None):
    beats, titles, st = story.compose(f, claims, "seed", used or {}, None, show)
    for b in beats:
        b["text_template"] = b["text_template"].replace("‖", "")
    segs = [{**b, "text_display": story.fill(b["text_template"], claims, "display"),
             "text_spoken": story.fill(b["text_template"], claims, "spoken")} for b in beats]
    for s in segs:
        s["claims_on_screen"] = sorted(x for x in s["scene"].get("claims", []) if x)
    return {"claims": f["claims"], "title_templates": titles,
            "title_candidates": [story.fill(t, claims, "display") for t in titles], "narration_segments": segs,
            "shots": [{"id": f"shot_{s['id']}", **s["scene"]} for s in segs], "structure": st}


def test_each_show_fits_its_own_situation():
    _, p, cl = _facts(_red_flag_snap(), "00-FALLER")
    assert "red_flag" in shows.cases(p, cl) and "myth" not in shows.cases(p, cl)
    _, p, cl = _facts(_myth_snap(), "00-FALLER")
    cs = shows.cases(p, cl)
    assert cs["myth"]["fit"] == 5 and "red_flag" not in cs          # snaps rose: not a "held" red flag
    _, p, cl = _facts(_heist_snap(ir=True), "00-RISER")
    assert shows.cases(p, cl)["heist"]["fit"] == 5
    _, p, cl = _facts(_heist_snap(ir=False), "00-RISER")
    assert shows.cases(p, cl)["heist"]["fit"] == 2                     # hinges on this week's report


def test_batch_editor_never_repeats_a_show_and_defers_the_weaker_story():
    rows = [{"slug": "a", "player": "A", "total": 30, "cases": {"red_flag": {"fit": 5, "why": ["x"]}}},
            {"slug": "b", "player": "B", "total": 34, "cases": {"red_flag": {"fit": 4, "why": ["y"]}}},
            {"slug": "c", "player": "C", "total": 28, "cases": {"heist": {"fit": 2, "why": ["z"]}}},
            {"slug": "d", "player": "D", "total": 29, "cases": {"heist": {"fit": 5, "why": ["w"]}}},
            {"slug": "e", "player": "E", "total": 26, "cases": {}}]
    plan = {d["slug"]: d for d in shows.plan_batch(rows)}
    assert plan["a"]["show"] == "red_flag"                             # the stronger fit wins, not the higher total
    assert plan["b"]["verdict"] == "defer" and "already this week's red_flag" in " ".join(plan["b"]["reasons"])
    assert plan["d"]["show"] == "heist" and plan["c"]["verdict"] == "defer"
    assert plan["e"]["show"] is None and plan["e"]["verdict"] == "make"     # keeps its earlier format
    recent = [{"structure": {"family": "red_flag"}}, {"structure": {"family": "red_flag"}}]
    assert shows.plan_batch(rows[:1], recent)[0]["show"] is None       # two of the last three were red flags


@pytest.mark.parametrize("snap_fn,gsis,show", [(_red_flag_snap, "00-FALLER", "red_flag"),
                                               (_myth_snap, "00-FALLER", "myth"),
                                               (lambda: _heist_snap(True), "00-RISER", "heist"),
                                               (lambda: _heist_snap(False), "00-RISER", "heist")])
def test_show_scripts_pass_the_factual_gates_and_are_structurally_distinct(snap_fn, gsis, show):
    snap = snap_fn()
    f, p, cl = _facts(snap, gsis)
    script = _script(f, cl, show)
    st = script["structure"]
    assert st["family"] == show and st["ending_mechanism"] in ("warning", "verdict", "cliffhanger", "takeaway")
    assert qc.gate_narration(script).passed, qc.gate_narration(script).failures
    assert qc.gate_claims(snap, f, script).passed, qc.gate_claims(snap, f, script).failures
    assert len(st["metrics"]) <= creative.STATS_MAX
    words = sum(len(s["text_spoken"].split()) for s in script["narration_segments"])
    assert words <= 75
    # the old template for the same story, compared on the five creative dimensions
    old = story.compose(f, cl, "seed", {})[2]
    assert len(creative.shared(st, old)) <= 1


def test_heist_ending_follows_the_status():
    f, p, cl = _facts(_heist_snap(True), "00-RISER")
    assert _script(f, cl, "heist")["structure"]["ending_mechanism"] == "verdict"
    f, p, cl = _facts(_heist_snap(False), "00-RISER")
    assert _script(f, cl, "heist")["structure"]["ending_mechanism"] == "cliffhanger"


@pytest.mark.parametrize("style", ["premium", "broadcast"])
@pytest.mark.parametrize("snap_fn,gsis,show", [(_red_flag_snap, "00-FALLER", "red_flag"),
                                               (_myth_snap, "00-FALLER", "myth"),
                                               (lambda: _heist_snap(True), "00-RISER", "heist")])
def test_show_scenes_pass_visual_qa(style, snap_fn, gsis, show):
    f, p, cl = _facts(snap_fn(), gsis)
    c = Ctx(f, cl, (255, 92, 92), show)
    samples = []
    script = _script(f, cl, show)
    for i, (seg, shot) in enumerate(zip(script["narration_segments"], script["shots"])):
        words = seg["text_spoken"].split()
        c.tokens = [{"text": w, "start": 0.3 * j, "end": 0.3 * (j + 1), "claim": None, "emph": False}
                    for j, w in enumerate(words)]
        F = vector.draw(style, {**shot, "_start": 0.0}, 3.9, 4.0, c, measure=True)
        samples.append({"frame": f"s{i}_97.png", "log": F.log})
    res = {n: fl for n, fl, _ in vqa.gates({"style": style, "samples": samples, "numbers_visible": {},
                                            "caption_top": vector.CAPTION_ZONE}, cl, None)}
    for gate in ("vqa_safe_zone", "vqa_overlap", "vqa_numbers", "vqa_text_size", "vqa_caption_clear"):
        assert not res[gate], (gate, res[gate][:3])


def test_creative_compare_flags_a_repeat_and_passes_a_different_show():
    a = {"family": "template:vanishing_targets", "hook_mechanism": "question", "scenes": ["player_card", "split"],
         "ending_mechanism": "buttons", "beats": ["hook", "twist"]}
    same = dict(a)
    diff = {"family": "red_flag", "hook_mechanism": "warning", "scenes": ["flag_open", "flag_chart"],
            "ending_mechanism": "warning", "beats": ["warning", "signal"]}
    hist = [{"episode_id": "x", "structure": same}]
    assert creative.compare(a, hist)[0]["repeat"]
    assert not creative.compare(diff, hist)[0]["repeat"]
    script = {"structure": {**diff, "metrics": ["a", "b", "c", "d"]}, "narration_segments": [], "shots": []}
    names = {n: ok for n, ok, _ in creative.checks(script)}
    assert not names["one_concept"] and names["clear_takeaway"]


def test_original_renderer_draws_show_scenes():
    from studio.stages import render_motion as RM
    f, p, cl = _facts(_red_flag_snap(), "00-FALLER")
    c = Ctx(f, cl, (255, 92, 92), "red_flag")
    c.tokens = []
    img = RM.draw_scene({"type": "flag_chart", "stage": "signal", "metric": "target_share", "claims": []}, 2.0, 3.0, c)
    assert img.size == (1080, 1920)


def test_legacy_scripts_are_fingerprinted_from_their_shots():
    old = {"structure": {"hook": "mystery", "beats": ["hook", "question"], "angle": "vanishing_targets"},
           "shots": [{"type": "player_card"}, {"type": "decision"}]}
    fp = creative.from_script(old)
    assert fp["scenes"] == ["player_card", "decision"] and fp["ending_mechanism"] == "buttons"
    assert fp["family"] == "template:vanishing_targets"
    assert creative.shared(fp, creative.from_script(old)) == list(creative.DIMENSIONS)


def test_legacy_history_rows_compare_like_new_ones():
    """A history row from before the fingerprint fields still counts as a repeat of the same template."""
    old_row = {"episode_id": "old", "structure": {"angle": "vanishing_targets", "format": "template", "hook": "mystery",
                                                  "beats": ["hook", "twist"], "ending": "decision:",
                                                  "scenes": ["player_card", "decision"]}}
    new = {"family": "template:vanishing_targets", "hook_mechanism": "question", "scenes": ["player_card", "decision"],
           "ending_mechanism": "buttons", "beats": ["hook", "twist"]}
    assert creative.compare(new, [old_row])[0]["shared"] == list(creative.DIMENSIONS)


def test_a_planned_detective_or_contingency_is_made_as_planned():
    """compose honours the show the batch editor planned instead of re-choosing the format."""
    from studio import editorial as E
    f, p, cl = _facts(_red_flag_snap(), "00-FALLER")
    called = []
    orig = E.choose_format
    E.choose_format = lambda *a: called.append(1) or orig(*a)
    try:
        story.compose(f, cl, "seed", {}, None, None)
        assert called                                       # no plan: the format is chosen from the data
        called.clear()
        try:
            story.compose(f, cl, "seed", {}, None, "detective")
        except Exception:
            pass                                            # this story has no mystery case; what matters is...
        assert not called                                   # ...that a planned show is never re-chosen
    finally:
        E.choose_format = orig


def test_preview_writes_only_under_its_own_directory(tmp_path, monkeypatch):
    from studio import storyboard
    src = tmp_path / "real" / "2026-w06-short-someone"
    src.mkdir(parents=True)
    keep = tmp_path / "real" / "2026-w06-short-someone-show-red_flag"
    keep.mkdir()
    (keep / "final.mp4").write_text("a real episode")
    monkeypatch.setenv("STUDIO_EPISODES", str(tmp_path / "real"))
    with pytest.raises(FileNotFoundError):                  # no snapshot to copy: fails before touching anything
        storyboard.preview(src, "red_flag", tmp_path / "previews")
    assert (keep / "final.mp4").exists()
    import os
    assert os.environ["STUDIO_EPISODES"] == str(tmp_path / "real")   # restored
