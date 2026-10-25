"""A league's Ours, K and D/ST runs derived from the global file's reference runs (app/analytics/rescore.py)."""

import json

import pytest

from app.analytics import production_forecasts as pf
from app.analytics import projection_source as ps
from app.analytics.league_scoring import COMPONENT_COLUMNS, MIX_COLUMNS, resolve_skill_scoring
from app.analytics.rescore import (DERIVED_TABLES, MISSING_COMPONENT_FLAG, NO_MIX_FLAG, RESCORED_FLAG, SHIFTED_FLAG,
                                   apply_reference, rescore_dst_rows, rescore_kicker_rows, rescore_skill_rows, stat_mix)
from app.db.connection import fetch_dicts, table_exists
from app.db.league_split import REFERENCE_TABLES

from synthetic_forecast import SCORING_ITEMS
from test_independent_projections import live  # noqa: F401  (fixture)
from test_projection_source import WEEKLY_SHA, _ifp2, _store

PPR = resolve_skill_scoring(SCORING_ITEMS)
HALF = [{**i, "points": 0.5} if i["statId"] == 53 else i for i in SCORING_ITEMS]
SIX_PT_PASS = [{**i, "points": 6.0} if i["statId"] == 4 else i for i in SCORING_ITEMS]
ZEROS = {c: 0.0 for c in COMPONENT_COLUMNS}


def _row(**kw):
    base = {"run_id": "r", "espn_player_id": 1, "gsis_id": "g1", "position": "WR", "coverage_status": "projected",
            "projected_points": 17.0, "interval_low": 5.0, "interval_high": 30.0, "p_play": 0.9,
            "expected_points": 15.3, "flags": "[]",
            "explanation": json.dumps([{"feature": "target_share", "contribution": 1.7, "text": "t"}])}
    return {**base, **kw}


# ----------------------------------------------------------------------------- the row transforms


def test_half_ppr_scales_a_projected_row_by_its_mix_ratio():
    mix = {"g1": {**ZEROS, "receptions": 6.0, "receiving_yards": 80.0, "receiving_tds": 0.5}}
    (out,) = rescore_skill_rows([_row()], mix, PPR, resolve_skill_scoring(HALF))
    f = (3 + 8 + 3) / (6 + 8 + 3)
    assert out["projected_points"] == round(17.0 * f, 2)
    assert (out["interval_low"], out["interval_high"]) == (round(5.0 * f, 2), round(30.0 * f, 2))
    assert out["expected_points"] == round(out["projected_points"] * 0.9, 2)
    assert json.loads(json.dumps(out["explanation"]))[0]["contribution"] == round(1.7 * f, 2)
    assert RESCORED_FLAG in out["flags"]


def test_the_reference_scoring_maps_every_row_onto_itself():
    mix = {"g1": {**ZEROS, "receptions": 6.0, "receiving_yards": 80.0, "receiving_tds": 0.5}}
    (out,) = rescore_skill_rows([_row()], mix, PPR, PPR)
    assert (out["projected_points"], out["interval_low"], out["interval_high"]) == (17.0, 5.0, 30.0)


def test_a_tiny_reference_mix_shifts_instead_and_never_goes_below_zero():
    mix = {"g1": {**ZEROS, "receptions": 0.5, "receiving_yards": 2.0}}          # 0.7 PPR points a game
    (out,) = rescore_skill_rows([_row(projected_points=0.3, interval_low=0.0)], mix, PPR, resolve_skill_scoring(HALF))
    assert out["projected_points"] == round(0.3 - 0.25, 2) and out["interval_low"] == 0.0
    assert SHIFTED_FLAG in out["flags"]


def test_a_player_without_a_mix_takes_his_positions_and_without_either_is_not_estimated():
    pos = {"__pos__WR": {**ZEROS, "receptions": 4.0, "receiving_yards": 50.0}}
    (a,) = rescore_skill_rows([_row()], pos, PPR, resolve_skill_scoring(HALF))
    assert a["projected_points"] == round(17.0 * (2 + 5) / (4 + 5), 2)
    (b,) = rescore_skill_rows([_row()], {}, PPR, resolve_skill_scoring(HALF))
    assert b["coverage_status"] == "insufficient_data" and b["projected_points"] is None and NO_MIX_FLAG in b["flags"]


def test_rows_that_are_not_projected_are_unchanged():
    row = _row(coverage_status="bye", projected_points=None, expected_points=0.0)
    assert rescore_skill_rows([row], {}, PPR, resolve_skill_scoring(HALF)) == [row]


def test_kicker_and_dst_rows_are_rescored_from_their_expected_statistics_or_not_at_all():
    from app.analytics.kicker_scoring import resolve_kicker_scoring

    stats = {"fg": {o: {b: 0.0 for b in ("0_39", "40_49", "50_59", "60_plus")} for o in ("made", "missed", "blocked")},
             "pat": {"made": 2.0, "missed": 0.0, "blocked": 0.0}}
    stats["fg"]["made"]["50_59"] = 1.0
    row = _row(position="K", projected_points=5.0, interval_low=1.0, interval_high=9.0, p_play=None,
               expected_points=5.0, stat_line=json.dumps({"scoring_stats": stats}))
    five = resolve_kicker_scoring([{"statId": 80, "points": 3.0}, {"statId": 74, "points": 5.0},
                                   {"statId": 86, "points": 1.0}])
    (k,) = rescore_kicker_rows([row], five)
    assert k["projected_points"] == 7.0 and (k["interval_low"], k["interval_high"]) == (3.0, 11.0)
    assert k["expected_points"] == 7.0
    assert rescore_kicker_rows([{**row, "stat_line": json.dumps({"fg_att": 1.0})}], five) is None
    d = _row(position="D/ST", projected_points=4.0, p_play=None, expected_points=4.0,
             stat_line=json.dumps({"scoring_stats": {"99": 2.5, "95": 1.0}}))
    (out,) = rescore_dst_rows([d], {99: 1.0, 95: 2.0})
    assert out["projected_points"] == 4.5


# ----------------------------------------------------------------------------- apply_reference on a league DB


def _publish_reference(con):
    """What split_database ships in the global file: ref_ copies of the model tables and the reference scoring."""
    for source, ref in REFERENCE_TABLES.items():
        if table_exists(con, source):
            con.execute(f"CREATE TABLE {ref} AS SELECT * FROM {source}")
    con.execute("CREATE TABLE ref_league_scoring AS SELECT season, scoring_items FROM leagues")


def _set_scoring(con, items):
    con.execute("UPDATE leagues SET scoring_items = ?", [json.dumps(items)])


def test_the_reference_league_gets_its_runs_verbatim(live):  # noqa: F811
    con, src, _ = live
    _store(con, src, _ifp2(src), sha=WEEKLY_SHA)
    before = fetch_dicts(con, "SELECT * FROM independent_projection_rows ORDER BY run_id, espn_player_id")
    runs = fetch_dicts(con, "SELECT * FROM independent_projection_runs ORDER BY run_id")
    _publish_reference(con)
    out = apply_reference(con, 2026)
    assert out["skill"] == {"copied": len(runs), "rescored": 0, "skipped": 0}
    assert fetch_dicts(con, "SELECT * FROM independent_projection_rows ORDER BY run_id, espn_player_id") == before
    assert fetch_dicts(con, "SELECT * FROM independent_projection_runs ORDER BY run_id") == runs


def test_another_scoring_gets_rescored_runs_that_the_consumers_verify_and_serve(live):  # noqa: F811
    con, src, _ = live
    _store(con, src, _ifp2(src), sha=WEEKLY_SHA)
    ref_sel = ps.select_run(con, 2026, 6)
    _publish_reference(con)
    _set_scoring(con, HALF)
    out = apply_reference(con, 2026)
    assert out["skill"]["rescored"] >= 1 and out["skill"]["copied"] == 0
    sel = ps.select_run(con, 2026, 6)
    half_fp = resolve_skill_scoring(HALF).fingerprint
    assert sel["run"] is not None and sel["scoring_fingerprint"] == half_fp
    assert sel["run"]["run_id"] == f"{ref_sel['run']['run_id']}~{half_fp[-8:]}"
    assert sel["run"]["artifact_id"] == ref_sel["run"]["artifact_id"]
    assert pf.integrity_problem(sel["run"], sel["rows"]) is None
    assert json.loads(sel["run"]["evaluation"])["rescored_from"]["run_id"] == ref_sel["run"]["run_id"]
    ref_rows = {r["espn_player_id"]: r for r in ref_sel["rows"]}
    moved = [r for r in sel["rows"] if r["coverage_status"] == "projected"
             and r["projected_points"] != ref_rows[r["espn_player_id"]]["projected_points"]]
    assert moved, "half-PPR moved no projection"
    # pass-heavy scoring moves quarterbacks up and leaves receivers alone
    _set_scoring(con, SIX_PT_PASS)
    apply_reference(con, 2026)
    six = {r["espn_player_id"]: r for r in ps.select_run(con, 2026, 6)["rows"]}
    for pid, r in ref_rows.items():
        if r["coverage_status"] == "projected" and r["position"] == "WR":
            assert six[pid]["projected_points"] == r["projected_points"]
        if r["coverage_status"] == "projected" and r["position"] == "QB":
            assert six[pid]["projected_points"] >= r["projected_points"]


def test_an_unsupported_scoring_gets_no_skill_runs_and_says_why(live):  # noqa: F811
    con, src, _ = live
    _store(con, src, _ifp2(src), sha=WEEKLY_SHA)
    _publish_reference(con)
    _set_scoring(con, SCORING_ITEMS + [{"statId": 21, "points": 0.5}])       # an offensive rule Ours cannot score
    assert apply_reference(con, 2026)["skill"] == {"copied": 0, "rescored": 0, "skipped": 0}
    sel = ps.select_run(con, 2026, 6)
    assert sel["run"] is None and "not supported" in sel["unavailable_reason"]


def test_carried_rows_are_replaced_and_an_old_global_file_leaves_the_tables_empty(live):  # noqa: F811
    con, src, _ = live
    _store(con, src, _ifp2(src), sha=WEEKLY_SHA)
    assert apply_reference(con, 2026) == {"skipped": "no reference runs in the global file"}
    for t in DERIVED_TABLES:
        if table_exists(con, t):
            assert con.execute(f"SELECT count(*) FROM {t}").fetchone() == (0,), t


def test_stat_mix_uses_the_last_games_up_to_the_cutoff_and_position_averages(live):  # noqa: F811
    con, _, _ = live
    mix = stat_mix(con, (2026, 5))
    assert any(k.startswith("__pos__") for k in mix)
    players = [k for k in mix if not k.startswith("__pos__")]
    assert players and all(set(mix[p]) <= set(MIX_COLUMNS) for p in players)
    earlier = stat_mix(con, (2026, 4))
    assert earlier != mix                      # week 5's games are only in the later cutoff


def test_projection_ranges_follow_only_the_reference_scoring_and_are_created_when_missing(live):  # noqa: F811
    con, src, _ = live
    con.execute("CREATE TABLE projection_range_quantiles (pin_id VARCHAR, scoring_fingerprint VARCHAR, r50 DOUBLE)")
    con.execute("INSERT INTO projection_range_quantiles VALUES ('p1', ?, 1.5)", [PPR.fingerprint])
    _publish_reference(con)
    con.execute("DROP TABLE projection_range_quantiles")
    assert apply_reference(con, 2026)["ranges"]["projection_range_quantiles"] == 1
    assert con.execute("SELECT * FROM projection_range_quantiles").fetchall() == [("p1", PPR.fingerprint, 1.5)]
    _set_scoring(con, HALF)
    assert apply_reference(con, 2026)["ranges"]["projection_range_quantiles"] == 0
    assert con.execute("SELECT count(*) FROM projection_range_quantiles").fetchone() == (0,)


def test_owner_research_is_not_a_reference_and_stays_the_laptop_leagues_own():
    """Its start/sit evaluation comes from the laptop league's lineup decisions: never shipped to other leagues."""
    from app.db.league_split import LEAGUE_TABLES
    for t in ("projection_performance_reports", "model_replays"):
        assert t not in REFERENCE_TABLES and t not in DERIVED_TABLES and t in LEAGUE_TABLES


# ----------------------------------------------------------------------------- #254: exact outcomes, missing != zero
#
# Synthetic box scores only. The reference is the full-PPR SCORING_ITEMS; the target scorings below are supported by
# league_scoring (Sleeper-style first downs, attempts and completions use its synthetic ids 2001-2006).

ALL_ZEROS = {c: 0.0 for c in MIX_COLUMNS}
REC_FD = SCORING_ITEMS + [{"statId": 2003, "points": 0.5, "pointsOverrides": {}}]       # 0.5 per receiving 1st down


def _with(items, **points_by_stat):
    """SCORING_ITEMS with these statIds re-valued (`s4=6.0` sets statId 4) and any missing statId added."""
    by = {int(k[1:]): v for k, v in points_by_stat.items()}
    out = [{**i, "points": by.pop(i["statId"])} if i["statId"] in by else i for i in items]
    return out + [{"statId": sid, "points": v, "pointsOverrides": {}} for sid, v in by.items()]


def _box_scores(rows: list[dict]):
    """An in-memory nfl_weekly_stats with every MIX column (a value left out is 0.0; None stays NULL)."""
    import duckdb
    con = duckdb.connect()
    con.execute("CREATE TABLE nfl_weekly_stats (player_id VARCHAR, position VARCHAR, season INTEGER, week INTEGER, "
                "season_type VARCHAR, " + ", ".join(f"{c} DOUBLE" for c in MIX_COLUMNS) + ")")
    for r in rows:
        vals = {**ALL_ZEROS, **{k: v for k, v in r.items() if k in MIX_COLUMNS}}
        con.execute(f"INSERT INTO nfl_weekly_stats VALUES (?, ?, 2026, ?, 'REG', {', '.join('?' * len(MIX_COLUMNS))})",
                    [r["player_id"], r["position"], r["week"], *[vals[c] for c in MIX_COLUMNS]])
    return con


def _wr_games(pid, fds):
    return [{"player_id": pid, "position": "WR", "week": w, "receptions": 5.0, "receiving_yards": 60.0,
             "receiving_first_downs": fd} for w, fd in enumerate(fds, start=1)]


def test_a_missing_box_score_component_is_unknown_in_the_mix_never_zero():
    """S1b: stat_mix used avg(coalesce(c, 0)), so a NULL first-down count read as 0 first downs and the re-score of a
    first-down league produced a number. The component is now unknown and the row is not estimated."""
    con = _box_scores(_wr_games("g1", [3.0, None, 3.0]) + _wr_games("g2", [4.0, 2.0, 3.0])
                      + _wr_games("g3", [5.0, 5.0]))                          # g3: two games, below MIX_MIN_GAMES
    mix = stat_mix(con, (2026, 3))
    assert mix["g1"]["receiving_first_downs"] is None                      # old code: (3 + 0 + 3) / 3 = 2.0
    assert mix["g1"]["receptions"] == 5.0                                  # the known components stay
    assert mix["g2"]["receiving_first_downs"] == 3.0
    assert mix["__pos__WR"]["receiving_first_downs"] == 3.0                # only the players who have it (g2)
    assert "g3" not in mix

    ref, target = PPR, resolve_skill_scoring(REC_FD)
    g1, g2, g3 = rescore_skill_rows([_row(gsis_id=g, projected_points=11.0, interval_low=4.0, interval_high=20.0,
                                          p_play=1.0, expected_points=11.0) for g in ("g1", "g2", "g3")],
                                    mix, ref, target)
    assert g1["coverage_status"] == "insufficient_data"
    assert (g1["projected_points"], g1["expected_points"], g1["interval_low"], g1["interval_high"]) == (None,) * 4
    assert MISSING_COMPONENT_FLAG in g1["flags"] and RESCORED_FLAG not in g1["flags"] and NO_MIX_FLAG not in g1["flags"]
    # g2 measured: PPR 5 + 6 = 11, target 11 + 0.5 x 3 = 12.5, so 11.0 x 12.5 / 11 = 12.5 exactly
    assert (g2["projected_points"], g2["interval_low"], g2["interval_high"]) == (12.5, round(4.0 * 12.5 / 11, 2),
                                                                                 round(20.0 * 12.5 / 11, 2))
    assert RESCORED_FLAG in g2["flags"]
    # g3 takes the position's mix, which is g2's here
    assert g3["projected_points"] == 12.5


def test_a_missing_component_neither_scoring_weights_does_not_block_the_re_score():
    con = _box_scores([{**g, "passing_first_downs": None} for g in _wr_games("g1", [2.0, 2.0, 2.0])])
    mix = stat_mix(con, (2026, 3))
    assert mix["g1"]["passing_first_downs"] is None
    (out,) = rescore_skill_rows([_row(projected_points=11.0)], mix, PPR, resolve_skill_scoring(REC_FD))
    assert out["projected_points"] == 12.0 and RESCORED_FLAG in out["flags"]     # 11 x (11 + 1) / 11


def test_a_component_absent_from_the_box_scores_is_missing_not_zero():
    """A league scoring a statistic the stats table does not carry (an older release without first downs)."""
    mix = {"g1": {c: v for c, v in {**ALL_ZEROS, "receptions": 5.0, "receiving_yards": 60.0}.items()
                  if c != "receiving_first_downs"}}
    (out,) = rescore_skill_rows([_row()], mix, PPR, resolve_skill_scoring(REC_FD))
    assert out["projected_points"] is None and MISSING_COMPONENT_FLAG in out["flags"]


def test_unusual_qb_scoring_rescores_exactly():
    """6-point pass TD, 0.05 per pass yard, -3 per interception, +0.5 per completion, -0.25 per attempt."""
    target = resolve_skill_scoring(_with(SCORING_ITEMS, s3=0.05, s4=6.0, s20=-3.0, s2005=0.5, s2004=-0.25))
    assert target.supported
    qb = {**ALL_ZEROS, "attempts": 30.0, "completions": 20.0, "passing_yards": 250.0, "passing_tds": 2.0,
          "passing_interceptions": 1.0, "rushing_yards": 20.0}
    # reference: 10 + 8 - 2 + 2 = 18; target: 12.5 + 12 - 3 + 2 + 10 - 7.5 = 26
    (out,) = rescore_skill_rows([_row(position="QB", projected_points=18.0, interval_low=9.0, interval_high=27.0,
                                      p_play=0.9, expected_points=16.2)], {"g1": qb}, PPR, target)
    assert (out["projected_points"], out["interval_low"], out["interval_high"], out["expected_points"]) == \
        (26.0, 13.0, 39.0, 23.4)


def test_a_scoring_that_awards_nothing_for_his_statistics_gives_a_computed_zero():
    """A receiver in a league that scores receiving at 0: the re-score is a computed 0.0 (a value, not a gap)."""
    target = resolve_skill_scoring(_with(SCORING_ITEMS, s42=0.0, s43=0.0, s53=0.0))
    mix = {"g1": {**ALL_ZEROS, "receptions": 6.0, "receiving_yards": 80.0, "receiving_tds": 0.5}}
    (out,) = rescore_skill_rows([_row()], mix, PPR, target)
    assert (out["projected_points"], out["interval_low"], out["interval_high"], out["expected_points"]) == \
        (0.0, 0.0, 0.0, 0.0)
    assert out["coverage_status"] == "projected" and RESCORED_FLAG in out["flags"]


def test_negative_modifiers_rescore_exactly_and_a_negative_result_keeps_an_ordered_range():
    # -8 per interception: reference 0.04 x 150 - 2 x 1 = 4, target 6 - 8 = -2, so f = -0.5
    target = resolve_skill_scoring(_with(SCORING_ITEMS, s20=-8.0))
    qb = {**ALL_ZEROS, "passing_yards": 150.0, "passing_interceptions": 1.0}
    (out,) = rescore_skill_rows([_row(position="QB", projected_points=10.0, interval_low=2.0, interval_high=18.0,
                                      p_play=1.0, expected_points=10.0)], {"g1": qb}, PPR, target)
    assert (out["projected_points"], out["interval_low"], out["interval_high"], out["expected_points"]) == \
        (-5.0, -9.0, -1.0, -5.0)
    # a fumble penalty: -2 per lost fumble in both, -4 in the target: 11 - 0.5 x 2 = 10 vs 11 - 0.5 x 4 = 9
    fum = resolve_skill_scoring(_with(SCORING_ITEMS, s72=-4.0))
    wr = {**ALL_ZEROS, "receptions": 5.0, "receiving_yards": 60.0, "fumbles_lost_total": 0.5}
    (w,) = rescore_skill_rows([_row(projected_points=10.0)], {"g1": wr}, PPR, fum)
    assert w["projected_points"] == 9.0


def test_a_dst_row_missing_a_scored_statistic_is_not_rescored():
    """A D/ST run stored before another provider's points-allowed tiers existed: its rows have no expectation for
    them, and the old re-score read the missing tier probability as 0."""
    d = _row(position="D/ST", projected_points=4.0, p_play=None, expected_points=4.0,
             stat_line=json.dumps({"scoring_stats": {"99": 2.5, "95": 1.0}}))
    assert rescore_dst_rows([d], {99: 1.0, 95: 2.0, 1090: 7.0}) is None
    (out,) = rescore_dst_rows([d], {99: 1.0, 95: 2.0})
    assert out["projected_points"] == 4.5
