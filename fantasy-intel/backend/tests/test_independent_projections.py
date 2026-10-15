"""Models, artifacts, live projection guards and the read-only API contract. Synthetic data only."""

import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient

from app.analytics.forecast_backtest import build_store, completed_weeks
from app.analytics.forecast_features import Sources, load_sources
from app.analytics.independent_projection_store import build_live, write_live
from app.analytics.independent_projections import (Artifact, RidgeModel, explain_row, fit_final, fit_ridge,
                                                   interval_for)
from app.analytics.league_scoring import resolve_skill_scoring

from synthetic_forecast import SCORING_ITEMS, add_league, database, gsis

SCORING = resolve_skill_scoring(SCORING_ITEMS)
APP = Path(__file__).resolve().parents[1] / "app"


# ----------------------------------------------------------------------------- ridge


def _toy(n=400, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(5, 2, n)
    df = pl.DataFrame({"season": [2024] * n, "week": [1 + i % 17 for i in range(n)], "position": ["WR"] * n,
                       **{f: [0.0] * n for f in ("air_yards_pg", "target_share", "snap_pct", "rz_opps_pg",
                                                  "games_recent", "xfp_missing", "snap_missing", "rz_missing")},
                       "targets_pg": x, "xfp_recent": x * 1.5 + rng.normal(0, 0.1, n),
                       "label": 3 + 2 * x + rng.normal(0, 0.5, n)})
    return df


def test_ridge_recovers_signal_and_contributions_add_up():
    df = _toy()
    m = fit_ridge(df, "WR", "B", [1e-6])[1e-6]
    pred = m.predict(df)
    assert np.abs(pred - df["label"].to_numpy()).mean() < 0.6
    raw = m.contributions(df).sum(1) + m.intercept
    assert np.allclose(np.maximum(raw, 0), pred)


def test_missing_values_are_imputed_with_indicator_not_zero():
    df = _toy().with_columns(pl.when(pl.int_range(pl.len()) % 5 == 0).then(None)
                             .otherwise(pl.col("xfp_recent")).alias("xfp_recent"))
    m = fit_ridge(df, "WR", "B", [0.01])[0.01]
    assert "xfp_recent__missing" in m.columns
    row = df.slice(0, 1)                                   # xfp_recent is null here
    x, imputed = m.design(row)
    i = m.columns.index("xfp_recent")
    assert imputed[0][i] and x[0][i] == pytest.approx(m.means[i]) and x[0][i] != 0
    expl = explain_row(m, row, top=20)
    assert all(e["value"] is None for e in expl if e["feature"] == "xfp_recent")


def test_artifact_round_trip_and_rejects_unknown_schema():
    m = fit_ridge(_toy(), "WR", "B", [0.01])[0.01]
    art = Artifact("v", "t", "fp", {}, {}, {"WR": m}, {}, {}, {})
    back = Artifact.from_dict(json.loads(json.dumps(art.to_dict())))
    assert back.models["WR"].coef == m.coef and isinstance(back.models["WR"], RidgeModel)
    with pytest.raises(ValueError):
        Artifact.from_dict({**art.to_dict(), "schema": "other"})


def test_interval_bands():
    spec = {"WR": {"bands": [{"low": None, "high": 5, "q_low": -3, "q_high": 4},
                             {"low": 5, "high": None, "q_low": -6, "q_high": 9}]}}
    assert interval_for(spec, "WR", 2.0) == (0.0, 6.0)
    assert interval_for(spec, "WR", 10.0) == (4.0, 19.0)
    assert interval_for(spec, "TE", 10.0) is None


# ----------------------------------------------------------------------------- live run


def _artifact(src, fingerprint=None, drop=()):
    store = build_store(src, completed_weeks(src, [2025, 2026]))
    sel = {"protocol": {}, "positions": {p: {"primary": "C", "lambda": {"B": 0.1, "C": 0.1}, "intervals": {}}
                                         for p in ("QB", "RB", "WR", "TE") if p not in drop}}
    ev = {"status": {p: {"status": "experimental", "reasons": ["synthetic"], "start_sit_ready": False}
                     for p in ("QB", "RB", "WR", "TE")}}
    desc = SCORING.describe()
    if fingerprint:
        desc["fingerprint"] = fingerprint
    return fit_final(store, sel, ev, desc, "2026-10-14T00:00:00+00:00", 2025)


PLAYERS = {  # espn id -> (name, position, gsis, confidence, injury)
    1: ("Healthy WR", "WR", gsis("AAA", "WR1"), "high", "ACTIVE"),
    2: ("Name-matched RB", "RB", gsis("BBB", "RB1"), "medium", "ACTIVE"),
    3: ("Reserve QB", "QB", gsis("CCC", "QB"), "high", "ACTIVE"),
    4: ("Out TE", "TE", gsis("DDD", "TE"), "high", "OUT"),
    5: ("Kicker", "K", None, "none", "ACTIVE"),
    6: ("IR-slot WR", "WR", gsis("BBB", "WR2"), "high", "ACTIVE"),
    7: ("Rookie RB", "RB", "00-ROOKIE", "high", "ACTIVE"),
    8: ("Bye WR", "WR", gsis("HHH", "WR1"), "high", "ACTIVE"),
    9: ("Healthy TE", "TE", gsis("FFF", "TE"), "high", "QUESTIONABLE"),
    10: ("Exempt-list WR", "WR", gsis("GGG", "WR1"), "high", "DAY_TO_DAY"),
    11: ("Backup WR", "WR", gsis("GGG", "WR2"), "high", "ACTIVE"),
}
ESPN_ZERO = {11}


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "live.duckdb"
    con = database(path, [2025, 2026], unfinished={2026: 6})
    add_league(con, 2026, 6)
    con.execute("DELETE FROM nfl_schedules WHERE season = 2026 AND week = 6 AND (home_team = 'HHH' OR away_team = 'HHH')")
    team = {g: g.split("-")[1] for _, (_, _, g, _, _) in PLAYERS.items() if g and g.count("-") == 2}
    roster = []
    for g, t in team.items():
        for w in range(1, 7):
            res = g == gsis("CCC", "QB") and w >= 5
            exe = g == gsis("GGG", "WR1")
            roster.append({"season": 2026, "week": w, "gsis_id": g, "team": t, "game_type": "REG",
                           "status": "RES" if res else "EXE" if exe else "ACT",
                           "status_description_abbr": "R01" if res else "E02" if exe else "A01"})
    roster.append({"season": 2026, "week": 6, "gsis_id": "00-ROOKIE", "team": "GGG", "game_type": "REG",
                   "status": "ACT", "status_description_abbr": "A01"})
    con.register("_r", pl.DataFrame(roster).to_arrow())
    con.execute("CREATE TABLE nfl_rosters_weekly AS SELECT * FROM _r")
    for pid, (name, pos, g, conf, inj) in PLAYERS.items():
        tm = team.get(g, "GGG")
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, injury_status, "
                    "on_team_id) VALUES (?, ?, ?, ?, ?, 1)", [pid, name, pos, tm, inj])
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence) VALUES "
                    "(?, ?, ?, ?)", [pid, g, "espn_id:nfl_players" if conf == "high" else "name_position_team", conf])
        con.execute("INSERT INTO espn_player_weekly_points VALUES (2026, ?, 6, NULL, ?, '2026-10-13 12:00:00')",
                    [pid, 0.0 if pid in ESPN_ZERO else 10.0 + pid])
        con.execute("INSERT INTO lineup_snapshots (league_id, season, scoring_period, team_id, espn_player_id, "
                    "lineup_slot_id, is_starter) VALUES (99, 2026, 6, 1, ?, ?, false)", [pid, 21 if pid == 6 else 20])
    src = load_sources(con, SCORING)
    yield con, src, path
    con.close()


def _by_id(rows):
    return {r["espn_player_id"]: r for r in rows}


def test_live_coverage_statuses_and_null_not_zero(live):
    con, src, _ = live
    meta, rows = build_live(con, src, _artifact(src), 2026, 6)
    r = _by_id(rows)
    assert r[1]["coverage_status"] == "projected" and r[1]["projected_points"] > 0 and r[1]["explanation"]
    assert r[2]["coverage_status"] == "uncertain_id_mapping" and r[2]["projected_points"] is None
    assert r[3]["coverage_status"] == "withheld_unavailable" and r[3]["projected_points"] is None   # NFL reserve
    assert r[4]["coverage_status"] == "withheld_unavailable"                                       # ESPN OUT
    assert r[5]["coverage_status"] == "unsupported_position"
    assert r[6]["coverage_status"] == "projected" and "in a fantasy IR slot" in r[6]["flags"]
    assert r[7]["coverage_status"] == "abstain_no_history" and r[7]["projected_points"] is None
    assert r[8]["coverage_status"] == "bye"
    assert any("QUESTIONABLE" in f for f in r[9]["flags"])
    assert r[10]["coverage_status"] == "withheld_unavailable" and "EXE" in r[10]["missing_inputs"][0]
    assert r[11]["coverage_status"] == "projected"
    assert meta["data_through_season"] == 2026 and meta["data_through_week"] == 5
    assert r[1]["latest_usage_week"] == 5 and r[1]["games_used"] == 5


def test_live_is_reproducible(live):
    con, src, _ = live
    art = _artifact(src)
    a, b = build_live(con, src, art, 2026, 6)[1], build_live(con, src, art, 2026, 6)[1]
    strip = lambda rs: [{k: v for k, v in r.items() if k != "run_id"} for r in rs]  # noqa: E731
    assert strip(a) == strip(b)


def test_live_refuses_mismatched_scoring_and_missing_models(live):
    con, src, _ = live
    _, rows = build_live(con, src, _artifact(src, fingerprint="different"), 2026, 6)
    assert {r["coverage_status"] for r in rows if r["position"] != "K"} <= {"unsupported_scoring"}
    _, rows = build_live(con, src, _artifact(src, drop=("WR",)), 2026, 6)
    assert _by_id(rows)[1]["coverage_status"] == "no_model"


def test_live_uses_only_weeks_before_target(live):
    con, src, _ = live
    art = _artifact(src)
    base = _by_id(build_live(con, src, art, 2026, 6)[1])[1]["projected_points"]
    # Add a fake week-6 game (as if a Thursday game were already final) with absurd production.
    fake = src.panel.filter((pl.col("season") == 2026) & (pl.col("week") == 5)).with_columns(
        pl.lit(6, dtype=pl.Int64).alias("week"), pl.col("league_points") + 99, pl.col("targets") + 30)
    corrupt = Sources(pl.concat([src.panel, fake]), src.games, src.defense)
    assert _by_id(build_live(con, corrupt, art, 2026, 6)[1])[1]["projected_points"] == base


# ----------------------------------------------------------------------------- API

TOP_KEYS = {"schema_version", "league", "week", "notes", "available", "unavailable_reason", "run", "model_status",
            "players"}
PLAYER_KEYS = {"espn_player_id", "gsis_id", "name", "position", "nfl_team", "opponent", "is_home", "fantasy_team_id",
               "injury_status", "independent", "espn", "comparison"}
INDEP_KEYS = {"projected_points", "interval_80", "model", "model_version", "model_status", "start_sit_ready",
              "coverage_status", "games_used", "latest_usage", "missing_inputs", "flags", "explanation",
              "is_espn_fallback"}


def _client(monkeypatch, path):
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    return TestClient(app)


def test_api_unavailable_before_any_run(live, monkeypatch):
    con, _, path = live
    con.close()
    body = _client(monkeypatch, path).get("/api/projections/independent", params={"week": 6}).json()
    assert set(body) == TOP_KEYS and body["available"] is False and body["players"] == []


def test_api_contract_is_stable(live, monkeypatch):
    con, src, path = live
    before = con.execute("SELECT * FROM espn_player_weekly_points ORDER BY ALL").fetchall()
    meta, rows = build_live(con, src, _artifact(src), 2026, 6)
    write_live(con, meta, rows)
    assert con.execute("SELECT * FROM espn_player_weekly_points ORDER BY ALL").fetchall() == before
    con.close()
    client = _client(monkeypatch, path)
    body = client.get("/api/projections/independent", params={"week": 6}).json()
    assert set(body) == TOP_KEYS and body["available"] is True and body["schema_version"] == "independent-projections/v1"
    assert body["run"]["feature_cutoff"]["data_through_week"] == 5
    players = {p["espn_player_id"]: p for p in body["players"]}
    for p in players.values():
        assert set(p) == PLAYER_KEYS and set(p["independent"]) == INDEP_KEYS
        assert p["independent"]["is_espn_fallback"] is False
        assert set(p["espn"]) == {"projected_points", "synced_at"}
    ok = players[1]
    assert ok["comparison"]["independent_minus_espn"] == pytest.approx(
        ok["independent"]["projected_points"] - ok["espn"]["projected_points"], abs=0.01)
    assert any(e["kind"] == "model_input" for e in ok["comparison"]["evidence"])
    assert ok["comparison"]["comparable"] is True
    out = players[4]
    assert out["independent"]["projected_points"] is None and out["espn"]["projected_points"] == 14.0
    assert out["comparison"] == {"independent_minus_espn": None, "comparable": False,
                                 "comparability_note": "one or both projections unavailable", "evidence": []}
    assert players[5]["independent"]["model_status"] == "unsupported"
    backup = players[11]["comparison"]
    assert backup["independent_minus_espn"] > 0 and backup["comparable"] is False
    assert "does not expect him to play" in backup["comparability_note"]
    wr = client.get("/api/projections/independent", params={"week": 6, "position": "wr"}).json()
    assert {p["position"] for p in wr["players"]} == {"WR"}
    assert client.get("/api/projections/independent", params={"week": 6, "position": "XX"}).status_code == 422
    assert client.get("/api/projections/independent", params={"week": 7}).json()["available"] is False


# ----------------------------------------------------------------------------- isolation


def test_canonical_espn_systems_do_not_read_independent_projections():
    """Lineups, trades, waivers, scores and ESPN projections must not depend on this research model."""
    allowed = {"independent_projections.py", "independent_projection_store.py", "forecast_backtest.py",
               "forecast_features.py", "league_scoring.py", "main.py", "schema.sql"}
    for f in APP.rglob("*"):
        if f.suffix not in (".py", ".sql") or f.name in allowed:
            continue
        text = f.read_text()
        for needle in ("independent_projection", "independent_player_projections", "forecast_features",
                       "forecast_backtest", "league_scoring"):
            assert needle not in text, f"{f.relative_to(APP)} references {needle}"
