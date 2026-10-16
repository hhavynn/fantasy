"""Rest-of-season ifp-2 projections (build_live with projected_from_week, the run store, the evidence readers).
Synthetic data only; temp DuckDB. The learning-cycle / CLI side is in test_learning_cycle.py."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

import polars as pl
import pytest

from app.analytics import production_forecasts as pf
from app.analytics import promotion_tracker as pt
from app.analytics.forecast_backtest import build_store, completed_weeks
from app.analytics.forecast_features import Sources, load_sources
from app.analytics.independent_projection_store import build_live, write_run
from app.analytics.independent_projections import MODEL_VERSION, fit_final
from app.analytics.league_scoring import resolve_skill_scoring
from app.analytics.participation import LogitModel, ParticipationArtifact
from app.db.connection import insert_rows

from synthetic_forecast import SCORING_ITEMS, add_league, database, gsis

SCORING = resolve_skill_scoring(SCORING_ITEMS)
W, LAST = 6, 17                       # first unplayed week; the synthetic season's last regular-season week
BYE_TEAM, BYE_WEEK = "HHH", 8
CREATED = datetime(2026, 9, 1)        # before every synthetic kickoff (all in September 2026)

# espn id -> (name, position, gsis, injury status)
PLAYERS = {
    1: ("Healthy WR", "WR", gsis("AAA", "WR1"), "ACTIVE"),
    2: ("Out RB", "RB", gsis("DDD", "RB1"), "OUT"),
    3: ("IR WR", "WR", gsis("EEE", "WR1"), "INJURY_RESERVE"),
    4: ("Suspended RB", "RB", gsis("FFF", "RB1"), "SUSPENSION"),
    5: ("NFL reserve WR", "WR", gsis("BBB", "WR3"), "ACTIVE"),       # reserve from week 5: eligible from week 9
    6: ("Practice squad RB", "RB", gsis("CCC", "RB2"), "ACTIVE"),    # roster status PRA: not ACT/INA
    7: ("Bye WR", "WR", gsis(BYE_TEAM, "WR1"), "ACTIVE"),
    8: ("Questionable WR", "WR", gsis("FFF", "WR1"), "QUESTIONABLE"),
    9: ("Healthy RB", "RB", gsis("BBB", "RB1"), "ACTIVE"),
    10: ("Healthy QB", "QB", gsis("AAA", "QB"), "ACTIVE"),
    11: ("Doubtful WR", "WR", gsis("GGG", "WR1"), "DOUBTFUL"),
}
RESERVE_ID, PS_ID = 5, 6


def artifact(src, created_at="2026-08-01T00:00:00+00:00"):
    store = build_store(src, completed_weeks(src, [2025, 2026]))
    sel = {"protocol": {}, "positions": {p: {"primary": "C", "lambda": {"B": 0.1, "C": 0.1}, "intervals": {}}
                                         for p in ("QB", "RB", "WR", "TE")}}
    ev = {"status": {p: {"status": "experimental", "reasons": ["synthetic"], "start_sit_ready": False}
                     for p in ("QB", "RB", "WR", "TE")}}
    return fit_final(store, sel, ev, SCORING.describe(), created_at, 2025)


def participation_artifact():
    """P(plays) = sigmoid(2 - 4 * inj_out): a player the week's report lists Out is unlikely to appear."""
    models = {p: LogitModel(p, ["inj_out"], ["inj_out"], [0.0], [1.0], [-4.0], 2.0, 0.01, 0, [])
              for p in ("QB", "RB", "WR", "TE")}
    status = {p: {"participation": "experimental", "expected_points": "experimental", "reasons": ["synthetic"]}
              for p in models}
    return ParticipationArtifact("ipm-test", "2026-08-01T00:00:00+00:00", {}, models, status, {})


def build_world(path):
    """Live DB with 2026 weeks 1-5 played and weeks 6-17 scheduled (HHH has a bye in week 8), a roster per player
    (weeks 1-6), and an ESPN side for every PLAYERS entry. Returns (con, sources)."""
    con = database(path, [2025, 2026], unfinished={2026: W})
    add_league(con, 2026, W)
    con.execute("DELETE FROM nfl_schedules WHERE season = 2026 AND week = ? AND (home_team = ? OR away_team = ?)",
                [BYE_WEEK, BYE_TEAM, BYE_TEAM])
    roster = []
    for pid, (_, pos, g, _) in PLAYERS.items():
        for w in range(1, W + 1):
            status, abbr = "ACT", "A01"
            if pid == RESERVE_ID and w >= 5:
                status, abbr = "ACT", "R01"        # Reserve/Injured since week 5: misses 5, 6, 7, 8; eligible week 9
            if pid == PS_ID and w >= 5:
                status, abbr = "PRA", "P01"
            roster.append({"season": 2026, "week": w, "gsis_id": g, "team": g.split("-")[1], "game_type": "REG",
                           "position": pos, "status": status, "status_description_abbr": abbr})
    con.register("_r", pl.DataFrame(roster).to_arrow())
    con.execute("CREATE TABLE nfl_rosters_weekly AS SELECT * FROM _r")
    for pid, (name, pos, g, inj) in PLAYERS.items():
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, injury_status, "
                    "on_team_id) VALUES (?, ?, ?, ?, ?, 1)", [pid, name, pos, g.split("-")[1], inj])
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence) VALUES "
                    "(?, ?, 'espn_id:nfl_players', 'high')", [pid, g])
    return con, load_sources(con, SCORING)


@pytest.fixture
def world(tmp_path):
    con, src = build_world(tmp_path / "live.duckdb")
    yield con, src
    con.close()


def by_id(rows):
    return {r["espn_player_id"]: r for r in rows}


def project(con, src, art, target, part=None, from_week=W):
    return build_live(con, src, art, 2026, target, participation=part,
                      artifact_sha256="a" * 64, projected_from_week=from_week)


def sources_with_injuries(src, rows):
    """`src` plus nflverse injury-report rows [(week, gsis, team, status)]."""
    inj = pl.DataFrame([{"season": 2026, "week": w, "gsis_id": g, "team": t, "report_status": st,
                         "practice_status": "Did Not Participate In Practice", "injury": "Knee", "not_injury": False}
                        for w, g, t, st in rows],
                       schema={"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "team": pl.String,
                               "report_status": pl.String, "practice_status": pl.String, "injury": pl.String,
                               "not_injury": pl.Boolean})
    return Sources(src.panel, src.games, src.defense, src.rosters, inj)


# ----------------------------------------------------------------------------- horizons and features


def test_horizon_and_projected_from_week_are_recorded(world):
    con, src = world
    art = artifact(src)
    for target in (W, W + 1, W + 5):
        meta, rows = project(con, src, art, target)
        assert (meta["forecast_horizon"], meta["projected_from_week"], meta["week"]) == (target - W, W, target)
        assert meta["data_through_week"] == W - 1                  # features are as of week W whatever the target
        stored = write_run(con, meta, rows)
        got = con.execute("SELECT forecast_horizon, projected_from_week, week FROM independent_projection_runs "
                          "WHERE run_id = ?", [stored["run_id"]]).fetchone()
        assert got == (target - W, W, target)
    with pytest.raises(ValueError):
        project(con, src, art, W, from_week=W + 1)


def test_future_week_uses_the_future_opponent_and_week_w_features(world):
    con, src = world
    art = artifact(src)
    _, now = project(con, src, art, W)
    base = by_id(now)[1]
    for target in (W + 1, W + 3, W + 4):                            # AAA has a bye in week 8 (its opponent HHH too)
        _, rows = project(con, src, art, target)
        r = by_id(rows)[1]
        game = src.games.filter((pl.col("season") == 2026) & (pl.col("week") == target)
                                & (pl.col("team") == "AAA")).row(0, named=True)
        assert (r["opponent"], r["is_home"]) == (game["opponent"], bool(game["is_home"]))
        assert r["coverage_status"] == "projected"
        # the player's own usage window is the one of week W: same games, same last game, no staleness
        assert (r["games_used"], r["latest_usage_season"], r["latest_usage_week"]) == \
            (base["games_used"], base["latest_usage_season"], base["latest_usage_week"])
        assert not any("returning after" in f for f in r["flags"])
        assert "future week: no injury report yet" in r["flags"]
        assert pf.HORIZON_NOTE.format(k=target - W) in r["flags"]
    assert not any("future week" in f for f in base["flags"])      # horizon 0 carries no future-week flag


def test_a_dud_or_breakout_in_the_latest_game_moves_every_future_week_the_same_way(world):
    con, src = world
    art = artifact(src)
    last = W - 1

    def with_last_game(points, targets, xfp):
        def edit(df):
            hit = (pl.col("gsis_id") == PLAYERS[1][2]) & (pl.col("season") == 2026) & (pl.col("week") == last)
            return df.with_columns(pl.when(hit).then(points).otherwise(pl.col("league_points")).alias("league_points"),
                                   pl.when(hit).then(targets).otherwise(pl.col("targets")).alias("targets"),
                                   pl.when(hit).then(xfp).otherwise(pl.col("xfp")).alias("xfp"))
        return Sources(edit(src.panel), src.games, src.defense, src.rosters, src.injuries)

    weeks = [t for t in range(W, LAST + 1) if t != BYE_WEEK]
    base = {t: by_id(project(con, src, art, t)[1])[1]["projected_points"] for t in weeks}
    boom = {t: by_id(project(con, with_last_game(45.0, 18, 30.0), art, t)[1])[1]["projected_points"] for t in weeks}
    dud = {t: by_id(project(con, with_last_game(0.0, 0, 0.0), art, t)[1])[1]["projected_points"] for t in weeks}
    for t in weeks:
        assert dud[t] < base[t] < boom[t], (t, dud[t], base[t], boom[t])
    # and it is a real shift, not noise, in the far weeks too
    assert boom[LAST] - dud[LAST] > 1.0
    assert all(boom[t] - base[t] > 0.3 and base[t] - dud[t] > 0.3 for t in weeks)


def test_byes_come_from_the_target_weeks_schedule(world):
    con, src = world
    art = artifact(src)
    _, now = project(con, src, art, W)
    _, bye = project(con, src, art, BYE_WEEK)
    assert by_id(now)[7]["coverage_status"] == "projected"
    assert by_id(bye)[7]["coverage_status"] == "bye" and by_id(bye)[7]["projected_points"] is None
    assert by_id(bye)[9]["coverage_status"] == "projected"


# ----------------------------------------------------------------------------- availability


def test_espn_out_questionable_and_doubtful_apply_to_horizon_zero_only(world):
    con, src = world
    art = artifact(src)
    r0 = by_id(project(con, src, art, W)[1])
    assert r0[2]["coverage_status"] == "withheld_unavailable"                       # OUT withholds now
    assert any("QUESTIONABLE" in f and "assumes he plays" in f for f in r0[8]["flags"])
    for target in (W + 1, W + 4):
        r = by_id(project(con, src, art, target)[1])
        assert r[2]["coverage_status"] == "projected" and r[2]["projected_points"] > 0   # OUT is the next game only
        assert any("OUT" in f and "next game only" in f for f in r[2]["flags"])
        assert r[8]["coverage_status"] == "projected" and r[11]["coverage_status"] == "projected"
        assert not any("assumes he plays" in f for f in r[8]["flags"])
        assert "future week: no injury report yet" in r[2]["flags"]


def test_injured_reserve_and_suspension_withhold_every_remaining_week(world):
    con, src = world
    art = artifact(src)
    for target in range(W, LAST + 1):
        if target == BYE_WEEK:
            continue
        r = by_id(project(con, src, art, target)[1])
        for pid in (3, 4):                                                       # ESPN INJURY_RESERVE / SUSPENSION
            assert r[pid]["coverage_status"] == "withheld_unavailable" and r[pid]["projected_points"] is None
            if target > W:
                assert r[pid]["missing_inputs"][0].startswith("long-term unavailable; return date unknown")
        assert r[PS_ID]["coverage_status"] == "withheld_unavailable"             # roster status PRA
        if target > W:
            assert r[PS_ID]["missing_inputs"][0] == "long-term unavailable; return date unknown"
            assert "not on the NFL active roster" in r[PS_ID]["missing_inputs"][1]


def test_nfl_reserve_unavailable_weeks_are_respected_per_target_week(world):
    con, src = world
    art = artifact(src)
    status = {t: by_id(project(con, src, art, t)[1])[RESERVE_ID]["coverage_status"] for t in range(W, W + 8)}
    # reserve since week 5, four games missed (5-8): ineligible weeks 6, 7, 8; eligible (unconfirmed) from week 9
    assert [status[t] for t in (6, 7, 8)] == ["withheld_unavailable"] * 3
    assert [status[t] for t in (9, 10, 11, 12, 13)] == ["projected"] * 5
    row = by_id(project(con, src, art, 9)[1])[RESERVE_ID]
    assert row["projected_points"] is not None and "future week: no injury report yet" in row["flags"]


# ----------------------------------------------------------------------------- participation


def test_participation_sees_no_week_t_designations_for_a_future_week(world):
    con, src = world
    art, part = artifact(src), participation_artifact()
    out_player = PLAYERS[1][2]
    src_inj = sources_with_injuries(src, [(W, out_player, "AAA", "Out")])
    now = by_id(project(con, src_inj, art, W, part)[1])[1]
    later = by_id(project(con, src_inj, art, W + 1, part)[1])[1]
    undesignated = 1 / (1 + 2.718281828459045 ** -2.0)
    assert now["p_play"] < 0.15                                    # horizon 0: the week-W report lists him Out
    assert later["p_play"] == pytest.approx(undesignated, abs=1e-3)  # horizon 1: no report for that week yet
    assert later["expected_points"] == pytest.approx(round(later["p_play"] * later["projected_points"], 2), abs=0.011)
    assert later["participation"]["flags"] and "future week" in later["participation"]["flags"][0]
    assert later["projected_points"] > 0                           # ESPN is not consulted: the conditional estimate stays


def test_espn_designation_fallback_is_not_used_for_future_weeks(world):
    con, src = world
    art, part = artifact(src), participation_artifact()
    con.execute("UPDATE fantasy_players SET injury_status = 'OUT' WHERE espn_player_id = 10")
    r0 = by_id(project(con, src, art, W, part)[1])[10]
    assert r0["coverage_status"] == "withheld_unavailable"
    con.execute("UPDATE fantasy_players SET injury_status = 'QUESTIONABLE' WHERE espn_player_id = 1")
    now = by_id(project(con, src, art, W, part)[1])[1]
    later = by_id(project(con, src, art, W + 1, part)[1])[1]
    assert any("ESPN injury status" in f for f in now["participation"]["flags"])          # fallback at horizon 0
    assert not any("ESPN injury status" in f for f in later["participation"]["flags"])    # none for a future week
    assert later["p_play"] > now["p_play"] or later["p_play"] == pytest.approx(now["p_play"], abs=1e-3)


# ----------------------------------------------------------------------------- horizon 0 is unchanged


def _round(v):
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, dict):
        return {k: _round(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_round(x) for x in v]
    return v


def snapshot_digest(path) -> dict:
    """Digest of the horizon-0 output (rows and the hashed run meta without the two new fields), with and without
    participation. The SAME function was run against the code before this feature; see HORIZON_ZERO_SNAPSHOT."""
    con, src = build_world(path)
    try:
        art, part = artifact(src), participation_artifact()
        out = {}
        for name, p in (("plain", None), ("participation", part)):
            kwargs = {"participation": p, "artifact_sha256": "a" * 64}
            meta, rows = build_live(con, src, art, 2026, W, **kwargs)
            payload = {"meta": {k: _round(v) for k, v in sorted(meta.items())
                                if k not in ("run_id", "created_at", "forecast_horizon", "projected_from_week")},
                       "rows": [_round({k: v for k, v in sorted(r.items()) if k != "run_id"})
                                for r in sorted(rows, key=lambda r: r["espn_player_id"])]}
            out[name] = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        return out
    finally:
        con.close()


# Produced by snapshot_digest() on the code of origin/feat/production-artifact-pin (before rest-of-season support).
HORIZON_ZERO_SNAPSHOT = {"plain": "0ed94eddc3385189b1121bdeef636223d9bd8f46dfd2fafe2697ad0c51eb0233",
                         "participation": "c7a2418235911d07ee75844c99fb2db2e72d9725ce93ce5de541da83d2d996d7"}


def test_horizon_zero_output_is_byte_identical_to_the_code_before_rest_of_season(tmp_path):
    assert snapshot_digest(tmp_path / "snap.duckdb") == HORIZON_ZERO_SNAPSHOT


def test_horizon_zero_via_projected_from_week_equals_the_plain_call(world):
    con, src = world
    art, part = artifact(src), participation_artifact()
    a_meta, a = build_live(con, src, art, 2026, W, participation=part, artifact_sha256="a" * 64)
    b_meta, b = build_live(con, src, art, 2026, W, participation=part, artifact_sha256="a" * 64, projected_from_week=W)
    strip = lambda rs: [{k: v for k, v in r.items() if k != "run_id"} for r in rs]  # noqa: E731
    assert strip(a) == strip(b)
    assert (a_meta["forecast_horizon"], a_meta["projected_from_week"]) == (0, W) == \
        (b_meta["forecast_horizon"], b_meta["projected_from_week"])


def test_horizon_zero_digest_is_the_one_sealed_before_the_columns_existed(world):
    """A legacy run (NULL horizon) and the same run stamped horizon 0 hash identically, so an unchanged rerun after
    the upgrade is a no-op; a horizon-k run hashes the two fields in."""
    con, src = world
    meta, rows = project(con, src, artifact(src), W, from_week=None)
    legacy = {k: v for k, v in meta.items() if k not in pf.HORIZON_META}
    assert pf.content_sha256(legacy, rows) == pf.content_sha256(meta, rows)
    assert write_run(con, legacy, rows)["created"] is True
    assert write_run(con, {**meta, "run_id": "ifp-again"}, rows)["created"] is False      # idempotent across the upgrade
    future_meta, future_rows = project(con, src, artifact(src), W + 1)
    assert pf.content_sha256(future_meta, future_rows) != pf.content_sha256(
        {**future_meta, "forecast_horizon": 0}, future_rows)


# ----------------------------------------------------------------------------- store: idempotence and evidence


def _store_all(con, src, art, part=None, created=CREATED, weeks=range(W, LAST + 1)):
    out = {}
    for target in weeks:
        meta, rows = project(con, src, art, target, part)
        out[target] = write_run(con, {**meta, "created_at": created}, rows)
    return out


def test_an_unchanged_cycle_writes_nothing_new(world):
    con, src = world
    art = artifact(src)
    first = _store_all(con, src, art)
    count = con.execute("SELECT count(*) FROM independent_projection_runs").fetchone()[0]
    assert count == LAST - W + 1 and all(r["created"] for r in first.values())
    again = _store_all(con, src, art, created=datetime(2026, 9, 2))
    assert all(not r["created"] for r in again.values())
    assert {t: r["run_id"] for t, r in again.items()} == {t: r["run_id"] for t, r in first.items()}
    assert con.execute("SELECT count(*) FROM independent_projection_runs").fetchone()[0] == count
    assert con.execute("SELECT count(*) FROM independent_projection_rows").fetchone()[0] == \
        sum(r["row_count"] for r in first.values())


def test_a_horizon_one_run_is_not_the_twin_of_a_horizon_zero_run(world):
    """Same week, model, scoring and artifact but a different horizon: separate runs (e.g. after a cutoff change)."""
    con, src = world
    art = artifact(src)
    m0, r0 = project(con, src, art, W + 1, from_week=W + 1)       # a horizon-0 run for week W+1
    m1, r1 = project(con, src, art, W + 1, from_week=W)           # a horizon-1 run for week W+1
    s0, s1 = write_run(con, {**m0, "created_at": CREATED}, r0), write_run(con, {**m1, "created_at": CREATED}, r1)
    assert s0["created"] and s1["created"] and s0["run_id"] != s1["run_id"]


def test_evidence_readers_ignore_future_horizon_runs_even_when_the_horizon_zero_run_is_missing(world):
    con, src = world
    art = artifact(src)
    fp = SCORING.fingerprint
    stored = _store_all(con, src, art, created=CREATED)
    # drop the week W+2 .. run for horizon 0 reasons: only week W has a horizon-0 run, W+1 .. LAST have horizon >= 1
    rows, excluded = pf.pregame_forecasts(con, 2026, None, model_version=MODEL_VERSION, scoring_fingerprint=fp,
                                          artifact_id=None)
    assert {r["week"] for r in rows} == {W}
    assert {r["run_id"] for r in rows} == {stored[W]["run_id"]}
    why = {e["week"]: e["reason"] for e in excluded}
    for target in range(W + 1, LAST + 1):
        assert why[target] == f"rest-of-season forecast (horizon {target - W}), not evidence"
    ids, ex = pf.eligible_run_ids(con, 2026, MODEL_VERSION, fp, artifact_id=None)
    assert ids == [stored[W]["run_id"]] and len(ex) == LAST - W
    # a week whose only run is a rest-of-season one yields no evidence row at all
    only, excluded = pf.pregame_forecasts(con, 2026, [W + 3], model_version=MODEL_VERSION, scoring_fingerprint=fp,
                                          artifact_id=None)
    assert only == [] and excluded[0]["reason"] == "rest-of-season forecast (horizon 3), not evidence"


def test_promotion_tracker_is_unaffected_by_future_week_runs(world):
    con, src = world
    art = artifact(src)
    fp = SCORING.fingerprint
    h0_meta, h0_rows = project(con, src, art, W)
    h0 = write_run(con, {**h0_meta, "created_at": CREATED}, h0_rows)
    before = pt.latest_run(con, 2026, fp)
    assert before["run_id"] == h0["run_id"]
    for target in range(W + 1, LAST + 1):                          # created LATER than the horizon-0 run
        m, r = project(con, src, art, target)
        write_run(con, {**m, "created_at": datetime(2026, 9, 2)}, r)
    assert pt.latest_run(con, 2026, fp)["run_id"] == h0["run_id"]
    ids, excluded = pt.eligible_runs(con, 2026, fp)
    assert ids == [h0["run_id"]]
    assert all(e["reason"].startswith("rest-of-season forecast") for e in excluded) and len(excluded) == LAST - W
    stored, _ = pt._stored_projections(con, 2026, fp)
    assert set(stored["week"].to_list()) <= {W}
    # with only future-week runs stored there is no "latest run" at all
    con.execute("DELETE FROM independent_projection_rows WHERE run_id = ?", [h0["run_id"]])
    con.execute("DELETE FROM independent_projection_runs WHERE run_id = ?", [h0["run_id"]])
    assert pt.latest_run(con, 2026, fp) is None


def test_the_tracker_page_lists_future_week_exclusions_once_per_week(world):
    con, src = world
    art = artifact(src)
    for from_week, created in ((W, datetime(2026, 9, 1)), (W + 1, datetime(2026, 9, 2))):    # two cycles
        for target in range(from_week, from_week + 3):
            m, r = project(con, src, art, target, from_week=from_week)
            write_run(con, {**m, "created_at": created, "artifact_id": f"{from_week:064d}"}, r)
    _, excluded = pf.pregame_forecasts(con, 2026, None, model_version=MODEL_VERSION,
                                       scoring_fingerprint=SCORING.fingerprint, artifact_id=None)
    shown = pt._collapse_future_exclusions(excluded)
    by_week = {e["week"]: e["reason"] for e in shown}
    assert by_week[W + 1] == "rest-of-season forecast (horizon 1), not evidence"
    assert by_week[W + 2] == "rest-of-season forecast (horizon 1-2), not evidence [2 runs]"      # two cycles, one line
    assert len(shown) == len({e["week"] for e in excluded}) == 3 and len(excluded) == 4
    assert all(e["reason"].startswith("rest-of-season forecast") for e in shown)


def test_the_consumer_latest_run_takes_the_newest_run_of_any_horizon(world):
    con, src = world
    art = artifact(src)
    fp = SCORING.fingerprint
    old_meta, old_rows = project(con, src, art, W + 2, from_week=W)           # horizon 2, from week W
    old = write_run(con, {**old_meta, "created_at": datetime(2026, 9, 1)}, old_rows)
    run = pf.latest_run(con, 2026, W + 2, model_version=MODEL_VERSION, scoring_fingerprint=fp, artifact_id=None)
    assert run["run_id"] == old["run_id"] and run["horizon"] == 2 and run["forecast_horizon"] == 2
    assert run["horizon_note"] == "rest-of-season forecast (horizon 2): experimental, not validated beyond the next week"
    # the week arrives: a horizon-0 run made later is newer and wins
    new_meta, new_rows = project(con, src, art, W + 2, from_week=W + 2)
    new = write_run(con, {**new_meta, "created_at": datetime(2026, 9, 3)}, new_rows)
    run = pf.latest_run(con, 2026, W + 2, model_version=MODEL_VERSION, scoring_fingerprint=fp, artifact_id=None)
    assert run["run_id"] == new["run_id"] and run["horizon"] == 0 and run["horizon_note"] is None
    assert pf.horizon_of({"forecast_horizon": None}) == 0                      # legacy NULL


def test_legacy_runs_without_the_columns_still_read(world):
    con, src = world
    art = artifact(src)
    meta, rows = project(con, src, art, W, from_week=None)
    stored = write_run(con, {**meta, "created_at": CREATED}, rows)
    con.execute("UPDATE independent_projection_runs SET forecast_horizon = NULL, projected_from_week = NULL")
    got, _ = pf.pregame_forecasts(con, 2026, [W], model_version=MODEL_VERSION, scoring_fingerprint=SCORING.fingerprint,
                                  artifact_id=None)
    assert {r["run_id"] for r in got} == {stored["run_id"]}
    assert pf.latest_run(con, 2026, W, model_version=MODEL_VERSION)["horizon"] == 0
