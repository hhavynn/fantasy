"""Player Rankings & Value Explorer (analytics/rankings.py, analytics/ratings_adapter.py, GET /api/rankings).

Temp DuckDB only, one small synthetic league (docs/player_rankings.md is the contract). Every number is an invented
test input. League: season 2026, week 7, regular season ends in week 14, final scoring period 17. DEN is on a bye in
week 7. Players (week 7 ESPN projection):

    QB  Quinn 22.0 (team 5)   Rex 7.0 (availability unknown)
    WR  Alpha 20.0 (MY team)  Bravo 15.0 / Charlie 15.004 (tied to 0.01, free agents)  Golf 12.0 (no NFL team)
        Foxtrot 8.0 (waivers, no id map)  Hotel (DEN bye, ESPN row 0.0)  India (no week-7 row)
    RB  Kilo 18.0 (team 5)    Yankee 9.0 / Zulu 9.0 (tied)
    TE  Tango 11.0 (ESPN Questionable)  Uniform 6.0 (NFL report Out)
    K   Kicker (never in the pool)
"""

import json
import shutil
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.analytics import production_forecasts as pf
from app.analytics import projection_source as ps
from app.analytics import rankings as rk
from app.analytics import ratings_adapter as ra
from app.db.connection import connect, fetch_dicts, init_db

SEASON, CURRENT, FINAL, LAST_REG = 2026, 7, 17, 14
ROS_WEEKS = list(range(CURRENT, LAST_REG + 1))
CREATED = datetime(2026, 10, 14, 12, 0)
APPROVED = "sha-approved"
ORIGINAL_SCORING = ps._scoring
MY_TEAM, OTHER_TEAM = 3, 5

# pid: (name, position, pro_team, week-7 ESPN projection, availability_status, on_team_id, ESPN injury status)
PLAYERS = {
    301: ("Quinn QB", "QB", "KC", 22.0, "ONTEAM", OTHER_TEAM, None),
    302: ("Rex QB", "QB", "BUF", 7.0, None, None, None),
    101: ("Alpha WR", "WR", "KC", 20.0, "ONTEAM", MY_TEAM, None),
    102: ("Bravo WR", "WR", "KC", 15.0, "FREEAGENT", None, None),
    103: ("Charlie WR", "WR", "BUF", 15.004, "FREEAGENT", None, None),
    107: ("Golf WR", "WR", None, 12.0, "FREEAGENT", None, None),
    106: ("Fóxtrot WR", "WR", "KC", 8.0, "WAIVERS", None, None),
    108: ("Hotel WR", "WR", "DEN", 0.0, "FREEAGENT", None, None),
    109: ("India WR", "WR", "KC", None, "FREEAGENT", None, None),            # no ESPN row for week 7
    201: ("Kilo RB", "RB", "BUF", 18.0, "ONTEAM", OTHER_TEAM, None),
    202: ("Yankee RB", "RB", "BUF", 9.0, "FREEAGENT", None, None),
    203: ("Zulu RB", "RB", "BUF", 9.0, "WAIVERS", None, None),
    401: ("Tango TE", "TE", "KC", 11.0, "FREEAGENT", None, "QUESTIONABLE"),
    402: ("Uniform TE", "TE", "BUF", 6.0, "ONTEAM", MY_TEAM, None),
    901: ("Kicker K", "K", "KC", 9.0, "FREEAGENT", None, None),
}
SKILL = [pid for pid, p in PLAYERS.items() if p[1] != "K"]
POOL = len(SKILL)                                                     # 14
GSIS = {pid: f"00-A{pid}" for pid in PLAYERS if pid != 106}            # Foxtrot has no id map

# gsis -> player_scores 0-10 values (value, opportunity, production), role_trend, xfp/g, ppr/g, games
SCORES = {
    101: dict(v=9.0, o=8.75, p=7.25, trend="UP", xfp=15.2, ppr=14.0, games=6),
    102: dict(v=6.0, o=5.0, p=4.0, trend="FLAT", xfp=10.0, ppr=11.0, games=3),
    103: dict(v=8.0, o=7.0, p=8.0, trend="DOWN", xfp=12.5, ppr=9.0, games=5),
    201: dict(v=7.0, o=9.0, p=6.0, trend="UP", xfp=14.0, ppr=13.0, games=6),
    301: dict(v=9.5, o=3.0, p=9.5, trend="FLAT", xfp=18.0, ppr=20.0, games=6),
    401: dict(v=None, o=6.0, p=5.5, trend="INSUFFICIENT", xfp=8.0, ppr=7.5, games=4),   # overall rating is null
    402: dict(v=None, o=None, p=None, trend="INSUFFICIENT", xfp=None, ppr=None, games=1),  # every score null
    109: dict(v=2.0, o=2.0, p=2.0, trend="DOWN", xfp=2.0, ppr=2.0, games=3),        # scored, but no week-7 projection
}

# Ours (stored runs, every week 7..14): pid -> expected points; Hotel is on the bye in week 7 only.
OURS = {101: 19.0, 102: 17.0, 103: 14.0, 201: 12.0, 401: 11.0, 301: 5.0, 108: 5.0}


def espn_week(pid: int, week: int) -> float | None:
    """The ESPN projection stored for each week 7..17: p + 0.5 per week after the first (Hotel: 0.0 on the bye,
    then 6.0; India: no week-7 row)."""
    p = PLAYERS[pid][3]
    if pid == 108:
        return 0.0 if week == CURRENT else 6.0
    if pid == 109:
        return None if week == CURRENT else 9.0
    return p + 0.5 * (week - CURRENT)


def espn_ros(pid: int) -> float | None:
    vals = [espn_week(pid, w) for w in ROS_WEEKS]
    return None if any(v is None for v in vals) else round(sum(vals), 2)


def ours_week(pid: int, week: int):
    if pid == 108 and week == CURRENT:
        return "bye", None
    return "projected", OURS[pid]


def add_player(con, pid, name, pos, team, proj, status, on_team, injury, *, mapped=True, scores=None, usage=None):
    con.execute("""INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, injury_status,
                   availability_status, on_team_id) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [pid, name, pos, team, injury, status, on_team])
    if mapped:
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id) VALUES (?, ?)", [pid, f"00-A{pid}"])


def add_scores(con, pid, s, pos):
    con.execute("""INSERT INTO player_scores (season, gsis_id, espn_player_id, position, as_of_week, games,
                   opportunity_basis, opportunity_tier, role_trend, market_signal, opportunity_score, production_score,
                   usage_trend_score, trend_label, fp_per_game, xfp_per_game, fp_minus_xfp, value_gap_label,
                   classification, value_score, components, reasons, computed_at)
                   VALUES (?, ?, ?, ?, 6, ?, 'xfp', 'HIGH', ?, 'HOLD', ?, ?, 0.4, ?, ?, ?, ?, 'ALIGNED', 'HOLD', ?,
                   ?, '[]', '2026-10-14 00:00:00')""",
                [SEASON, f"00-A{pid}", pid, pos, s["games"], s["trend"], s["o"], s["p"], s["trend"],
                 s["ppr"], s["xfp"], None if s["ppr"] is None or s["xfp"] is None else round(s["ppr"] - s["xfp"], 2),
                 s["v"], json.dumps({"production_sustainability": "ALIGNED", "opportunity_basis": "xfp",
                                     "recent_opportunity_per_game": None if s["xfp"] is None else s["xfp"] + 1.0,
                                     "recent_ppr_per_game": None if s["ppr"] is None else s["ppr"] + 1.0})])


def add_usage(con, pid, pos, team, points, *, dnp_week=None):
    for week, pts in enumerate(points, start=1):
        con.execute("""INSERT INTO player_week_usage (season, week, gsis_id, team, position, offense_snaps, targets,
                       carries, fantasy_points_ppr) VALUES (?, ?, ?, ?, ?, 50, 8, 0, ?)""",
                    [SEASON, week, f"00-A{pid}", team, pos, pts])
    if dnp_week:      # on the roster, did not play: not a game
        con.execute("""INSERT INTO player_week_usage (season, week, gsis_id, team, position, offense_snaps, targets,
                       carries, fantasy_points_ppr) VALUES (?, ?, ?, ?, ?, 0, 0, 0, NULL)""",
                    [SEASON, dnp_week, f"00-A{pid}", team, pos])


def add_run(con, week, extra_ours=None):
    """One ifp-2.0.0 run for `week` (horizon week - CURRENT) with a row per Ours player, sealed."""
    ours = {**OURS, **(extra_ours or {})}
    horizon = week - CURRENT
    run_id = f"run-{week}-{APPROVED}"
    rows = []
    for pid in ours:
        status, exp = ours_week(pid, week) if pid in OURS else ("projected", ours[pid])
        rows.append((pid, status, exp))
    con.execute("""INSERT INTO independent_projection_runs (run_id, season, week, model_version, scoring_fingerprint,
                   model_status, created_at, row_count, artifact_id, forecast_horizon, projected_from_week)
                   VALUES (?, ?, ?, ?, 'fp', ?, ?, ?, ?, ?, ?)""",
                [run_id, SEASON, week, ps.MODEL_VERSION, json.dumps({"WR": {"status": "experimental"}}), CREATED,
                 len(rows), APPROVED, horizon, CURRENT])
    for pid, status, exp in rows:
        con.execute("""INSERT INTO independent_projection_rows (run_id, season, week, espn_player_id, coverage_status,
                       expected_points) VALUES (?, ?, ?, ?, ?, ?)""", [run_id, SEASON, week, pid, status, exp])
    run = fetch_dicts(con, "SELECT * FROM independent_projection_runs WHERE run_id = ?", [run_id])[0]
    stored = fetch_dicts(con, "SELECT * FROM independent_projection_rows WHERE run_id = ?", [run_id])
    con.execute("UPDATE independent_projection_runs SET content_sha256 = ? WHERE run_id = ?",
                [pf.content_sha256(run, stored), run_id])


def build(con, *, extra=0, reg_season_matchups=LAST_REG, runs=True, teams=True, injuries=True):
    """The synthetic league. `extra` adds that many generated skill players (scores, usage, ESPN rows and an Ours row
    each) for the query-count test."""
    con.execute("""INSERT INTO leagues (league_id, season, name, current_week, final_scoring_period,
                   reg_season_matchups, scoring_items, settings_raw, synced_at)
                   VALUES (1, ?, 'Test League', ?, ?, ?, '[]', '{}', '2026-10-14 00:00:00')""",
                [SEASON, CURRENT, FINAL, reg_season_matchups])
    if teams:
        for tid, name, mine in ((MY_TEAM, "My Squad", True), (OTHER_TEAM, "Team Five", False)):
            con.execute("""INSERT INTO fantasy_teams (league_id, season, team_id, abbrev, name, is_mine, synced_at)
                           VALUES (1, ?, ?, ?, ?, ?, '2026-10-14 00:00:00')""", [SEASON, tid, f"T{tid}", name, mine])
    for pid, (name, pos, team, proj, status, on_team, injury) in PLAYERS.items():
        add_player(con, pid, name, pos, team, proj, status, on_team, injury, mapped=pid != 106)
        for week in range(CURRENT, FINAL + 1):
            v = espn_week(pid, week) if pid in OURS or pid in SKILL else proj
            if pid == 901:
                v = 9.0
            if v is not None:
                con.execute("INSERT INTO espn_player_weekly_points VALUES (?, ?, ?, NULL, ?, '2026-10-14 00:00:00')",
                            [SEASON, pid, week, v])
    for pid, s in SCORES.items():
        add_scores(con, pid, s, PLAYERS[pid][1])
    add_usage(con, 101, "WR", "KC", [10, 20, 10, 20, 10, 20])
    add_usage(con, 102, "WR", "KC", [5, 5, 5], dnp_week=4)
    con.execute("""CREATE TABLE IF NOT EXISTS nfl_schedules (season INT, week INT, game_type VARCHAR, game_id VARCHAR,
                   home_team VARCHAR, away_team VARCHAR, spread_line DOUBLE, total_line DOUBLE)""")
    for w in range(1, 19):
        con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, 'KC', 'BUF', NULL, NULL)", [SEASON, w, f"k{w}"])
        if w != CURRENT:      # DEN is on a bye in week 7
            con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, 'DEN', 'MIA', NULL, NULL)",
                        [SEASON, w, f"d{w}"])
    if injuries:
        con.execute("""CREATE TABLE IF NOT EXISTS nfl_injuries (season INTEGER, game_type VARCHAR, team VARCHAR,
                       week INTEGER, gsis_id VARCHAR, report_status VARCHAR, report_primary_injury VARCHAR,
                       practice_status VARCHAR, date_modified TIMESTAMP)""")
        con.execute("INSERT INTO nfl_injuries VALUES (2026, 'REG', 'BUF', ?, '00-A402', 'Out', 'Knee', "
                    "'Did Not Participate', '2026-10-14 10:00:00')", [CURRENT])
    extra_ours = {}
    for i in range(extra):
        pid, pos = 1000 + i, ("WR", "RB", "TE", "QB")[i % 4]
        add_player(con, pid, f"Gen{i:03d} {pos}", pos, ("KC", "BUF", "DEN")[i % 3], 10.0,
                   ("FREEAGENT", "WAIVERS", "ONTEAM")[i % 3], (None, None, OTHER_TEAM)[i % 3], None)
        add_scores(con, pid, dict(v=(i % 10) + 0.5, o=(i % 9) + 0.25, p=(i % 8) + 0.75, trend=("UP", "FLAT", "DOWN")[i % 3],
                                  xfp=5.0 + i % 7, ppr=4.0 + i % 5, games=5), pos)
        add_usage(con, pid, pos, "KC", [3.0 + (i + k) % 6 for k in range(4)])
        for week in range(CURRENT, FINAL + 1):
            con.execute("INSERT INTO espn_player_weekly_points VALUES (?, ?, ?, NULL, ?, '2026-10-14 00:00:00')",
                        [SEASON, pid, week, 6.0 + (i % 11) + 0.5 * (week - CURRENT)])
        extra_ours[pid] = 4.0 + (i % 13)
    if runs:
        # the week-7 ifp-2.0.0 artifact is registered, with a data cutoff before week 7: it covers every later week too
        con.execute("INSERT INTO approved_artifacts VALUES (?, ?, ?, ?, ?, NULL, 'learning_cycle', '2026-10-14 00:00:00')",
                    [ps.MODEL_VERSION, APPROVED, SEASON, CURRENT, SEASON * 100 + CURRENT - 1])
        for w in ROS_WEEKS:
            add_run(con, w, extra_ours)


@pytest.fixture(autouse=True)
def fingerprint(monkeypatch):
    """A usable league scoring for the Ours switch; individual tests restore the real resolver."""
    monkeypatch.setattr(ps, "_scoring", lambda con, season: ("fp", None))


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    """The default league, built once; every test gets its own copy (a test may edit it)."""
    path = tmp_path_factory.mktemp("rankings_template") / "league.duckdb"
    c = connect(path)
    init_db(c)
    build(c)
    c.execute("CHECKPOINT")
    c.close()
    return path


@pytest.fixture
def league(template_db, tmp_path):
    path = tmp_path / "league.duckdb"
    shutil.copyfile(template_db, path)
    c = connect(path)
    yield c
    c.close()


def get(con, **kw):
    kw.setdefault("limit", 300)
    return rk.build_rankings(con, **kw)


def rows(body):
    return {p["espn_player_id"]: p for p in body["players"]}


def ids(body):
    return [p["espn_player_id"] for p in body["players"]]


def names(body):
    return [p["full_name"] for p in body["players"]]


# ============================================================================= rating adapter: correct 0-100 display


def test_adapter_contract_constants():
    assert ra.SCALE == "0-100" and ra.DECIMALS == 1
    assert isinstance(ra.CONTRACT, dict)
    assert ra.CONTRACT.get("scale", ra.SCALE) == "0-100" and ra.CONTRACT.get("decimals", ra.DECIMALS) == 1


@pytest.mark.parametrize("score,expected", [
    (8.75, 87.5), (10, 100.0), (0, 0.0), (7.25, 72.5), (5, 50.0), (0.04, 0.4), (9.999, 100.0), (0.05, 0.5),
    (10.4, 100.0), (250, 100.0), (-1, 0.0), (-0.001, 0.0),
])
def test_to_public_maps_0_10_to_0_100_one_decimal_and_clamps(score, expected):
    out = ra.to_public(score)
    assert out == expected and isinstance(out, float)


@pytest.mark.parametrize("missing", [None, float("nan")])
def test_to_public_missing_is_none_never_zero(missing):
    assert ra.to_public(missing) is None


def test_public_rating_maps_the_three_score_columns():
    row = {"value_score": 9.0, "opportunity_score": 8.75, "production_score": 7.25, "other": 1}
    assert ra.public_rating(row) == {"overall": 90.0, "opportunity": 87.5, "production": 72.5}
    assert ra.public_rating({"value_score": None, "opportunity_score": 6.0, "production_score": float("nan")}) == {
        "overall": None, "opportunity": 60.0, "production": None}


def test_ratings_in_the_response_are_one_decimal_floats_in_range(league):
    body = get(league)
    for p in body["players"]:
        assert set(p["rating"]) == {"overall", "opportunity", "production"}
        for v in p["rating"].values():
            assert v is None or (isinstance(v, float) and 0.0 <= v <= 100.0 and v == round(v, 1)), (p["full_name"], v)
    alpha = rows(body)[101]
    assert alpha["rating"] == {"overall": 90.0, "opportunity": 87.5, "production": 72.5}
    block = body["rating"]
    assert block["scale"] == "0-100" and block["decimals"] == 1 and block["as_of_week"] == 6
    assert block["scale"] == ra.CONTRACT.get("scale", "0-100")


# ============================================================================= null ratings


def test_missing_or_null_scores_are_null_ratings_never_zero(league):
    r = rows(get(league))
    assert r[402]["rating"] == {"overall": None, "opportunity": None, "production": None}       # row of null scores
    assert r[106]["rating"] == {"overall": None, "opportunity": None, "production": None}       # no id map, no scores
    assert r[107]["rating"] == {"overall": None, "opportunity": None, "production": None}       # mapped, no scores row
    assert r[401]["rating"] == {"overall": None, "opportunity": 60.0, "production": 55.0}      # null overall only
    assert r[202]["rating"]["overall"] is None
    # the rest of the row still shows what is known, with nothing invented
    assert r[402]["opportunity"]["xfp_per_game"] is None and r[402]["consistency"]["ppr_sd"] is None


# ============================================================================= competition_ranks (unit)


def test_competition_ranks_descending_with_ties():
    out = rk.competition_ranks({"a": 10.0, "b": 9.0, "c": 9.0, "d": 8.0})
    assert out == {"a": (1, False), "b": (2, True), "c": (2, True), "d": (4, False)}


def test_competition_ranks_three_way_tie_skips_ranks():
    out = rk.competition_ranks({"a": 5.0, "b": 5.0, "c": 5.0, "d": 1.0, "e": 9.0})
    assert out["e"] == (1, False)
    assert [out[k] for k in "abc"] == [(2, True)] * 3
    assert out["d"] == (5, False)


def test_competition_ranks_none_is_unranked_and_zero_is_a_value():
    out = rk.competition_ranks({"a": 3.0, "b": None, "c": 0.0, "d": None})
    assert out["a"] == (1, False) and out["c"] == (2, False)
    assert out["b"] == (None, False) and out["d"] == (None, False)
    assert rk.competition_ranks({}) == {}
    assert rk.competition_ranks({"x": None}) == {"x": (None, False)}


def test_competition_ranks_equality_is_to_two_decimals():
    out = rk.competition_ranks({"a": 15.004, "b": 15.0, "c": 14.99})
    assert out["a"] == (1, True) and out["b"] == (1, True) and out["c"] == (3, False)
    near = rk.competition_ranks({"a": 1.006, "b": 1.0})            # 1.01 vs 1.00: not equal
    assert near == {"a": (1, False), "b": (2, False)}
    assert rk.competition_ranks({"a": 0.1 + 0.2, "b": 0.3}) == {"a": (1, True), "b": (1, True)}


def test_competition_ranks_all_tied_and_keys_preserved():
    out = rk.competition_ranks({1: 2.0, 2: 2.0})
    assert out == {1: (1, True), 2: (1, True)}
    assert set(rk.competition_ranks({("x", 1): 1.0, ("y", 2): 2.0})) == {("x", 1), ("y", 2)}


def test_module_constants():
    assert set(rk.POSITIONS) - {"ALL"} == {"QB", "RB", "WR", "TE"}
    assert tuple(rk.HORIZONS) == ("week", "ros") and tuple(rk.STATUSES) == ("all", "rostered", "available")
    assert set(rk.SORT_KEYS) == {"rank", "name", "espn", "ours", "rating", "opportunity", "production", "xfp", "ppr",
                                 "consistency", "trend"}
    assert rk.MAX_LIMIT == 300 and issubclass(rk.RankingsUnavailable, Exception)


# ============================================================================= ranking ties in the response


def test_weekly_response_shape_and_window(league):
    body = get(league)
    assert (body["season"], body["week"], body["horizon"], body["source"], body["position"]) == (
        SEASON, CURRENT, "week", "espn", "ALL")
    assert body["sort"] == {"key": "rank", "dir": "asc"}
    w = body["window"]
    assert w["weeks"] == [CURRENT] and w["first"] == w["last"] == CURRENT and w["regular_season_only"] is True
    assert w["last_regular_season_week"] == LAST_REG
    assert body["projection"]["basis"] == "espn" and body["projection"]["ours"] is None
    for key in ("label", "espn"):
        assert body["projection"][key]
    assert body["scoring"]["projection_units"] == "league scoring" and "PPR" in body["scoring"]["rating_units"]
    for key in ("freshness", "counts", "notes", "players", "league_context"):
        assert key in body
    assert {"espn_projections_synced_at", "player_scores_computed_at", "player_scores_as_of_week"} <= set(body["freshness"])


def test_tied_projections_share_a_rank_and_the_next_rank_skips(league):
    r = rows(get(league))
    assert [r[pid]["rank"]["overall"] for pid in (301, 101, 201)] == [1, 2, 3]
    # Bravo 15.0 and Charlie 15.004 are equal to 0.01
    for pid in (102, 103):
        assert r[pid]["rank"]["overall"] == 4 and r[pid]["rank"]["tied"] is True
    assert r[107]["rank"]["overall"] == 6 and r[107]["rank"]["tied"] is False          # 4 + 2: rank 5 is skipped
    for pid in (202, 203):
        assert r[pid]["rank"]["overall"] == 8 and r[pid]["rank"]["tied"] is True
    assert r[106]["rank"]["overall"] == 10 and r[302]["rank"]["overall"] == 11 and r[402]["rank"]["overall"] == 12
    assert not any(p["rank"]["tied"] for pid, p in r.items() if pid not in (102, 103, 202, 203))
    # the value is ESPN's, unmodified
    assert r[103]["projection"]["value"] == 15.004 and r[103]["projection"]["espn"] == 15.004


def test_tied_position_labels_and_deterministic_display_order(league):
    body = get(league)
    r = rows(body)
    assert r[102]["rank"]["position_label"] == "WR2" and r[103]["rank"]["position_label"] == "WR2"
    assert r[202]["rank"]["position_label"] == "RB2" and r[203]["rank"]["position_label"] == "RB2"
    assert r[202]["rank"]["position"] == 2 and r[203]["rank"]["tied"] is True
    order = ids(body)
    # inside the tie: higher overall rating first (Charlie 80.0 over Bravo 60.0); no ratings: by name
    assert order.index(103) < order.index(102)
    assert order.index(202) < order.index(203)
    assert ids(get(league)) == order                    # deterministic


def test_overall_order_is_by_rank_with_unranked_last(league):
    body = get(league)
    assert names(body) == ["Quinn QB", "Alpha WR", "Kilo RB", "Charlie WR", "Bravo WR", "Golf WR", "Tango TE",
                           "Yankee RB", "Zulu RB", "Fóxtrot WR", "Rex QB", "Uniform TE"] + names(body)[12:]
    assert {p["full_name"] for p in body["players"][12:]} == {"Hotel WR", "India WR"}
    assert all(p["rank"]["overall"] is None for p in body["players"][12:])


# ============================================================================= different positions


def test_position_filter_returns_only_that_position_with_labels(league):
    wr = get(league, position="WR")
    assert {p["position"] for p in wr["players"]} == {"WR"}
    ranked = [p for p in wr["players"] if p["rank"]["position"] is not None]
    assert [p["rank"]["position_label"] for p in ranked][:1] == ["WR1"]
    assert sorted({p["rank"]["position_label"] for p in ranked}) == ["WR1", "WR2", "WR4", "WR5"]
    assert wr["position"] == "WR" and wr["counts"]["matched"] == 7
    for pos in ("QB", "RB", "TE"):
        body = get(league, position=pos)
        assert {p["position"] for p in body["players"]} == {pos}
        assert all(p["rank"]["position_label"].startswith(pos) for p in body["players"] if p["rank"]["position"])


def test_all_has_overall_rank_across_positions_and_consistent_position_labels(league):
    r = rows(get(league))
    assert r[301]["rank"] == {"overall": 1, "position": 1, "position_label": "QB1", "tied": False}
    assert r[201]["rank"]["position_label"] == "RB1" and r[201]["rank"]["overall"] == 3
    assert r[401]["rank"]["position_label"] == "TE1" and r[401]["rank"]["overall"] == 7
    assert r[302]["rank"]["position_label"] == "QB2" and r[402]["rank"]["position_label"] == "TE2"
    # the position view agrees with ALL, rank for rank
    for pos in ("QB", "RB", "WR", "TE"):
        for pid, p in rows(get(league, position=pos)).items():
            assert p["rank"] == r[pid]["rank"], (pos, pid)


def test_kickers_and_other_positions_are_not_in_the_pool(league):
    body = get(league)
    assert 901 not in rows(body) and {p["position"] for p in body["players"]} <= {"QB", "RB", "WR", "TE"}
    assert body["counts"]["pool"] == POOL


def test_ranks_do_not_change_with_search_or_roster_filters(league):
    full = rows(get(league))
    for kw in ({"status": "available"}, {"status": "rostered"}, {"q": "o"}, {"q": "fox"}, {"position": "WR", "status": "available"}):
        for pid, p in rows(get(league, **kw)).items():
            assert p["rank"] == full[pid]["rank"], (kw, pid)
    avail = rows(get(league, status="available"))
    assert avail[107]["rank"]["overall"] == 6                    # a free agent keeps his rank from the whole pool
    assert avail[102]["rank"]["position_label"] == "WR2"


# ============================================================================= filters, paging, counts


def test_status_filters(league):
    rostered = rows(get(league, status="rostered"))
    assert set(rostered) == {101, 301, 201, 402}
    available = rows(get(league, status="available"))
    assert set(available) == {102, 103, 107, 106, 108, 109, 202, 203, 401}       # FREE_AGENT or WAIVERS
    assert 302 not in rostered and 302 not in available                          # unknown roster state: only in "all"
    assert 302 in rows(get(league, status="all"))
    assert get(league, status="available")["counts"]["matched"] == len(available)


def test_search_is_case_and_accent_insensitive_and_does_not_renumber(league):
    for q in ("fox", "FOX", "fóx", "Fóxtrot", "xtrot", " fox "):
        assert set(rows(get(league, q=q))) == {106}, q
    assert set(rows(get(league, q="foxtrot"))) == {106}
    assert rows(get(league, q="FÓXTROT"))[106]["rank"]["overall"] == 10
    assert get(league, q="no such player")["players"] == [] and get(league, q="zzz")["counts"]["matched"] == 0


def test_limit_and_offset_page_through_the_same_order(league):
    full = ids(get(league))
    assert ids(get(league, limit=5)) == full[:5]
    page = get(league, limit=3, offset=4)
    assert ids(page) == full[4:7] and page["counts"]["returned"] == 3 and page["counts"]["matched"] == POOL
    assert get(league, offset=500)["players"] == []


def test_counts_block(league):
    c = get(league)["counts"]
    assert c["pool"] == POOL and c["ranked"] == 12 and c["matched"] == POOL and c["returned"] == POOL
    assert c["unranked"] == {"missing": 1, "bye": 1, "incomplete": 0, "no_ours": 0}
    assert c["ranked"] + sum(c["unranked"].values()) == c["pool"]


# ============================================================================= missing projections


def test_player_without_an_espn_row_is_missing_and_sorted_after_ranked(league):
    body = get(league)
    india = rows(body)[109]
    assert india["projection"]["status"] == "missing" and india["projection"]["value"] is None
    assert india["rank"] == {"overall": None, "position": None, "position_label": None, "tied": False}
    assert india["projection"]["missing_weeks"] == [CURRENT]
    last = body["players"][-2:]
    assert 109 in [p["espn_player_id"] for p in last]
    assert body["counts"]["unranked"]["missing"] == 1
    assert india["rating"]["overall"] == 20.0                      # a rating without a projection is still shown


def test_projected_zero_is_a_real_value_but_not_for_a_bye(league):
    # a real ESPN zero is ranked ...
    league.execute("UPDATE espn_player_weekly_points SET projected_points = 0.0 WHERE espn_player_id = 402 "
                   "AND scoring_period = ?", [CURRENT])
    p = rows(get(league))[402]
    assert p["projection"]["value"] == 0.0 and p["projection"]["status"] == "ok" and p["rank"]["overall"] == 12


def test_ros_with_one_missing_week_is_incomplete_with_no_partial_total(league):
    body = get(league, horizon="ros")
    india = rows(body)[109]
    assert india["projection"]["status"] == "incomplete" and india["projection"]["value"] is None
    assert india["projection"]["missing_weeks"] == [CURRENT]
    assert india["projection"]["weeks_available"] == india["projection"]["weeks_expected"] - 1
    assert india["rank"]["overall"] is None
    assert body["counts"]["unranked"]["incomplete"] == 1 and body["counts"]["unranked"]["missing"] == 0
    assert body["players"][-1]["espn_player_id"] in (109,) or 109 in ids(body)[-2:]
    # a gap in the middle of the window is the same
    league.execute("DELETE FROM espn_player_weekly_points WHERE espn_player_id = 101 AND scoring_period = 10")
    alpha = rows(get(league, horizon="ros"))[101]
    assert alpha["projection"]["status"] == "incomplete" and alpha["projection"]["missing_weeks"] == [10]
    assert alpha["projection"]["value"] is None and alpha["rank"]["overall"] is None


# ============================================================================= rest of season window


def test_ros_window_is_the_remaining_regular_season(league):
    body = get(league, horizon="ros")
    w = body["window"]
    assert w["weeks"] == ROS_WEEKS and (w["first"], w["last"]) == (CURRENT, LAST_REG)
    assert w["regular_season_only"] is True and w["last_regular_season_week"] == LAST_REG
    r = rows(body)
    for pid in (101, 102, 103, 201, 301, 401):
        assert r[pid]["projection"]["value"] == espn_ros(pid), pid           # same window for everyone, no playoff weeks
        assert r[pid]["projection"]["status"] == "ok"
        assert r[pid]["projection"]["weeks_expected"] == r[pid]["projection"]["weeks_available"] == len(ROS_WEEKS)
    assert r[101]["projection"]["value"] == 8 * 20.0 + 14.0
    # ranked by the total: Quinn 190, Alpha 174, Kilo 158, Charlie 134.03, Bravo 134.0 are NOT tied (differ by 0.03)
    assert r[103]["rank"]["tied"] is False and r[102]["rank"]["tied"] is False
    assert r[103]["rank"]["overall"] < r[102]["rank"]["overall"]
    assert [r[pid]["rank"]["overall"] for pid in (301, 101, 201)] == [1, 2, 3]


def test_ros_bye_week_row_counts_as_available_and_the_total_is_complete(league):
    hotel = rows(get(league, horizon="ros"))[108]
    assert hotel["projection"]["status"] == "ok"
    assert hotel["projection"]["value"] == espn_ros(108) == 42.0                  # ESPN's own bye-week 0.0 + 7 x 6.0
    assert hotel["projection"]["weeks_available"] == hotel["projection"]["weeks_expected"]
    assert hotel["projection"]["missing_weeks"] == [] and hotel["projection"]["bye_weeks"] == [CURRENT]
    assert hotel["rank"]["overall"] is not None


def test_ros_bye_week_without_an_espn_row_is_not_a_gap(league):
    """A schedule bye is never required: DEN's week-7 bye has no ESPN row and the ESPN total stays complete."""
    league.execute("DELETE FROM espn_player_weekly_points WHERE espn_player_id = 108 AND scoring_period = ?", [CURRENT])
    hotel = rows(get(league, horizon="ros"))[108]
    assert hotel["projection"]["status"] == "ok" and hotel["projection"]["value"] == 42.0     # 7 x 6.0
    assert hotel["projection"]["missing_weeks"] == [] and hotel["projection"]["bye_weeks"] == [CURRENT]
    assert hotel["rank"]["overall"] is not None
    # a missing row in a week he plays is still a gap
    league.execute("DELETE FROM espn_player_weekly_points WHERE espn_player_id = 108 AND scoring_period = 9")
    again = rows(get(league, horizon="ros"))[108]
    assert again["projection"]["status"] == "incomplete" and again["projection"]["missing_weeks"] == [9]
    assert again["projection"]["value"] is None


def test_ros_end_of_regular_season_unknown_uses_the_full_window_and_says_so(con):
    build(con, reg_season_matchups=None)
    body = get(con, horizon="ros")
    w = body["window"]
    assert w["weeks"] == list(range(CURRENT, FINAL + 1)) and w["last_regular_season_week"] is None
    assert w["note"]
    assert rows(body)[101]["projection"]["value"] == round(sum(espn_week(101, x) for x in range(CURRENT, FINAL + 1)), 2)
    ind = get(con, horizon="ros", source="independent")                  # Ours needs the regular-season end
    assert ind["projection"]["ours"]["available"] is False and ind["projection"]["ours"]["reason"]
    assert ind["projection"]["basis"] == "espn"


# ============================================================================= bye weeks


def test_team_on_bye_is_unranked_even_with_a_zero_row(league):
    body = get(league)
    hotel = rows(body)[108]
    assert hotel["projection"]["status"] == "bye" and hotel["projection"]["value"] is None
    assert hotel["projection"]["espn"] in (None, 0.0)
    assert hotel["rank"]["overall"] is None and hotel["rank"]["position_label"] is None
    assert hotel["game"] == {"week": CURRENT, "opponent": None, "is_home": None, "on_bye": True}
    assert hotel["projection"]["bye_weeks"] == [CURRENT]
    assert body["counts"]["unranked"]["bye"] == 1
    assert any("bye" in s.lower() for s in hotel["why"])
    # nobody else is flagged as on a bye
    assert [pid for pid, p in rows(body).items() if p["game"]["on_bye"]] == [108]
    # ranked players below him are not renumbered by the zero: the bye is not a projected 0
    assert rows(body)[402]["rank"]["overall"] == 12


def test_game_context_opponent_home_away(league):
    r = rows(get(league))
    assert r[101]["game"] == {"week": CURRENT, "opponent": "BUF", "is_home": True, "on_bye": False}
    assert r[302]["game"] == {"week": CURRENT, "opponent": "KC", "is_home": False, "on_bye": False}


# ============================================================================= Ours / forecast sources


def test_ours_ranks_by_the_independent_projection_and_keeps_espn_beside_it(league):
    body = get(league, source="independent")
    assert body["source"] == "independent" and body["projection"]["basis"] == "ours"
    ours = body["projection"]["ours"]
    assert ours["available"] is True and ours["model_version"] == ps.MODEL_VERSION and ours["experimental"] is False
    assert ours["reason"] is None and ours["run_ids"]
    r = rows(body)
    for pid, value in OURS.items():
        if pid == 108:
            continue
        p = r[pid]
        assert p["projection"]["value"] == value and p["projection"]["ours"] == value
        assert p["projection"]["ours_origin"] == "ours" and p["projection"]["status"] == "ok"
        assert p["projection"]["espn"] == PLAYERS[pid][3]                     # ESPN's column is still there
    assert [r[pid]["rank"]["overall"] for pid in (101, 102, 103, 201, 401, 301)] == [1, 2, 3, 4, 5, 6]
    assert [r[pid]["rank"]["position_label"] for pid in (101, 102, 103)] == ["WR1", "WR2", "WR3"]
    assert body["freshness"]["ours_run_created_at"]
    assert body["counts"]["ranked"] == 6


def test_a_player_without_an_ours_value_is_no_ours_never_ranked_on_espn(league):
    body = get(league, source="independent")
    r = rows(body)
    for pid in (202, 203, 302, 402, 106, 107):
        p = r[pid]
        assert p["projection"]["status"] == "no_ours" and p["projection"]["value"] is None, pid
        assert p["rank"]["overall"] is None and p["projection"]["ours"] is None
        assert p["projection"]["espn"] == PLAYERS[pid][3]
    assert body["counts"]["unranked"]["no_ours"] >= 6
    c = body["counts"]
    assert c["ranked"] + sum(c["unranked"].values()) == c["pool"]
    # sorted after every ranked player
    ranked_n = c["ranked"]
    assert all(p["rank"]["overall"] is not None for p in body["players"][:ranked_n])
    assert all(p["rank"]["overall"] is None for p in body["players"][ranked_n:])


def test_ours_bye_is_still_a_bye(league):
    hotel = rows(get(league, source="independent"))[108]
    assert hotel["projection"]["status"] == "bye" and hotel["rank"]["overall"] is None


def test_ours_rest_of_season_is_flagged_and_labeled_without_hedging(league):
    body = get(league, source="independent", horizon="ros")
    ours = body["projection"]["ours"]
    assert ours["available"] and ours["experimental"] is True          # the horizon flag stays in the API
    assert ours["label"].startswith("Fantasy Intel projections (")
    shown = (ours["label"] + json.dumps(body["notes"])).lower()
    assert "experimental" not in shown and "owner override" not in shown and "better than espn" not in shown
    r = rows(body)
    assert r[101]["projection"]["value"] == r[101]["projection"]["ours"] == 8 * OURS[101]
    assert r[101]["projection"]["espn"] == espn_ros(101)
    assert r[108]["projection"]["ours"] == 7 * OURS[108]                 # the bye week is Ours' zero, not a gap
    assert body["window"]["weeks"] == ROS_WEEKS
    assert [r[pid]["rank"]["overall"] for pid in (101, 102, 103)] == [1, 2, 3]


def test_ours_missing_a_remaining_week_is_incomplete(league):
    league.execute("DELETE FROM independent_projection_rows WHERE week = 10 AND espn_player_id = 102")
    run = fetch_dicts(league, "SELECT * FROM independent_projection_runs WHERE week = 10")[0]
    stored = fetch_dicts(league, "SELECT * FROM independent_projection_rows WHERE run_id = ?", [run["run_id"]])
    league.execute("UPDATE independent_projection_runs SET content_sha256 = ?, row_count = ? WHERE run_id = ?",
                   [pf.content_sha256(run, stored), len(stored), run["run_id"]])
    bravo = rows(get(league, source="independent", horizon="ros"))[102]
    assert bravo["projection"]["value"] is None and bravo["rank"]["overall"] is None
    assert bravo["projection"]["status"] in ("incomplete", "no_ours")


def test_espn_default_is_unchanged_by_stored_ours_runs(con, tmp_path):
    build(con, runs=False)
    without = get(con)
    other = connect(tmp_path / "other.duckdb")
    init_db(other)
    build(other)
    with_runs = get(other)
    other.close()
    assert [(p["espn_player_id"], p["rank"], p["projection"]) for p in without["players"]] == [
        (p["espn_player_id"], p["rank"], p["projection"]) for p in with_runs["players"]]
    assert with_runs["projection"]["basis"] == "espn" and with_runs["projection"]["ours"] is None


# ============================================================================= league scoring compatibility


def test_scoring_block_describes_the_units(league):
    s = get(league)["scoring"]
    assert s["projection_units"] == "league scoring" and s["rating_units"] == "full PPR (nflverse)"
    assert s["ours_supported"] is True and s["ours_reason"] is None


def test_unsupported_league_scoring_makes_ours_unavailable_and_ranks_on_espn(league, monkeypatch):
    monkeypatch.setattr(ps, "_scoring", ORIGINAL_SCORING)          # scoring_items '[]': unsupported or unsynced
    body = get(league, source="independent")
    ours = body["projection"]["ours"]
    assert ours["available"] is False and ours["reason"]
    assert body["projection"]["basis"] == "espn"
    assert body["scoring"]["ours_supported"] is False and body["scoring"]["ours_reason"]
    assert body["notes"] and any("espn" in n.lower() for n in body["notes"])
    base = get(league)
    r = rows(body)
    for pid, p in rows(base).items():          # ranked exactly like the ESPN view, values are ESPN's
        assert r[pid]["rank"] == p["rank"] and r[pid]["projection"]["value"] == p["projection"]["espn"], pid
        assert r[pid]["projection"]["status"] != "no_ours"
    assert body["counts"]["unranked"]["no_ours"] == 0


def test_ros_ours_with_unusable_scoring_also_falls_back(league, monkeypatch):
    monkeypatch.setattr(ps, "_scoring", ORIGINAL_SCORING)
    body = get(league, source="independent", horizon="ros")
    assert body["projection"]["basis"] == "espn" and body["projection"]["ours"]["available"] is False
    assert rows(body)[101]["projection"]["value"] == espn_ros(101)


# ============================================================================= missing team / league context


def test_no_league_row_is_unavailable(con):
    with pytest.raises(rk.RankingsUnavailable):
        get(con)


def test_unknown_current_week_is_unavailable(con):
    build(con)
    con.execute("UPDATE leagues SET current_week = NULL")
    with pytest.raises(rk.RankingsUnavailable):
        get(con)


def test_league_context_present_with_teams(league):
    ctx = get(league)["league_context"]
    assert ctx["available"] is True and ctx["league_name"] == "Test League"
    assert ctx["teams"] == 2 and ctx["my_team_id"] == MY_TEAM


def test_roster_labels_from_availability_status(league):
    r = rows(get(league))
    assert r[101]["roster"] == {"status": "ROSTERED", "fantasy_team_id": MY_TEAM, "fantasy_team": "My Squad", "is_mine": True}
    assert r[301]["roster"] == {"status": "ROSTERED", "fantasy_team_id": OTHER_TEAM, "fantasy_team": "Team Five",
                                "is_mine": False}
    assert r[102]["roster"]["status"] == "FREE_AGENT" and r[102]["roster"]["fantasy_team"] is None
    assert r[102]["roster"]["is_mine"] is False
    assert r[106]["roster"]["status"] == "WAIVERS"
    assert r[302]["roster"]["status"] == "UNKNOWN" and r[302]["roster"]["is_mine"] is False


def test_no_fantasy_teams_means_no_league_context_and_unknown_roster(con):
    build(con, teams=False)
    body = get(con)
    assert body["league_context"]["available"] is False
    r = rows(body)
    assert r[302]["roster"]["status"] == "UNKNOWN"
    assert all(p["roster"]["is_mine"] is False and p["roster"]["fantasy_team"] is None for p in body["players"])
    assert len(body["players"]) == POOL            # rankings still work without the roster context


def test_player_without_an_nfl_team_has_no_game_context_and_does_not_crash(league):
    golf = rows(get(league))[107]
    assert golf["pro_team"] is None
    assert golf["game"]["opponent"] is None and golf["game"]["on_bye"] is None and golf["game"]["is_home"] is None
    assert golf["projection"]["status"] == "ok" and golf["rank"]["overall"] == 6          # still ranked on his projection


def test_availability_combines_espn_and_the_nfl_report(league):
    r = rows(get(league))
    assert r[401]["availability"]["status"] == "QUESTIONABLE" and r[401]["availability"]["espn_injury_status"] == "QUESTIONABLE"
    assert r[402]["availability"]["status"] == "OUT" and r[402]["availability"]["nfl_report_status"] == "Out"
    assert r[101]["availability"]["status"] == "AVAILABLE" and r[101]["availability"]["weight"] == 1.0
    assert r[402]["projection"]["status"] == "ok"                  # availability is shown, it does not unrank him


def test_missing_injury_table_does_not_crash(con):
    build(con, injuries=False)
    r = rows(get(con))
    assert r[402]["availability"]["status"] == "AVAILABLE" and len(r) == POOL


# ============================================================================= other columns, links, why


def test_every_row_has_an_integer_espn_id_for_the_profile_link(league):
    body = get(league)
    assert len(body["players"]) == POOL
    for p in body["players"]:
        assert isinstance(p["espn_player_id"], int) and not isinstance(p["espn_player_id"], bool)
        assert p["full_name"] and p["position"] in ("QB", "RB", "WR", "TE")
    assert {p["espn_player_id"] for p in body["players"]} == set(SKILL)
    assert rows(body)[101]["gsis_id"] == "00-A101" and rows(body)[106]["gsis_id"] is None


def test_opportunity_production_consistency_trend_columns(league):
    alpha = rows(get(league))[101]
    assert alpha["opportunity"]["xfp_per_game"] == 15.2 and alpha["opportunity"]["basis"] == "xfp"
    assert alpha["opportunity"]["recent_per_game"] == 16.2 and alpha["opportunity"]["tier"] == "HIGH"
    assert alpha["production"]["ppr_per_game"] == 14.0 and alpha["production"]["sustainability"] == "ALIGNED"
    assert alpha["production"]["recent_ppr_per_game"] == 15.0 and alpha["production"]["fp_minus_xfp"] == -1.2
    assert alpha["role_trend"] == "UP" and alpha["games"] == 6
    assert alpha["consistency"]["games"] == 6
    assert alpha["consistency"]["ppr_sd"] is not None and 4.9 < alpha["consistency"]["ppr_sd"] < 5.6     # 10/20 alternating
    bravo = rows(get(league))[102]
    assert bravo["consistency"]["games"] == 3 and bravo["consistency"]["ppr_sd"] == 0.0     # the DNP row is not a game
    assert rows(get(league))[202]["consistency"]["ppr_sd"] is None


def test_why_is_one_to_three_data_derived_strings(league):
    for p in get(league)["players"]:
        assert 1 <= len(p["why"]) <= 3 and all(isinstance(s, str) and s for s in p["why"]), p["full_name"]


def test_nothing_is_written(league):
    tables = ("fantasy_players", "player_scores", "espn_player_weekly_points", "independent_projection_runs", "leagues")
    before = {t: league.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    for kw in ({}, {"horizon": "ros"}, {"source": "independent"}, {"source": "independent", "horizon": "ros"}):
        get(league, **kw)
    assert before == {t: league.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


# ============================================================================= sorting, parameter validation

TREND_ORDER = {"UP": 2, "FLAT": 1, "DOWN": 0}
ACCESSORS = {
    "rank": [lambda p: p["rank"]["overall"]],
    "name": [lambda p: p["full_name"].casefold()],
    "espn": [lambda p: p["projection"]["espn"]],
    "ours": [lambda p: p["projection"]["ours"]],
    "rating": [lambda p: p["rating"]["overall"]],
    "opportunity": [lambda p: p["rating"]["opportunity"]],
    "production": [lambda p: p["rating"]["production"]],
    "xfp": [lambda p: p["opportunity"]["xfp_per_game"]],
    "ppr": [lambda p: p["production"]["ppr_per_game"]],
    "consistency": [lambda p: p["consistency"]["ppr_sd"]],
    "trend": [lambda p: TREND_ORDER.get(p["role_trend"])],
}


def ordered(values, direction) -> bool:
    """Non-null values are monotonic in `direction` and every null comes after them."""
    present = [v for v in values if v is not None]
    if values[:len(present)] != present:
        return False
    return present == sorted(present, reverse=direction == "desc")


def test_every_sort_key_has_an_accessor():
    assert set(ACCESSORS) == set(rk.SORT_KEYS)


@pytest.mark.parametrize("key", sorted(ACCESSORS))
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_each_sort_key_sorts_in_both_directions_with_nulls_last(league, key, direction):
    body = get(league, source="independent", sort=key, dir=direction)
    assert body["sort"] == {"key": key, "dir": direction} and len(body["players"]) == POOL
    candidates = [[acc(p) for p in body["players"]] for acc in ACCESSORS[key]]
    meaningful = [vals for vals in candidates if len({v for v in vals if v is not None}) >= 2]
    assert meaningful, f"the synthetic league has no spread for sort={key}"
    assert any(ordered(vals, direction) for vals in meaningful), (key, direction, candidates)


@pytest.mark.parametrize("key,default", [("rank", "asc"), ("name", "asc"), ("espn", "desc"), ("rating", "desc"),
                                         ("xfp", "desc"), ("consistency", "asc"), ("trend", "desc")])
def test_default_direction(league, key, default):
    assert get(league, sort=key)["sort"] == {"key": key, "dir": default}
    assert get(league)["sort"] == {"key": "rank", "dir": "asc"}


def test_unranked_stay_last_when_rank_is_sorted_descending(league):
    body = get(league, sort="rank", dir="desc")
    assert [p["rank"]["overall"] for p in body["players"][:12]] == sorted(
        (p["rank"]["overall"] for p in body["players"][:12]), reverse=True)
    assert all(p["rank"]["overall"] is None for p in body["players"][12:])


def test_sort_ties_keep_rank_order(league):
    by_rank = {pid: i for i, pid in enumerate(ids(get(league)))}
    body = get(league, sort="trend", dir="desc")
    for a, b in zip(body["players"], body["players"][1:]):
        if a["role_trend"] == b["role_trend"]:
            assert by_rank[a["espn_player_id"]] < by_rank[b["espn_player_id"]]


def test_sorting_does_not_change_ranks(league):
    base = {pid: p["rank"] for pid, p in rows(get(league)).items()}
    for key in rk.SORT_KEYS:
        assert {pid: p["rank"] for pid, p in rows(get(league, sort=key)).items()} == base, key


@pytest.mark.parametrize("kw", [
    {"position": "K"}, {"position": "wr "}, {"horizon": "month"}, {"source": "vegas"}, {"status": "everyone"},
    {"sort": "bogus"}, {"dir": "sideways"}, {"limit": 0}, {"limit": 301}, {"limit": -5}, {"offset": -1},
])
def test_bad_parameters_are_value_errors(league, kw):
    with pytest.raises(ValueError):
        rk.build_rankings(league, **kw)


def test_limit_bounds_and_defaults(league):
    assert len(rk.build_rankings(league, limit=300)["players"]) == POOL
    assert len(rk.build_rankings(league, limit=1)["players"]) == 1
    assert rk.build_rankings(league)["counts"]["returned"] == POOL           # default limit 100


def test_position_and_source_are_accepted_case_insensitively_or_exactly(league):
    for pos in ("ALL", "QB", "RB", "WR", "TE"):
        assert rk.build_rankings(league, position=pos)["position"] == pos


# ============================================================================= efficiency


class Counting:
    """Counts execute() calls on a connection."""
    def __init__(self, con):
        self.con, self.n = con, 0

    def execute(self, *a, **k):
        self.n += 1
        return self.con.execute(*a, **k)

    def __getattr__(self, name):
        return getattr(self.con, name)


def queries(con, **kw) -> int:
    counting = Counting(con)
    body = rk.build_rankings(counting, limit=300, **kw)
    assert body["players"]
    return counting.n


@pytest.fixture(scope="module")
def small_and_large(tmp_path_factory):
    """The same league with 5 and with 60 extra generated players (built once; 12x the pool)."""
    root = tmp_path_factory.mktemp("rankings_sizes")
    cons = []
    for name, extra in (("small", 5), ("large", 60)):
        c = connect(root / f"{name}.duckdb")
        init_db(c)
        mp = pytest.MonkeyPatch()
        mp.setattr(ps, "_scoring", lambda con, season: ("fp", None))
        try:
            build(c, extra=extra)
        finally:
            mp.undo()
        cons.append(c)
    yield cons
    for c in cons:
        c.close()


@pytest.mark.parametrize("kw", [
    {}, {"horizon": "ros"}, {"source": "independent"}, {"source": "independent", "horizon": "ros"},
    {"position": "WR", "status": "available", "q": "gen", "sort": "consistency"},
])
def test_query_count_does_not_grow_with_the_number_of_players(small_and_large, kw):
    small, large = small_and_large
    n_small, n_large = queries(small, **kw), queries(large, **kw)
    assert n_small == n_large, (kw, n_small, n_large)
    assert n_large <= 40, n_large
    pool = lambda c: c.execute("SELECT count(*) FROM fantasy_players").fetchone()[0]    # noqa: E731
    assert pool(large) > 3 * pool(small) // 2


# ============================================================================= API


@pytest.fixture
def api(template_db, tmp_path, monkeypatch):
    path = tmp_path / "api.duckdb"
    shutil.copyfile(template_db, path)
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    return TestClient(app)


def test_rankings_route_returns_the_contract_shape(api):
    res = api.get("/api/rankings")
    assert res.status_code == 200
    body = res.json()
    for key in ("season", "week", "horizon", "source", "position", "sort", "window", "projection", "rating", "scoring",
                "league_context", "freshness", "counts", "notes", "players"):
        assert key in body, key
    assert body["horizon"] == "week" and body["source"] == "espn" and body["sort"] == {"key": "rank", "dir": "asc"}
    assert len(body["players"]) == POOL and body["counts"]["pool"] == POOL
    first = body["players"][0]
    assert first["espn_player_id"] == 301 and first["rank"]["position_label"] == "QB1"
    assert first["rating"] == {"overall": 95.0, "opportunity": 30.0, "production": 95.0}
    for key in ("projection", "opportunity", "production", "consistency", "availability", "game", "roster", "why"):
        assert key in first, key


def test_rankings_route_params(api):
    wr = api.get("/api/rankings", params={"position": "WR", "status": "available", "q": "bravo", "sort": "name",
                                          "dir": "desc", "limit": 5, "offset": 0}).json()
    assert [p["full_name"] for p in wr["players"]] == ["Bravo WR"] and wr["counts"]["matched"] == 1
    assert wr["players"][0]["rank"]["position_label"] == "WR2"
    ros = api.get("/api/rankings", params={"horizon": "ros"}).json()
    assert ros["window"]["weeks"] == ROS_WEEKS
    ours = api.get("/api/rankings", params={"source": "independent"}).json()
    assert ours["projection"]["basis"] == "ours" and ours["projection"]["ours"]["available"] is True
    paged = api.get("/api/rankings", params={"limit": 2, "offset": 2}).json()
    assert [p["espn_player_id"] for p in paged["players"]] == ids(get_from(api))[2:4]


def get_from(api):
    return api.get("/api/rankings", params={"limit": 300}).json()


@pytest.mark.parametrize("params", [
    {"position": "K"}, {"horizon": "month"}, {"source": "vegas"}, {"status": "everyone"}, {"sort": "bogus"},
    {"dir": "sideways"}, {"limit": 0}, {"limit": 301}, {"offset": -1}, {"limit": "abc"},
])
def test_rankings_route_rejects_bad_parameters_with_422(api, params):
    assert api.get("/api/rankings", params=params).status_code == 422


def test_rankings_route_is_503_when_the_league_is_not_synced(tmp_path, monkeypatch):
    path = tmp_path / "empty.duckdb"
    c = connect(path)
    init_db(c)
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    assert TestClient(app).get("/api/rankings").status_code == 503


def test_rankings_route_without_roster_context_still_200(tmp_path, monkeypatch):
    path = tmp_path / "noteams.duckdb"
    c = connect(path)
    init_db(c)
    build(c, teams=False)
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    res = TestClient(app).get("/api/rankings")
    assert res.status_code == 200 and res.json()["league_context"]["available"] is False
