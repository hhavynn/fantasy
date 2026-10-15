from __future__ import annotations

import re

from studio.core import Episode, read_json, write_json
from studio.stages import facts, qc, short_script
from studio.stages.facts import pick_stories
from studio.stages.render_short import caption_chunks_short
from studio.stages.short_script import angle_for, choose

from conftest import make_snapshot


def _metric(column, dot):
    return {"column": column, "label": column, "delta_over_threshold": dot, "baseline": "c001", "recent": "c002"}


def test_angle_follows_the_data():
    assert angle_for({"role": "faller", "metrics": [_metric("target_share", -3)]}) == "contraction"
    assert angle_for({"role": "riser", "metrics": [_metric("backfield_rush_share", 5)]}) == "takeover"
    assert angle_for({"role": "riser", "metrics": [_metric("target_share", 4)]}) == "expansion"
    # Snaps up and targets followed -> expansion; snaps up, targets did not -> unresolved.
    assert angle_for({"role": "riser", "metrics": [_metric("offense_pct", 6), _metric("target_share", 2)]}) == "expansion"
    assert angle_for({"role": "riser", "metrics": [_metric("offense_pct", 6), _metric("target_share", 0.3)]}) == "unresolved"


def test_hook_rotation_is_least_recently_used():
    opts = ["a", "b", "c"]
    assert choose(opts, ["a", "b"], "seed") == "c"            # never used wins
    assert choose(opts, ["c", "a", "b"], "seed") == "c"       # oldest use wins
    assert choose(opts, [], "x") == choose(opts, [], "x")     # deterministic


def test_structure_gate_blocks_a_third_identical_opening():
    hist = [{"episode_id": f"2026-w06-short-p{i}", "format": "short", "week": 6, "order": i, "players": [f"p{i}"],
             "narration": [f"unique words number {i} here and there"], "structure": {"hook": "number_first"}}
            for i in range(2)]
    me = {"hook": "number_first", "_week": 6, "_order": 2}
    assert "in a row" in " ".join(qc.gate_originality("2026-w06-short-p2", ["fresh text about someone"], ["p2"],
                                                     hist, me).failures)
    assert qc.gate_originality("2026-w06-short-p2", ["fresh text about someone"], ["p2"], hist,
                               {**me, "hook": "contrarian"}).passed


def test_stories_are_one_per_team():
    snap = make_snapshot()
    snap["tables"]["player_week_usage"] += [
        {**r, "gsis_id": "00-RISER2"} for r in snap["tables"]["player_week_usage"] if r["gsis_id"] == "00-RISER"]
    snap["tables"]["nfl_players"].append({"gsis_id": "00-RISER2", "display_name": "Second Riser", "position": "WR"})
    stories = pick_stories(snap, 3, 1, set())
    teams = [s["team"] for s in stories]
    assert len(teams) == len(set(teams))
    assert [s["order"] for s in stories] == list(range(len(stories)))


def test_long_claims_split_across_caption_cards():
    claims = {"c001": {"display": "two middle-of-the-pack matchups and one tough one",
                       "spoken": "two middle-of-the-pack matchups and one tough one"},
              "c002": {"display": "88%", "spoken": "eighty-eight percent"}}
    template = "So far this season, that's {{c001}}. It wasn't one big week, either."
    chunks = caption_chunks_short(template, claims)
    assert all(len(c["display"].split()) <= 9 for c in chunks)
    assert not any(c["display"] in ("either.",) for c in chunks)                  # no orphans
    spoken = template.replace("{{c001}}", claims["c001"]["spoken"])
    assert sum(c["spoken_words"] for c in chunks) == len(spoken.split())          # timing weight preserved


def test_short_script_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_EPISODES", str(tmp_path / "episodes"))
    from studio.core import Config
    cfg = Config.load()
    ep = Episode(2026, 6, "short", cfg, log=lambda *_: None, slug="rising-receiver")
    ep.dir.mkdir(parents=True)
    snap = make_snapshot()
    write_json(ep.path("snapshot.json"), snap)
    story = next(s for s in pick_stories(snap, 2, 1, set()) if s["gsis_id"] == "00-RISER")
    write_json(ep.path("story.json"), story)
    facts.run(ep)
    short_script.run(ep)
    s = read_json(ep.path("script.json"))
    assert s["structure"]["angle"] in ("expansion", "unresolved")
    assert [seg["section"] for seg in s["narration_segments"]] == [
        "hook", "reveal", "evidence", "context", "volume", "verdict", "cta"]
    assert s["narration_segments"][0]["text_display"].count("Rising Receiver") >= 1
    for seg in s["narration_segments"]:
        assert not re.search(r"\d", seg["text_spoken"])
    assert qc.gate_narration(s).passed
    assert qc.gate_claims(read_json(ep.path("snapshot.json")), read_json(ep.path("facts.json")), s).passed


# ----------------------------------------------------------------------------------- injury context

from studio.numbers import fmt_status
from studio.stages.facts import ALL, build_facts, evaluate_claim
from conftest import _rows


def _with_teammate(missed_weeks, injuries=(), rosters=()):
    """Rising Receiver (PHI) plus a PHI starter who held 30% of targets and missed `missed_weeks`."""
    snap = make_snapshot()
    snaps = [None if w in missed_weeks else 0.95 for w in range(1, 6)]
    shares = [None if w in missed_weeks else 0.30 for w in range(1, 6)]
    snap["tables"]["player_week_usage"] += _rows("00-STAR", "PHI", "WR", snaps, shares)
    snap["tables"]["nfl_players"].append({"gsis_id": "00-STAR", "display_name": "Star Receiver", "position": "WR"})
    snap["tables"]["nfl_injuries"] += list(injuries)
    snap["tables"]["nfl_rosters_weekly"] += list(rosters)
    return snap


def _riser(snap):
    f = build_facts(snap, ALL, set(), only="00-RISER")
    return f, f["players"][0], {c["id"]: c for c in f["claims"]}


def test_vacated_role_makes_a_fill_in_story():
    inj = [{"gsis_id": "00-STAR", "team": "PHI", "week": w, "report_status": "Out",
            "report_primary_injury": "Hamstring", "practice_status": "Did Not Participate In Practice"} for w in (4, 5)]
    inj.append({"gsis_id": "00-STAR", "team": "PHI", "week": 6, "report_status": None,
                "report_primary_injury": "Hamstring", "practice_status": "Did Not Participate In Practice"})
    f, p, cl = _riser(_with_teammate({4, 5}, inj))
    tm = p["teammate"]
    assert tm["kind"] == "vacated" and tm["name"] == "Star Receiver" and not tm["returned"]
    assert cl[tm["missed"]]["display"] == "Weeks 4 and 5"
    assert cl[tm["reason"]]["display"] == "a hamstring injury"
    assert cl[tm["role"]]["display"] == "30%"
    assert cl[tm["status"]]["display"] == "not practicing, per the Week 6 injury report"
    assert angle_for(p) == "fill_in"
    segs, titles, structure = short_script.compose(f, cl, "seed", {})
    text = " ".join(s["text_template"] for s in segs)
    assert "Star Receiver missed" in text and structure["teammate"] == "Star Receiver"
    assert qc.gate_narration({"claims": f["claims"], "title_templates": titles, "title_candidates": [
        short_script.fill(t, cl, "display") for t in titles], "narration_segments": [
        {**s, "text_display": short_script.fill(s["text_template"], cl, "display"),
         "text_spoken": short_script.fill(s["text_template"], cl, "spoken")} for s in segs]}).passed


def test_teammate_back_in_the_latest_game_makes_a_held_story():
    f, p, cl = _riser(_with_teammate({4}))
    assert p["teammate"]["returned"] and angle_for(p) == "held"
    assert cl[p["teammate"]["back_week"]]["display"] == "Week 5"


def test_a_returning_teammate_explains_a_faller():
    snap = make_snapshot()
    snap["tables"]["player_week_usage"] += _rows("00-BACK", "DAL", "WR", [None, None, None, 0.95, 0.95],
                                                 [None, None, None, 0.30, 0.32])
    snap["tables"]["nfl_players"].append({"gsis_id": "00-BACK", "display_name": "Back Receiver", "position": "WR"})
    f = build_facts(snap, ALL, set(), only="00-FALLER")
    p = f["players"][0]
    assert p["teammate"]["kind"] == "returned" and angle_for(p) == "displaced"


def test_status_reads_ir_then_designation_then_practice():
    snap = _with_teammate({4, 5}, rosters=[{"gsis_id": "00-STAR", "team": "PHI", "week": 6, "status": "RES"}])
    assert evaluate_claim(snap, {"kind": "player_status", "gsis_id": "00-STAR", "week": 6}) == ["ir", 6, ""]
    assert fmt_status(["designation", 6, "Questionable"])[0] == "listed questionable for Week 6"
    assert fmt_status(["none", 6, ""])[1] == "not on the Week six injury report"
