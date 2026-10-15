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
    assert [seg["section"] for seg in s["narration_segments"]] == ["hook", "evidence", "context", "verdict", "cta"]
    assert s["narration_segments"][0]["text_display"].count("Rising Receiver") >= 1
    for seg in s["narration_segments"]:
        assert not re.search(r"\d", seg["text_spoken"])
    assert qc.gate_narration(s).passed
    assert qc.gate_claims(read_json(ep.path("snapshot.json")), read_json(ep.path("facts.json")), s).passed
