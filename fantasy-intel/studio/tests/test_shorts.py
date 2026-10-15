from __future__ import annotations

import re

from studio.core import Episode, read_json, write_json
from studio.stages import facts, qc, story
from studio.stages.facts import pick_stories
from studio.stages.render_motion import align, phrases
from studio.stages.story import choose, storyline

from conftest import make_snapshot


def _metric(column, dot, change=None):
    return {"column": column, "label": column, "delta_over_threshold": dot, "baseline": "c001", "recent": "c002",
            "change": change or "c009"}


def test_storyline_follows_the_data():
    claims = {"c009": {"value": -0.03}, "c010": {"value": -0.20}}
    # Targets fell while snaps barely moved: the "where did his targets go?" story.
    assert storyline({"role": "faller", "metrics": [_metric("target_share", -3), _metric("offense_pct", -0.4)]},
                     claims) == "vanishing_targets"
    assert storyline({"role": "faller", "metrics": [_metric("target_share", -3), _metric("offense_pct", -2, "c010")]},
                     claims) == "shrinking"
    assert storyline({"role": "riser", "metrics": [_metric("backfield_rush_share", 5)]}, claims) == "takeover"
    assert storyline({"role": "riser", "metrics": [_metric("offense_pct", 6), _metric("target_share", 2)]},
                     claims) == "breakout"
    assert storyline({"role": "riser", "metrics": [_metric("offense_pct", 6), _metric("target_share", 0.3)]},
                     claims) == "unresolved"


def test_hook_rotation_prefers_unused_then_listed_order():
    opts = ["a", "b", "c"]
    assert choose(opts, [], "x") == "a"                       # the preferred (first-listed) hook by default
    assert choose(opts, ["a", "b"], "seed") == "c"            # never used wins
    assert choose(opts, ["c", "a", "b"], "seed") == "c"       # oldest use wins


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


def test_words_align_to_display_tokens_and_phrases():
    claims = {"c001": {"display": "21%", "spoken": "twenty-one percent"},
              "c002": {"display": "9%", "spoken": "nine percent"}}
    seg = {"text_template": "His target share went from {{c001}} to just {{c002}}.", "emphasis": ["just"]}
    spoken = "His target share went from twenty-one percent to just nine percent .".split()
    words = [{"text": w, "start": i * 0.3, "end": i * 0.3 + 0.28} for i, w in enumerate(spoken)]
    toks = align(seg, words, claims)
    assert [t["text"] for t in toks] == ["His", "target", "share", "went", "from", "21%", "to", "just", "9%."]
    nine = toks[-1]
    assert nine["claim"] == "c002" and abs(nine["start"] - words[9]["start"]) < 1e-3   # lands on "nine"
    assert toks[5]["emph"] and toks[7]["emph"] and not toks[0]["emph"]          # numbers and chosen words
    assert all(len(p) <= 4 for p in phrases(toks))


def test_short_script_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_EPISODES", str(tmp_path / "episodes"))
    from studio.core import Config
    cfg = Config.load()
    ep = Episode(2026, 6, "short", cfg, log=lambda *_: None, slug="rising-receiver")
    ep.dir.mkdir(parents=True)
    snap = make_snapshot()
    write_json(ep.path("snapshot.json"), snap)
    picked = next(s for s in pick_stories(snap, 2, 1, set()) if s["gsis_id"] == "00-RISER")
    write_json(ep.path("story.json"), picked)
    facts.run(ep)
    story.run(ep)
    s = read_json(ep.path("script.json"))
    assert s["structure"]["angle"] in ("breakout", "unresolved")
    assert s["narration_segments"][-1]["section"] == "question"
    assert all(seg["scene"]["type"] for seg in s["narration_segments"])
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
    assert storyline(p, cl) == "fill_in"
    segs, titles, structure = story.compose(f, cl, "seed", {})
    text = " ".join(s["text_template"] for s in segs)
    assert "Star Receiver missed" in text and structure["teammate"] == "Star Receiver"
    assert qc.gate_narration({"claims": f["claims"], "title_templates": titles, "title_candidates": [
        story.fill(t, cl, "display") for t in titles], "narration_segments": [
        {**s, "text_display": story.fill(s["text_template"], cl, "display"),
         "text_spoken": story.fill(s["text_template"], cl, "spoken")} for s in segs]}).passed


def test_teammate_back_in_the_latest_game_makes_a_held_story():
    f, p, cl = _riser(_with_teammate({4}))
    assert p["teammate"]["returned"] and storyline(p, cl) == "held"
    assert cl[p["teammate"]["back_week"]]["display"] == "Week 5"


def test_a_returning_teammate_explains_a_faller():
    snap = make_snapshot()
    snap["tables"]["player_week_usage"] += _rows("00-BACK", "DAL", "WR", [None, None, None, 0.95, 0.95],
                                                 [None, None, None, 0.30, 0.32])
    snap["tables"]["nfl_players"].append({"gsis_id": "00-BACK", "display_name": "Back Receiver", "position": "WR"})
    f = build_facts(snap, ALL, set(), only="00-FALLER")
    p = f["players"][0]
    cl = {c["id"]: c for c in f["claims"]}
    assert p["teammate"]["kind"] == "returned" and storyline(p, cl) == "displaced"


def test_status_reads_ir_then_designation_then_practice():
    snap = _with_teammate({4, 5}, rosters=[{"gsis_id": "00-STAR", "team": "PHI", "week": 6, "status": "RES"}])
    assert evaluate_claim(snap, {"kind": "player_status", "gsis_id": "00-STAR", "week": 6}) == ["ir", 6, ""]
    assert fmt_status(["designation", 6, "Questionable"])[0] == "listed questionable for Week 6"
    assert fmt_status(["none", 6, ""])[1] == "not on the Week six injury report"


def test_vanishing_targets_story_answers_its_hook():
    """Faller whose snaps held: the story says where the targets went, from real teammate shares."""
    snap = make_snapshot()
    snap["tables"]["player_week_usage"] = [r for r in snap["tables"]["player_week_usage"] if r["gsis_id"] != "00-FALLER"]
    snap["tables"]["player_week_usage"] += _rows("00-FALLER", "DAL", "TE", [0.70, 0.66, 0.67, 0.65, 0.66],
                                                 [0.21, 0.29, 0.12, 0.07, 0.11])
    snap["tables"]["player_week_usage"] += _rows("00-GAINER", "DAL", "WR", [0.8] * 5, [0.10, 0.10, 0.10, 0.20, 0.22])
    snap["tables"]["nfl_players"].append({"gsis_id": "00-GAINER", "display_name": "Gaining Receiver", "position": "WR"})
    f = build_facts(snap, ALL, set(), only="00-FALLER")
    cl = {c["id"]: c for c in f["claims"]}
    p = f["players"][0]
    assert storyline(p, cl) == "vanishing_targets"
    assert [g["name"] for g in p["gainers"]] == ["Gaining Receiver"]
    beats, titles, st = story.compose(f, cl, "seed", {})
    assert st["hook"] == "mystery" and beats[0]["text_template"].endswith("So why did his targets dry up?")
    assert beats[0]["scene"]["type"] == "player_card"                 # identity and conflict on frame 0
    assert [b["section"] for b in beats] == ["hook", "evidence", "twist", "takeaway", "question"]
    # Per-game counts, split where they fell off: "7 targets, then 10. Since then: 4, 2, 4."
    assert "Since then:" in story.fill(beats[1]["text_template"], cl, "display")
    # No teammate attribution in the narration: two games of other players' shares cannot show causation.
    assert not any("Gaining Receiver" in b["text_template"] for b in beats)
    alt, _, st2 = story.compose(f, cl, "seed", {}, hook_override="comparison")
    assert st2["hook"] == "comparison" and "twist" not in [b["section"] for b in alt]   # hook already said it
    assert alt[0]["scene"]["type"] == "split"


def test_decline_split_finds_the_drop_off():
    from studio.stages.story import decline_split
    assert decline_split([6, 8, 3, 2, 2]) == 2           # 6, then 8. Since then: 3, 2, 2
    assert decline_split([6, 6, 6, 6]) is None           # no drop-off
    assert decline_split([6, 3, 3]) == 1
    assert decline_split([6, 2]) is None                 # needs at least two games after the break


def test_headshots_are_opt_in_and_fall_back_to_the_jersey(tmp_path):
    from PIL import Image
    from studio import headshots, motion as M
    off = {"shorts": {"headshots": False}}
    assert headshots.for_player(off, {"name": "A B", "espn_id": 15847}) is None      # no download when off
    assert headshots.for_player({"shorts": {"headshots": True}}, {"name": "A B", "espn_id": None}) is None
    jersey = M.player_art("KC", "87", "Kelce", None, 300)
    assert jersey.size == M.jersey("KC", "87", "Kelce", 300).size
    shot = tmp_path / "h.png"
    Image.new("RGBA", (600, 436), (200, 150, 120, 255)).save(shot)
    art = M.player_art("KC", "87", "Kelce", shot, 300)
    assert art.size == jersey.size                       # same footprint, drops into the same layout


def test_a_number_range_stays_on_one_caption_card():
    words = ["As", "a", "share,", "that's", "23%", "down", "to", "12%."]
    toks = [{"text": w, "claim": ("c1" if w == "23%" else "c2" if w == "12%." else None)} for w in words]
    cards = [" ".join(t["text"] for t in ph) for ph in phrases(toks)]
    assert "that's 23% down to 12%." in cards


def test_targets_falling_while_snaps_rise_is_not_a_shrinking_role():
    claims = {"c009": {"value": 0.06}}
    p = {"role": "faller", "metrics": [_metric("target_share", -3), _metric("offense_pct", 1.2)]}
    assert storyline(p, claims) == "vanishing_targets"


def test_an_absence_after_the_rise_does_not_explain_it():
    # The riser was already up in Week 4 with the starter playing; the starter missed only Week 5.
    f, p, cl = _riser(_with_teammate({5}))
    assert p["teammate"] is None
    assert storyline(p, cl) != "fill_in"
