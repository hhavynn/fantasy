"""Weekly projection movers (analytics/projection_movers.py, GET /api/projections/movers).

Temp DuckDB only. ESPN snapshots are written to espn_projection_history the way the sync does; ours are stored runs
inserted with the store's schema (one run per target week, sealed with the content hash), as in test_forward_ours.py.
The synthetic season has 2026 week-N kickoffs on Thursdays from Sep 10, 13:00 Eastern (17:00 UTC in October)."""

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.analytics import production_forecasts as pf
from app.analytics import production_pin
from app.analytics import projection_movers as pm
from app.analytics import projection_source as ps
from app.db.connection import connect, fetch_dicts, init_db

SEASON, FINAL, LAST_REG = 2026, 17, 14
PLAYERS = {1: ("Ace Receiver", "WR", "KC"), 2: ("Bo Receiver", "WR", "KC"), 3: ("Cy Back", "RB", "BUF"),
           4: ("Dee Passer", "QB", "DEN"), 5: ("Ed End", "TE", "KC"), 6: ("Fay Kicker", "K", "KC"),
           7: ("Gus Bench", "WR", "LV")}
BASE = {1: 10.0, 2: 12.0, 3: 15.0, 4: 18.0, 5: 8.0, 6: 8.0, 7: 1.0}
SHIFT = {1: 3.0, 2: -1.5, 3: 0.0, 4: 0.001, 5: 1.0, 6: 0.0, 7: 1.0}      # ESPN's change per week, latest minus earlier
BYES = {("DEN", 9), ("LV", 9), ("KC", 11), ("LV", 11)}                  # teams without a game that week (see seed)
BEFORE = (date(2026, 10, 14), datetime(2026, 10, 14, 12, 0))            # before week 6 (kickoff Oct 15 17:00 UTC)
AFTER = (date(2026, 10, 21), datetime(2026, 10, 21, 12, 0))             # after week 6, before week 7 (Oct 22)
C5, C6, C7 = datetime(2026, 10, 7, 12, 0), datetime(2026, 10, 14, 12, 0), datetime(2026, 10, 21, 12, 0)
APPROVED = "sha-approved"
LATE = "kicked off before the latest snapshot"


def base(pid):
    return BASE.get(pid, 5.0)


@pytest.fixture(autouse=True)
def fingerprint(monkeypatch):
    monkeypatch.setattr(ps, "_scoring", lambda con, season: ("fp", None))


# ----------------------------------------------------------------------------- seeding


def seed(con, players=PLAYERS):
    """League (window to week 17), players, a schedule with kickoffs (KC bye in 11, DEN bye in 9, LV bye in 9 and 11)
    and the registered ifp-2.0.0 artifact (covers every week after its data cutoff)."""
    con.execute("""INSERT INTO leagues (league_id, season, name, current_week, final_scoring_period, reg_season_matchups,
                   scoring_items, settings_raw, synced_at) VALUES (1, ?, 'L', 6, ?, ?, '[]', '{}', '2026-10-14 00:00:00')""",
                [SEASON, FINAL, LAST_REG])
    for pid, (name, pos, team) in players.items():
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team) VALUES (?, ?, ?, ?)",
                    [pid, name, pos, team])
    con.execute("""INSERT INTO player_id_map (espn_player_id, gsis_id, match_confidence) VALUES
                   (1, '00-0000001', 'high'), (2, '00-0000002', 'none')""")
    con.execute("""CREATE TABLE IF NOT EXISTS nfl_schedules (season INT, week INT, game_type VARCHAR, game_id VARCHAR,
                   gameday VARCHAR, gametime VARCHAR, home_team VARCHAR, away_team VARCHAR)""")
    for w in range(1, 19):
        day = (date(2026, 9, 10) + timedelta(days=7 * (w - 1))).isoformat()
        games = [("BUF", "DEN", "16:25")] if w == 11 else [("KC", "BUF", "13:00")] + ([] if w == 9 else [("DEN", "LV", "16:25")])
        for i, (home, away, clock) in enumerate(games):
            con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, ?, ?, ?, ?)",
                        [SEASON, w, f"g{w}-{i}", day, clock, home, away])
    con.execute("INSERT INTO approved_artifacts VALUES (?, ?, ?, 6, ?, NULL, 'learning_cycle', '2026-10-14 00:00:00')",
                [ps.MODEL_VERSION, APPROVED, SEASON, SEASON * 100 + 5])
    for col in ("forecast_horizon", "projected_from_week"):
        con.execute(f"ALTER TABLE independent_projection_runs ADD COLUMN IF NOT EXISTS {col} INTEGER")


def put_espn(con, snap, value, *, weeks=range(6, 18), players=PLAYERS, skip=()):
    """One ESPN snapshot: a row per (player, week) from `value(pid, week)`, none for a bye or a skipped (pid, week)."""
    day, synced = snap
    rows = [(day, SEASON, pid, w, value(pid, w), synced) for pid, (_, _, team) in players.items() for w in weeks
            if (team, w) not in BYES and (pid, w) not in skip and value(pid, w) is not None]
    con.executemany("INSERT INTO espn_projection_history VALUES (?, ?, ?, ?, ?, ?)", rows)


def espn(con, *, shift=SHIFT, players=PLAYERS, before=BEFORE, after=AFTER, skip_before=(), skip_after=()):
    put_espn(con, before, lambda p, w: base(p), players=players, skip=skip_before)
    put_espn(con, after, lambda p, w: base(p) + shift.get(p, 0.0), players=players, skip=skip_after)


def add_run(con, cutoff, week, created, *, tweak=None, players=PLAYERS, version=ps.MODEL_VERSION, fp="fp",
            artifact=None, sealed=True, tag="a"):
    """One stored run for target `week` of cycle `cutoff`: a row per player (projected p=0.9 x base, a bye row on the
    team's bye, K unsupported). `tweak(pid, week)` overrides {status, p, x} or returns {"skip": True}."""
    if artifact is None:
        artifact = APPROVED if version == ps.MODEL_VERSION else production_pin.APPROVED_ARTIFACT_SHA256
    run_id = f"run-{version}-c{cutoff}-t{week}-{created:%d%H%M}-{tag}"
    rows = []
    for pid, (_, pos, team) in players.items():
        spec = ({"status": "unsupported_position", "p": None, "x": None} if pos == "K" else
                {"status": "bye", "p": None, "x": None} if (team, week) in BYES else
                {"status": "projected", "p": 0.9, "x": base(pid)})
        spec.update((tweak(pid, week) if tweak else None) or {})
        if spec.get("skip"):
            continue
        exp = round(spec["p"] * spec["x"], 2) if spec["status"] == "projected" else None
        rows.append((run_id, SEASON, week, pid, pos, team, spec["status"], spec["x"], spec["p"], exp))
    con.execute("""INSERT INTO independent_projection_runs (run_id, season, week, model_version, scoring_fingerprint,
                   model_status, created_at, row_count, artifact_id, forecast_horizon, projected_from_week)
                   VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?)""",
                [run_id, SEASON, week, version, fp, created, len(rows), artifact, week - cutoff, cutoff])
    con.executemany("""INSERT INTO independent_projection_rows (run_id, season, week, espn_player_id, position, nfl_team,
                       coverage_status, projected_points, p_play, expected_points) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
    if sealed:
        seal(con, run_id)
    return run_id


def seal(con, run_id):
    run = fetch_dicts(con, "SELECT * FROM independent_projection_runs WHERE run_id = ?", [run_id])[0]
    rows = fetch_dicts(con, "SELECT * FROM independent_projection_rows WHERE run_id = ?", [run_id])
    con.execute("UPDATE independent_projection_runs SET content_sha256 = ? WHERE run_id = ?",
                [pf.content_sha256(run, rows), run_id])


def cycle(con, cutoff, created, *, weeks=None, **kw):
    """The rest-of-season cycle with cutoff `cutoff`: one run per target week to the end of the regular season."""
    return {w: add_run(con, cutoff, w, created, **kw) for w in (weeks or range(cutoff, LAST_REG + 1))}


def ours(con, **kw):
    return pm.movement(con, SEASON, source="independent", **kw)


def two_cycles(con, *, tweak7=None, tweak6=None, **kw):
    seed(con)
    base_runs = cycle(con, 6, C6, tweak=tweak6, **kw)
    cur_runs = cycle(con, 7, C7, tweak=tweak7, **kw)
    return base_runs, cur_runs


def by_id(result, key="players"):
    return {m["espn_player_id"]: m for m in result[key]}


def ids(result, key):
    return [m["espn_player_id"] for m in result[key]]


# ----------------------------------------------------------------------------- ESPN: the comparison


def test_genuine_forecast_change_orders_risers_and_fallers_with_rank_movement(con):
    seed(con)
    espn(con)
    r = pm.movement(con, SEASON)
    assert r["contract"] == "projection-movers/v1" and r["available"] and r["reason"] is None
    assert r["source"] == "espn" and r["view"] == "ros" and r["position"] is None and r["label"] == "ESPN projections"
    assert r["window"]["weeks"] == list(range(7, 18))
    assert ids(r, "risers") == [1, 5] and ids(r, "fallers") == [2]       # Gus is not relevant (about 1.6 a week)
    assert ids(r, "players") == [1, 2, 5, 7, 4, 3]                       # by |delta|; the kicker is not a skill position
    ace, bo, gus = by_id(r)[1], by_id(r)[2], by_id(r)[7]
    assert (ace["before"], ace["after"], ace["delta"], ace["pct"]) == (100.0, 130.0, 30.0, 0.3)
    assert (ace["per_week_before"], ace["per_week_after"]) == (9.09, 11.82)
    assert (bo["before"], bo["after"], bo["delta"], bo["pct"]) == (120.0, 105.0, -15.0, -0.125)
    assert (ace["rank_before"], ace["rank_after"], ace["rank_change"]) == (2, 1, 1)       # Ace passes Bo within WR
    assert (bo["rank_before"], bo["rank_after"], bo["rank_change"]) == (1, 2, -1)
    assert (gus["delta"], gus["direction"], gus["relevant"]) == (9.0, "up", False)
    assert (ace["direction"], bo["direction"]) == ("up", "down")
    assert ace["name"] == "Ace Receiver" and ace["nfl_team"] == "KC" and ace["gsis_id"] == "00-0000001"
    assert bo["gsis_id"] is None                                          # an uncertain id mapping is not shown
    assert ace["components"] is None and all(m["evidence"] is None for m in r["players"])
    first = ace["weeks"][0]
    assert first == {"week": 7, "before": 10.0, "after": 13.0, "delta": 3.0, "bye": False, "bye_before": False, "bye_after": False,
                     "status_before": "projected",
                     "status_after": "projected", "horizon_before": None, "horizon_after": None}
    assert r["summary"] == {"compared": 6, "risers": 3, "fallers": 1, "unchanged": 2, "not_comparable": 0,
                            "not_comparable_reasons": {}}
    assert r["baseline"]["id"] == "2026-10-14" and r["current"]["id"] == "2026-10-21"
    assert (r["baseline"]["cycle_week"], r["current"]["cycle_week"]) == (6, 7)
    assert r["baseline"]["taken_at"] == "2026-10-14T12:00:00+00:00" and r["baseline"]["run_ids"] is None
    assert r["baselines"] == [{k: r["baseline"][k] for k in ("id", "label", "taken_at", "cycle_week")}]
    assert r["baseline"]["label"] == "Oct 14 (before week 6)" and r["governance"] is None and r["experimental"] is False
    assert r["window"]["scoring_verified"] is False and "scoring" in r["window"]["scoring_note"]
    assert r["window"]["excluded_weeks"] == [{"week": 6, "reason": LATE}]
    assert r["thresholds"] == {"min_delta": 0.5, "relevant_per_week": 3.0, "pct_floor": 1.0, "flat": 0.05}
    assert r["skipped"] == [] and r["excluded_runs"] == []


def test_identical_consecutive_forecasts_are_all_flat(con):
    seed(con)
    espn(con, shift={})
    r = pm.movement(con, SEASON)
    assert r["available"] and r["risers"] == [] and r["fallers"] == []
    assert r["summary"]["compared"] == 6 and r["summary"]["unchanged"] == 6
    assert r["summary"]["risers"] == r["summary"]["fallers"] == 0
    assert {m["direction"] for m in r["players"]} == {"flat"} and {m["delta"] for m in r["players"]} == {0.0}
    assert {m["rank_change"] for m in r["players"]} == {0}


def test_different_remaining_season_week_sets_use_the_intersection(con):
    seed(con)
    put_espn(con, BEFORE, lambda p, w: base(p), weeks=range(6, 16))             # no weeks 16, 17
    put_espn(con, AFTER, lambda p, w: base(p) + SHIFT.get(p, 0.0), weeks=range(8, 18))     # no weeks 6, 7
    r = pm.movement(con, SEASON)
    assert r["window"]["weeks"] == list(range(8, 16))
    assert r["window"]["excluded_weeks"] == [
        {"week": 6, "reason": LATE}, {"week": 7, "reason": "not in the latest snapshot"},
        {"week": 16, "reason": "not in the earlier snapshot"}, {"week": 17, "reason": "not in the earlier snapshot"}]
    assert by_id(r)[3]["before"] == 120.0           # eight weeks of 15, nothing from an excluded week


def test_a_week_that_kicked_off_before_the_latest_snapshot_is_excluded_and_unread(con):
    seed(con)
    late = (date(2026, 10, 23), datetime(2026, 10, 23, 12, 0))      # week 7 (Oct 22) has kicked off
    put_espn(con, BEFORE, lambda p, w: base(p))
    put_espn(con, late, lambda p, w: base(p) + (50.0 if w == 7 else SHIFT.get(p, 0.0)))     # week 7: post-kickoff noise
    r = pm.movement(con, SEASON)
    assert r["current"]["cycle_week"] == 8 and r["baseline"]["cycle_week"] == 6
    assert r["window"]["weeks"] == list(range(8, 18))
    assert r["window"]["excluded_weeks"] == [{"week": 6, "reason": LATE}, {"week": 7, "reason": LATE}]
    assert by_id(r)[3]["before"] == by_id(r)[3]["after"] == 150.0          # ten weeks of 15


def test_a_missing_prior_snapshot_is_never_reconstructed(con):
    seed(con)
    put_espn(con, AFTER, lambda p, w: base(p))
    r = pm.movement(con, SEASON)
    assert r["available"] is False and "Only one" in r["reason"] and "never reconstructed" in r["reason"]
    assert (r["risers"], r["fallers"], r["players"], r["baselines"]) == ([], [], [], [])
    assert r["baseline"] is None and r["current"]["id"] == "2026-10-21"
    assert r["summary"]["compared"] == 0 and r["window"]["weeks"] == []


def test_no_snapshot_at_all_is_unavailable_with_a_reason(con):
    seed(con)
    r = pm.movement(con, SEASON)
    assert r["available"] is False and "No ESPN projection snapshot" in r["reason"] and r["current"] is None


def test_default_baseline_is_last_weeks_outlook_not_an_earlier_snapshot_of_this_week(con):
    seed(con)
    mid = (date(2026, 10, 20), datetime(2026, 10, 20, 12, 0))
    put_espn(con, BEFORE, lambda p, w: base(p))
    put_espn(con, mid, lambda p, w: base(p) + 1.0)
    put_espn(con, AFTER, lambda p, w: base(p) + 2.0)
    r = pm.movement(con, SEASON)
    assert r["baseline"]["id"] == "2026-10-14" and [b["id"] for b in r["baselines"]] == ["2026-10-20", "2026-10-14"]
    assert by_id(r)[3]["delta"] == 22.0                               # 11 weeks x 2.0
    explicit = pm.movement(con, SEASON, baseline="2026-10-20")
    assert explicit["baseline"]["id"] == "2026-10-20" and by_id(explicit)[3]["delta"] == 11.0


def test_with_only_snapshots_of_one_week_the_baseline_is_labeled_earlier_this_week(con):
    seed(con)
    mid = (date(2026, 10, 20), datetime(2026, 10, 20, 12, 0))
    put_espn(con, mid, lambda p, w: base(p))
    put_espn(con, AFTER, lambda p, w: base(p) + 1.0)
    r = pm.movement(con, SEASON)
    assert r["baseline"]["id"] == "2026-10-20" and any("earlier snapshot of the same week" in n for n in r["notes"])


def test_an_explicit_baseline_must_be_older_than_the_latest_snapshot(con):
    seed(con)
    espn(con)
    for bad in ("2026-10-21", "2026-10-22", "2026-10-13", "soon", "w6"):
        with pytest.raises(ValueError):
            pm.movement(con, SEASON, baseline=bad)
    assert pm.movement(con, SEASON, baseline="2026-10-14")["baseline"]["id"] == "2026-10-14"


@pytest.mark.parametrize("bad", [{"source": "vegas"}, {"view": "month"}, {"position": "K"}, {"baseline": "w7"}])
def test_invalid_arguments_raise(con, bad):
    seed(con)
    espn(con)
    with pytest.raises(ValueError):
        pm.movement(con, SEASON, **bad)


def test_espn_bye_weeks_count_zero_and_a_missing_non_bye_week_is_not_comparable(con):
    seed(con)
    espn(con, skip_before={(2, 9)}, skip_after={(5, 12)})     # both play that week: a real gap, not a bye
    r = pm.movement(con, SEASON)
    ace_w11 = by_id(r)[1]["weeks"][4]
    assert ace_w11 == {"week": 11, "before": 0.0, "after": 0.0, "delta": 0.0, "bye": True, "bye_before": True, "bye_after": True,
                       "status_before": "bye",
                       "status_after": "bye", "horizon_before": None, "horizon_after": None}
    assert by_id(r)[1]["before"] == 100.0                      # the bye week adds nothing and does not block him
    assert 2 not in by_id(r) and 5 not in by_id(r)
    assert r["summary"]["not_comparable"] == 2 and r["summary"]["compared"] == 4
    assert r["summary"]["not_comparable_reasons"] == {"no ESPN projection for week 9 in the earlier snapshot": 1,
                                                       "no ESPN projection for week 12 in the latest snapshot": 1}


def test_position_filter_keeps_ranks_within_position(con):
    seed(con)
    espn(con)
    everyone = pm.movement(con, SEASON)
    wr = pm.movement(con, SEASON, position="wr")
    assert wr["position"] == "WR" and set(by_id(wr)) == {1, 2, 7}
    assert wr["summary"]["compared"] == 3 and ids(wr, "risers") == [1] and ids(wr, "fallers") == [2]
    assert (by_id(wr)[1]["rank_before"], by_id(wr)[1]["rank_after"]) == (by_id(everyone)[1]["rank_before"],
                                                                         by_id(everyone)[1]["rank_after"])
    assert ids(pm.movement(con, SEASON, position="QB"), "players") == [4]


def test_espn_next_week_view_compares_only_the_first_window_week(con):
    seed(con)
    espn(con)
    r = pm.movement(con, SEASON, view="week")
    assert r["view"] == "week" and r["window"]["weeks"] == [7] and r["experimental"] is False
    ace = by_id(r)[1]
    assert (ace["before"], ace["after"], ace["delta"], len(ace["weeks"])) == (10.0, 13.0, 3.0, 1)
    assert by_id(r)[4]["delta"] == 0.0 and by_id(r)[4]["direction"] == "flat"


def test_limit_caps_the_lists_and_players_are_capped_at_300(con):
    players = {**PLAYERS, **{100 + i: (f"Extra {i:03d}", "WR", "BUF") for i in range(320)}}
    seed(con, players)
    espn(con, players=players, shift={100 + i: 1.0 + i / 100 for i in range(320)})
    r = pm.movement(con, SEASON, limit=3)
    assert len(r["risers"]) == 3 and len(r["players"]) == 300 and r["summary"]["compared"] == 326
    assert [m["delta"] for m in r["risers"]] == sorted((m["delta"] for m in r["risers"]), reverse=True)
    assert len(pm.movement(con, SEASON, limit=500)["risers"]) <= pm.MAX_LIMIT


def test_snapshots_lists_newest_first(con):
    seed(con)
    espn(con)
    assert [s["id"] for s in pm.snapshots(con, SEASON, "espn")] == ["2026-10-21", "2026-10-14"]
    assert pm.snapshots(con, SEASON, "independent") == []
    with pytest.raises(ValueError):
        pm.snapshots(con, SEASON, "vegas")


# ----------------------------------------------------------------------------- ours


def test_ours_changes_split_into_availability_and_per_game(con):
    def tweak7(pid, week):      # Ace: +2 points when he plays; Bo: 0.9 -> 0.5 chance to appear
        return {"x": base(1) + 2.0} if pid == 1 else {"p": 0.5} if pid == 2 else None
    base_runs, cur_runs = two_cycles(con, tweak7=tweak7)
    r = ours(con)
    assert r["available"] and r["source"] == "independent" and r["label"] == f"Fantasy Intel projections ({ps.MODEL_VERSION})"
    assert r["baseline"]["id"] == "w6" and r["current"]["id"] == "w7"
    assert (r["baseline"]["model_version"], r["current"]["model_version"]) == (ps.MODEL_VERSION, ps.MODEL_VERSION)
    assert r["baseline"]["run_ids"] == {str(w): base_runs[w] for w in range(6, 15)}
    assert r["current"]["run_ids"] == {str(w): cur_runs[w] for w in range(7, 15)}
    assert r["window"]["weeks"] == list(range(7, 15)) and r["window"]["excluded_weeks"] == [{"week": 6, "reason": LATE}]
    assert r["window"]["scoring_verified"] is True and r["skipped"] == [] and r["excluded_runs"] == []
    ace, bo = by_id(r)[1], by_id(r)[2]
    assert (ace["before"], ace["after"], ace["delta"]) == (63.0, 75.6, 12.6)         # seven playing weeks
    assert ace["components"] == {"availability": 0.0, "per_game": 12.6}
    assert (bo["before"], bo["after"], bo["delta"]) == (75.6, 42.0, -33.6)
    assert bo["components"] == {"availability": -33.6, "per_game": 0.0}
    assert ids(r, "risers") == [1] and ids(r, "fallers") == [2]
    # nothing changed for the rest: the components of an unchanged player are zero
    assert by_id(r)[3]["components"] == {"availability": 0.0, "per_game": 0.0} and by_id(r)[3]["direction"] == "flat"


def test_ours_carries_governance_and_plain_notes(con):
    two_cycles(con)
    r = ours(con)
    assert r["experimental"] is True
    g = r["governance"]
    assert g["espn_superiority_established"] is False and g["start_sit_ready"] is False
    assert g["served_version"] == ps.MODEL_VERSION and g["production_version"] == ps.MODEL_VERSION
    assert any("made further ahead than the latest" in n for n in r["notes"])
    assert not any(w in n.lower() for n in r["notes"] for w in ("experimental", "owner override", "better than espn"))
    first, last = by_id(r)[1]["weeks"][0], by_id(r)[1]["weeks"][-1]
    assert (first["horizon_before"], first["horizon_after"]) == (1, 0)       # week 7: forecast from week 6, then from week 7
    assert (last["horizon_before"], last["horizon_after"]) == (8, 7)


def test_ours_next_week_view_compares_horizon_one_with_horizon_zero(con):
    two_cycles(con, tweak7=lambda pid, week: {"x": base(1) + 2.0} if pid == 1 else None)
    r = ours(con, view="week")
    assert r["window"]["weeks"] == [7] and r["experimental"] is True
    ace = by_id(r)[1]
    assert (ace["before"], ace["after"], ace["delta"]) == (9.0, 10.8, 1.8) and len(ace["weeks"]) == 1
    assert (ace["weeks"][0]["horizon_before"], ace["weeks"][0]["horizon_after"]) == (1, 0)
    assert ace["components"] == {"availability": 0.0, "per_game": 1.8}


def test_injury_related_change_shows_up_as_availability(con):
    def tweak6(pid, week):      # Ace was withheld for week 8 in the earlier forecast
        return {"status": "withheld_unavailable", "p": None, "x": None} if (pid, week) == (1, 8) else None
    two_cycles(con, tweak6=tweak6, tweak7=lambda pid, week: {"x": base(1) + 1.0} if pid == 1 else None)
    r = ours(con)
    ace = by_id(r)[1]
    week8 = next(w for w in ace["weeks"] if w["week"] == 8)
    assert (week8["status_before"], week8["status_after"], week8["before"], week8["after"]) == (
        "withheld_unavailable", "projected", 0.0, 9.9)
    assert ace["delta"] == pytest.approx(15.3) and ace["direction"] == "up" and ace["relevant"]
    assert ace["components"]["availability"] == pytest.approx(9.9)                   # p1 * x for the withheld week
    assert ace["components"]["per_game"] == pytest.approx(5.4)                       # +1 point in the six other weeks
    assert ace["components"]["availability"] + ace["components"]["per_game"] == pytest.approx(ace["delta"], abs=0.05)
    assert 1 in ids(r, "risers")


def test_ours_byes_count_zero_and_a_week_without_an_estimate_is_not_comparable(con):
    def tweak6(pid, week):
        return {"skip": True} if (pid, week) == (2, 12) else None                    # no row at all for Bo in week 12

    def tweak7(pid, week):
        return {"status": "abstain_no_history", "p": None, "x": None} if (pid, week) == (5, 8) else None
    two_cycles(con, tweak6=tweak6, tweak7=tweak7)
    r = ours(con)
    w11 = by_id(r)[1]["weeks"][4]
    assert (w11["week"], w11["before"], w11["after"], w11["bye"], w11["status_before"]) == (11, 0.0, 0.0, True, "bye")
    assert 1 in by_id(r) and 2 not in by_id(r) and 5 not in by_id(r)
    assert r["summary"]["not_comparable_reasons"] == {
        "no independent estimate for week 12 in the earlier run": 1,
        "no independent estimate for week 8 (no NFL history) in the latest run": 1}


def test_ours_runs_under_another_scoring_are_never_compared_and_are_listed(con):
    seed(con)
    other = cycle(con, 6, C6, fp="other-fingerprint")
    cycle(con, 7, C7)
    r = ours(con)
    assert r["available"] is False and "different league scoring" in r["reason"]
    assert {x["run_id"] for x in r["excluded_runs"]} == set(other.values())
    assert {x["reason"] for x in r["excluded_runs"]} == {"made under different league scoring"}
    assert r["baselines"] == [] and r["players"] == [] and r["current"]["id"] == "w7"


def test_a_corrupted_run_is_skipped_and_the_next_older_valid_run_of_the_cutoff_is_used(con):
    seed(con)
    cycle(con, 6, C6)
    older = add_run(con, 7, 9, datetime(2026, 10, 20, 12, 0), tag="old")
    newest = cycle(con, 7, C7)[9]
    con.execute("UPDATE independent_projection_rows SET expected_points = expected_points + 5 "
                "WHERE run_id = ? AND espn_player_id = 1", [newest])          # edited after sealing
    r = ours(con)
    assert r["available"] and 9 in r["window"]["weeks"]
    assert r["current"]["run_ids"]["9"] == older
    assert len(r["skipped"]) == 1 and r["skipped"][0]["run_id"] == newest and r["skipped"][0]["week"] == 9
    assert r["skipped"][0]["reason"].startswith("integrity")


def test_a_corrupted_run_without_an_older_one_drops_its_week(con):
    seed(con)
    cycle(con, 6, C6)
    newest = cycle(con, 7, C7)[9]
    con.execute("UPDATE independent_projection_rows SET expected_points = expected_points + 5 "
                "WHERE run_id = ? AND espn_player_id = 1", [newest])
    r = ours(con)
    assert r["available"] and 9 not in r["window"]["weeks"] and "9" not in r["current"]["run_ids"]
    assert {"week": 9, "reason": "not in the latest snapshot"} in r["window"]["excluded_weeks"]
    assert [x["run_id"] for x in r["skipped"]] == [newest]
    assert by_id(r)[1]["before"] == 63.0 - 9.0                                    # one fewer playing week (9 is a KC game)


def test_a_run_of_an_unapproved_artifact_is_rejected_and_never_used(con):
    seed(con)
    cycle(con, 6, C6)
    cycle(con, 7, C7, weeks=[7, 9, 10, 11, 12, 13, 14])
    bad = add_run(con, 7, 8, C7, artifact="sha-not-registered", tag="bad")
    r = ours(con)
    assert r["available"] and 8 not in r["window"]["weeks"]
    assert {"week": 8, "reason": "not in the latest snapshot"} in r["window"]["excluded_weeks"]
    assert [(x["run_id"], x["week"]) for x in r["skipped"]] == [(bad, 8)]
    assert r["skipped"][0]["reason"].startswith(pf.NOT_REGISTERED)
    assert bad not in r["current"]["run_ids"].values()


def test_a_run_made_after_its_weeks_first_kickoff_is_not_eligible(con):
    seed(con)
    cycle(con, 6, C6)
    cycle(con, 7, C7)
    late = add_run(con, 7, 7, datetime(2026, 10, 22, 18, 0), tag="late")             # an hour after week 7 kicked off
    r = ours(con)
    assert r["available"] and r["current"]["run_ids"]["7"] != late
    assert [(x["run_id"], x["reason"]) for x in r["skipped"]] == [(late, "made after week 7's first kickoff")]


def test_a_week_without_a_known_kickoff_is_not_eligible_for_ours(con):
    seed(con)
    cycle(con, 6, C6)
    cycle(con, 7, C7)
    con.execute("UPDATE nfl_schedules SET gametime = NULL WHERE week = 10")
    r = ours(con)
    assert 10 not in r["window"]["weeks"]
    assert {"week": 10, "reason": "kickoff time unknown"} in r["window"]["excluded_weeks"]
    assert all("kickoff time of week 10 unknown" in x["reason"] for x in r["skipped"]) and len(r["skipped"]) == 2


def test_previous_model_runs_are_never_a_snapshot_and_the_reason_says_so(con):
    seed(con)
    cycle(con, 5, C5, version=ps.FALLBACK_VERSION)
    cycle(con, 6, C6)
    r = ours(con)
    assert r["available"] is False and ps.FALLBACK_VERSION in r["reason"] and "never shown" in r["reason"]
    assert [s["id"] for s in pm.snapshots(con, SEASON, "independent")] == ["w6"]        # w5 is not a snapshot
    assert r["governance"]["espn_superiority_established"] is False                      # also when unavailable
    explicit = ours(con, baseline="w5")
    assert explicit["available"] is False and ps.FALLBACK_VERSION in explicit["reason"]
    with pytest.raises(ValueError):
        ours(con, baseline="w4")
    with pytest.raises(ValueError):
        ours(con, baseline="2026-10-14")


def test_an_older_ifp2_snapshot_is_the_baseline_and_a_skipped_previous_model_cutoff_is_noted(con):
    seed(con)
    cycle(con, 4, datetime(2026, 10, 1, 12, 0))
    cycle(con, 5, C5, version=ps.FALLBACK_VERSION)
    cycle(con, 7, C7)
    r = ours(con)
    assert r["available"] and r["baseline"]["id"] == "w4"
    assert any(ps.FALLBACK_VERSION in n and "week 5" in n for n in r["notes"])


def test_an_explicit_ours_baseline_must_be_older(con):
    two_cycles(con)
    for bad in ("w7", "w8", "w3", "nope"):
        with pytest.raises(ValueError):
            ours(con, baseline=bad)
    assert ours(con, baseline="w6")["baseline"]["id"] == "w6"


def test_the_real_database_shape_with_one_late_run_is_unavailable_with_a_reason(con):
    """One run of week 6 made after week 6 kicked off (as in the local database): nothing to compare."""
    seed(con)
    add_run(con, 6, 6, datetime(2026, 10, 16, 4, 0))
    r = ours(con)
    assert r["available"] is False and "made after its week's first kickoff" in r["reason"]
    assert len(r["skipped"]) == 1 and r["players"] == []


def test_right_after_the_model_switch_movers_explain_why_nothing_is_compared(con):
    """The real database after the switch to ifp-2.1.0 (2026-10-18): an ifp-2.0.0 week-6 run, then the ifp-2.1.0
    cycle with cutoff 6 made during week 6 (its week-6 run after kickoff, weeks 7.. before theirs). One ifp-2.1.0
    snapshot exists, so Ours movers are unavailable with an explicit reason; nothing crosses model versions."""
    seed(con)
    add_run(con, 6, 6, datetime(2026, 10, 16, 4, 0), version=ps.FALLBACK_VERSION)
    cycle(con, 6, datetime(2026, 10, 18, 21, 0))
    r = ours(con)
    assert r["available"] is False and r["players"] == []
    assert f"two stored {ps.MODEL_VERSION} forecasts" in r["reason"] and "1 such forecast is stored" in r["reason"]
    assert "made after its week's first kickoff" in r["reason"]           # the ifp-2.1.0 week-6 run
    assert ps.FALLBACK_VERSION in r["reason"] and "never shown" in r["reason"]
    assert r["current"]["model_version"] == ps.MODEL_VERSION and r["baseline"] is None
    assert [s["id"] for s in pm.snapshots(con, SEASON, "independent")] == ["w6"]


# ----------------------------------------------------------------------------- reads only


def snapshot_of(con):
    return json.dumps([pm.movement(con, SEASON), ours(con)], sort_keys=True, default=str)


def test_outcomes_of_the_window_weeks_are_never_read(con):
    two_cycles(con)
    espn(con)
    con.executemany("INSERT INTO espn_player_weekly_points VALUES (?, ?, ?, NULL, 5.0, '2026-10-14 00:00:00')",
                    [(SEASON, pid, w) for pid in PLAYERS for w in range(6, 18)])
    before = snapshot_of(con)
    con.execute("UPDATE espn_player_weekly_points SET actual_points = 77.0, projected_points = 99.0")
    assert snapshot_of(con) == before
    con.execute("UPDATE espn_player_weekly_points SET actual_points = NULL, projected_points = NULL")
    assert snapshot_of(con) == before


class Counting:
    """Counts the statements a function sends (nothing else about the connection changes)."""

    def __init__(self, con):
        self.con, self.n = con, 0

    def execute(self, *a, **k):
        self.n += 1
        return self.con.execute(*a, **k)

    def __getattr__(self, name):
        return getattr(self.con, name)


@pytest.mark.parametrize("source", ["espn", "independent"])
def test_the_number_of_queries_does_not_grow_with_the_number_of_players(tmp_path, source):
    def queries(extra):
        c = connect(tmp_path / f"{source}-{extra}.duckdb")
        init_db(c)
        players = {**PLAYERS, **{100 + i: (f"Extra {i:03d}", "WR", "BUF") for i in range(extra)}}
        seed(c, players)
        espn(c, players=players)
        cycle(c, 6, C6, players=players)
        cycle(c, 7, C7, players=players, tweak=lambda pid, week: {"x": base(pid) + 1.0})
        counter = Counting(c)
        result = pm.movement(counter, SEASON, source=source)
        assert result["available"] and result["summary"]["compared"] >= 6 + extra - 1
        c.close()
        return counter.n
    assert queries(0) == queries(40)


def test_missing_tables_and_columns_degrade_to_an_unavailable_reason(con):
    seed(con)
    cycle(con, 6, C6)
    cycle(con, 7, C7)
    con.execute("ALTER TABLE independent_projection_runs DROP COLUMN forecast_horizon")
    con.execute("ALTER TABLE independent_projection_runs DROP COLUMN projected_from_week")
    legacy = ours(con)          # every run is its own cutoff and horizon 0: no week is in two snapshots
    assert legacy["available"] is False and legacy["window"]["weeks"] == [] and legacy["reason"]
    con.execute("DROP TABLE independent_projection_rows")
    assert "schema v8" in ours(con)["reason"]
    con.execute("DROP TABLE independent_projection_runs")
    assert "No Fantasy Intel forecast" in ours(con)["reason"]
    assert pm.movement(con, SEASON)["available"] is False            # espn_projection_history is empty
    con.execute("DROP TABLE espn_projection_history")
    assert pm.movement(con, SEASON)["available"] is False and pm.snapshots(con, SEASON, "espn") == []


def test_unsupported_league_scoring_makes_ours_unavailable(con, monkeypatch):
    two_cycles(con)
    monkeypatch.setattr(ps, "_scoring", lambda c, s: (None, "the league's scoring settings are not synced"))
    r = ours(con)
    assert r["available"] is False and "scoring settings are not synced" in r["reason"]
    assert r["governance"]["espn_superiority_established"] is False


# ----------------------------------------------------------------------------- the route


def plain(result):
    """The payload without the evidence block, which another module fills (app.analytics.mover_evidence)."""
    out = json.loads(json.dumps(result, default=str))
    for key in ("risers", "fallers", "players"):
        for m in out[key]:
            m["evidence"] = None
    return out


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = tmp_path / "api.duckdb"
    c = connect(path)
    init_db(c)
    seed(c)
    espn(c)
    cycle(c, 6, C6)
    cycle(c, 7, C7, tweak=lambda pid, week: {"x": base(pid) + 2.0})
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    return TestClient(app), path


def test_route_returns_the_same_payload_as_movement_and_defaults_to_espn(api):
    client, path = api
    default = client.get("/api/projections/movers", params={"season": SEASON})
    assert default.status_code == 200
    con = connect(path, read_only=True)
    try:
        assert plain(default.json()) == plain(pm.movement(con, SEASON))
        assert default.json()["source"] == "espn" and default.json()["governance"] is None
        mine = client.get("/api/projections/movers", params={"season": SEASON, "source": "independent"}).json()
        assert plain(mine) == plain(pm.movement(con, SEASON, source="independent"))
        wr = client.get("/api/projections/movers", params={"season": SEASON, "position": "WR", "view": "week",
                                                           "limit": 1, "baseline": "2026-10-14"}).json()
        assert plain(wr) == plain(pm.movement(con, SEASON, position="WR", view="week", limit=1, baseline="2026-10-14"))
        assert len(wr["risers"]) <= 1 and wr["window"]["weeks"] == [7]
    finally:
        con.close()
    assert mine["available"] and mine["governance"]["espn_superiority_established"] is False


@pytest.mark.parametrize("params", [{"source": "vegas"}, {"view": "month"}, {"position": "K"}, {"baseline": "2026-10-21"},
                                    {"baseline": "garbage"}, {"source": "independent", "baseline": "w7"},
                                    {"limit": 0}, {"limit": 51}])
def test_route_answers_422_for_invalid_arguments(api, params):
    client, _ = api
    assert client.get("/api/projections/movers", params={"season": SEASON, **params}).status_code == 422


# ----------------------------------------------------------------------------- review follow-ups


def test_espn_zero_rows_on_a_bye_and_a_traded_players_old_bye_week(con):
    """Real ESPN stores 0.0 on a bye. A player now on DEN (bye in week 9) who still has a real week-9 projection
    from his old team is not flagged as a bye."""
    seed(con, players={**PLAYERS, 3: ("Cy Back", "RB", "DEN")})
    espn(con)                                           # rows from PLAYERS: Cy (BUF there) has week 9 rows
    zero = [(d, SEASON, 1, 11, 0.0, t) for d, t in (BEFORE, AFTER)]      # Ace's KC bye as an explicit 0.0
    con.executemany("INSERT INTO espn_projection_history VALUES (?, ?, ?, ?, ?, ?)", zero)
    r = pm.movement(con, SEASON)
    cy9 = next(w for w in by_id(r)[3]["weeks"] if w["week"] == 9)
    assert (cy9["bye"], cy9["status_before"], cy9["before"]) == (False, "projected", 15.0)
    ace11 = next(w for w in by_id(r)[1]["weeks"] if w["week"] == 11)
    assert (ace11["bye"], ace11["status_after"], ace11["after"]) == (True, "bye", 0.0)


def test_ranks_count_only_players_comparable_on_both_sides(con):
    """Bo (the WR1 before) has no latest week-12 projection: he must not hold a rank on one side only and make
    Ace and Gus look like they moved up a spot with him gone."""
    seed(con)
    espn(con, skip_after={(2, 12)})
    r = pm.movement(con, SEASON)
    assert 2 not in by_id(r)
    assert [(by_id(r)[p]["rank_before"], by_id(r)[p]["rank_after"]) for p in (1, 7)] == [(1, 1), (2, 2)]


def test_a_bye_in_only_one_snapshot_keeps_the_other_snapshots_value(con):
    """Cy, traded to DEN (bye in week 9), still has a week-9 projection in the earlier snapshot only: only the
    latest side is a bye, and evidence names which snapshot has it rather than claiming 0 in both."""
    seed(con, players={**PLAYERS, 3: ("Cy Back", "RB", "DEN")})
    espn(con, skip_after={(3, 9)})                      # rows from PLAYERS (Cy on BUF): week 9 in the earlier only
    r = pm.movement(con, SEASON)
    cy9 = next(w for w in by_id(r)[3]["weeks"] if w["week"] == 9)
    assert (cy9["before"], cy9["after"]) == (15.0, 0.0)
    assert (cy9["bye"], cy9["bye_before"], cy9["bye_after"]) == (False, False, True)
    ace11 = next(w for w in by_id(r)[1]["weeks"] if w["week"] == 11)
    assert (ace11["bye"], ace11["bye_before"], ace11["bye_after"]) == (True, True, True)


def test_weeks_after_the_final_scoring_period_are_listed(con):
    seed(con)
    put_espn(con, BEFORE, lambda p, w: base(p), weeks=range(6, 19))
    put_espn(con, AFTER, lambda p, w: base(p) + SHIFT.get(p, 0.0), weeks=range(6, 19))
    r = pm.movement(con, SEASON)
    assert r["window"]["weeks"] == list(range(7, 18))
    assert {"week": 18, "reason": "after the league's final scoring period"} in r["window"]["excluded_weeks"]


def test_a_baseline_stored_after_the_latest_forecast_is_not_compared(con):
    two_cycles(con)
    add_run(con, 6, 14, C7 + timedelta(hours=1), tweak=lambda pid, week: {"x": base(pid) + 1.0}, tag="late")
    r = ours(con)
    assert r["available"] is False and "stored after the latest one" in r["reason"]
    assert r["risers"] == r["fallers"] == r["players"] == []


def test_a_newer_cycle_with_no_usable_run_is_reported(con):
    two_cycles(con)
    late = cycle(con, 8, datetime(2026, 10, 28, 12, 0), artifact="sha-unknown")
    r = ours(con)
    assert r["available"] and r["current"]["id"] == "w7"
    assert {s["run_id"] for s in r["skipped"]} >= set(late.values())
    assert any("newer forecast cycle of week 8" in n for n in r["notes"])


def test_components_add_up_to_the_change_exactly(con):
    def tweak7(pid, week):          # values whose p*x does not round cleanly
        return {"p": 0.333, "x": base(pid) + 0.777} if pid in (1, 3) else {"p": 0.61} if pid == 2 else None
    two_cycles(con, tweak7=tweak7)
    r = ours(con)
    for m in r["players"]:
        c = m["components"]
        assert round(c["availability"] + c["per_game"], 2) == m["delta"], m["name"]


def test_the_route_fills_evidence_for_listed_movers_only(api):
    client, _ = api
    for source in ("espn", "independent"):
        body = client.get("/api/projections/movers", params={"season": SEASON, "source": source}).json()
        assert body["available"]
        listed = body["risers"] + body["fallers"]
        assert listed and all(m["evidence"] and m["evidence"]["note"] for m in listed)
        assert all(m["evidence"] is None for m in body["players"])


def test_the_route_never_fails_on_a_baseline_stored_after_the_latest(tmp_path, monkeypatch):
    path = tmp_path / "late.duckdb"
    c = connect(path)
    init_db(c)
    seed(c)
    cycle(c, 6, C6)
    cycle(c, 7, C7)
    add_run(c, 6, 14, C7 + timedelta(hours=1), tweak=lambda pid, week: {"x": base(pid) + 1.0}, tag="late")
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    res = TestClient(app).get("/api/projections/movers", params={"season": SEASON, "source": "independent"})
    assert res.status_code == 200 and res.json()["available"] is False


def test_the_engine_reads_no_outcome_table():
    """Movement compares stored forecasts only: no actual points, no box scores (the evidence module is separate)."""
    import inspect
    src = inspect.getsource(pm)
    for needle in ("actual_points", "espn_player_weekly_points", "nfl_weekly_stats", "player_week_usage"):
        assert needle not in src
