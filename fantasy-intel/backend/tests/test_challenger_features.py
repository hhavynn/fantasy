"""ifp-2 challenger inputs: game classification, healthy-game usage, previous-season priors and
the empirical-Bayes established workload. Hand-built panels with invented values: these tests check
behaviour and point-in-time safety, not forecasting accuracy."""

from __future__ import annotations

import polars as pl
import pytest

from app.analytics import challenger as ch
from app.analytics.forecast_features import (EXITS_SCHEMA, INJURY_SCHEMA, PANEL_COLUMNS, SHRINK_GAMES,
                                             USAGE_PRIOR_GAMES, Sources, _panel_schema, classify_games,
                                             features_for_week)
from app.analytics.independent_projections import RidgeModel, fit_ridge
from app.nfl.pbp_exits import derive

TEAMS = ("AAA", "BBB", "CCC", "DDD")
P = "00-RB1"


def row(season, week, gid=P, pos="RB", team="AAA", *, carries=18.0, targets=4.0, snap=0.75, pts=None,
        xfpl=None, xfpl_td=None, td_points=0.0):
    xfpl = 0.1 * 4.2 * carries + 1.0 * 0.65 * targets + 0.1 * 7.5 * targets + 6 * 0.03 * carries if xfpl is None else xfpl
    return {"season": season, "week": week, "gsis_id": gid, "position": pos, "team": team,
            "opponent": {"AAA": "BBB", "BBB": "AAA", "CCC": "DDD", "DDD": "CCC"}[team], "has_stats": True,
            "has_snaps": snap is not None, "league_points": xfpl if pts is None else pts, "pass_attempts": 0.0,
            "carries": carries, "targets": targets, "receptions": 0.65 * targets, "air_yards": 6.0 * targets,
            "passing_yards": 0.0, "rushing_yards": 4.2 * carries, "receiving_yards": 5.0 * targets,
            "offense_pct": snap, "xfp": xfpl, "rz_targets": 0.0, "rz_carries": 1.0, "gl_carries": 0.0,
            "designed_rushes": carries, "team_targets": 35.0, "team_rush_attempts": 26.0, "team_rb_carries": 22.0,
            "td_points": td_points, "xfpl": xfpl, "xfpl_td": (0.18 * carries if xfpl_td is None else xfpl_td),
            "team_plays": 66.0}


def games(seasons_weeks, margins=None):
    out = []
    for s, w in seasons_weeks:
        m = (margins or {}).get((s, w), 4)
        for home, away in (("AAA", "BBB"), ("CCC", "DDD")):
            for team, opp, is_home, pf, pa in ((home, away, True, 20 + m, 20), (away, home, False, 20, 20 + m)):
                out.append({"season": s, "week": w, "game_id": f"{s}_{w}_{away}_{home}", "team": team, "opponent": opp,
                            "is_home": is_home, "gameday": f"{s}-09-{min(28, w):02d}", "gametime": "13:00",
                            "points_for": float(pf), "points_against": float(pa)})
    return pl.DataFrame(out)


def sources(rows, *, injuries=None, exits=None, margins=None, extra_weeks=()):
    sw = sorted({(r["season"], r["week"]) for r in rows} | set(extra_weeks))
    panel = pl.DataFrame(rows).cast(_panel_schema(), strict=False).select(PANEL_COLUMNS)
    defense = pl.DataFrame(schema={"season": pl.Int64, "week": pl.Int64, "defteam": pl.String, "pass_plays": pl.Float64,
                                   "pass_epa_sum": pl.Float64, "rush_plays": pl.Float64, "rush_epa_sum": pl.Float64})
    inj = pl.DataFrame(injuries or [], schema=INJURY_SCHEMA)
    ex = None if exits is None else pl.DataFrame(exits, schema=EXITS_SCHEMA)
    return Sources(panel, games(sw, margins), defense, None, inj, ex)


def feats(src, season, week, gid=P, pos="RB", team="AAA"):
    t = pl.DataFrame({"gsis_id": [gid], "position": [pos], "team": [team],
                      "opponent": [{"AAA": "BBB", "CCC": "DDD"}.get(team, "AAA")], "is_home": [True]})
    return features_for_week(src, season, week, t).row(0, named=True)


def exit_row(season, week, gid=P):
    return {"season": season, "week": week, "gsis_id": gid, "injured_in_game": 1, "injury_sec": 300.0,
            "last_touch_sec": 300.0, "team_last_sec": 3600.0}


# ----------------------------------------------------------------------------- Case A / Example C


def test_injury_exit_does_not_pull_down_healthy_usage():
    """Elite RB: 18 carries on 75% snaps, then 2 carries on 8% snaps with a play-by-play injury."""
    rows = [row(2026, w) for w in (1, 2, 3, 4)] + [row(2026, 5, carries=2, targets=0, snap=0.08)]
    src = sources(rows, exits=[exit_row(2026, 5)], extra_weeks=[(2026, 6)])
    f = feats(src, 2026, 6)
    assert f["last_injury_exit"] == 1 and f["inj_exits_recent"] == 1 and f["last_role_low"] == 0
    assert f["carries_pg"] == pytest.approx((18 * 4 + 2) / 5)           # the ifp-1.0.0 input drops ~20%
    assert f["h_carries_pg"] == pytest.approx(18.0)                     # healthy-game usage does not
    assert f["h_games_recent"] == 4 and f["h_snap_pct"] == pytest.approx(0.75)


def test_injury_exit_needs_evidence_and_a_new_report_counts():
    """Same snap drop: a NEW injury listing after the game marks an exit; a listing that was already
    on that week's own report is ambiguous; no evidence at all is a possible role cut."""
    rows = [row(2026, w) for w in (1, 2, 3, 4)] + [row(2026, 5, carries=3, snap=0.2)]
    listing = {"season": 2026, "week": 6, "gsis_id": P, "team": "AAA", "report_status": "Questionable",
               "practice_status": "Limited", "injury": "Ankle", "not_injury": False}
    new = feats(sources(rows, injuries=[listing], extra_weeks=[(2026, 6)]), 2026, 6)
    assert new["last_injury_exit"] == 1
    chronic = feats(sources(rows, injuries=[listing, {**listing, "week": 5}], extra_weeks=[(2026, 6)]), 2026, 6)
    assert chronic["last_injury_exit"] == 0 and chronic["last_role_low"] == 0
    none = feats(sources(rows, extra_weeks=[(2026, 6)]), 2026, 6)
    assert none["last_injury_exit"] == 0 and none["last_role_low"] == 1
    rest = feats(sources(rows, injuries=[{**listing, "not_injury": True}], extra_weeks=[(2026, 6)]), 2026, 6)
    assert rest["last_injury_exit"] == 0        # "not injury related" rest listings are never exits


def test_blowout_snap_drop_is_game_script_not_injury_or_demotion():
    rows = [row(2026, w) for w in (1, 2, 3, 4)] + [row(2026, 5, carries=8, snap=0.45)]
    f = feats(sources(rows, margins={(2026, 5): 31}, extra_weeks=[(2026, 6)]), 2026, 6)
    assert f["last_blowout_low"] == 1 and f["last_injury_exit"] == 0 and f["last_role_low"] == 0


def test_low_fantasy_score_alone_is_never_an_injury_exit():
    rows = [row(2026, w) for w in (1, 2, 3, 4)] + [row(2026, 5, pts=0.5)]   # normal snaps, bad game
    f = feats(sources(rows, extra_weeks=[(2026, 6)]), 2026, 6)
    assert f["last_injury_exit"] == 0 and f["last_role_low"] == 0 and f["inj_exits_recent"] == 0


# ----------------------------------------------------------------------------- Case B / Case D


def test_genuine_demotion_lowers_healthy_usage_and_established_workload():
    """Three healthy full games at a reduced role (no injury evidence) are counted, not discounted,
    and the current-season blend moves away from last season's heavy workload."""
    prev = [row(2025, w, carries=20) for w in range(1, 16)]
    cur = [row(2026, w, carries=20) for w in (1, 2)] + [row(2026, w, carries=6, snap=0.40) for w in (3, 4, 5)]
    f = feats(sources(prev + cur, extra_weeks=[(2026, 6)]), 2026, 6)
    assert f["inj_exits_recent"] == 0
    assert f["h_carries_pg"] == pytest.approx((20 * 2 + 6 * 3) / 5)
    heavy = feats(sources(prev + [row(2026, w, carries=20) for w in range(1, 6)], extra_weeks=[(2026, 6)]), 2026, 6)
    assert f["xfpl_eb"] < heavy["xfpl_eb"] - 1.0


def test_repeated_partial_participation_is_counted_separately():
    rows = [row(2026, 1), row(2026, 2, carries=3, snap=0.1), row(2026, 3), row(2026, 4, carries=2, snap=0.1),
            row(2026, 5)]
    f = feats(sources(rows, exits=[exit_row(2026, 2), exit_row(2026, 4)], extra_weeks=[(2026, 6)]), 2026, 6)
    assert f["inj_exits_recent"] == 2 and f["last_injury_exit"] == 0
    assert f["h_carries_pg"] == pytest.approx(18.0)                     # usage when healthy is preserved
    assert ch.FLAGS[0] == "inj_exits_recent"                            # and the exit count is its own input


# ----------------------------------------------------------------------------- Case C (breakout), priors


def test_breakout_overtakes_previous_season_as_evidence_accumulates():
    """Second-year WR: 4 targets/game last season, 11 this season. The established workload moves
    toward the current level with every healthy game (EB weight n / (n + k))."""
    prev = [row(2025, w, pos="WR", gid="00-WR", carries=0, targets=4) for w in range(1, 16)]
    cur = [row(2026, w, pos="WR", gid="00-WR", carries=0, targets=11) for w in range(1, 7)]
    src = sources(prev + cur, extra_weeks=[(2026, 7)])
    gaps = []
    for week in (2, 4, 7):
        f = feats(src, 2026, week, gid="00-WR", pos="WR")
        n = f["cur_h_games"]
        assert f["xfpl_eb"] == pytest.approx((f["cur_h_xfpl_pg"] * n + f["prev_xfpl_pg"] * USAGE_PRIOR_GAMES)
                                             / (n + USAGE_PRIOR_GAMES))
        gaps.append(abs(f["xfpl_eb"] - f["cur_h_xfpl_pg"]))
    assert gaps[0] > gaps[1] > gaps[2]
    assert feats(src, 2026, 7, gid="00-WR", pos="WR")["xfpl_eb"] > 0.6 * f["cur_h_xfpl_pg"] + 0.4 * f["prev_xfpl_pg"]


def test_no_current_season_history_uses_shrunk_previous_season():
    prev = [row(2025, w) for w in range(1, 11)]
    f = feats(sources(prev, extra_weeks=[(2026, 1)]), 2026, 1)
    assert f["cur_h_games"] == 0 and f["games_season"] == 0
    assert f["xfpl_eb"] == pytest.approx(f["prev_xfpl_pg"])
    assert f["prev_h_games"] == 10
    expected = (sum(r["xfpl"] for r in prev) + f["prior_xfpl"] * SHRINK_GAMES) / (10 + SHRINK_GAMES)
    assert f["prev_xfpl_pg"] == pytest.approx(expected)


# ----------------------------------------------------------------------------- Case E / Case F


def test_team_change_is_flagged_for_previous_season_usage():
    prev = [row(2025, w, team="CCC", carries=22) for w in range(1, 10)]
    f = feats(sources(prev + [row(2026, 1, team="AAA", carries=7)], extra_weeks=[(2026, 2)]), 2026, 2)
    assert f["prev_team_changed"] == 1 and f["team_changed"] == 0     # changed in the offseason
    f2 = feats(sources(prev, extra_weeks=[(2026, 1)]), 2026, 1, team="AAA")
    assert f2["prev_team_changed"] == 1 and f2["team_changed"] == 1


def test_rookie_has_no_fabricated_history():
    other = [row(2026, w, gid="00-VET") for w in (1, 2)]
    f = feats(sources(other, extra_weeks=[(2026, 3)]), 2026, 3, gid="00-ROOKIE")
    assert f["games_recent"] == 0 and f["prev_h_games"] == 0 and f["cur_h_games"] == 0
    for c in ("xfpl_eb", "prev_xfpl_pg", "h_xfpl_pg", "h_carries_pg", "xfpl_recent"):
        assert f[c] is None


def test_return_from_multigame_absence():
    rows = [row(2026, w) for w in (1, 2)] + [row(2026, 7, gid="00-OTHER")]
    f = feats(sources(rows, extra_weeks=[(2026, 8)]), 2026, 8)
    assert f["weeks_since_last"] == 6 and f["games_recent"] == 2 and f["h_carries_pg"] == pytest.approx(18.0)


# ----------------------------------------------------------------------------- Example A, missing data


def test_touchdown_boom_is_split_from_opportunity():
    """3 targets, 2 touchdowns: points jump, expected points do not; the excess sits in td_over_exp."""
    base = [row(2026, w, pos="WR", gid="00-WR", carries=0, targets=3) for w in (1, 2, 3, 4)]
    x = base[0]["xfpl"]
    boom = row(2026, 5, pos="WR", gid="00-WR", carries=0, targets=3, pts=x + 12 + 9, td_points=12.0)
    f = feats(sources(base + [boom], extra_weeks=[(2026, 6)]), 2026, 6, gid="00-WR", pos="WR")
    calm = feats(sources(base + [row(2026, 5, pos="WR", gid="00-WR", carries=0, targets=3)],
                         extra_weeks=[(2026, 6)]), 2026, 6, gid="00-WR", pos="WR")
    assert f["xfpl_recent"] == pytest.approx(calm["xfpl_recent"])      # opportunity unchanged
    assert f["pts_recent"] > calm["pts_recent"] + 3                      # raw production jumps
    assert f["td_over_exp"] == pytest.approx((12 - 5 * 0.0) / (5 + SHRINK_GAMES))
    assert f["yd_over_exp"] == pytest.approx(9 / (5 + SHRINK_GAMES))


def test_missing_xfp_and_snaps_stay_null_with_indicators():
    rows = [row(2026, w, snap=None, xfpl=None) for w in (1, 2, 3)]
    for r in rows:
        r.update(xfpl=None, xfpl_td=None, xfp=None, offense_pct=None, has_snaps=False)
    f = feats(sources(rows, extra_weeks=[(2026, 4)]), 2026, 4)
    assert f["xfpl_missing"] == 1 and f["xfpl_recent"] is None and f["h_xfpl_pg"] is None
    assert f["h_snap_pct"] is None and f["last_injury_exit"] == 0       # "unknown" class is never an exit
    assert f["h_carries_pg"] == pytest.approx(18.0)


# ----------------------------------------------------------------------------- point-in-time safety


def test_classification_never_reads_information_after_the_cutoff():
    """A listing published for week 7 cannot relabel week 5 in a week-6 forecast, and play-by-play
    of the target week itself is ignored."""
    rows = [row(2026, w) for w in (1, 2, 3, 4)] + [row(2026, 5, carries=3, snap=0.2)]
    late = {"season": 2026, "week": 7, "gsis_id": P, "team": "AAA", "report_status": "Out",
            "practice_status": "DNP", "injury": "Knee", "not_injury": False}
    base = sources(rows, extra_weeks=[(2026, 6), (2026, 7)])
    with_late = sources(rows, injuries=[late], exits=[exit_row(2026, 6)], extra_weeks=[(2026, 6), (2026, 7)])
    assert feats(base, 2026, 6) == feats(with_late, 2026, 6)
    assert feats(with_late, 2026, 6)["last_injury_exit"] == 0
    # ... but the week-7 forecast may use the week-7 pregame report
    assert classify_games(with_late.panel, with_late, 2026, 7).filter(
        pl.col("week") == 5)["gclass"].item() == "injury_exit"


def test_future_rows_do_not_change_any_feature():
    rows = [row(2026, w) for w in range(1, 6)]
    future = [row(2026, w, carries=50, snap=0.1, pts=99) for w in (6, 7)]
    a = feats(sources(rows, extra_weeks=[(2026, 6)]), 2026, 6)
    b = feats(sources(rows + future, exits=[exit_row(2026, 6)]), 2026, 6)
    assert a == b


# ----------------------------------------------------------------------------- models


def test_fit_linear_equals_existing_ridge_and_is_deterministic():
    rows = [row(s, w, gid=f"00-{i}", carries=5 + i % 9, pts=None) for s in (2024, 2025) for w in range(1, 18)
            for i in range(30)]
    src = sources(rows, extra_weeks=[(2026, 1)])
    store = pl.concat([features_for_week(src, 2025, w, pl.DataFrame(
        {"gsis_id": [f"00-{i}" for i in range(30)], "position": ["RB"] * 30, "team": ["AAA"] * 30,
         "opponent": ["BBB"] * 30, "is_home": [True] * 30})).with_columns(pl.lit(float(w)).alias("label"))
                       for w in range(3, 18)], how="diagonal_relaxed")
    store = store.with_columns(pl.col("label") + pl.col("carries_pg"))
    ref = fit_ridge(store, "RB", "C", [0.01])[0.01]
    new = ch.fit_linear(store, "RB", ref.features, 0.01)
    assert new.coef == pytest.approx(ref.coef, abs=1e-9) and new.intercept == pytest.approx(ref.intercept)
    h1 = ch.fit_linear(store, "RB", ref.features, 0.01, loss="huber", half_life_weeks=36, target_key=202601)
    h2 = ch.fit_linear(store, "RB", ref.features, 0.01, loss="huber", half_life_weeks=36, target_key=202601)
    assert h1.coef == h2.coef and isinstance(h1, RidgeModel)


def test_frozen_config_loads_and_matches_feature_constants():
    cfg = ch.ChallengerConfig.load()
    assert cfg.version == ch.CHALLENGER_VERSION and cfg.selected_on == [2021, 2022]
    assert cfg.positions["QB"]["groups"] == ""                          # QB keeps the ifp-1.0.0 inputs
    for pos in ("RB", "WR", "TE"):
        assert "xfpl_eb" in cfg.features(pos) and "last_injury_exit" in cfg.features(pos)
    assert cfg.hash() == ch.ChallengerConfig.load().hash()


# ----------------------------------------------------------------------------- play-by-play parsing


def test_pbp_injury_mentions_need_team_jersey_and_surname():
    pbp = pl.DataFrame({
        "game_id": ["g1"] * 4, "season": [2026] * 4, "week": [3] * 4, "season_type": ["REG"] * 4,
        "posteam": ["AAA", "AAA", "BBB", "AAA"], "defteam": ["BBB", "BBB", "AAA", "BBB"], "qtr": [1, 1, 2, 3],
        "game_seconds_remaining": [3500.0, 3400.0, 2000.0, 1500.0], "play_id": [1, 2, 3, 4],
        "play_type": ["run", "run", "pass", "pass"], "sack": [0, 0, 0, 0],
        "rusher_player_id": ["00-RB1", "00-RB1", None, None], "passer_player_id": [None, None, "00-QB", "00-QB2"],
        "receiver_player_id": [None, None, "00-WR", "00-TE"],
        "desc": ["(14:20) 26-S.Back up the middle for 3 yards. AAA-26-S.Back was injured during the play.",
                 "(12:00) 26-S.Back left end for 2 yards.",
                 "(5:00) pass to 1-W.Ide. AAA-80-X.Wrongname was injured during the play.",
                 "(10:00) pass to 85-T.End for 9 yards."]})
    rosters = pl.DataFrame({"season": [2026] * 3, "week": [3] * 3, "team": ["AAA", "AAA", "BBB"],
                            "jersey_number": [26, 80, 1], "gsis_id": ["00-RB1", "00-TE", "00-WR"],
                            "position": ["RB", "TE", "WR"], "last_name": ["Back", "End", "Ide"]})
    d = derive(pbp, rosters)
    rb = d.filter(pl.col("gsis_id") == "00-RB1").row(0, named=True)
    assert rb["injured_in_game"] == 1 and rb["injury_sec"] == 100.0 and rb["touches"] == 2
    te = d.filter(pl.col("gsis_id") == "00-TE").row(0, named=True)
    assert te["injured_in_game"] == 0                                   # surname mismatch: dropped, not guessed
