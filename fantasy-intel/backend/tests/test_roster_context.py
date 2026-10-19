"""QB and teammate context (research only): expected starter, shrunk QB profiles, teammate
availability and the team opportunity budget, point-in-time safety, missing data, and isolation
from production. Hand-built frames with invented values: these tests check behaviour, not
forecasting accuracy. The player names are labels for a hypothetical scenario only."""

from __future__ import annotations

import pathlib
import subprocess
import sys

import duckdb
import numpy as np
import polars as pl
import pytest

from app.analytics import challenger as ch
from app.analytics import context_challenger as cc
from app.analytics import roster_context as rc
from app.analytics.forecast_features import (INJURY_SCHEMA, PANEL_COLUMNS, ROSTER_SCHEMA, Sources, _panel_schema,
                                             load_injuries)

S, W = 2025, 6
OPP = {"CHI": "GB", "GB": "CHI", "DET": "MIN", "MIN": "DET"}
CALEB, KEENUM, LOVE, GOFF, DARNOLD, BK = "00-CALEB", "00-KEENUM", "00-LOVE", "00-GOFF", "00-DARNOLD", "00-BK1"
DJM, BURDEN, KMET, SWIFT, RB2, RB3 = "00-DJM", "00-BURDEN", "00-KMET", "00-SWIFT", "00-RB2", "00-RB3"
CHI_SKILL = {CALEB: ("QB", 0, 4), DJM: ("WR", 9, 0), BURDEN: ("WR", 7, 0), KMET: ("TE", 5, 0),
             SWIFT: ("RB", 3, 15), RB2: ("RB", 2, 6), RB3: ("RB", 1, 3)}     # gsis -> (pos, targets, carries)


def prow(season, week, gid, pos, team, targets=0.0, carries=0.0, tt=27.0, tr=28.0):
    return {"season": season, "week": week, "gsis_id": gid, "position": pos, "team": team, "opponent": OPP[team],
            "has_stats": True, "has_snaps": True, "league_points": 10.0, "pass_attempts": 0.0, "carries": carries,
            "targets": targets, "receptions": 0.6 * targets, "air_yards": 8.0 * targets, "passing_yards": 0.0,
            "rushing_yards": 4.0 * carries, "receiving_yards": 7.0 * targets, "offense_pct": 0.8, "xfp": 10.0,
            "rz_targets": 0.0, "rz_carries": 0.0, "gl_carries": 0.0, "designed_rushes": carries,
            "team_targets": tt, "team_rush_attempts": tr, "team_rb_carries": tr - 4, "td_points": 0.0, "xfpl": 10.0,
            "xfpl_td": 1.0, "team_plays": 62.0}


def world(weeks=range(1, W), *, skip=None, injuries=(), roster_status=None, roster_team=None, starters=None,
          extra_qb=(), keenum=(15.0, 1.0, -4.8, -6.0)):
    """CHI/GB/DET/MIN, season 2025 weeks 1-5 played. `skip`: {gsis: weeks absent}."""
    skip = skip or {}
    panel, ros, st, qb = [], [], [], []
    for w in weeks:
        for g, (pos, t, c) in CHI_SKILL.items():
            if w not in skip.get(g, ()):
                panel.append(prow(S, w, g, pos, "CHI", t, c))
        for team, q in (("GB", LOVE), ("DET", GOFF), ("MIN", DARNOLD)):
            panel.append(prow(S, w, q, "QB", team, 0, 3))
        for team, q in (("CHI", CALEB), ("GB", LOVE), ("DET", GOFF), ("MIN", DARNOLD)):
            st.append({"season": S, "week": w, "team": team, "qb_id": (starters or {}).get((team, w), q)})
        qb += [{"season": S, "week": w, "gsis_id": CALEB, "team": "CHI", "attempts": 35.0, "sacks": 2.0,
                "epa": 5.55, "cpoe": 3.0},
               {"season": S, "week": w, "gsis_id": LOVE, "team": "GB", "attempts": 33.0, "sacks": 2.0, "epa": 0.0,
                "cpoe": 0.0},
               {"season": S, "week": w, "gsis_id": GOFF, "team": "DET", "attempts": 34.0, "sacks": 1.0, "epa": 3.0,
                "cpoe": 2.0},
               {"season": S, "week": w, "gsis_id": DARNOLD, "team": "MIN", "attempts": 30.0, "sacks": 3.0,
                "epa": -2.0, "cpoe": -1.0}]
        for g, (pos, _, _) in [*CHI_SKILL.items(), (KEENUM, ("QB", 0, 0))]:
            status = (roster_status or {}).get((g, w), "ACT")
            team = (roster_team or {}).get((g, w), "CHI")
            ros.append({"season": S, "week": w, "gsis_id": g, "team": team, "position": pos, "status": status})
    a, sk, e, c = keenum
    qb += [{"season": S - 1, "week": 3, "gsis_id": KEENUM, "team": "CHI", "attempts": a, "sacks": sk, "epa": e,
            "cpoe": c},
           {"season": S - 1, "week": 4, "gsis_id": BK, "team": "DET", "attempts": 50.0, "sacks": 4.0, "epa": -10.8,
            "cpoe": -4.0}]
    qb += list(extra_qb)
    panel_df = pl.DataFrame(panel).cast(_panel_schema(), strict=False).select(PANEL_COLUMNS)
    src = Sources(panel_df, pl.DataFrame(schema={"season": pl.Int64, "week": pl.Int64}), pl.DataFrame(),
                  pl.DataFrame(ros, schema=ROSTER_SCHEMA),
                  None if injuries is None else pl.DataFrame(list(injuries), schema=INJURY_SCHEMA))
    ctx = rc.ContextSources(pl.DataFrame(st, schema=rc.STARTER_SCHEMA), pl.DataFrame(qb, schema=rc.QB_SCHEMA))
    return src, ctx


def inj(gid, status, week=W, team="CHI"):
    return {"season": S, "week": week, "gsis_id": gid, "team": team, "report_status": status,
            "practice_status": None, "injury": "Knee", "not_injury": False}


def ctx_rows(src, ctx, players=(BURDEN, KMET, SWIFT, RB2, RB3, DJM, CALEB), week=W, team="CHI", **kw):
    pos = {g: v[0] for g, v in CHI_SKILL.items()} | {KEENUM: "QB"}
    t = pl.DataFrame({"gsis_id": list(players), "position": [pos.get(g, "WR") for g in players],
                      "team": [team] * len(players), "opponent": [OPP[team]] * len(players)})
    out = rc.context_for_week(src, ctx, S, week, t, **kw)
    return {r["gsis_id"]: r for r in out.iter_rows(named=True)}


# ----------------------------------------------------------------------------- QB context


def test_burden_recognises_caleb_to_keenum_change_and_profiles_update():
    src, ctx = world(injuries=[inj(CALEB, "Out")])
    r = ctx_rows(src, ctx)[BURDEN]
    assert r["expected_qb_id"] == KEENUM and r["recent_qb_id"] == CALEB and r["qb_changed"] == 1
    assert r["prev_starter_status"] == "out" and r["qb_confidence"] == "uncertain"   # no start, no relief work
    profiles, prior = rc.qb_profiles(ctx, S, W)
    # Keenum: 16 dropbacks, shrunk toward the replacement-level prior (completed backup seasons)
    assert profiles[KEENUM]["dropbacks"] == 16.0
    assert r["exp_qb_epa"] == pytest.approx(rc.shrink(-4.8, 16.0, prior["epa"], rc.QB_SHRINK_DROPBACKS))
    assert r["exp_qb_dropbacks"] == 16.0
    assert r["qb_epa_delta"] == pytest.approx(profiles[KEENUM]["epa"] - profiles[CALEB]["epa"])
    assert r["qb_cpoe_delta"] == pytest.approx(profiles[KEENUM]["cpoe"] - profiles[CALEB]["cpoe"])
    # the same week without the injury: Caleb expected, no change, delta 0
    src2, ctx2 = world(injuries=[inj(KMET, "Questionable")])
    r2 = ctx_rows(src2, ctx2)[BURDEN]
    assert r2["expected_qb_id"] == CALEB and r2["qb_confidence"] == "supported"
    assert r2["qb_changed"] == 0 and r2["qb_epa_delta"] == pytest.approx(0.0)


def _fitted_model(feats=("pts_recent", "qb_epa_delta", "qb_changed")):
    rng = np.random.default_rng(3)
    n = 600
    d = pl.DataFrame({"season": [2024] * n, "week": list(rng.integers(1, 18, n)), "pts_recent": rng.uniform(2, 20, n),
                      "qb_epa_delta": rng.normal(0, 0.1, n), "qb_changed": rng.integers(0, 2, n).astype(float)})
    d = d.with_columns((pl.col("pts_recent") + 15 * pl.col("qb_epa_delta") + rng.normal(0, 2, n)).alias("label"))
    return ch.fit_linear(d, "WR", list(feats), 0.01)


def test_burden_projection_change_follows_inputs_and_coefficients_not_a_hardcoded_downgrade():
    src, ctx = world(injuries=[inj(CALEB, "Out")])
    scen = ctx_rows(src, ctx)[BURDEN]
    m = _fitted_model()
    base_row = pl.DataFrame({"pts_recent": [12.0], "qb_epa_delta": [0.0], "qb_changed": [0.0]})
    scen_row = pl.DataFrame({"pts_recent": [12.0], "qb_epa_delta": [scen["qb_epa_delta"]],
                             "qb_changed": [float(scen["qb_changed"])]})
    diff = float(m.predict(scen_row)[0] - m.predict(base_row)[0])
    beta = dict(zip(m.columns, np.array(m.coef) / np.array(m.stds)))
    expected = beta["qb_epa_delta"] * scen["qb_epa_delta"] + beta["qb_changed"] * 1.0
    assert diff == pytest.approx(expected, abs=1e-9)          # exactly the fitted linear response
    # if the two QB profiles are identical the delta is 0 and only the (fitted) change flag can move it
    src2, ctx2 = world(injuries=[inj(CALEB, "Out")], keenum=(35.0 * 5, 10.0, 5.55 * 5, 3.0))
    flat = ctx_rows(src2, ctx2)[BURDEN]
    assert flat["qb_epa_delta"] == pytest.approx(0.0) and flat["qb_cpoe_delta"] == pytest.approx(0.0)
    flat_row = scen_row.with_columns(pl.lit(0.0).alias("qb_epa_delta"))
    assert float(m.predict(flat_row)[0] - m.predict(base_row)[0]) == pytest.approx(beta["qb_changed"], abs=1e-9)


def test_backup_with_tiny_sample_is_shrunk_toward_backups_not_starters():
    src, ctx = world()
    profiles, prior = rc.qb_profiles(ctx, S, W)
    raw = -4.8 / 16.0
    k = profiles[KEENUM]
    assert abs(k["epa"] - prior["epa"]) < abs(raw - prior["epa"])
    assert (k["epa"] - prior["epa"]) == pytest.approx((raw - prior["epa"]) * 16 / (16 + rc.QB_SHRINK_DROPBACKS))
    # the prior comes from completed low-volume seasons (Keenum + BK1), well below Caleb's level
    assert prior["epa"] == pytest.approx((-4.8 - 10.8) / (16 + 54))
    assert prior["epa"] < profiles[CALEB]["epa"]
    # a known QB with no passing history gets the prior at n = 0; an unknown QB gets nothing
    assert rc.profile_of(profiles, prior, "00-NOBODY") == {"epa": prior["epa"], "cpoe": prior["cpoe"],
                                                           "dropbacks": 0.0, "attempts": 0.0}
    assert rc.profile_of(profiles, prior, None) is None


def test_ambiguous_starter_levels():
    src, ctx = world(injuries=[inj(CALEB, "Doubtful")])
    q = rc.expected_starters(src, ctx, S, W, ["CHI"])["CHI"]
    assert q["expected_qb_id"] == KEENUM and q["qb_confidence"] == "uncertain"
    src, ctx = world(injuries=[inj(CALEB, "Questionable")])
    q = rc.expected_starters(src, ctx, S, W, ["CHI"])["CHI"]
    assert q["expected_qb_id"] == CALEB and q["qb_confidence"] == "probable"


def test_takeover_in_last_game_makes_the_starter_uncertain():
    extra = [{"season": S, "week": 5, "gsis_id": KEENUM, "team": "CHI", "attempts": 40.0, "sacks": 1.0, "epa": 0.0,
              "cpoe": 0.0}]
    src, ctx = world(injuries=[inj(KMET, "Questionable")], extra_qb=extra)
    q = rc.expected_starters(src, ctx, S, W, ["CHI"])["CHI"]
    assert q["expected_qb_id"] == CALEB and q["qb_confidence"] == "uncertain" and q["alt_qb_id"] == KEENUM
    # and if Caleb is then ruled out, Keenum's relief work makes him a probable (not uncertain) starter
    src, ctx = world(injuries=[inj(CALEB, "Out")], extra_qb=extra)
    q = rc.expected_starters(src, ctx, S, W, ["CHI"])["CHI"]
    assert q["expected_qb_id"] == KEENUM and q["qb_confidence"] == "probable"


def _keenum_started_week5(**kw):
    src, ctx = world(starters={("CHI", 5): KEENUM}, **kw)    # Caleb started weeks 1-4, Keenum week 5
    qb = ctx.qb_stats.filter(~((pl.col("gsis_id") == CALEB) & (pl.col("week") == 5)))
    qb = pl.concat([qb, pl.DataFrame([{"season": S, "week": 5, "gsis_id": KEENUM, "team": "CHI", "attempts": 30.0,
                                       "sacks": 2.0, "epa": 0.0, "cpoe": 0.0}], schema=rc.QB_SCHEMA)])
    return src, rc.ContextSources(ctx.starters, qb)


def test_returning_incumbent_needs_pregame_evidence():
    src, ctx = _keenum_started_week5(injuries=[inj(KMET, "Questionable")])
    q = rc.expected_starters(src, ctx, S, W, ["CHI"])["CHI"]
    assert q["expected_qb_id"] == KEENUM and q["qb_confidence"] == "supported"   # no evidence Caleb is back
    src, ctx = _keenum_started_week5(injuries=[inj(CALEB, None)])               # listed, no designation
    q = rc.expected_starters(src, ctx, S, W, ["CHI"])["CHI"]
    assert q["expected_qb_id"] == CALEB and q["qb_confidence"] == "uncertain" and q["qb_reason"] == "incumbent_returning"
    assert q["alt_qb_id"] == KEENUM
    src, ctx = _keenum_started_week5(injuries=[inj(CALEB, "Questionable")])
    q = rc.expected_starters(src, ctx, S, W, ["CHI"])["CHI"]
    assert q["expected_qb_id"] == KEENUM and q["qb_confidence"] == "uncertain" and q["alt_qb_id"] == CALEB
    src, ctx = _keenum_started_week5(roster_status={(CALEB, 5): "RES"}, injuries=[inj(KMET, "Questionable")])
    q = rc.expected_starters(src, ctx, S, W, ["CHI"])["CHI"]
    assert q["expected_qb_id"] == KEENUM                                         # on reserve: not back


def test_prior_season_qb_needs_current_season_roster_evidence():
    old = "00-OLDQB"
    extra = [{"season": S - 1, "week": w, "gsis_id": old, "team": "CHI", "attempts": 35.0, "sacks": 2.0, "epa": 2.0,
              "cpoe": 1.0} for w in range(1, 18)]

    def with_old_qb(roster_status=None):
        src, ctx = world(injuries=[inj(CALEB, "Out")], extra_qb=extra)
        old_panel = pl.DataFrame([prow(S - 1, 17, old, "QB", "CHI", 0, 3)]).cast(_panel_schema(), strict=False).select(
            PANEL_COLUMNS)
        ros = src.rosters
        if roster_status:
            ros = pl.concat([ros, pl.DataFrame([{"season": S, "week": 5, "gsis_id": old, "team": "CHI",
                                                 "position": "QB", "status": roster_status}], schema=ROSTER_SCHEMA)])
        return Sources(pl.concat([old_panel, src.panel]), src.games, src.defense, ros, src.injuries), ctx

    # left in the offseason (no current-season roster row): never a candidate, despite 629 old team dropbacks
    src, ctx = with_old_qb()
    assert rc.expected_starters(src, ctx, S, W, ["CHI"])["CHI"]["expected_qb_id"] == KEENUM
    # still on the active roster this season: a legitimate candidate, ranked on his team dropbacks
    src, ctx = with_old_qb("ACT")
    assert rc.expected_starters(src, ctx, S, W, ["CHI"])["CHI"]["expected_qb_id"] == old
    # on reserve this season: not available
    src, ctx = with_old_qb("RES")
    assert rc.expected_starters(src, ctx, S, W, ["CHI"])["CHI"]["expected_qb_id"] == KEENUM


def test_missing_starter_id_is_unknown_with_null_features():
    src, ctx = world(starters={("CHI", 5): None})
    r = ctx_rows(src, ctx)[BURDEN]
    assert r["qb_confidence"] == "unknown" and r["expected_qb_id"] is None
    assert r["exp_qb_epa"] is None and r["qb_epa_delta"] is None and r["qb_changed"] is None


def test_week_one_has_no_expected_starter():
    src, ctx = world()
    out = rc.context_for_week(src, ctx, S, 1, pl.DataFrame({"gsis_id": [BURDEN], "position": ["WR"], "team": ["CHI"],
                                                            "opponent": ["GB"]}))
    r = out.row(0, named=True)
    assert r["qb_confidence"] == "unknown" and r["qb_reason"] == "no_game_this_season"
    assert r["tm_tgt_vacated"] is None and r["tm_games"] is None


# ----------------------------------------------------------------------------- teammates


def test_wr1_ruled_out_redistributes_within_the_team_budget():
    src, ctx = world(injuries=[inj(DJM, "Out")])
    rows = ctx_rows(src, ctx)
    b = rows[BURDEN]
    assert b["tm_tgt_vacated"] == pytest.approx(9 / 27) and b["top_tgt_comp_out"] == 1
    assert b["tm_tgt_gain_pg"] == pytest.approx((7 / 27 / (1 - 9 / 27) - 7 / 27) * 27)
    assert rows[KMET]["tm_tgt_gain_pg"] > 0
    # the budget: available players' gains add up to exactly the vacated targets per game
    gains = sum(rows[g]["tm_tgt_gain_pg"] for g in (BURDEN, KMET, SWIFT, RB2, RB3, CALEB))
    assert gains == pytest.approx(9.0)
    # the unavailable player receives nothing and the available shares sum to one
    claims = {g: v[1] / 27 for g, v in CHI_SKILL.items()}
    shares = rc.redistribute(claims, {DJM})
    assert shares[DJM] is None and sum(v for v in shares.values() if v is not None) == pytest.approx(1.0)
    assert shares[BURDEN] == pytest.approx(rc.expected_share(7 / 27, 9 / 27, 0.0))
    assert all(0 <= v <= 1 for v in shares.values() if v is not None)
    # his own row never counts himself as vacated
    assert rows[DJM]["tm_tgt_vacated"] == pytest.approx(0.0)


def test_wr1_returning_takes_back_volume():
    src, ctx = world(skip={DJM: (4, 5)}, roster_status={(DJM, 4): "INA", (DJM, 5): "INA"}, injuries=[inj(KMET, "Questionable")])
    b = ctx_rows(src, ctx)[BURDEN]
    # DJM's share when playing (weeks 1-3) x the 2 of Burden's 5 games he missed
    assert b["tm_tgt_returning"] == pytest.approx(9 / 27 * 2 / 5)
    assert b["tm_tgt_gain_pg"] < 0 and b["top_tgt_comp_return"] == 1
    # on injured reserve before W: not returning; his earlier share in Burden's games is vacated
    src, ctx = world(skip={DJM: (4, 5)}, roster_status={(DJM, 4): "RES", (DJM, 5): "RES"},
                     injuries=[inj(KMET, "Questionable")])
    b = ctx_rows(src, ctx)[BURDEN]
    assert b["tm_tgt_returning"] == pytest.approx(0.0) and b["tm_tgt_vacated"] == pytest.approx(27 / 135)


def test_stale_teammate_is_not_returning():
    src, ctx = world(weeks=range(1, 12), skip={DJM: range(3, 12)},
                     roster_status={(DJM, w): "INA" for w in range(3, 12)}, injuries=[inj(KMET, "Questionable", week=12)])
    b = ctx_rows(src, ctx, week=12)[BURDEN]
    assert b["tm_tgt_returning"] == pytest.approx(0.0)       # last game week 2 < 12 - STALE_WEEKS


def test_rb1_out_and_committee_split_in_proportion():
    src, ctx = world(injuries=[inj(SWIFT, "Out")])
    rows = ctx_rows(src, ctx)
    assert rows[RB2]["rb1_out"] == 1 and rows[RB2]["tm_car_vacated"] == pytest.approx(15 / 28)
    g2, g3 = rows[RB2]["tm_car_gain_pg"], rows[RB3]["tm_car_gain_pg"]
    assert g2 > g3 > 0 and g2 / g3 == pytest.approx(6 / 3)
    assert g2 + g3 + rows[CALEB]["tm_car_gain_pg"] == pytest.approx(15.0)   # budget incl. QB runs
    # a backup becoming the clear lead back vs one joining a committee: same rule, different inputs
    src, ctx = world(injuries=[inj(SWIFT, "Out"), inj(RB3, "Out")])
    solo = ctx_rows(src, ctx)[RB2]
    assert solo["tm_car_gain_pg"] > g2


def test_missing_injury_report_is_null_not_zero():
    src, ctx = world(injuries=None)
    r = ctx_rows(src, ctx)[BURDEN]
    assert r["ctx_report_used"] == 0
    for c in ("tm_tgt_vacated", "tm_tgt_returning", "tm_tgt_gain_pg", "tm_car_gain_pg", "top_tgt_comp_out"):
        assert r[c] is None, c
    assert r["qb_confidence"] == "probable"        # never "supported" without a report


def test_player_changing_teams():
    team = {(BURDEN, 5): "GB"}
    src, ctx = world(roster_team=team, injuries=[inj(KMET, "Questionable")])
    burden_gb = ctx_rows(src, ctx, players=(BURDEN,), team="GB")[BURDEN]
    assert burden_gb["recent_qb_id"] == CALEB and burden_gb["expected_qb_id"] == LOVE
    assert burden_gb["qb_changed"] == 1
    assert burden_gb["tm_games"] is None and burden_gb["tm_tgt_vacated"] is None   # no GB games: unknown, not 0
    kmet = ctx_rows(src, ctx, players=(KMET,))[KMET]
    assert kmet["tm_tgt_vacated"] == pytest.approx(7 / 27)                    # Burden is gone from CHI


def test_bye_week_uses_the_games_before_it():
    src, ctx = world(weeks=(1, 2, 3, 4, 6), injuries=[inj(KMET, "Questionable", week=7)])
    r = ctx_rows(src, ctx, week=7)[BURDEN]
    assert r["expected_qb_id"] == CALEB and r["tm_games"] == 5 and r["tm_tgt_vacated"] == pytest.approx(0.0)


def test_rest_of_season_horizon_ignores_the_unpublished_report():
    src, ctx = world(injuries=[inj(CALEB, "Out"), inj(DJM, "Out")])
    r = ctx_rows(src, ctx, report_mode="none")[BURDEN]
    assert r["expected_qb_id"] == CALEB and r["qb_confidence"] == "probable" and r["ctx_report_used"] is None
    assert r["tm_tgt_vacated"] == pytest.approx(0.0)        # roster-only availability by construction
    # but a long-term absence known before the forecast week still counts
    src, ctx = world(roster_status={(DJM, 5): "RES"})
    r = ctx_rows(src, ctx, report_mode="none")[BURDEN]
    assert r["tm_tgt_vacated"] == pytest.approx(9 / 27)


# ----------------------------------------------------------------------------- point-in-time safety


def test_week_w_and_later_information_never_changes_features():
    src, ctx = world(injuries=[inj(CALEB, "Out")])
    before = rc.context_for_week(src, ctx, S, W, pl.DataFrame({"gsis_id": [BURDEN, CALEB], "position": ["WR", "QB"],
                                                                "team": ["CHI", "CHI"], "opponent": ["GB", "GB"]}))
    # add the week-W (and W+1) starting QB, box scores, roster statuses and a later injury report
    late_panel = pl.DataFrame([prow(S, w, g, v[0], "CHI", 30, 30) for w in (W, W + 1) for g, v in CHI_SKILL.items()]
                              ).cast(_panel_schema(), strict=False).select(PANEL_COLUMNS)
    late_ros = pl.DataFrame([{"season": S, "week": w, "gsis_id": g, "team": "CHI", "position": "WR", "status": "RES"}
                             for w in (W, W + 1) for g in CHI_SKILL], schema=ROSTER_SCHEMA)
    src2 = Sources(pl.concat([src.panel, late_panel]), src.games, src.defense, pl.concat([src.rosters, late_ros]),
                   pl.concat([src.injuries, pl.DataFrame([inj(DJM, "Out", week=W + 1)], schema=INJURY_SCHEMA)]))
    late_st = pl.DataFrame([{"season": S, "week": w, "team": "CHI", "qb_id": "00-SOMEONE"} for w in (W, W + 1)],
                           schema=rc.STARTER_SCHEMA)
    late_qb = pl.DataFrame([{"season": S, "week": W, "gsis_id": KEENUM, "team": "CHI", "attempts": 50.0,
                             "sacks": 0.0, "epa": 30.0, "cpoe": 10.0}], schema=rc.QB_SCHEMA)
    ctx2 = rc.ContextSources(pl.concat([ctx.starters, late_st]), pl.concat([ctx.qb_stats, late_qb]))
    after = rc.context_for_week(src2, ctx2, S, W, before.select("gsis_id").with_columns(
        pl.Series("position", ["WR", "QB"]), pl.lit("CHI").alias("team"), pl.lit("GB").alias("opponent")))
    assert before.equals(after)


def test_report_rows_modified_after_kickoff_are_dropped_before_the_context_sees_them():
    con = duckdb.connect()
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, gameday VARCHAR, gametime VARCHAR,"
                " home_team VARCHAR, away_team VARCHAR)")
    con.execute(f"INSERT INTO nfl_schedules VALUES ({S}, {W}, 'REG', '{S}-10-12', '13:00', 'CHI', 'GB')")
    con.execute("CREATE TABLE nfl_injuries (season INT, week INT, game_type VARCHAR, gsis_id VARCHAR, team VARCHAR, "
                "report_status VARCHAR, practice_status VARCHAR, report_primary_injury VARCHAR, "
                "practice_primary_injury VARCHAR, date_modified TIMESTAMPTZ)")
    con.execute(f"INSERT INTO nfl_injuries VALUES ({S}, {W}, 'REG', '{CALEB}', 'CHI', 'Out', 'DNP', 'Knee', 'Knee', "
                f"'{S}-10-12 18:30:00+00')")   # 14:30 New York: after the 13:00 kickoff
    con.execute(f"INSERT INTO nfl_injuries VALUES ({S}, {W}, 'REG', '{KMET}', 'CHI', 'Questionable', 'LP', 'Ankle', "
                f"'Ankle', '{S}-10-10 20:00:00+00')")   # Friday report: pregame
    rows = load_injuries(con)
    assert rows["gsis_id"].to_list() == [KMET]
    src, ctx = world(injuries=rows.to_dicts())
    assert ctx_rows(src, ctx)[BURDEN]["expected_qb_id"] == CALEB


# ----------------------------------------------------------------------------- challenger plumbing


def _store():
    rng = np.random.default_rng(11)
    rows = []
    for w in range(1, 9):
        for i in range(260):
            pos = ("QB", "RB", "WR", "TE")[i % 4]
            delta = None if i % 9 == 0 else float(rng.normal(0, 0.1))
            rows.append({"season": 2025, "week": w, "key": 202500 + w, "gsis_id": f"p{i}", "position": pos,
                         "games_recent": 0 if i == 5 else 3, "pts_recent": float(rng.uniform(2, 20)),
                         "qb_epa_delta": delta, "qb_cpoe_delta": None if delta is None else delta * 10,
                         "qb_changed": None if delta is None else float(rng.integers(0, 2)),
                         "label": float(rng.uniform(0, 25))})
    return pl.DataFrame(rows)


def test_baseline_column_is_exactly_the_production_challenger():
    cfg = ch.ChallengerConfig.load()
    store = _store()
    weeks = [(2025, w) for w in (7, 8)]
    ours = cc.walk_forward(store, weeks, cfg, candidates=("A",))
    theirs = ch.challenger_predictions(store, weeks, cfg)
    j = ours.join(theirs.rename({"pred_ifp2": "prod"}), on=["key", "gsis_id"])
    assert j.height == ours.height == store.filter(pl.col("week") >= 7).height
    assert np.allclose(j["pred_ifp2"].cast(pl.Float64).fill_null(np.nan).to_numpy(),
                       j["prod"].cast(pl.Float64).fill_null(np.nan).to_numpy(), equal_nan=True)
    assert j.filter(pl.col("gsis_id") == "p5")["pred_A"].null_count() == 2       # abstains with no recent game


def test_candidate_features_extend_ifp2_and_composite_uses_the_adopted_candidate():
    cfg = ch.ChallengerConfig.load()
    assert cc.candidate_features(cfg, "WR", None) == cfg.features("WR")
    assert cc.candidate_features(cfg, "WR", "C")[-6:] == ["qb_epa_delta", "qb_cpoe_delta", "qb_changed",
                                                           "tm_tgt_gain_pg", "tm_tgt_vacated", "tm_tgt_returning"]
    store = _store()
    p = cc.walk_forward(store, [(2025, 8)], cfg, candidates=("A",), per_position={"WR": "A"})
    wr = p.join(store.select("key", "gsis_id", "position"), on=["key", "gsis_id"])
    assert wr.filter(pl.col("position") == "WR")["pred_ctx"].equals(wr.filter(pl.col("position") == "WR")["pred_A"])
    assert wr.filter(pl.col("position") == "QB")["pred_ctx"].equals(wr.filter(pl.col("position") == "QB")["pred_ifp2"])


def test_explanation_metadata_is_structured_and_not_causal():
    cfg = ch.ChallengerConfig.load()
    store = _store()
    train = ch.train_rows(store, 202508, "WR")
    base = cc.fit_candidate(train, cfg, "WR", None, 202508)
    ctx = cc.fit_candidate(train, cfg, "WR", "A", 202508)
    row = store.filter((pl.col("key") == 202508) & (pl.col("position") == "WR")).head(1).with_columns(
        pl.lit("00-X").alias("expected_qb_id"), pl.lit("uncertain").alias("qb_confidence"))
    e = cc.explain_change(base, ctx, row, ("qb",))
    assert e["status"] == "research_only" and e["factors"][0]["group"] == "qb"
    assert e["difference"] == pytest.approx(e["challenger_points"] - e["baseline_points"], abs=0.011)
    assert "not proof" in e["caveats"][0]


def test_app_never_imports_the_research_context_modules():
    backend = pathlib.Path(__file__).resolve().parents[1]
    code = ("import sys, app.main; bad = [m for m in ('app.analytics.roster_context', "
            "'app.analytics.context_challenger') if m in sys.modules]; assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], cwd=backend, check=True)
    for path in (backend / "app").rglob("*.py"):
        if path.name not in ("roster_context.py", "context_challenger.py", "returning_context.py",
                             "qb_receiver_compat.py"):   # research only
            text = path.read_text()
            assert "roster_context" not in text and "context_challenger" not in text, path
