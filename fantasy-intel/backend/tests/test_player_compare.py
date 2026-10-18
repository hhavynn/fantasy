"""Player Comparison Lab (analytics/player_compare.py, GET /api/compare, docs/player_comparison_lab.md).

Temp DuckDB only. The ESPN projections, schedule and Fantasy Intel runs come from test_forward_ours.populate (league
current_week 6, so completed weeks are 1..5; KC has a bye in week 9); this module adds usage rows, ESPN actuals,
scores, injuries and a few more players on top of it."""

import json

import pytest
from fastapi.testclient import TestClient

from app.analytics import player_compare as pc
from app.analytics import projection_source as ps
from app.db.connection import connect, init_db

from test_forward_ours import CURRENT, KC_BYE, SEASON, ours_value, populate

ACE, KICKER, ZED, QUINN, WALLY, GHOST, NOVA = 1, 2, 3, 4, 5, 6, 7
G = {ACE: "G-ACE", ZED: "G-ZED", QUINN: "G-QUINN", WALLY: "G-WALLY", NOVA: "G-NOVA"}
ZED_BYE_WEEK = 3          # BUF has no game in week 3 once the schedule is edited in world()


@pytest.fixture(autouse=True)
def fingerprint(monkeypatch):
    monkeypatch.setattr(ps, "_scoring", lambda con, season: ("fp", None))


# ------------------------------------------------------------------------------------------------ the world


def add_player(con, pid, name, pos, team, gsis, *, proj=True, injury=None):
    con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, injury_status) "
                "VALUES (?, ?, ?, ?, ?)", [pid, name, pos, team, injury])
    con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence) "
                "VALUES (?, ?, 'espn_id:nfl_players', 'high')", [pid, gsis])
    if proj:
        for w in range(CURRENT, 18):
            con.execute("INSERT INTO espn_player_weekly_points VALUES (?, ?, ?, NULL, ?, '2026-10-14 00:00:00')",
                        [SEASON, pid, w, 5.0 + pid])


def actuals(con, pid, by_week):
    for w, pts in by_week.items():
        con.execute("""INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points,
                       synced_at) VALUES (?, ?, ?, ?, '2026-10-14 00:00:00')
                       ON CONFLICT (season, espn_player_id, scoring_period) DO UPDATE SET actual_points = excluded.actual_points""",
                    [SEASON, pid, w, pts])


def usage(con, gsis, week, pos, team, opp, *, snaps=50, pct=0.8, targets=0, tshare=None, air=None, rush=0,
          xfp=None, ppr=None):
    con.execute("""INSERT INTO player_week_usage (season, week, gsis_id, team, opponent, position, offense_snaps,
                   offense_pct, targets, target_share, air_yards, rush_attempts, opportunities, fantasy_points_ppr,
                   xfp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [SEASON, week, gsis, team, opp, pos, snaps, pct, targets, tshare, air, rush, targets + rush, ppr, xfp])


def score(con, gsis, pid, pos, *, as_of=5, opp=8.0, prod=7.0, trend=0.5, label="UP", basis="xfp", tier="HIGH",
          role="UP", trends=None):
    comps = {"trends": trends if trends is not None else [
        {"metric": "targets", "label": "UP", "baseline_avg": 6.0, "recent_avg": 9.0, "delta": 3.0, "threshold": 1.5,
         "recent_weeks": [4, 5], "baseline_weeks": [1, 2, 3]},
        {"metric": "offense_pct", "label": "DOWN", "baseline_avg": 0.9, "recent_avg": 0.7, "delta": -0.2,
         "threshold": 0.08, "recent_weeks": [4, 5], "baseline_weeks": [1, 2, 3]},
        {"metric": "xfp", "label": "FLAT", "baseline_avg": 14.0, "recent_avg": 14.5, "delta": 0.5, "threshold": 2.0,
         "recent_weeks": [4, 5], "baseline_weeks": [1, 2, 3]}]}
    con.execute("""INSERT INTO player_scores (season, gsis_id, espn_player_id, position, as_of_week, games,
                   opportunity_basis, opportunity_tier, role_trend, opportunity_score, production_score,
                   usage_trend_score, trend_label, components) VALUES (?, ?, ?, ?, ?, 5, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [SEASON, gsis, pid, pos, as_of, basis, tier, role, opp, prod, trend, label, json.dumps(comps)])


def world(con, *, run_weeks=range(CURRENT, 15), skip=None):
    """Ace WR (KC), Kicker K (KC), Zed RB (BUF) from populate, plus Quinn QB, Wally WR (BUF), Ghost WR (KC, no
    nflverse match) and Nova WR (KC, usage but no ESPN projections). BUF is on bye in week 3."""
    populate(con, run_weeks=run_weeks, skip=skip)
    con.execute("UPDATE nfl_schedules SET home_team = 'DEN', away_team = 'KC' WHERE week = ?", [ZED_BYE_WEEK])
    for pid, gsis in ((ACE, G[ACE]), (ZED, G[ZED])):
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence) "
                    "VALUES (?, ?, 'espn_id:nfl_players', 'high')", [pid, gsis])
    add_player(con, QUINN, "Quinn QB", "QB", "BUF", G[QUINN])
    add_player(con, WALLY, "Wally WR", "WR", "BUF", G[WALLY])
    add_player(con, NOVA, "Nova WR", "WR", "KC", G[NOVA], proj=False)
    add_player(con, GHOST, "Ghost WR", "WR", "KC", None)
    con.execute("UPDATE player_id_map SET gsis_id = NULL, match_method = 'unmatched', match_confidence = 'none' "
                "WHERE espn_player_id = ?", [GHOST])
    for i, w in enumerate(range(1, 6)):          # Ace: five games, KC plays every week 1-5
        usage(con, G[ACE], w, "WR", "KC", "BUF", pct=[.9, .9, .85, .9, .95][i], targets=[8, 9, 7, 10, 8][i],
              tshare=[.25, .27, .22, .30, .26][i], air=[90, 100, 80, 110, 95][i], xfp=[14, 16, 12, 18, 15][i],
              ppr=[15, 20, 9, 22, 18][i])
        usage(con, G[NOVA], w, "WR", "KC", "BUF", pct=.7, targets=5, tshare=.2, air=60, xfp=9, ppr=8)
    actuals(con, ACE, {1: 14.0, 2: 18.5, 3: 9.0, 4: 21.0, 5: 17.5})
    zed_weeks = [1, 2, 4, 5]                      # no game in week 3 (bye)
    for i, w in enumerate(zed_weeks):
        usage(con, G[ZED], w, "RB", "BUF", "KC", pct=[.7, .65, .6, .7][i], targets=[3, 4, 2, 3][i],
              tshare=[.1, .12, .08, .1][i], rush=[15, 18, 14, 16][i], xfp=[12, 13, 10, 12][i], ppr=[13, 15, 8, 11][i])
        usage(con, G[QUINN], w, "QB", "BUF", "KC", pct=1.0, rush=[4, 5, 3, 6][i], xfp=[18, 20, 17, 19][i],
              ppr=[20, 22, 15, 21][i])
        usage(con, G[WALLY], w, "WR", "BUF", "KC", pct=.6, targets=[5, 6, 4, 5][i], tshare=.18,
              air=[50, 60, 40, 50][i], xfp=[9, 10, 8, 9][i], ppr=[8, 12, 6, 9][i])
    actuals(con, ZED, {1: 12.0, 2: 14.0, ZED_BYE_WEEK: 0.0, 4: 8.0, 5: 10.0})
    actuals(con, QUINN, {1: 19.0, 2: 21.0, ZED_BYE_WEEK: 0.0, 4: 14.0, 5: 20.0})
    actuals(con, WALLY, {1: 8.0, 2: 12.0, ZED_BYE_WEEK: 0.0, 4: 6.0, 5: 9.0})
    score(con, G[ACE], ACE, "WR")
    score(con, G[WALLY], WALLY, "WR", opp=6.0, prod=5.0, trend=0.1, label="FLAT", role="FLAT", tier="MEDIUM")
    score(con, G[QUINN], QUINN, "QB", opp=9.0, prod=8.0, trend=-0.3, label="DOWN", role="DOWN")
    score(con, G[ZED], ZED, "RB", opp=7.0, prod=6.0, trend=0.4, label="UP", role="UP")


def set_scoring(con, reception=1.0):
    con.execute("UPDATE leagues SET reception_points = ?, scoring_type = 'PPR', "
                "scoring_items = '[{\"statId\": 53, \"points\": 1}]'", [reception])


def add_100_scale(con):
    con.execute("ALTER TABLE player_scores ADD COLUMN IF NOT EXISTS opportunity_score_100 DOUBLE")
    con.execute("ALTER TABLE player_scores ADD COLUMN IF NOT EXISTS production_score_100 DOUBLE")
    con.execute("ALTER TABLE player_scores ADD COLUMN IF NOT EXISTS rating_scale_version VARCHAR")


def row(out, key):
    return next(r for r in out["rows"] if r["key"] == key)


def card(out, side="a"):
    return out["players"][0 if side == "a" else 1]


def metric(out, key, side="a"):
    return card(out, side)["usage"]["metrics"][key]


@pytest.fixture
def w(con):
    world(con)
    return con


# ------------------------------------------------------------------------------ 1. identical player twice


def test_identical_player_selected_twice(w):
    out = pc.compare(w, ACE, ACE, "espn")
    assert out["identical"] is True and out["same_position"] is True
    assert out["players"][0] == out["players"][1]
    assert out["rows"] and all(r["edge"] == "n/a" for r in out["rows"])
    assert all(r["note"] == pc.SAME_PLAYER_NOTE for r in out["rows"])
    assert len(out["summary"]) == 1
    f = out["summary"][0]
    assert f["leader"] == "even" and "twice" in f["text"] and "Ace WR" in f["text"]
    # independent adds its rows, still all n/a
    ind = pc.compare(w, ACE, ACE, "independent")
    assert {"next_week_ours", "ros_ours"} <= {r["key"] for r in ind["rows"]}
    assert all(r["edge"] == "n/a" for r in ind["rows"])


# --------------------------------------------------------------------------------- 2. different positions


def test_different_positions_mark_rows_not_applicable(w):
    out = pc.compare(w, QUINN, ACE, "espn")          # QB vs WR
    assert out["same_position"] is False
    qb = card(out, "a")["usage"]["metrics"]
    for key in ("targets_pg", "target_share", "air_yards_pg"):
        assert qb[key] == {"value": None, "applicable": False, "why": pc.WHY_QB_RECEIVING}
    assert qb["carries_pg"]["applicable"] and qb["carries_pg"]["value"] == 4.5
    wr = card(out, "b")["usage"]["metrics"]
    assert wr["carries_pg"] == {"value": None, "applicable": False, "why": pc.WHY_REC_CARRIES}
    assert row(out, "targets_pg")["a"] is None and row(out, "targets_pg")["edge"] == "n/a"
    # the row names who it does not apply to; the reason itself lives once on the card's metric
    assert row(out, "target_share")["note"] == "Not applicable for Quinn QB (QB)"
    assert row(out, "carries_pg")["edge"] == "n/a"
    for key in ("opportunity_rating", "production_rating"):
        r = row(out, key)
        assert r["edge"] == "n/a" and pc.POSITION_RELATIVE_NOTE in r["note"]
        assert r["a"] is not None and r["b"] is not None     # values are shown, just not ranked
    # snap share is valid for both positions and still comparable
    assert row(out, "snap_share")["edge"] == "a"


def test_rb_air_yards_and_kicker_rows(w):
    out = pc.compare(w, ZED, KICKER, "espn")
    assert metric(out, "air_yards_pg")["why"] == pc.WHY_RB_AIR_YARDS
    k = card(out, "b")["usage"]["metrics"]
    assert all(not k[key]["applicable"] and k[key]["why"] == pc.WHY_NO_USAGE_DATA
               for key in k if key != "league_pts_pg")
    assert k["league_pts_pg"]["applicable"]
    assert card(out, "b")["usage"]["games"] == 0


# ---------------------------------------------------------------------------------- 3. missing source data


def test_unmatched_player_has_no_usage_but_nulls_not_zeros(w):
    out = pc.compare(w, GHOST, ACE, "espn")
    ghost = card(out, "a")
    assert ghost["usage"]["games"] == 0 and ghost["usage"]["weeks"] == [] and ghost["weekly"] == []
    assert ghost["recent_games"] == [] and ghost["usage"]["stability"] == {"cv": None, "games": 0, "metric": None}
    for key in ("snap_share", "targets_pg", "xfp_pg", "ppr_pg"):
        assert metric(out, key) == {"value": None, "applicable": True, "why": pc.WHY_NO_RECORD}
        assert row(out, key)["a"] is None and row(out, key)["edge"] == "n/a"
    assert metric(out, "league_pts_pg")["value"] is None
    assert ghost["usage"]["trend"] == {"label": None, "score": None}
    assert ghost["usage"]["ratings"]["opportunity"] is None
    assert any("Ghost WR" in x and "no nflverse match" in x for x in out["limitations"])
    assert out["summary"][1]["leader"] is None and out["summary"][3]["leader"] is None


def test_player_without_espn_projections_gets_nulls_and_limitations(w):
    out = pc.compare(w, NOVA, ACE, "espn")
    proj = card(out, "a")["projection"]
    assert proj["next_week"]["espn"] is None and proj["ros"]["espn"] is None
    assert proj["ros"]["espn_weeks_available"] == 0 and proj["ros"]["espn_weeks_expected"] == 12
    assert all(wk["espn"] is None for wk in proj["weeks"])
    assert row(out, "next_week_espn")["a"] is None and row(out, "next_week_espn")["edge"] == "n/a"
    assert row(out, "ros_espn")["a"] is None and row(out, "ros_espn")["edge"] == "n/a"
    assert any("Nova WR" in x and "no projection for any remaining week" in x for x in out["limitations"])
    assert out["summary"][0]["leader"] is None and "no ESPN projection" in out["summary"][0]["text"]
    assert out["summary"][2]["leader"] is None


def test_independent_without_runs_says_ours_unavailable(con):
    world(con, run_weeks=())
    out = pc.compare(con, ACE, ZED, "independent")
    for s in (card(out, "a"), card(out, "b")):
        ros = s["projection"]["ros"]
        assert ros["ours_available"] is False and ros["ours"] is None and ros["ours_reason"]
        assert "No ifp-2.0.0 projection run is stored" in ros["ours_reason"]
        nw = s["projection"]["next_week"]
        assert nw["ours"] is None and nw["ours_origin"] is None
    assert row(out, "next_week_ours")["edge"] == "n/a" and row(out, "ros_ours")["edge"] == "n/a"
    # the run-level reason is the same for both players, so it is listed once, not per player
    ours_lines = [x for x in out["limitations"] if x.startswith("Fantasy Intel projections")]
    assert ours_lines == [f"Fantasy Intel projections: {card(out, 'a')['projection']['ros']['ours_reason']}"]
    # ESPN is untouched
    assert row(out, "next_week_espn")["a"] == 6.0


def test_source_espn_has_no_ours_fields_or_rows(w):
    out = pc.compare(w, ACE, ZED, "espn")
    assert "next_week_ours" not in {r["key"] for r in out["rows"]} and "ros_ours" not in {r["key"] for r in out["rows"]}
    p = card(out)["projection"]
    assert p["next_week"]["ours_origin"] is None and p["ros"]["ours_available"] is None
    assert all(wk["ours"] is None and wk["ours_origin"] is None for wk in p["weeks"])


# ------------------------------------------------------------------------------ 4. missing scoring settings


def test_missing_scoring_settings_label_and_http_200(tmp_path, monkeypatch):
    path = tmp_path / "scoring.duckdb"
    c = connect(path)
    init_db(c)
    world(c)
    out = pc.compare(c, ACE, ZED)
    c.close()
    assert out["scoring"]["settings_synced"] is False
    assert out["scoring"]["label"] == "League scoring (settings not synced)"
    assert out["scoring"]["reception_points"] is None
    assert "nflverse PPR" in out["scoring"]["note"] and "full PPR" in out["scoring"]["note"]
    assert any("scoring settings are not synced" in x for x in out["limitations"])
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    resp = TestClient(app).get("/api/compare", params={"a": ACE, "b": ZED})
    assert resp.status_code == 200
    assert resp.json()["scoring"]["label"] == "League scoring (settings not synced)"


def test_null_reception_points_with_items_is_not_synced(w):
    w.execute("UPDATE leagues SET scoring_items = '[{\"statId\": 53}]', reception_points = NULL")
    assert pc.compare(w, ACE, ZED)["scoring"]["settings_synced"] is False
    w.execute("UPDATE leagues SET scoring_items = NULL, reception_points = 1.0")
    assert pc.compare(w, ACE, ZED)["scoring"]["settings_synced"] is False


def test_scoring_label_variants():
    assert pc.scoring_label(1.0) == "PPR (1 pt per reception)"
    assert pc.scoring_label(0.5) == "Half PPR (0.5 pt per reception)"
    assert pc.scoring_label(0) == "Standard (no reception points)"
    assert pc.scoring_label(0.25) == "0.25 pt per reception"
    assert pc.scoring_label(1.5) == "1.5 pt per reception"
    assert pc.scoring_label(None) == "League scoring (settings not synced)"
    assert pc.scoring_label(1.0, synced=False) == "League scoring (settings not synced)"


def test_synced_scoring_block(w):
    set_scoring(w, 0.5)
    s = pc.compare(w, ACE, ZED)["scoring"]
    assert s == {"scoring_type": "PPR", "reception_points": 0.5, "settings_synced": True,
                 "label": "Half PPR (0.5 pt per reception)", "note": s["note"]}
    assert "always full PPR" in s["note"]


# ----------------------------------------------------------------------------------------- 5. bye weeks


def test_bye_week_in_remaining_window(w):
    out = pc.compare(w, ACE, ZED, "espn")
    ace = card(out, "a")["projection"]
    assert ace["bye_weeks"] == [KC_BYE] and ace["bye_weeks_remaining"] == 1
    assert ace["games_remaining"] == 11 and card(out, "b")["projection"]["games_remaining"] == 12
    wk9 = next(x for x in ace["weeks"] if x["week"] == KC_BYE)
    assert wk9["on_bye"] is True and wk9["espn"] is None and wk9["opponent"] is None      # stored 6.0, but a bye
    assert next(x for x in ace["weeks"] if x["week"] == KC_BYE + 1)["on_bye"] is False
    g = row(out, "games_remaining")
    assert (g["a"], g["b"], g["edge"], g["unit"]) == (11, 12, "b", "games")


def test_bye_week_never_a_zero_in_league_points_or_weekly(w):
    out = pc.compare(w, ZED, ACE, "espn")
    # Zed's ESPN week-3 score is a stored 0.0 on BUF's bye: excluded, so 12 + 14 + 8 + 10 over 4 games
    assert metric(out, "league_pts_pg")["value"] == 11.0
    assert [x["week"] for x in card(out, "a")["weekly"]] == [1, 2, 4, 5]
    assert card(out, "a")["usage"]["weeks"] == [1, 2, 4, 5]
    assert card(out, "a")["usage"]["games"] == 4


def test_next_week_on_bye_is_reported_as_bye_not_zero(w):
    w.execute("UPDATE leagues SET current_week = ?", [KC_BYE])
    w.execute("UPDATE espn_player_weekly_points SET projected_points = 0.0 WHERE espn_player_id = ? AND scoring_period = ?",
              [ACE, KC_BYE])
    out = pc.compare(w, ACE, ZED, "independent")
    nw = card(out, "a")["projection"]["next_week"]
    assert nw["on_bye"] is True and nw["espn"] is None
    assert nw["ours"] is None and nw["ours_origin"] == "bye"            # the run's 0.0 for a bye is not shown as a number
    assert next(e for e in card(out, "a")["projection"]["weeks"] if e["week"] == KC_BYE)["ours"] is None
    assert row(out, "next_week_espn")["a"] is None and row(out, "next_week_espn")["edge"] == "n/a"
    assert "on bye" in row(out, "next_week_espn")["note"]
    f = out["summary"][0]
    assert f["leader"] is None and "on bye" in f["text"] and "Ace WR" in f["text"]
    assert card(out, "a")["projection"]["games_remaining"] == 17 - KC_BYE      # weeks 10..17; the bye week is excluded


# ----------------------------------------------------------------------------------------- 6. horizons


def test_next_week_ours_is_the_horizon_zero_value(w):
    out = pc.compare(w, ACE, ZED, "independent")
    nw = card(out, "a")["projection"]["next_week"]
    assert nw["ours"] == ours_value(CURRENT, ACE) and nw["ours_origin"] == "ours" and nw["ours_horizon"] == 0
    nb = card(out, "b")["projection"]["next_week"]
    assert nb["ours"] == ours_value(CURRENT, ZED) and nb["ours_horizon"] == 0
    r = row(out, "next_week_ours")
    assert (r["a"], r["b"]) == (ours_value(CURRENT, ACE), ours_value(CURRENT, ZED))
    weeks = {x["week"]: x for x in card(out, "a")["projection"]["weeks"]}
    assert weeks[7]["ours"] == ours_value(7, ACE) and weeks[7]["ours_origin"] == "ours"
    assert weeks[KC_BYE]["ours"] is None and weeks[KC_BYE]["ours_origin"] == "bye"           # a bye is a bye, not a 0
    assert weeks[15]["ours"] is None and weeks[15]["ours_origin"] is None                    # playoffs: no independent series


def test_ros_ours_is_experimental_and_complete_only(con):
    world(con, run_weeks=range(CURRENT, 15))
    out = pc.compare(con, ACE, ZED, "independent")
    ros = card(out, "a")["projection"]["ros"]
    assert ros["ours_experimental"] is True and ros["ours_available"] is True
    assert ros["ours"] == round(sum(ours_value(wk, ACE) for wk in range(6, 15) if wk != KC_BYE), 2)
    assert ros["ours_weeks_available"] == ros["ours_weeks_expected"] == 8
    assert row(out, "ros_ours")["note"] == "Remaining regular-season weeks only"


def test_ros_ours_null_when_a_week_has_no_run(con):
    world(con, run_weeks=range(CURRENT, 12))                  # weeks 12-14 have no run: ESPN fallback weeks
    out = pc.compare(con, ACE, ZED, "independent")
    ros = card(out, "a")["projection"]["ros"]
    assert ros["ours"] is None and ros["ours_available"] is True
    assert ros["ours_weeks_available"] < ros["ours_weeks_expected"]
    assert ros["ours_reason"] and "ESPN" in ros["ours_reason"]
    assert row(out, "ros_ours")["edge"] == "n/a" and row(out, "ros_ours")["a"] is None
    assert card(out, "a")["projection"]["next_week"]["ours"] == ours_value(CURRENT, ACE)    # next week is still fine
    assert row(out, "ros_espn")["a"] is not None                                              # ESPN totals unaffected


def test_espn_fallback_for_the_as_of_week_gives_no_ours(con):
    world(con, skip={CURRENT: [ACE]})                         # the week-6 run has no row for Ace
    out = pc.compare(con, ACE, ZED, "independent")
    nw = card(out, "a")["projection"]["next_week"]
    assert nw["ours"] is None and nw["ours_origin"] == "espn"
    assert nw["espn"] == 6.0                                  # ESPN's own number stays under `espn`
    r = row(out, "next_week_ours")
    assert r["a"] is None and r["edge"] == "n/a" and "ESPN's number is not shown" in r["note"]
    assert card(out, "b")["projection"]["next_week"]["ours"] == ours_value(CURRENT, ZED)


def test_older_horizon_for_the_as_of_week_is_flagged(con):
    world(con, run_weeks=())
    from test_forward_ours import add_run
    add_run(con, CURRENT, horizon=2)                          # the only week-6 run was made two weeks earlier
    out = pc.compare(con, ACE, ZED, "independent")
    nw = card(out, "a")["projection"]["next_week"]
    assert nw["ours_horizon"] == 2
    assert any("2 week(s) earlier" in x for x in out["limitations"])


# ------------------------------------------------------------------------------------------ 7. invalid ids


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = tmp_path / "compare.duckdb"
    c = connect(path)
    init_db(c)
    world(c)
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    return TestClient(app)


def test_invalid_ids_and_source(api):
    ok = api.get("/api/compare", params={"a": ACE, "b": ZED})
    assert ok.status_code == 200 and ok.json()["source"] == "espn"
    assert api.get("/api/compare", params={"a": "x", "b": ZED}).status_code == 422
    assert api.get("/api/compare", params={"a": ACE, "b": "1.5"}).status_code == 422
    assert api.get("/api/compare", params={"a": ACE}).status_code == 422
    unknown_a = api.get("/api/compare", params={"a": 999, "b": ZED})
    assert unknown_a.status_code == 404 and unknown_a.json()["detail"] == "Unknown ESPN player id 999 (a)"
    unknown_b = api.get("/api/compare", params={"a": ACE, "b": 998})
    assert unknown_b.status_code == 404 and unknown_b.json()["detail"] == "Unknown ESPN player id 998 (b)"
    both = api.get("/api/compare", params={"a": 999, "b": 998})
    assert both.json()["detail"] == "Unknown ESPN player id 999 (a)"            # a is checked first
    assert api.get("/api/compare", params={"a": ACE, "b": ZED, "source": "vegas"}).status_code == 422
    assert api.get("/api/compare", params={"a": ACE, "b": ZED, "source": "independent"}).json()["source"] == "independent"
    same = api.get("/api/compare", params={"a": ACE, "b": ACE})
    assert same.status_code == 200 and same.json()["identical"] is True


def test_no_league_is_503(tmp_path, monkeypatch):
    path = tmp_path / "empty.duckdb"
    c = connect(path)
    init_db(c)
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    assert TestClient(app).get("/api/compare", params={"a": 1, "b": 2}).status_code == 503


def test_compare_raises_typed_errors(w):
    with pytest.raises(pc.UnknownPlayer) as err:
        pc.compare(w, 999, 998)
    assert (err.value.which, err.value.pid) == ("a", 999)
    with pytest.raises(pc.UnknownPlayer) as err:
        pc.compare(w, ACE, 998)
    assert err.value.which == "b"
    with pytest.raises(ValueError):
        pc.compare(w, ACE, ZED, "vegas")


# ------------------------------------------------------------------------------- 8. units and scale labels


UNITS = {"next_week_espn": "pts", "next_week_ours": "pts", "ros_espn": "pts", "ros_ours": "pts",
         "games_remaining": "games", "snap_share": "share", "carries_pg": "per_game", "targets_pg": "per_game",
         "target_share": "share", "air_yards_pg": "yards_per_game", "xfp_pg": "pts_per_game", "ppr_pg": "pts_per_game",
         "league_pts_pg": "pts_per_game", "trend_score": "trend", "opportunity_rating": "rating",
         "production_rating": "rating"}


def test_row_order_units_groups_and_share_range(w):
    out = pc.compare(w, ACE, WALLY, "independent")
    assert [r["key"] for r in out["rows"]] == list(UNITS)
    assert {r["key"]: r["unit"] for r in out["rows"]} == UNITS
    groups = {r["key"]: r["group"] for r in out["rows"]}
    assert groups["next_week_espn"] == groups["games_remaining"] == "projection"
    assert groups["snap_share"] == groups["trend_score"] == "usage" and groups["opportunity_rating"] == "rating"
    for key in ("snap_share", "target_share"):                # shares are fractions, never percentages
        r = row(out, key)
        assert 0 <= r["a"] <= 1 and 0 <= r["b"] <= 1
    assert row(out, "snap_share")["a"] == 0.9 and row(out, "target_share")["a"] == 0.26
    assert row(out, "air_yards_pg")["a"] == 95.0 and row(out, "targets_pg")["a"] == 8.4
    assert row(out, "xfp_pg")["a"] == 15.0 and row(out, "ppr_pg")["a"] == 16.8
    assert row(out, "league_pts_pg")["a"] == 16.0 and row(out, "league_pts_pg")["b"] == 8.75
    espn_only = pc.compare(w, ACE, WALLY, "espn")
    assert [r["key"] for r in espn_only["rows"]] == [k for k in UNITS if not k.endswith("_ours")]
    assert [s["key"] for s in espn_only["summary"]] == ["next_week", "stability", "ros", "trend"]


def test_legacy_ratings_are_flagged_and_never_rescaled(w):
    out = pc.compare(w, ACE, WALLY)
    r = card(out, "a")["usage"]["ratings"]
    assert r["opportunity"] == {"value": 8.0, "scale": 10, "legacy": True}
    assert r["production"] == {"value": 7.0, "scale": 10, "legacy": True}
    assert r["version"] is None and r["basis"] == "xfp" and r["as_of_week"] == 5
    for key in ("opportunity_rating", "production_rating"):
        x = row(out, key)
        assert x["edge"] == "n/a" and "Legacy" in x["note"] and x["a"] in (8.0, 7.0)     # not multiplied by 10
    assert any("legacy 0-10" in x for x in out["limitations"])


def test_hundred_scale_ratings_compare_and_do_not_mix_with_legacy(w):
    add_100_scale(w)
    w.execute("""UPDATE player_scores SET opportunity_score_100 = 82.0, production_score_100 = 71.0,
                 rating_scale_version = 'ratings-100.v1' WHERE gsis_id = ?""", [G[ACE]])
    out = pc.compare(w, ACE, WALLY)                           # Wally is still legacy only
    assert card(out, "a")["usage"]["ratings"]["opportunity"] == {"value": 82.0, "scale": 100, "legacy": False}
    assert card(out, "a")["usage"]["ratings"]["version"] == "ratings-100.v1"
    assert card(out, "b")["usage"]["ratings"]["opportunity"] == {"value": 6.0, "scale": 10, "legacy": True}
    assert row(out, "opportunity_rating")["edge"] == "n/a" and "Legacy" in row(out, "opportunity_rating")["note"]
    # both on 0-100: an edge only past 5 points
    w.execute("""UPDATE player_scores SET opportunity_score_100 = 70.0, production_score_100 = 68.0,
                 rating_scale_version = 'ratings-100.v1' WHERE gsis_id = ?""", [G[WALLY]])
    out = pc.compare(w, ACE, WALLY)
    assert (row(out, "opportunity_rating")["a"], row(out, "opportunity_rating")["b"]) == (82.0, 70.0)
    assert row(out, "opportunity_rating")["edge"] == "a"
    assert row(out, "production_rating")["edge"] == "even"    # 71 vs 68
    # different opportunity bases are not compared
    w.execute("UPDATE player_scores SET opportunity_basis = 'targets+carries' WHERE gsis_id = ?", [G[WALLY]])
    out = pc.compare(w, ACE, WALLY)
    assert row(out, "opportunity_rating")["edge"] == "n/a" and "bases differ" in row(out, "opportunity_rating")["note"]
    assert row(out, "production_rating")["edge"] == "even"


# -------------------------------------------------------------------------------- 9. no postgame leakage


def test_current_week_actuals_and_usage_do_not_leak(w):
    before = pc.compare(w, ACE, ZED, "independent")
    # the Thursday game: Ace has already played week 6 (the as-of week); Zed has not
    actuals(w, ACE, {CURRENT: 99.0})
    usage(w, G[ACE], CURRENT, "WR", "KC", "BUF", pct=1.0, targets=30, tshare=.9, air=400, xfp=40.0, ppr=80.0)
    after = pc.compare(w, ACE, ZED, "independent")
    assert after == before
    a = card(after, "a")
    assert a["usage"]["games"] == 5 and a["usage"]["weeks"] == [1, 2, 3, 4, 5]
    assert metric(after, "league_pts_pg")["value"] == 16.0 and metric(after, "ppr_pg")["value"] == 16.8
    assert metric(after, "targets_pg")["value"] == 8.4
    assert [x["week"] for x in a["weekly"]] == [1, 2, 3, 4, 5]
    assert [x["week"] for x in a["recent_games"]] == [5, 4, 3, 2]
    assert a["usage"]["stability"] == before["players"][0]["usage"]["stability"]
    nw = a["projection"]["next_week"]
    assert nw["espn"] == 6.0 and nw["espn"] != 99.0           # the stored pregame projection, never the actual
    assert after["completed_through_week"] == 5 and after["as_of_week"] == 6


def test_stale_ratings_through_the_current_week_are_called_out(w):
    w.execute("UPDATE player_scores SET as_of_week = ? WHERE gsis_id = ?", [CURRENT, G[ACE]])
    out = pc.compare(w, ACE, ZED)
    assert any("Ace WR" in x and "includes games from the current week" in x for x in out["limitations"])
    clean = pc.compare(w, ZED, WALLY)
    assert not any("includes games from the current week" in x for x in clean["limitations"])


# --------------------------------------------------------------------------- card content: recent, role


def test_weekly_recent_games_and_stability(w):
    out = pc.compare(w, ACE, ZED)
    a = card(out, "a")
    assert [x["week"] for x in a["weekly"]] == [1, 2, 3, 4, 5]
    assert a["weekly"][0] == {"week": 1, "opponent": "BUF", "league_points": 14.0, "ppr": 15.0, "xfp": 14.0,
                              "snap_share": 0.9, "carries": 0, "targets": 8}
    assert [x["week"] for x in a["recent_games"]] == [5, 4, 3, 2]          # last four, newest first
    st = a["usage"]["stability"]
    assert st["metric"] == "xfp" and st["games"] == 5
    assert st["cv"] == pytest.approx(pc.coefficient_of_variation([14, 16, 12, 18, 15]), abs=1e-4) == pytest.approx(0.1491, abs=1e-3)
    b = card(out, "b")["usage"]["stability"]
    assert b["metric"] == "xfp" and b["games"] == 4


def test_role_evidence_keeps_only_up_and_down(w):
    sa = card(pc.compare(w, ACE, ZED), "a")["role"]
    assert sa["role_trend"] == "UP" and sa["opportunity_tier"] == "HIGH"
    assert [(e["metric"], e["label"], e["unit"]) for e in sa["evidence"]] == [
        ("targets", "UP", "per_game"), ("offense_pct", "DOWN", "pct")]
    assert sa["evidence"][0]["recent_weeks"] == [4, 5] and sa["evidence"][0]["delta"] == 3.0
    assert sa["depth_chart"] is None


def test_depth_chart_is_included_when_present(w):
    w.execute("""CREATE TABLE nfl_depth_charts (season INT, gsis_id VARCHAR, dt TIMESTAMP, team VARCHAR,
                 pos_abb VARCHAR, pos_rank INT)""")
    w.execute("INSERT INTO nfl_depth_charts VALUES (?, ?, '2026-10-12 00:00:00', 'KC', 'WR', 1)", [SEASON, G[ACE]])
    d = card(pc.compare(w, ACE, ZED), "a")["role"]["depth_chart"]
    assert d == {"pos_abb": "WR", "pos_rank": 1, "dt": "2026-10-12T00:00:00", "team": "KC"}


# -------------------------------------------------------------------------------------- availability


def test_availability_never_claims_healthy(w):
    out = pc.compare(w, ACE, ZED)
    for s in (card(out, "a"), card(out, "b")):
        av = s["availability"]
        assert av == {"espn_status": None, "has_designation": False, "nfl_report": None, "report_is_current": None,
                      "label": "No designation reported", "note": pc.NO_DESIGNATION_NOTE}
        text = (av["label"] + av["note"]).lower()
        assert "healthy" not in text and "available" not in text and "not confirmation" in text


def test_availability_combines_espn_and_current_nfl_report(w):
    w.execute("""CREATE TABLE nfl_injuries (season INT, week INT, gsis_id VARCHAR, report_status VARCHAR,
                 report_primary_injury VARCHAR, practice_status VARCHAR)""")
    w.execute("INSERT INTO nfl_injuries VALUES (?, 6, ?, 'Questionable', 'Hamstring', 'Limited Participation')",
              [SEASON, G[ACE]])
    w.execute("INSERT INTO nfl_injuries VALUES (?, 4, ?, 'Out', 'Ankle', 'Did Not Participate')", [SEASON, G[ZED]])
    w.execute("UPDATE fantasy_players SET injury_status = 'QUESTIONABLE' WHERE espn_player_id = ?", [ACE])
    w.execute("UPDATE fantasy_players SET injury_status = 'ACTIVE' WHERE espn_player_id = ?", [ZED])
    out = pc.compare(w, ACE, ZED)
    a = card(out, "a")["availability"]
    assert a["has_designation"] and a["report_is_current"] is True
    assert a["label"] == "Questionable (ESPN and NFL report, week 6)"
    assert a["nfl_report"] == {"week": 6, "report_status": "Questionable", "report_primary_injury": "Hamstring",
                               "practice_status": "Limited Participation"}
    b = card(out, "b")["availability"]       # ESPN ACTIVE and only a stale week-4 report: still no current designation
    assert b["has_designation"] is False and b["report_is_current"] is False
    assert b["label"] == "No designation reported" and "week 4" in b["note"] and "not confirmation" in b["note"]


def test_availability_block_severity_and_sources():
    esp = pc.availability_block("OUT", None, 7)
    assert esp["label"] == "Out (ESPN)" and esp["has_designation"]
    both = pc.availability_block("QUESTIONABLE", {"week": 7, "report_status": "Out", "report_primary_injury": None,
                                                  "practice_status": None}, 7)
    assert both["label"] == "Out (NFL report, week 7); also Questionable (ESPN)"
    assert not pc.availability_block("NORMAL", None, 7)["has_designation"]
    assert pc.availability_block("INJURY_RESERVE", None, 7)["label"] == "Injured reserve (ESPN)"


# ----------------------------------------------------------------------------- findings, thresholds


def test_findings_next_week_even_and_edge(w):
    out = pc.compare(w, ACE, ZED)                      # ESPN 6.0 vs 8.0
    f = out["summary"][0]
    assert f["key"] == "next_week" and f["title"] == "Higher current projected points"
    assert f["leader"] == "b" and "Zed RB" in f["text"] and "8.0" in f["text"] and "6.0" in f["text"]
    assert row(out, "next_week_espn")["edge"] == "b"
    w.execute("UPDATE espn_player_weekly_points SET projected_points = 6.4 WHERE espn_player_id = ? "
              "AND scoring_period = ?", [ZED, CURRENT])
    out = pc.compare(w, ACE, ZED)                      # 6.0 vs 6.4: under 0.5
    assert out["summary"][0]["leader"] == "even" and row(out, "next_week_espn")["edge"] == "even"


def test_findings_stability_trend_and_ros(w):
    out = pc.compare(w, ACE, WALLY)
    keys = [s["key"] for s in out["summary"]]
    assert keys == ["next_week", "stability", "ros", "trend"]
    stab = out["summary"][1]
    assert stab["title"] == "More stable opportunity profile" and stab["leader"] in ("a", "b", "even")
    trend = out["summary"][3]
    assert trend["title"] == "Bigger apparent usage trend"
    assert trend["leader"] == "a" and "+0.50" in trend["text"] and "+0.10" in trend["text"]    # 0.5 vs 0.1: gap 0.4
    ros = out["summary"][2]
    assert ros["title"] == "Stronger rest-of-season projection"
    assert ros["leader"] == "b" and "Games remaining" in ros["text"]       # Wally 5+5=10/wk vs Ace 6/wk, over 12 weeks
    assert all(set(s) == {"key", "title", "leader", "text", "basis"} for s in out["summary"])
    assert "win probability" not in json.dumps(out["summary"]).lower().replace("does not estimate win probability", "")


def test_trend_row_edge_and_threshold(w):
    out = pc.compare(w, ACE, WALLY)
    r = row(out, "trend_score")
    assert (r["a"], r["b"], r["edge"]) == (0.5, 0.1, "a")
    w.execute("UPDATE player_scores SET usage_trend_score = 0.4 WHERE gsis_id = ?", [G[WALLY]])
    assert row(pc.compare(w, ACE, WALLY), "trend_score")["edge"] == "even"      # 0.5 vs 0.4: under 0.15
    assert pc.compare(w, ACE, WALLY)["summary"][3]["leader"] == "even"


def test_usage_rows_need_three_games(w):
    w.execute("DELETE FROM player_week_usage WHERE gsis_id = ? AND week > 2", [G[ACE]])
    out = pc.compare(w, ACE, WALLY)
    r = row(out, "xfp_pg")
    assert r["edge"] == "n/a" and r["note"] == "Fewer than 3 games for Ace WR"
    assert r["a"] is not None and r["b"] is not None              # the numbers are still shown
    assert metric(out, "xfp_pg")["value"] == 15.0
    assert row(out, "league_pts_pg")["edge"] != "n/a"             # ESPN points still cover five weeks
    assert out["summary"][1]["leader"] is None and "does not support a call" in out["summary"][1]["text"]


# --------------------------------------------------------------------------------------- pure helpers


@pytest.mark.parametrize("a,b,unit,key,expected", [
    (6.0, 6.4, "pts", "next_week_espn", "even"),
    (6.0, 6.5, "pts", "next_week_espn", "b"),
    (7.0, 6.0, "pts", "next_week_espn", "a"),
    (100.0, 102.9, "pts", "ros_espn", "even"),             # max(3.0, 3% of 102.9 = 3.087) = 3.087
    (100.0, 103.1, "pts", "ros_espn", "b"),
    (200.0, 205.9, "pts", "ros_ours", "even"),             # 3% of 205.9 = 6.177
    (200.0, 206.5, "pts", "ros_ours", "b"),
    (10.0, 10.4, "pts_per_game", "xfp_pg", "even"),
    (10.0, 10.5, "pts_per_game", "xfp_pg", "b"),
    (3.0, 2.4, "per_game", "targets_pg", "a"),
    (90.0, 94.9, "yards_per_game", "air_yards_pg", "even"),
    (90.0, 95.0, "yards_per_game", "air_yards_pg", "b"),
    (0.50, 0.52, "share", "snap_share", "b"),
    (0.50, 0.519, "share", "snap_share", "even"),
    (11, 12, "games", "games_remaining", "b"),
    (11, 11, "games", "games_remaining", "even"),
    (0.30, 0.14, "trend", "trend_score", "a"),
    (0.30, 0.16, "trend", "trend_score", "even"),
    (80.0, 74.9, "rating", "opportunity_rating", "a"),
    (80.0, 75.5, "rating", "opportunity_rating", "even"),
    (None, 1.0, "pts", "next_week_espn", "n/a"),
])
def test_edge_thresholds(a, b, unit, key, expected):
    assert pc.edge_for(a, b, pc.edge_threshold(unit, key, a, b)) == expected


def test_stability_needs_four_games():
    assert pc.coefficient_of_variation([10.0, 12.0, 11.0]) is None
    cv = pc.coefficient_of_variation([10.0, 12.0, 11.0, 13.0])
    assert cv is not None and cv == pytest.approx(0.1123, abs=1e-3)
    assert pc.coefficient_of_variation([0.0, 0.0, 0.0, 0.0]) is None           # no mean to scale by
    rows = [{"xfp": x, "opportunities": o} for x, o in ((10.0, 9), (12.0, 11), (11.0, 10), (None, 12))]
    assert pc.stability_block(rows) == {"cv": pc.coefficient_of_variation([9, 11, 10, 12]), "games": 4,
                                        "metric": "opportunities"}              # xFP has only three values
    assert pc.stability_block(rows[:3]) == {"cv": None, "games": 3, "metric": None}


def test_pair_stability_uses_one_metric_for_both():
    xfp = [{"xfp": v, "opportunities": 8 + i} for i, v in enumerate((10.0, 12.0, 11.0, 13.0))]
    short = [{"xfp": v if i < 3 else None, "opportunities": 20 - i} for i, v in enumerate((9.0, 9.5, 9.0, 9.0))]
    assert pc.pair_stability(xfp, xfp)["metric"] == "xfp"
    mixed = pc.pair_stability(xfp, short)
    assert mixed["metric"] == "opportunities"
    assert mixed["a"] == pc.coefficient_of_variation([8, 9, 10, 11])
    assert pc.pair_stability(xfp, xfp[:3])["metric"] is None


def test_stability_finding_gap_rule(w):
    # Quinn's xFP is nearly flat, Wally's swings: a clear leader
    w.execute("UPDATE player_week_usage SET xfp = CASE week WHEN 1 THEN 17.0 WHEN 2 THEN 17.5 WHEN 4 THEN 17.0 "
              "ELSE 17.2 END WHERE gsis_id = ?", [G[QUINN]])
    w.execute("UPDATE player_week_usage SET xfp = CASE week WHEN 1 THEN 4.0 WHEN 2 THEN 12.0 WHEN 4 THEN 6.0 "
              "ELSE 14.0 END WHERE gsis_id = ?", [G[WALLY]])
    f = pc.compare(w, QUINN, WALLY)["summary"][1]
    assert f["leader"] == "a" and "Quinn QB" in f["text"] and "xFP" in f["text"]
    assert pc.compare(w, WALLY, QUINN)["summary"][1]["leader"] == "b"
    w.execute("UPDATE player_week_usage SET xfp = 17.0 + week * 0.2 WHERE gsis_id IN (?, ?)", [G[QUINN], G[WALLY]])
    assert pc.compare(w, QUINN, WALLY)["summary"][1]["leader"] == "even"


def test_applicability_table():
    for pos in ("QB", "RB", "WR", "TE"):
        assert pc.applicability(pos, "snap_share") == (True, None)
        assert pc.applicability(pos, "league_pts_pg") == (True, None)
    assert pc.applicability("RB", "targets_pg") == (True, None)
    assert pc.applicability("TE", "air_yards_pg") == (True, None)
    assert pc.applicability("RB", "air_yards_pg") == (False, pc.WHY_RB_AIR_YARDS)
    assert pc.applicability("QB", "air_yards_pg") == (False, pc.WHY_QB_RECEIVING)
    assert pc.applicability("WR", "carries_pg") == (False, pc.WHY_REC_CARRIES)
    assert pc.applicability("D/ST", "xfp_pg") == (False, pc.WHY_NO_USAGE_DATA)
    assert pc.applicability("K", "league_pts_pg") == (True, None)
    ok, why = pc.applicability("LB", "snap_share")
    assert not ok and "LB" in why


def test_aggregate_usage_skips_null_cells():
    games = [{"offense_pct": .8, "targets": 4, "xfp": None}, {"offense_pct": None, "targets": 6, "xfp": None}]
    agg = pc.aggregate_usage(games)
    assert agg["snap_share"] == 0.8 and agg["targets_pg"] == 5.0 and agg["xfp_pg"] is None


def test_scored_game_weeks_drops_byes_not_real_zeros():
    games = {("BUF", 1): ("KC", True), ("BUF", 2): ("KC", True), ("BUF", 4): ("KC", True)}
    pts, unknown = pc.scored_game_weeks({1: 10.0, 2: 0.0, 3: 0.0, 4: 6.0}, "BUF", games, {1, 2, 3, 4}, {1, 4})
    assert pts == {1: 10.0, 2: 0.0, 4: 6.0} and unknown == []          # week 2 is a real 0 (inactive); week 3 a bye
    pts, unknown = pc.scored_game_weeks({1: 10.0, 2: 0.0, 3: 5.0}, "BUF", {}, set(), {1})
    assert pts == {1: 10.0, 3: 5.0} and unknown == [2]                  # no schedule: a bare 0 cannot be trusted


def test_ratings_block_prefers_100_scale():
    r = pc.ratings_block({"opportunity_score_100": 71.5, "opportunity_score": 6.0, "production_score": 5.0,
                          "opportunity_basis": "xfp", "rating_scale_version": "ratings-100.v1", "as_of_week": 5})
    assert r["opportunity"] == {"value": 71.5, "scale": 100, "legacy": False}
    assert r["production"] == {"value": 5.0, "scale": 10, "legacy": True}
    assert pc.ratings_block(None) == {"opportunity": None, "production": None, "basis": None, "version": None,
                                      "as_of_week": None}


# ------------------------------------------------------------------------------ pregame lock for the as-of week


def _kickoffs(con, kick_day="2026-10-15", clock="20:15"):
    """Give the fixture schedule kickoff times (Eastern): every week on `kick_day` + 7 days per week from week 6."""
    from datetime import date, timedelta
    con.execute("ALTER TABLE nfl_schedules ADD COLUMN IF NOT EXISTS gameday VARCHAR")
    con.execute("ALTER TABLE nfl_schedules ADD COLUMN IF NOT EXISTS gametime VARCHAR")
    for wk in range(1, 19):
        day = date.fromisoformat(kick_day) + timedelta(days=7 * (wk - CURRENT))
        con.execute("UPDATE nfl_schedules SET gameday = ?, gametime = ? WHERE week = ?", [day.isoformat(), clock, wk])


def test_after_kickoff_the_as_of_week_uses_the_last_pregame_snapshot(w):
    """Week 6 KC kicks off 2026-10-16 00:15 UTC. ESPN's live value was overwritten after kickoff (6.0 -> 2.5); the
    comparison reads the history row stored before kickoff, never the live one, and the ROS total follows."""
    from datetime import datetime
    _kickoffs(w)
    w.execute("INSERT INTO espn_projection_history VALUES ('2026-10-15', ?, ?, ?, 13.0, '2026-10-15 12:00:00')",
              [SEASON, ACE, CURRENT])
    before = pc.compare(w, ACE, ZED, "espn", now=datetime(2026, 10, 15, 12, 30))       # before kickoff: live value
    live = card(before, "a")["projection"]
    w.execute("UPDATE espn_player_weekly_points SET projected_points = 2.5 WHERE espn_player_id = ? AND scoring_period = ?",
              [ACE, CURRENT])
    after = pc.compare(w, ACE, ZED, "espn", now=datetime(2026, 10, 16, 3, 0))
    ace = card(after, "a")["projection"]
    assert ace["next_week"]["espn"] == 13.0 and next(x for x in ace["weeks"] if x["week"] == CURRENT)["espn"] == 13.0
    assert ace["ros"]["espn"] == pytest.approx(live["ros"]["espn"] - live["next_week"]["espn"] + 13.0)
    assert any("last projection stored before kickoff" in n for n in after["limitations"])
    assert not any("kicked off" in n for n in before["limitations"])


def test_after_kickoff_without_a_pregame_snapshot_the_week_is_left_out(w):
    from datetime import datetime
    _kickoffs(w)
    after = pc.compare(w, ACE, ZED, "espn", now=datetime(2026, 10, 16, 3, 0))
    ace = card(after, "a")["projection"]
    assert ace["next_week"]["espn"] is None
    assert ace["ros"]["espn_weeks_available"] == ace["ros"]["espn_weeks_expected"] - 1
    assert any("no ESPN projection stored before kickoff" in n for n in after["limitations"])
