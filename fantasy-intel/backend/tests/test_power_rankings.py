"""League Power Rankings: league-relative scaling, the weighted composite, shared ranks, all-play math over
completed periods only, depth and replacement components from the canonical optimizer, and week-over-week
movement from a point-in-time previous edition (rosters and the last projection snapshot before kickoff).
Synthetic inputs only: they say nothing about real Los Subies outcomes."""

from dataclasses import replace
from datetime import datetime, timezone

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.analytics import power_rankings as pr
from app.analytics.power_rankings import (WEIGHTS, Previous, all_play_before, build_power_rankings, composite,
                                          load_previous, movement, rank, scale)
from app.analytics.playoff_race import classify_matchups
from app.analytics.team_lineups import RosterPlayer, project_team
from app.analytics.team_value import NotFound, league_ranks

import synthetic_race as sr


def team(res, tid):
    return next(t for t in res["teams"] if t["team_id"] == tid)


def small(**kw):
    """6 teams, slots QB/RB/FLEX; weekly optimal total = QB strength (20 + t) + 15; weeks 5-10, regular 1-8."""
    return sr.race(n_teams=6, reg_periods=8, spots=4, start_week=5, final_week=10, **kw)


# ------------------------------------------------------------------ pure math --

def test_scale_is_league_relative_best_100_worst_0_and_ignores_missing():
    assert scale({1: 10.0, 2: 20.0, 3: 15.0, 4: None}) == {1: 0.0, 2: 100.0, 3: 50.0, 4: None}
    assert scale({1: 7.0, 2: 7.0}) == {1: 50.0, 2: 50.0}
    assert scale({1: None}) == {1: None}


def test_composite_is_the_weighted_mean_and_renormalizes_a_component_nobody_has():
    scores = {"strength": {1: 100.0, 2: 0.0}, "all_play": {1: 0.0, 2: 100.0}, "depth": {1: 50.0, 2: 50.0},
              "replacement": {1: 100.0, 2: 0.0}}
    comp, w = composite(scores)
    assert w == WEIGHTS
    assert comp[1] == pytest.approx(round(45 + 0 + 7.5 + 10, 1))
    assert comp[2] == pytest.approx(round(0 + 30 + 7.5 + 0, 1))
    # No completed weeks yet: all-play is dropped for everyone and the other weights scale up to 1.
    comp, w = composite({**scores, "all_play": {1: None, 2: None}})
    assert w["all_play"] == 0 and sum(w.values()) == pytest.approx(1, abs=1e-3)
    assert comp[1] == pytest.approx(round((45 + 7.5 + 10) / 0.7, 1))


def test_rank_shares_equal_values_and_skips_missing():
    assert rank({1: 80.0, 2: 90.0, 3: 80.0, 4: None, 5: 10.0}) == {2: 1, 1: 2, 3: 2, 5: 4}


def test_movement_is_places_gained_and_none_without_a_previous_rank():
    assert movement({1: 1, 2: 2, 3: 3}, {1: 3, 2: 2}) == {1: 2, 2: 0, 3: None}
    assert movement({1: 1}, None) == {1: None}


# ------------------------------------------------------------------ all-play --

def test_all_play_counts_every_other_team_each_completed_period_and_respects_the_cutoff():
    # 4 teams, periods 1-3 final. Scores chosen so each period's order is known.
    scores = {(1, 1): 120, (1, 2): 110, (1, 3): 100, (1, 4): 90,
              (2, 1): 90, (2, 2): 120, (2, 3): 100, (2, 4): 100,
              (3, 1): 50, (3, 2): 60, (3, 3): 70, (3, 4): 80}
    data = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=4, final_week=8,
                   scores={k: float(v) for k, v in scores.items()})
    regular, _, _ = classify_matchups(data, {})
    full = all_play_before(regular, [1, 2, 3, 4], None)
    # Team 1: P1 3-0, P2 0-3, P3 0-3 -> 3-6. Team 3: P1 1-2, P2 1-1-1, P3 2-1 -> 4-4-1.
    assert (full[1]["wins"], full[1]["losses"], full[1]["ties"]) == (3, 6, 0)
    assert (full[3]["wins"], full[3]["losses"], full[3]["ties"]) == (4, 4, 1)
    assert full[3]["pct"] == round(4.5 / 9, 4)
    assert all(sum(full[t][k] for k in ("wins", "losses", "ties")) == 9 for t in full)
    # Point-in-time cutoff: before week 3 only periods 1-2 count.
    early = all_play_before(regular, [1, 2, 3, 4], 3)
    assert early[1]["periods"] == [1, 2] and (early[1]["wins"], early[1]["losses"]) == (3, 3)


def test_in_progress_periods_never_count_toward_all_play():
    data = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=4, final_week=8, in_progress={3})
    res = build_power_rankings(data, None)
    assert res["window"]["all_play_periods"] == [1, 2]
    assert all(t["all_play"]["wins"] + t["all_play"]["losses"] + t["all_play"]["ties"] == 6 for t in res["teams"])


# ------------------------------------------------------------- composition --

def test_lineup_strength_is_team_values_comparable_week_average_and_drives_rank():
    data = small()
    res = build_power_rankings(data, None, kdst_streaming=False)
    st = data.state
    window = st.window()
    proj = {t: project_team(st.rosters[t], st.slots, window, st.projections) for t in st.rosters}
    expected = {x["team_id"]: x["average_weekly"] for x in league_ranks(proj, window, {})["phases"]["all"]["teams"]}
    for t in res["teams"]:
        assert t["components"]["strength"]["value"] == expected[t["team_id"]] == 20 + t["team_id"] + 15
    # Team 6 has the strongest QB, team 1 the best all-play (it scores most every week in the fixture).
    assert team(res, 6)["components"]["strength"]["score"] == 100 and team(res, 1)["components"]["strength"]["score"] == 0
    assert team(res, 1)["components"]["all_play"]["score"] == 100


def test_power_score_is_exactly_the_weighted_mean_of_the_visible_component_scores():
    res = build_power_rankings(small(), None)
    w = res["weights"]
    for t in res["teams"]:
        c = t["components"]
        want = sum(w[k] * c[k]["score"] for k in w if c[k]["score"] is not None) / \
            sum(w[k] for k in w if c[k]["score"] is not None)
        assert t["power_score"] == pytest.approx(round(want, 1), abs=0.05)
    ranks = [t["rank"] for t in res["teams"]]
    assert ranks == sorted(ranks)
    scores = [t["power_score"] for t in res["teams"]]
    assert scores == sorted(scores, reverse=True)
    assert "probab" not in str(res["teams"]).lower()


def test_depth_rewards_a_bench_that_covers_a_lost_starter():
    data = small()
    # Team 3's bench RB projects 4.5 instead of 1 (still below its FLEX WR's 5): losing a starter costs less.
    for w in data.state.projections[304]:
        data.state.projections[304][w] = 4.5
    res = build_power_rankings(data, None, kdst_streaming=False)
    deep, thin = team(res, 3), team(res, 2)
    assert deep["components"]["depth"]["value"] > thin["components"]["depth"]["value"]
    assert deep["components"]["depth"]["score"] == 100
    # Replacement: no free agents in the fixture, so a bench RB is pure edge over the (empty) wire.
    rb = next(p for p in deep["replacement"]["positions"] if p["position"] == "RB")
    assert rb["best_bench"] == 4.5 and rb["edge"] == 4.5 and rb["best_free_agent"] is None


def test_replacement_edge_is_measured_against_the_best_free_agent_and_floored_at_zero():
    data = small()
    weeks = range(5, 11)
    data.state.free_agents = [RosterPlayer(9001, "RB", "NYJ", "FA RB")]
    data.state.projections[9001] = {w: 2.0 for w in weeks}
    for w in weeks:
        data.state.projections[304][w] = 4.0       # team 3's bench RB beats the wire by 2
        data.state.projections[204][w] = 1.0       # team 2's bench RB is below the wire: no edge, not negative
    res = build_power_rankings(data, None, kdst_streaming=False)
    rb3 = next(p for p in team(res, 3)["replacement"]["positions"] if p["position"] == "RB")
    rb2 = next(p for p in team(res, 2)["replacement"]["positions"] if p["position"] == "RB")
    assert rb3["edge"] == 2.0 and rb3["best_free_agent"] == 2.0
    assert rb2["edge"] == 0.0 and rb2["best_bench"] == 1.0


def test_team_without_a_roster_is_listed_unranked_never_scored_zero():
    res = build_power_rankings(small(rosters_missing={4}), None)
    t4 = team(res, 4)
    assert t4["rank"] is None and t4["power_score"] is None and t4["unranked_reason"] == "no roster snapshot"
    assert res["teams"][-1]["team_id"] == 4
    assert len([t for t in res["teams"] if t["rank"] is not None]) == 5


def test_focus_team_defaults_to_mine_and_rejects_unknown_teams():
    assert build_power_rankings(small(mine=3), None)["focus_team_id"] == 3
    assert build_power_rankings(small(), None, team_id=5)["focus_team_id"] == 5
    with pytest.raises(NotFound):
        build_power_rankings(small(), None, team_id=99)


def test_season_over_is_an_empty_edition_not_an_error():
    res = build_power_rankings(sr.race(n_teams=4, reg_periods=6, spots=2, start_week=None, final_week=8), None)
    assert res["empty"] and res["week"] is None and res["teams"] == [] and res["message"]


# ------------------------------------------------------------- week over week --

def _previous(data, qb_strength: dict[int, float]) -> Previous:
    """Week 4 edition: same rosters, the QB projections a snapshot before week 4's kickoff would have held."""
    proj = {pid: dict(v) for pid, v in data.state.projections.items()}
    for pid in list(proj):
        proj[pid].update({4: proj[pid][5]})
    for t, s in qb_strength.items():
        proj[t * 100 + 1] = {w: s for w in range(4, 11)}
    st = replace(data.state, projections=proj, start_week=4)
    return Previous(4, st, None, "2026-09-30", "2026-10-01T12:00:00")


def test_week_over_week_movement_compares_with_the_point_in_time_previous_edition():
    data = small()
    # Last week team 1 had the best QB and team 6 the worst: team 6 rose, team 1 fell.
    prev = _previous(data, {1: 40.0, 2: 24.0, 3: 25.0, 4: 26.0, 5: 27.0, 6: 10.0})
    res = build_power_rankings(data, prev)
    assert res["previous"]["available"] and res["previous"]["week"] == 4
    assert res["previous"]["projection_snapshot"] == "2026-09-30"
    # The previous edition's all-play stops before week 4: periods 1-3 only.
    assert res["previous"]["all_play_periods"] == [1, 2, 3]
    t1, t6 = team(res, 1), team(res, 6)
    assert t1["previous_rank"] < t1["rank"] and t1["movement"] == t1["previous_rank"] - t1["rank"] < 0
    assert t6["previous_rank"] > t6["rank"] and t6["movement"] > 0
    assert t6["components"]["strength"]["previous_value"] == 10.0 + 15
    assert sum(t["movement"] for t in res["teams"]) == 0


def test_movement_is_absent_with_a_reason_when_the_previous_edition_cannot_be_rebuilt():
    res = build_power_rankings(small(), Previous(4, None, "no ESPN projection snapshot was stored before week 4's first kickoff"))
    assert not res["previous"]["available"] and "snapshot" in res["previous"]["reason"]
    assert all(t["movement"] is None and t["previous_rank"] is None for t in res["teams"])


def _history_db(kickoff: datetime):
    con = duckdb.connect()
    con.execute("CREATE TABLE lineup_snapshots (league_id BIGINT, season INT, scoring_period INT, team_id INT, "
                "espn_player_id BIGINT, lineup_slot_id INT, injury_status VARCHAR, acquisition_date TIMESTAMP, "
                "synced_at TIMESTAMP)")
    con.execute("CREATE TABLE fantasy_players (espn_player_id BIGINT, full_name VARCHAR, position VARCHAR, "
                "pro_team VARCHAR, injury_status VARCHAR)")
    con.execute("CREATE TABLE espn_projection_history (snapshot_date DATE, season INT, espn_player_id BIGINT, "
                "scoring_period INT, projected_points DOUBLE, synced_at TIMESTAMP)")
    for pid, pos in ((101, "QB"), (102, "RB"), (201, "QB"), (202, "RB"), (301, "RB")):
        con.execute("INSERT INTO fantasy_players VALUES (?, ?, ?, 'KC', NULL)", [pid, f"P{pid}", pos])
    # Week 4 rosters: team 1 had 101+102, team 2 had 201+202+301 (301 on IR). Week 5 moved 301 to team 1.
    # Synced after kickoff, but every acquisition predates it: provably the kickoff rosters.
    k = kickoff.replace(tzinfo=None)
    for t, pid, slot in ((1, 101, 0), (1, 102, 2), (2, 201, 0), (2, 202, 2), (2, 301, 21)):
        con.execute("INSERT INTO lineup_snapshots VALUES (1, 2026, 4, ?, ?, ?, NULL, '2026-08-20', ?)",
                    [t, pid, slot, k.replace(day=k.day + 1)])
    con.execute("INSERT INTO lineup_snapshots VALUES (1, 2026, 5, 1, 301, 2, NULL, '2026-08-20', ?)", [k])
    for date, synced, pts in (("2026-09-29", datetime(2026, 9, 30, 6), 11.0), ("2026-10-01", k.replace(hour=k.hour + 1), 99.0)):
        for pid in (101, 102, 201, 202, 301):
            for w in range(4, 11):
                con.execute("INSERT INTO espn_projection_history VALUES (?, 2026, ?, ?, ?, ?)", [date, pid, w, pts, synced])
    return con


def test_load_previous_uses_week_rosters_and_the_last_snapshot_before_kickoff(monkeypatch):
    kickoff = datetime(2026, 10, 1, 17, tzinfo=timezone.utc)
    monkeypatch.setattr(pr.pf, "first_kickoffs", lambda con, season: {4: kickoff})
    con = _history_db(kickoff)
    data = small()
    data.state.free_agents = [RosterPlayer(301, "RB", "KC", "P301")]
    prev = load_previous(con, data)
    assert prev.reason is None and prev.week == 4 and prev.projection_snapshot == "2026-09-29"
    st = prev.state
    assert st.start_week == 4 and st.window().weeks[0] == 4
    assert sorted(p.espn_player_id for p in st.rosters[2]) == [201, 202, 301]
    assert next(p for p in st.rosters[2] if p.espn_player_id == 301).on_ir
    # The snapshot synced after kickoff (99 points) is never used.
    assert st.projections[101][4] == 11.0 and all(v == 11.0 for per in st.projections.values() for v in per.values())
    # 301 was rostered at week 4, so it is not a week-4 free agent.
    assert 301 not in {p.espn_player_id for p in st.free_agents}


def test_load_previous_explains_a_missing_snapshot_or_roster(monkeypatch):
    kickoff = datetime(2026, 9, 1, tzinfo=timezone.utc)      # every stored snapshot is after this kickoff
    monkeypatch.setattr(pr.pf, "first_kickoffs", lambda con, season: {4: kickoff})
    prev = load_previous(_history_db(datetime(2026, 10, 1, 17, tzinfo=timezone.utc)), small())
    assert prev.state is None and "before week 4's first kickoff" in prev.reason and "history starts" in prev.reason
    con = _history_db(datetime(2026, 10, 1, 17, tzinfo=timezone.utc))
    con.execute("DELETE FROM lineup_snapshots WHERE scoring_period = 4")
    prev = load_previous(con, small())
    assert prev.state is None and prev.reason == "no roster snapshot is stored for week 4"


def test_a_roster_changed_after_kickoff_is_never_used_as_the_kickoff_roster(monkeypatch):
    kickoff = datetime(2026, 10, 1, 17, tzinfo=timezone.utc)
    monkeypatch.setattr(pr.pf, "first_kickoffs", lambda con, season: {4: kickoff})
    con = _history_db(kickoff)
    # Team 2 picked 202 up on waivers after week 4 kicked off; the sync rewrote week 4 with that roster.
    con.execute("UPDATE lineup_snapshots SET acquisition_date = '2026-10-02 08:00' WHERE scoring_period = 4 "
                "AND espn_player_id = 202")
    prev = load_previous(con, small())
    assert prev.state is None and "1 team changed its roster after week 4's first kickoff" in prev.reason
    res = build_power_rankings(small(), prev)
    assert not res["previous"]["available"] and all(t["movement"] is None for t in res["teams"])
    # Without acquisition dates a snapshot synced after kickoff cannot be verified either.
    con.execute("UPDATE lineup_snapshots SET acquisition_date = NULL")
    assert "carry no acquisition dates" in load_previous(con, small()).reason
    # A snapshot synced before kickoff is the kickoff roster whatever its acquisition dates say.
    con.execute("UPDATE lineup_snapshots SET synced_at = '2026-10-01 12:00' WHERE scoring_period = 4")
    assert load_previous(con, small()).reason is None


# --------------------------------------------------------------------- API --

def test_api_route_returns_the_contract(monkeypatch):
    from app.api import power_rankings as api
    from app.main import app
    data = small()
    monkeypatch.setattr(api, "load_power_inputs", lambda con: (data, None, {}))

    class _Ctx:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(api, "db", lambda: _Ctx())
    client = TestClient(app)
    res = client.get("/api/power-rankings")
    assert res.status_code == 200
    body = res.json()
    assert body["contract"] == "power-rankings/v1" and len(body["teams"]) == 6
    assert [c["key"] for c in body["components"]] == ["strength", "depth", "replacement", "all_play"]
    assert client.get("/api/power-rankings?team_id=99").status_code == 404
