"""The flame / praise series showrunner (stages/showrunner.py): angles, briefs, formats, scenes, QC and history."""

from __future__ import annotations

import json

from conftest import make_snapshot

from studio import flame_scenes as FPS
from studio.core import canonical_json, sha256_bytes
from studio.numbers import render
from studio.stages import flame, qc
from studio.stages import showrunner as SR
from studio.stages.facts import evaluate_claim
from studio.stages.script import BANNED, DIGIT, NUMBER_WORDS, PLACEHOLDER
from studio.stages.story import finish

NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet"]
TEAMS = ["PHI", "DAL", "NYJ", "SEA", "BUF", "MIA", "KC", "LV", "DEN", "LAC"]


def _qb(gid, team, week, att, air, epa, yds, sacks=1, ints=0):
    return {"week": week, "gsis_id": gid, "team": team, "opponent": "XXX", "attempts": att, "completions": att // 2,
            "passing_yards": yds, "passing_air_yards": air, "passing_yards_after_catch": yds // 2,
            "passing_epa": epa, "sacks_suffered": sacks, "passing_tds": 1, "passing_interceptions": ints}


def qb_snapshot(latest_air=40.0, latest_yds=120, sacks=1, ints=0) -> dict:
    snap = make_snapshot()
    rows = []
    for i, team in enumerate(TEAMS):
        for week in range(1, 6):
            if i == 0 and week == 5:
                continue
            rows.append(_qb(f"QB{i:02d}", team, week, 32, 220 + 9 * ((i * 7 + week * 3) % 11),
                            float(-3 + ((i * 5 + week) % 9)), 230 + 3 * ((i + week) % 7)))
    rows.append(_qb("QB00", "PHI", 5, 30, latest_air, -14.0, latest_yds, sacks, ints))
    snap["tables"]["qb_week_passing"] = rows
    snap["tables"]["nfl_players"] += [{"gsis_id": f"QB{i:02d}", "display_name": f"{NAMES[i]} Passer",
                                       "position": "QB", "draft_round": 1} for i in range(10)]
    snap["content_sha256"] = sha256_bytes(canonical_json(snap["tables"]).encode())
    return snap


def qb_facts(**kw):
    snap = qb_snapshot(**kw)
    c = flame.pick(snap, only="alpha-passer")[0]
    facts = flame.build_facts(snap, {"gsis_id": c["gsis_id"], "name": c["name"], "role": c["kind"]})
    return snap, facts, {cl["id"]: cl for cl in facts["claims"]}


def _c(cid, value, fmt="int"):
    d, s = render(fmt, value)
    return {"id": cid, "value": value, "display": d, "spoken": s, "format": fmt, "label": cid,
            "source": {"kind": "test"}}


def receiver(targets=24, opp_targets=18, teams_fewer=4, rank=1, recognizable=True, result="lost"):
    """A praise case for a receiver, as facts.build_facts shapes it (claims made by hand)."""
    claims = {c["id"]: c for c in [
        _c("t", targets), _c("r", 17), _c("y", 138), _c("td", 1), _c("ot", opp_targets), _c("tf", teams_fewer),
        _c("rk", rank, "ordinal"), _c("n", 412), _c("pts", 34), _c("opts", 37), _c("res", result, "text"),
        _c("ven", "vs", "text"), _c("opp", "BUF", "team"), _c("tm", "LA", "team"), _c("wk", 5, "week"),
        _c("lv", targets), _c("lr", rank, "ordinal"), _c("lg", 412)]}
    hog = {"id": "target_hog", "broad": True, "flavor": "volume", "opp_targets": "ot", "teams_fewer": "tf",
           "rank": "rk", "games": "n", "opp_targets_value": opp_targets, "teams_fewer_value": teams_fewer,
           "rank_value": rank, "beats_opponent": opp_targets < targets, "strength": 5}
    p = {"gsis_id": "WR1", "name": "Test Receiver", "position": "WR", "team": "LA", "role": "praise", "flame": True,
         "week": 5, "team_name": "tm", "opponent": "opp", "week_claim": "wk", "recognizable": recognizable,
         "recognition": "test", "draft_round": None, "years_exp": 1,
         "lead": {"metric": "targets", "label": "targets", "screen": "TARGETS", "value": "lv", "rank": "lr",
                  "games": "lg", "rank_value": rank, "games_value": 412, "sup": "most"},
         "game": {"points": "pts", "opp_points": "opts", "result": "res", "venue": "ven", "result_value": result,
                  "home": True},
         "box": {"targets": "t", "targets_value": targets, "receptions": "r", "receptions_value": 17,
                 "receiving_yards": "y", "receiving_yards_value": 138, "receiving_tds": "td", "receiving_tds_value": 1},
         "angles": [hog, {"id": "stat_outlier", "broad": True, "flavor": "stat", "strength": 2}]}
    return p, claims


def compose_all(p, claims):
    out = {}
    for fmt, spec in SR.FORMATS.items():
        for a in p["angles"]:
            if p["role"] in spec["kinds"] and SR.FIT.get((fmt, a["id"])) and SR.eligible(fmt, a, p):
                brief = SR.choose(p, [], fmt)
                beats, titles = SR.compose({"players": [p]}, claims, brief)
                beats, titles, _ = finish(beats, p, {"column": a["id"]}, p["role"], fmt, None, {"tm": ""}, titles=titles)
                out[fmt] = (brief, beats, titles)
                break
    return out


def _loose_numbers(text: str) -> list:
    bare = PLACEHOLDER.sub(" ", text.replace("⏸", " ").replace("‖", " "))
    return DIGIT.findall(bare) + NUMBER_WORDS.findall(bare) + BANNED.findall(bare)


# ----------------------------------------------------------------------------------------- selection

def test_short_throws_is_found_and_the_comparison_tells_it():
    _, facts, claims = qb_facts()
    p = facts["players"][0]
    ids = [a["id"] for a in p["angles"]]
    assert "short_throws" in ids and "rough_day" not in ids
    brief = SR.choose(p, [])
    assert brief["format"] == "comparison" and brief["angle"] == "short_throws"
    assert brief["entertainment_test"]["pass"]


def test_a_rough_day_goes_to_court():
    _, facts, _ = qb_facts(latest_air=250.0, sacks=5, ints=2)
    p = facts["players"][0]
    assert [a["id"] for a in p["angles"]][0] == "rough_day"
    assert SR.choose(p, [])["format"] == "courtroom"


def test_recent_history_moves_the_choice_to_a_different_show():
    _, facts, _ = qb_facts()
    p = facts["players"][0]
    first = SR.choose(p, [])
    recent = [{"format_family": first["format"], "visual_style": first["visual_style"], "opening": first["opening"],
               "joke": first["joke"], "ending": first["ending"]}] * 2
    second = SR.choose(p, recent)
    assert second["format"] != first["format"]
    pen, why = SR.repetition(first["format"], recent)
    assert pen > 0 and any("format_family" in w for w in why)


def test_a_forced_treatment_is_honoured_when_the_story_supports_it():
    _, facts, _ = qb_facts()
    assert SR.choose(facts["players"][0], [], "roast_monologue")["format"] == "roast_monologue"


def test_the_entertainment_test_rejects_a_fantasy_only_premise():
    p, _ = receiver(recognizable=False, rank=7)
    p["lead"]["rank_value"] = 7
    p["angles"] = [{"id": "stat_outlier", "broad": False, "flavor": "stat", "strength": 2}]
    brief = SR.choose(p, [])
    assert not brief["entertainment_test"]["pass"]


# ---------------------------------------------------------------------------------- unsupported claims

def test_a_comparison_needs_a_benchmark_the_data_supports():
    # he did not out-target the other offense, and no team drew fewer targets: nothing to compare with
    p, _ = receiver(targets=16, opp_targets=30, teams_fewer=0)
    hog = p["angles"][0]
    assert not SR.eligible("comparison", hog, p)
    assert SR.choose(p, [])["format"] != "comparison"


def test_about_half_is_only_said_when_it_is_about_half():
    assert SR._about_half({"yac_share_value": 0.5}) and not SR._about_half({"yac_share_value": 0.62})


def test_an_ordinary_passing_distance_is_not_a_short_throws_story():
    snap = qb_snapshot(latest_air=230.0, latest_yds=120)
    cands = [c for c in flame.candidates(snap) if c["gsis_id"] == "QB00"]
    if cands:
        facts = flame.build_facts(snap, {"gsis_id": "QB00", "name": "x", "role": cands[0]["kind"]})
        assert "short_throws" not in [a["id"] for a in facts["players"][0]["angles"]]


# ---------------------------------------------------------------------------- formats, endings, claims

def test_every_format_is_its_own_show_and_none_ends_on_a_fantasy_question():
    _, facts, claims = qb_facts()
    flames = compose_all(facts["players"][0], claims)
    p, rclaims = receiver()
    praises = compose_all(p, rclaims)
    assert set(flames) >= {"roast_monologue", "courtroom", "comparison"}
    assert set(praises) >= {"main_character", "fan_meltdown", "comparison"}
    shows = list(flames.values()) + [v for k, v in praises.items() if k != "comparison"]
    scene_sets = [frozenset(b["scene"]["type"] for b in beats) for _, beats, _ in shows]
    assert len(set(scene_sets)) == len(scene_sets)
    assert all(not (a & b) for i, a in enumerate(scene_sets) for b in scene_sets[i + 1:])     # no shared scenes
    endings = {brief["ending"] for brief, _, _ in shows}
    assert len(endings) == len(shows)
    for brief, beats, titles in shows:
        assert not SR.fantasy_ending(beats[-1]["text_template"])
        for b in beats:
            assert not _loose_numbers(b["text_template"]), b["text_template"]
            assert b["scene"]["type"] in FPS.SCENES
        for t in titles:
            assert not _loose_numbers(t), t


def test_lengths_differ_by_format():
    ranges = {f: tuple(s["length"]) for f, s in SR.FORMATS.items()}
    assert ranges["roast_monologue"][1] < ranges["courtroom"][1]
    assert len(set(ranges.values())) >= 4


def test_every_claim_in_a_series_script_re_derives_from_the_snapshot():
    snap, facts, claims = qb_facts()
    for fmt in ("comparison", "roast_monologue", "courtroom"):
        brief = SR.choose(facts["players"][0], [], fmt)
        beats, titles = SR.compose(facts, claims, brief)
        used = {c for b in beats for c in PLACEHOLDER.findall(b["text_template"]) + b["scene"].get("claims", [])}
        used |= {c for t in titles for c in PLACEHOLDER.findall(t)}
        for cid in used:
            assert evaluate_claim(snap, claims[cid]["source"]) == claims[cid]["value"], claims[cid]["label"]


def test_scenes_draw_a_frame_in_every_format():
    from studio.stages import render_motion as R
    _, facts, claims = qb_facts()
    p = facts["players"][0]
    ctx = R.Ctx(facts, claims, FPS.ACCENT["flame"], "flame")
    for fmt in ("comparison", "roast_monologue", "courtroom"):
        beats, _ = SR.compose(facts, claims, SR.choose(p, [], fmt))
        for b in beats:
            for t in (0.0, 1.5):
                img = R.draw_scene({**b["scene"], "_start": 0.0}, t, 3.0, ctx)
                assert img.size == (1080, 1920)


# --------------------------------------------------------------------------------------------------- QC

def _script(beats, structure, episode_id="e"):
    return {"episode_id": episode_id, "structure": structure, "narration_segments":
            [{"text_display": b["text_template"]} for b in beats], "shots": [b["scene"] for b in beats]}


def test_the_series_gate(tmp_path):
    _, facts, claims = qb_facts()
    p = facts["players"][0]
    brief = SR.choose(p, [], "courtroom")
    beats, _ = SR.compose(facts, claims, brief)
    st = SR.structure(brief, beats)
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(brief))
    assert qc.gate_series(_script(beats, st), path, []).passed
    bad = beats[:-1] + [{**beats[-1], "text_template": "Still starting him in your lineup?"}]
    assert not qc.gate_series(_script(bad, st), path, []).passed
    unlabeled = beats + [{"text_template": "x", "scene": {"type": "dp_line"}}]
    assert not qc.gate_series(_script(unlabeled, st), path, []).passed
    history = [{"episode_id": f"h{i}", "structure": {"series": SR.SERIES, "format_family": "courtroom"}} for i in range(2)]
    assert not qc.gate_series(_script(beats, st), path, history).passed
    assert not qc.gate_series(_script(beats, st), tmp_path / "missing.json", []).passed


# ---------------------------------------------------------------------------- history and metadata

def test_history_records_what_a_viewer_would_notice_repeating(tmp_path):
    _, facts, claims = qb_facts()
    brief = SR.choose(facts["players"][0], [])
    beats, _ = SR.compose(facts, claims, brief)
    st = SR.structure(brief, beats)
    for key in ("series", "format_family", "opening", "joke", "ending", "visual_style", "music_mood", "scenes",
                "story_angle", "duration_range"):
        assert key in st
    (tmp_path / "history.jsonl").write_text(json.dumps({"episode_id": "a", "season": 2026, "week": 6,
                                                        "format": "short", "structure": st}) + "\n"
                                            + json.dumps({"episode_id": "b", "season": 2026, "week": 6,
                                                          "format": "short", "structure": {"angle": "x"}}) + "\n")
    assert [r["format_family"] for r in SR.series_history(tmp_path, 2026)] == [brief["format"]]


def test_metadata_speaks_nfl_not_fantasy():
    _, facts, claims = qb_facts()
    p = facts["players"][0]
    tags = SR.tags(p, claims)
    assert "nfl" in tags and not any("fantasy" in t for t in tags)
    brief = SR.choose(p, [], "courtroom")
    beats, _ = SR.compose(facts, claims, brief)
    st = SR.structure(brief, beats)
    desc = SR.description(p, claims, st)
    assert any("Parody" in line for line in desc)
    kicker, big, label = flame.thumbnail_text(p, claims, st)
    assert kicker and big and not DIGIT.search(kicker.replace(claims[p["week_claim"]]["display"], ""))


def test_the_classic_template_still_composes():
    _, facts, claims = qb_facts()
    beats, titles, structure = flame.compose(facts, claims, {})
    assert structure["format"] == "flame" and beats[0]["scene"]["type"] == "fp_hook"
