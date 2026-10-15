"""My Team Value dashboard: exact equality with the canonical lineup and marginal-value functions,
league ranks on comparable weeks, missing vs zero, FLEX and changing weekly starters, IR and
activation sensitivity, removal-scenario depth, bench usage without double counting, waiver roster
capacity, regular-season/playoff splits, Playoff Race Lab integration, determinism, and read-only
behaviour with no effect on trade or projection outputs. Synthetic inputs only: they say nothing
about real Los Subies outcomes."""

import copy
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.analytics.playoff_race import build_playoff_race
from app.analytics.team_lineups import (RosterPlayer, best_fa_addition, compare, ir_activation_sensitivity,
                                        project_team, roster_marginal_value, starters, without)
from app.analytics.team_value import NotFound, build_team_value, load_team_value_inputs
from app.analytics.trades import evaluate_trade
from app.db.connection import connect, fetch_dicts

import synthetic_race as sr
import synthetic_value as sv
from test_playoff_race import _db_league
from test_trade_data import _raw_snapshot

EXTRAS = {"bye_weeks": {}, "waivers": []}


def player(res, pid):
    return next(p for p in res["players"] if p["espn_player_id"] == pid)


def slot(res, name):
    return next(s for s in res["positions"]["slots"] if s["slot"] == name)


def small(**kw):
    """6 teams, slots QB/RB/FLEX; each roster QB (20+t), RB 10, WR 5, bench RB 1; weeks 5-10, regular 5-8."""
    return sr.race(n_teams=6, reg_periods=8, spots=4, start_week=5, final_week=10, **kw)


def add_fa(d, pid, pos, pts, weeks=range(5, 11)):
    d.state.free_agents = [*d.state.free_agents, RosterPlayer(pid, pos, "SEA", f"FA {pos} {pid}")]
    d.state.projections[pid] = {w: pts for w in weeks}


# ----------------------------------------------------------- canonical equality --

def test_overview_and_marginals_equal_the_canonical_functions():
    d = sv.value_race(fa_count=40)
    res = build_team_value(d, EXTRAS, trades=False)
    st, window = d.state, d.state.window()
    roster = st.rosters[1]
    base = project_team(roster, st.slots, window, st.projections)
    assert res["overview"]["summary"] == base["summary"]
    assert [w["projected_lineup_points"] for w in res["overview"]["weekly"]] == \
        [w["projected_lineup_points"] for w in base["weeks"]]
    open_spots = max(0, st.active_capacity - sum(1 for p in roster if not p.on_ir))
    for p in roster:
        mv = roster_marginal_value(roster, p.espn_player_id, st.slots, window, st.projections, base, open_spots)
        row = player(res, p.espn_player_id)
        assert row["marginal"] == {"all": mv["roster_marginal_lineup_points"],
                                   "regular": mv["regular_season_marginal_lineup_points"],
                                   "playoff": mv["playoff_marginal_lineup_points"],
                                   "known_all": mv["known_marginal_lineup_points"]}
        assert row["weeks_started"] == mv["weeks_started"] and row["weekly"] == mv["weeks"]
        assert sorted(row["weeks_started"] + row["weeks_benched"]) == list(window.weeks)
    # Marginal values are never summed into a team value.
    flat = json.dumps(res)
    assert "total_marginal" not in flat and "marginal_total" not in flat
    assert any("never add them up" in n for n in res["notes"])


def test_regular_and_playoff_splits_partition_the_window():
    d = sv.value_race(fa_count=10)
    res = build_team_value(d, EXTRAS, trades=False)
    ph = res["overview"]["phases"]
    assert ph["regular"]["weeks"] == list(range(6, 15)) and ph["playoff"]["weeks"] == [15, 16, 17]
    if ph["all"]["projected_points"] is not None:
        assert ph["regular"]["projected_points"] + ph["playoff"]["projected_points"] == pytest.approx(ph["all"]["projected_points"])
    for p in res["players"]:
        m = p["marginal"]
        if m["all"] is not None:
            assert m["regular"] + m["playoff"] == pytest.approx(m["all"])
        raw = p["espn_projected"]
        if raw["all"]["points"] is not None:
            assert raw["regular"]["points"] + raw["playoff"]["points"] == pytest.approx(raw["all"]["points"])


# ------------------------------------------------------------ comparable ranks --

def test_league_ranks_use_only_weeks_every_team_completes():
    d = small(projection_patch={301: {7: None}})            # team 3's QB unknown in week 7
    res = build_team_value(d, EXTRAS, trades=False)
    ranks = res["overview"]["league_ranks"]
    assert ranks["excluded_weeks"] == [{"week": 7, "incomplete_teams": ["Team 3"]}]
    assert ranks["phases"]["all"]["weeks"] == [5, 6, 8, 9, 10]
    assert ranks["phases"]["regular"]["weeks"] == [5, 6, 8]
    rows = ranks["phases"]["all"]["teams"]
    # Weekly totals = strength + 15 = 36 + (t - 1): team 6 strongest, ranks follow, all on the same 5 weeks.
    assert [x["team_id"] for x in rows] == [6, 5, 4, 3, 2, 1]
    assert {x["team_id"]: x["total"] for x in rows} == {t: (35.0 + t) * 5 for t in range(1, 7)}
    assert res["overview"]["phases"]["all"]["league_rank"]["rank"] == 6
    # My own complete forecast is still shown in full (6 weeks), separately from the comparable rank.
    assert res["overview"]["phases"]["all"]["projected_points"] == 36.0 * 6
    d2 = small(rosters_missing={4})                          # a team without a roster snapshot
    r2 = build_team_value(d2, EXTRAS, trades=False)["overview"]["league_ranks"]
    assert r2["teams_unranked"] == [4] and 4 not in [x["team_id"] for x in r2["phases"]["all"]["teams"]]


def test_position_strength_ranks_slot_groups_against_the_league():
    d = small(strength={1: 40.0, 2: 20.0, 3: 21.0, 4: 22.0, 5: 23.0, 6: 24.0})
    res = build_team_value(d, EXTRAS, trades=False)
    qb = slot(res, "QB")["strength"]["all"]
    assert qb["average"] == 40.0 and qb["rank"] == 1 and qb["teams_ranked"] == 6
    assert slot(res, "RB")["strength"]["all"]["rank"] == 1          # every RB slot 10.0: a six-way tie at 1
    assert "QB" in res["positions"]["strongest"]


# --------------------------------------------------------------- missing vs zero --

def test_missing_projection_is_unknown_and_zero_and_bye_are_real():
    d = small(projection_patch={101: {6: None}, 102: {7: 0.0}, 103: {8: 0.0}})
    res = build_team_value(d, {"bye_weeks": {103: [8]}, "waivers": []}, trades=False)
    ov = res["overview"]
    assert ov["phases"]["all"]["projected_points"] is None and ov["phases"]["all"]["incomplete_weeks"] == [6]
    assert ov["phases"]["all"]["known_points"] is not None and ov["missing_players"] == ["QB 1"]
    week6 = next(w for w in ov["weekly"] if w["week"] == 6)
    assert week6["projected_lineup_points"] is None and week6["projection_complete"] is False
    qb = player(res, 101)
    assert qb["espn_projected"]["all"]["points"] is None and qb["espn_projected"]["all"]["missing_weeks"] == [6]
    assert next(b for b in qb["bench"] if b["week"] == 6)["code"] == "no_projection"
    rb = player(res, 102)
    assert next(b for b in rb["bench"] if b["week"] == 7)["code"] == "zero_projection"
    wr = player(res, 103)
    assert next(b for b in wr["bench"] if b["week"] == 8)["code"] == "bye"
    assert rb["espn_projected"]["all"]["points"] == 10.0 * 5                           # a 0 week counts as 0
    # Week 6 is incomplete for my team, so it is excluded from everyone's comparable rank.
    assert 6 not in ov["league_ranks"]["phases"]["all"]["weeks"]


# ------------------------------------------------------------- FLEX and starters --

def test_flex_assignment_and_changing_weekly_starters():
    # Team 1's bench RB (104) out-projects the WR (103, 5.0) in weeks 9-10, so it takes FLEX there.
    d = small(projection_patch={104: {9: 7.0, 10: 7.0}})
    res = build_team_value(d, EXTRAS, trades=False)
    rb2, wr = player(res, 104), player(res, 103)
    assert rb2["weeks_started"] == [9, 10] and wr["weeks_started"] == [5, 6, 7, 8]
    assert rb2["marginal"]["all"] == pytest.approx(2 * (7.0 - 5.0))                  # only what it adds over the WR
    assert wr["marginal"]["all"] == pytest.approx(4 * (5.0 - 1.0))
    assert {b["code"] for b in wr["bench"]} == {"flex_competition"} and [b["week"] for b in wr["bench"]] == [9, 10]
    assert "FLEX taken by RB 1" not in wr["bench"][0]["text"] and "Bench RB 1" in wr["bench"][0]["text"]
    flex = slot(res, "FLEX")
    assert {x["espn_player_id"] for x in flex["removal_scenarios"]} == {103, 104}


# ---------------------------------------------------------------------- IR --

def test_ir_player_is_inactive_and_activation_sensitivity_is_separate():
    d = small()
    d.state.rosters[1] = [replace(p, on_ir=True) if p.espn_player_id == 104 else p for p in d.state.rosters[1]]
    d.state.projections[104] = {w: 30.0 for w in range(5, 11)}               # would start if active
    res = build_team_value(d, EXTRAS, trades=False)
    ir = player(res, 104)
    assert ir["on_ir"] and ir["marginal"]["all"] == 0.0 and ir["weeks_started"] == []
    assert {b["code"] for b in ir["bench"]} == {"ir"} and ir["unused_projected_points"] == 0.0
    st, window = d.state, d.state.window()
    roster = st.rosters[1]
    open_spots = max(0, st.active_capacity - sum(1 for p in roster if not p.on_ir))
    assert ir["ir_activation_sensitivity"] == ir_activation_sensitivity(roster, 104, st.slots, window, st.projections,
                                                                        open_spots)
    assert ir["ir_activation_sensitivity"]["lineup_points_upside"] > 0
    assert res["bench"]["ir_players"] == [104] and 104 not in [x["espn_player_id"] for x in res["bench"]["players"]]
    assert res["overview"]["summary"]["ros_projected_starting_points"] == 36.0 * 6   # IR upside never in the total


# ------------------------------------------------------------------ depth --

def test_removal_scenarios_match_explicit_rosters_without_the_player():
    d = sv.value_race(fa_count=10)
    res = build_team_value(d, EXTRAS, trades=False)
    st, window = d.state, d.state.window()
    roster = st.rosters[1]
    base = project_team(roster, st.slots, window, st.projections)
    for row in res["positions"]["slots"]:
        for rem in row["removal_scenarios"]:
            pid = rem["espn_player_id"]
            rest = project_team(without(roster, [pid]), st.slots, window, st.projections)
            assert rem["known_lineup_points_lost"] == compare(rest, base, window)["known_ros_delta"]
            fill = {}
            for bw, rw in zip(base["weeks"], rest["weeks"]):
                for q in starters(rw) - starters(bw):
                    fill.setdefault(q, []).append(bw["week"])
            assert {f["espn_player_id"]: f["weeks"] for f in rem["fill_ins"]} == fill
            if rem["lineup_points_lost"] is not None:
                assert rem["recovered_by_roster"] == pytest.approx(rem["own_started_points"] - rem["lineup_points_lost"])
    assert "not a prediction of injuries" in res["positions"]["note"]


# ------------------------------------------------------------ bench & surplus --

def test_bench_unused_projection_is_per_player_and_never_double_counted():
    d = small(projection_patch={104: {9: 7.0, 10: 7.0}})
    res = build_team_value(d, EXTRAS, trades=False)
    for p in res["players"]:
        expect = sum(b["projection"] for b in p["bench"] if b["projection"] and not p["on_ir"])
        assert p["unused_projected_points"] == pytest.approx(expect)
        assert p["marginal"]["known_all"] <= p["started_points"] + 1e-9           # a starter never adds more than he scores
    bench = res["bench"]
    assert [x["espn_player_id"] for x in bench["players"]] == [103, 104]          # most unused first (10.0 vs 4.0)
    assert bench["consolidation_candidates"] == [103, 104]
    assert "not automatically a sell" in bench["note"]
    assert "unused_projected_points" not in json.dumps({k: v for k, v in res["overview"].items()})


# --------------------------------------------------------------- upgrades --

def test_free_addition_add_drop_and_roster_capacity():
    d = small()
    add_fa(d, 9001, "WR", 8.0)
    add_fa(d, 9002, "QB", 22.0)
    res = build_team_value(d, EXTRAS, trades=False)
    up = res["upgrades"]
    st, window = d.state, d.state.window()
    roster = st.rosters[1]
    base = project_team(roster, st.slots, window, st.projections)
    free = best_fa_addition(roster, st.free_agents, st.slots, window, st.projections)
    assert up["free_addition"]["player"] == free["player"] and up["free_addition"]["lineup_points_added"] == free["lineup_points_added"]
    assert up["free_addition"]["open_active_roster_spots"] == 0 and up["free_addition"]["realistic"] is False
    assert up["free_addition"]["label"].startswith("Optimistic")
    best = up["add_drop"][0]
    assert best["drop"]["espn_player_id"] == 104                                  # equal gain: drop the bench RB
    after = project_team(without(roster, [104]) + [next(p for p in st.free_agents if p.espn_player_id == best["add"]["espn_player_id"])],
                         st.slots, window, st.projections)
    assert best["ros_delta"] == compare(base, after, window)["ros_delta"] == 3.0 * 6
    # With an open active roster spot the free addition is realistic.
    d.state.active_capacity = 5
    res = build_team_value(d, EXTRAS, trades=False)
    assert res["upgrades"]["free_addition"]["realistic"] is True and res["upgrades"]["free_addition"]["open_active_roster_spots"] == 1
    gaps = res["upgrades"]["slot_gaps"]
    assert gaps and all(g["gap"] > 0 for g in gaps)
    assert any(g["slot"] == "FLEX" and g["best_available"]["espn_player_id"] == 9001 for g in gaps)


def test_stored_waiver_suggestions_are_rechecked_on_the_lineup_engine():
    d = small()
    add_fa(d, 9001, "WR", 8.0)
    waivers = [{"espn_player_id": 9001, "position": "WR", "recommendation": "CLAIM", "waiver_score": 7.1,
                "suggested_drop_id": 104},
               {"espn_player_id": 9999, "position": "RB", "recommendation": "WAIT", "waiver_score": 5.0,
                "suggested_drop_id": None}]
    res = build_team_value(d, {"bye_weeks": {}, "waivers": waivers}, trades=False)
    w1, w2 = res["upgrades"]["waiver_checks"]
    assert w1["status"] == "add/drop" and w1["ros_delta"] == 18.0 and w1["drop_name"] == "Bench RB 1"
    assert w2["status"] == "no longer a free agent or waiver player"
    other = build_team_value(d, {"bye_weeks": {}, "waivers": waivers}, team_id=2, trades=False)
    assert other["upgrades"]["waiver_checks"] == []                               # stored waivers are for my team only


def test_trade_preview_rows_equal_the_manual_evaluator():
    d = sv.value_race(fa_count=10)
    res = build_team_value(d, EXTRAS, trades=True)
    rows = res["upgrades"]["trade_preview"]
    assert rows and all(r["consistent_with_manual_evaluator"] for r in rows)
    for r in rows[:2]:
        ev = evaluate_trade(d.state, 1, r["opponent_team_id"], r["sends"], r["receives"])
        assert r["mine"]["ros_delta"] == ev["team_a"]["deltas"]["ros_delta"]
        assert r["theirs"]["ros_delta"] == ev["team_b"]["deltas"]["ros_delta"]
        assert r["evaluator_link"] == {"ta": 1, "tb": r["opponent_team_id"], "a": r["sends"], "b": r["receives"]}
    assert "No acceptance estimate" in res["upgrades"]["trade_preview_rule"]


# ------------------------------------------------------------ playoff race --

def test_playoff_race_context_and_deterministic_matchup_effects():
    d = small()
    add_fa(d, 9001, "WR", 8.0)
    res = build_team_value(d, EXTRAS, trades=True)
    race = build_playoff_race(d, team_id=1)
    me = next(t for t in race["teams"] if t["team_id"] == 1)
    pr = res["playoff_race"]
    assert pr["remaining"] == me["remaining"] and pr["standing_projected"] == me["projected"]
    assert pr["close_games"] == [g for g in me["remaining"] if g["close"]]
    eff = pr["upgrade_effects"]["add_drop"]
    st, window = d.state, d.state.window()
    after = project_team(without(st.rosters[1], [104]) + [st.free_agents[0]], st.slots, window, st.projections)
    by_week = {w["week"]: w["projected_lineup_points"] for w in after["weeks"]}
    for e in eff["matchups"]:
        m = next(x for x in race["matchups"]["regular"] if x["matchup_id"] == e["matchup_id"])
        home = m["home_team_id"] == 1
        mine_b = (m["home_projection"] if home else m["away_projection"])["projected_points"]
        opp_b = (m["away_projection"] if home else m["home_projection"])["projected_points"]
        assert e["margin_before"] == pytest.approx(mine_b - opp_b)
        assert e["margin_after"] == pytest.approx(sum(by_week[w] for w in m["weeks"]) - opp_b)
        assert e["flips"] == (e["margin_before"] * e["margin_after"] < 0)
        assert e["changes_projected_result"] == ((e["margin_before"] > 0, e["margin_before"] < 0)
                                                 != (e["margin_after"] > 0, e["margin_after"] < 0))
    assert any(e["flips"] for e in eff["matchups"])                               # week 5: 36-37 becomes 39-37
    assert "not win probabilities" in pr["rule"]
    trade = pr["upgrade_effects"]["trade"]
    if trade:
        assert all(e["margin_after"] is not None for e in trade["matchups"])


# ------------------------------------------------------ determinism, errors --

def test_deterministic_inputs_unchanged_and_errors():
    d = sv.value_race(fa_count=20)
    snap = copy.deepcopy(d)
    a = build_team_value(d, EXTRAS)
    b = build_team_value(d, EXTRAS)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    assert d.state.projections == snap.state.projections and d.state.rosters == snap.state.rosters
    assert d.matchups == snap.matchups
    with pytest.raises(NotFound):
        build_team_value(d, EXTRAS, team_id=99)
    d.teams = [{**t, "is_mine": False} for t in d.teams]
    with pytest.raises(NotFound):
        build_team_value(d, EXTRAS)
    e = small()
    e.state.start_week = None
    out = build_team_value(e, EXTRAS)
    assert out["empty"] is True and "No remaining" in out["message"]
    f = small(rosters_missing={1})
    assert build_team_value(f, EXTRAS)["empty"] is True


# ---------------------------------------------------------- database & API --

def test_api_is_read_only_and_leaves_trade_and_projection_outputs_unchanged(tmp_path, monkeypatch):
    db_path, con = _db_league(tmp_path)
    lid = fetch_dicts(con, "SELECT league_id FROM leagues")[0]["league_id"]
    con.execute("UPDATE matchups SET league_id = ?", [lid])
    race, extras = load_team_value_inputs(con)
    assert race.state.league_id == lid and "bye_weeks" in extras
    raw = _raw_snapshot(con)
    tables = {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY ALL") for t in
              ("player_scores", "player_ros_projections", "team_week_lineup_projections", "team_ros_lineup_projections",
               "waiver_recommendations", "team_position_needs", "fantasy_teams", "matchups")}
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    from test_schedule_intelligence import stable
    api = TestClient(app)
    trade = {"team_a_id": 2, "team_b_id": 1, "team_a_sends": [304], "team_b_sends": [301]}
    before = (api.post("/api/trades/evaluate", json=trade).json(), api.get("/api/trades/discover").json(),
              api.get("/api/lineup-projections").json(), api.get("/api/teams/2/lineup-projection").json(),
              api.get("/api/playoff-race").json())
    mtime = db_path.stat().st_mtime_ns
    res = api.get("/api/my-team/value")
    assert res.status_code == 200
    body = res.json()
    assert body["team"]["team_id"] == 2 and body["team"]["is_mine"]
    if not body.get("empty"):
        lp = before[3]
        mv = {m["espn_player_id"]: m["roster_marginal_lineup_points"] for m in lp["marginal_values"]}
        assert {p["espn_player_id"]: p["marginal"]["all"] for p in body["players"]} == mv   # same as the existing endpoint
    assert api.get("/api/my-team/value", params={"team_id": 3, "trades": "false"}).json()["team"]["team_id"] == 3
    assert api.get("/api/my-team/value", params={"team_id": 99}).status_code == 404
    after = (api.post("/api/trades/evaluate", json=trade).json(), api.get("/api/trades/discover").json(),
             api.get("/api/lineup-projections").json(), api.get("/api/teams/2/lineup-projection").json(),
             api.get("/api/playoff-race").json())
    assert stable(before) == stable(after)
    assert db_path.stat().st_mtime_ns == mtime
    con = connect(db_path, read_only=True)
    assert _raw_snapshot(con) == raw
    assert {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY ALL") for t in tables} == tables
    con.close()


def test_api_without_league_and_module_isolation(tmp_path, monkeypatch):
    from app.db.connection import init_db
    db_path = tmp_path / "empty.duckdb"
    con = connect(db_path)
    init_db(con)
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    assert TestClient(app).get("/api/my-team/value").status_code == 503
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app" / "analytics"
    for f in ("player_scores.py", "waivers.py", "manager_metrics.py", "manager_scouting.py", "projections.py",
              "trades.py", "team_lineups.py", "trade_data.py", "trade_discovery.py", "pipeline.py", "context.py",
              "schedule_intelligence.py", "playoff_race.py", "lineup.py"):
        assert "team_value" not in (root / f).read_text(), f
