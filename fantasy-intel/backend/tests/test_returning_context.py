"""Returning-teammate challenger (ifp-ret, research only): role holders, the returning rule, the
reserve route, point-in-time safety, missing data, the challenger plumbing and isolation from
production and from ifp-ctx. Hand-built frames with invented values (the world() of
test_roster_context): these tests check behaviour, not forecasting accuracy."""

from __future__ import annotations

import pathlib
import subprocess
import sys

import numpy as np
import polars as pl
import pytest

from app.analytics import challenger as ch
from app.analytics import context_challenger as cc
from app.analytics import returning_context as rt
from app.analytics.forecast_features import INJURY_SCHEMA, PANEL_COLUMNS, ROSTER_SCHEMA, Sources, _panel_schema
from tests.test_roster_context import (BURDEN, CALEB, CHI_SKILL, DJM, KMET, RB2, RB3, SWIFT, S, W, _store, inj, prow,
                                       world)


def ret_rows(src, players=(BURDEN, KMET, SWIFT, RB2, RB3, DJM, CALEB), week=W, **kw):
    t = pl.DataFrame({"gsis_id": list(players), "team": ["CHI"] * len(players)})
    out = rt.returning_for_week(src, S, week, t, **kw)
    return {r["gsis_id"]: r for r in out.iter_rows(named=True)}


def test_returning_wr1_reclaims_share_and_the_player_cedes_targets():
    src, _ = world(skip={DJM: (4, 5)}, roster_status={(DJM, 4): "INA", (DJM, 5): "INA"},
                   injuries=[inj(KMET, "Questionable")])
    rows = ret_rows(src)
    b = rows[BURDEN]
    # DJM's share when playing (weeks 1-3) x the 2 of Burden's 5 games he missed
    reclaim = 9 / 27 * 2 / 5
    assert b["ret_tgt_reclaim"] == pytest.approx(reclaim) and b["ret_wr1"] == 1
    s = 7 / 27
    assert b["ret_tgt_loss_pg"] == pytest.approx((s - s / (1 + reclaim)) * 27)
    assert b["ret_te1"] == 0 and b["ret_rb1"] == 0 and b["ret_returners"] == DJM
    assert b["ret_missed_max"] == pytest.approx(2 / 5) and b["ret_back_now"] == 1    # absent last game, back in W
    # the returner is not his own competitor; nobody else at CHI missed a game
    assert rows[DJM]["ret_tgt_reclaim"] == pytest.approx(0.0) and rows[DJM]["ret_wr1"] == 0


def test_only_role_holders_count_not_every_returning_teammate():
    # Burden (WR2) and RB3 miss games: neither is a role holder, so nobody "returns"
    src, _ = world(skip={BURDEN: (4, 5), RB3: (4, 5)}, injuries=[inj(KMET, "Questionable")])
    rows = ret_rows(src)
    assert rows[DJM]["ret_tgt_reclaim"] == pytest.approx(0.0) and rows[SWIFT]["ret_car_reclaim"] == pytest.approx(0.0)
    # the RB1 returning is a carry competitor for the other backs
    src, _ = world(skip={SWIFT: (4, 5)}, injuries=[inj(KMET, "Questionable")])
    r2 = ret_rows(src)[RB2]
    assert r2["ret_rb1"] == 1 and r2["ret_car_reclaim"] == pytest.approx(15 / 28 * 2 / 5)
    assert r2["ret_car_loss_pg"] > 0


def test_role_is_ranked_on_share_when_playing_so_an_injured_wr1_keeps_it():
    # DJM played only weeks 1-2 (9 targets/game); Burden 7/game every week. DJM is still the WR1.
    src, _ = world(skip={DJM: (3, 4, 5)}, roster_status={(DJM, w): "INA" for w in (3, 4, 5)},
                   injuries=[inj(KMET, "Questionable")])
    b = ret_rows(src)[BURDEN]
    assert b["ret_wr1"] == 1 and b["ret_tgt_reclaim"] == pytest.approx(9 / 27 * 3 / 5)
    # a role holder who missed an earlier window game but played last week: returning, not "back now"
    src, _ = world(skip={DJM: (2,)}, injuries=[inj(KMET, "Questionable")])
    b = ret_rows(src)[BURDEN]
    assert b["ret_wr1"] == 1 and b["ret_back_now"] == 0 and b["ret_tgt_reclaim"] == pytest.approx(9 / 27 / 5)


def test_long_absence_is_not_cut_off_by_staleness():
    # roster_context ignores a teammate whose last game is > 6 weeks old; a returning role holder is not ignored
    src, _ = world(weeks=range(1, 12), skip={DJM: range(3, 12)}, roster_status={(DJM, w): "INA" for w in range(3, 12)},
                   injuries=[inj(KMET, "Questionable", week=12)])
    b = ret_rows(src, week=12)[BURDEN]
    assert b["ret_wr1"] == 1 and b["ret_tgt_reclaim"] == pytest.approx(9 / 27)   # missed all 5 window games


def test_unavailable_or_unevidenced_role_holder_is_not_returning():
    base = dict(skip={DJM: (4, 5)}, roster_status={(DJM, 4): "INA", (DJM, 5): "INA"})
    for status in ("Out", "Doubtful"):
        src, _ = world(**base, injuries=[inj(DJM, status)])
        assert ret_rows(src)[BURDEN]["ret_wr1"] == 0
    # on a reserve list before W and not on the report: no evidence he is back
    src, _ = world(skip={DJM: (4, 5)}, roster_status={(DJM, 4): "RES", (DJM, 5): "RES"},
                   injuries=[inj(KMET, "Questionable")])
    b = ret_rows(src)[BURDEN]
    assert b["ret_wr1"] == 0 and b["ret_tgt_reclaim"] == pytest.approx(0.0)
    # traded away: off the team
    src, _ = world(skip={DJM: (4, 5)}, roster_team={(DJM, 5): "GB"}, injuries=[inj(KMET, "Questionable")])
    assert ret_rows(src)[BURDEN]["ret_wr1"] == 0


def test_reserve_return_needs_a_report_listing_without_out_or_doubtful():
    res = dict(skip={DJM: (4, 5)}, roster_status={(DJM, 4): "RES", (DJM, 5): "RES"})
    src, _ = world(**res, injuries=[inj(DJM, "Questionable")])
    b = ret_rows(src)[BURDEN]
    assert b["ret_wr1"] == 1 and b["ret_from_reserve"] == 1
    src, _ = world(**res, injuries=[inj(DJM, None)])           # listed, no game designation
    assert ret_rows(src)[BURDEN]["ret_from_reserve"] == 1
    src, _ = world(**res, injuries=[inj(DJM, "Out")])
    assert ret_rows(src)[BURDEN]["ret_wr1"] == 0


def test_prior_season_role_holder_needs_current_season_roster_evidence():
    prev = [prow(S - 1, w, DJM, "WR", "CHI", 10, 0) for w in (14, 15, 16)]
    src, _ = world(skip={DJM: range(1, W)}, injuries=[inj(DJM, "Questionable")])
    cur_roster = src.rosters.filter(pl.col("gsis_id") != DJM)
    panel = pl.concat([src.panel, pl.DataFrame(prev).cast(_panel_schema(), strict=False).select(PANEL_COLUMNS)])
    # no current-season roster row for DJM: never a role holder (an offseason departure would look like this)
    src2 = Sources(panel, src.games, src.defense, cur_roster, src.injuries)
    assert ret_rows(src2)[BURDEN]["ret_wr1"] == 0
    # on CHI's current-season roster (active) and listed: last season's WR1 coming back
    src3 = Sources(panel, src.games, src.defense, src.rosters, src.injuries)
    b = ret_rows(src3)[BURDEN]
    assert b["ret_wr1"] == 1 and b["ret_tgt_reclaim"] == pytest.approx(10 / 27)


def test_missing_report_is_null_and_week_one_has_no_window():
    src, _ = world(skip={DJM: (4, 5)}, injuries=None)
    b = ret_rows(src)[BURDEN]
    assert all(b[c] is None for c in (*rt.FEATURE_COLUMNS, *rt.FLAG_COLUMNS))
    src, _ = world(injuries=[inj(KMET, "Questionable", week=1)])
    assert all(v is None for k, v in ret_rows(src, week=1)[BURDEN].items() if k != "gsis_id")
    # no returner: a measured zero, not null
    src, _ = world(injuries=[inj(KMET, "Questionable")])
    b = ret_rows(src)[BURDEN]
    assert b["ret_tgt_reclaim"] == 0.0 and b["ret_tgt_loss_pg"] == 0.0 and b["ret_wr1"] == 0


def test_week_w_and_later_information_never_changes_features():
    src, _ = world(skip={DJM: (4, 5)}, roster_status={(DJM, 4): "INA", (DJM, 5): "INA"},
                   injuries=[inj(KMET, "Questionable")])
    before = rt.returning_for_week(src, S, W, pl.DataFrame({"gsis_id": [BURDEN, RB2], "team": ["CHI", "CHI"]}))
    late_panel = pl.DataFrame([prow(S, w, g, v[0], "CHI", 30, 30) for w in (W, W + 1) for g, v in CHI_SKILL.items()]
                              ).cast(_panel_schema(), strict=False).select(PANEL_COLUMNS)
    late_ros = pl.DataFrame([{"season": S, "week": w, "gsis_id": g, "team": "CHI", "position": "WR", "status": "RES"}
                             for w in (W, W + 1) for g in CHI_SKILL], schema=ROSTER_SCHEMA)
    src2 = Sources(pl.concat([src.panel, late_panel]), src.games, src.defense, pl.concat([src.rosters, late_ros]),
                   pl.concat([src.injuries, pl.DataFrame([inj(DJM, "Out", week=W + 1)], schema=INJURY_SCHEMA)]))
    after = rt.returning_for_week(src2, S, W, pl.DataFrame({"gsis_id": [BURDEN, RB2], "team": ["CHI", "CHI"]}))
    assert before.equals(after)


# ----------------------------------------------------------------------------- challenger plumbing


def _ret_store():
    rng = np.random.default_rng(5)
    return _store().with_columns(
        pl.Series("ret_tgt_reclaim", rng.uniform(0, 0.2, 2080)), pl.Series("ret_car_reclaim", rng.uniform(0, 0.2, 2080)),
        pl.Series("ret_tgt_loss_pg", rng.uniform(0, 1.5, 2080)), pl.Series("ret_car_loss_pg", rng.uniform(0, 1.5, 2080)))


def test_baseline_column_is_exactly_the_production_challenger_and_composite_follows_selection():
    cfg = ch.ChallengerConfig.load()
    store = _ret_store()
    weeks = [(2025, w) for w in (7, 8)]
    ours = rt.walk_forward(store, weeks, cfg, per_position={"WR": "R"})
    theirs = ch.challenger_predictions(store, weeks, cfg)
    j = ours.join(theirs.rename({"pred_ifp2": "prod"}), on=["key", "gsis_id"]).join(
        store.select("key", "gsis_id", "position"), on=["key", "gsis_id"])
    assert np.allclose(j["pred_ifp2"].cast(pl.Float64).fill_null(np.nan).to_numpy(),
                       j["prod"].cast(pl.Float64).fill_null(np.nan).to_numpy(), equal_nan=True)
    wr, qb = j.filter(pl.col("position") == "WR"), j.filter(pl.col("position") == "QB")
    assert wr["pred_ret"].equals(wr["pred_R"]) and qb["pred_ret"].equals(qb["pred_ifp2"])
    assert not wr["pred_R"].equals(wr["pred_ifp2"])
    assert j.filter(pl.col("gsis_id") == "p5")["pred_R"].null_count() == 2       # abstains with no recent game


def test_candidate_adds_only_the_returning_group():
    cfg = ch.ChallengerConfig.load()
    assert rt.candidate_features(cfg, "WR", None) == cfg.features("WR")
    assert rt.candidate_features(cfg, "WR", "R")[-2:] == ["ret_tgt_loss_pg", "ret_tgt_reclaim"]
    for pos in ("QB", "RB", "WR", "TE"):
        extra = set(rt.candidate_features(cfg, pos, "R")) - set(cfg.features(pos))
        assert extra and all(f.startswith("ret_") for f in extra)
        assert not any("vacated" in f or f.startswith("tm_") for f in extra)


def test_ifp_ctx_p1_is_untouched():
    assert cc.PROSPECTIVE_VERSION == "ifp-ctx-p1"
    assert cc.PROSPECTIVE_SPEC == {"QB": "A", "RB": "C", "WR": "C", "TE": "ifp2"}
    assert "R" not in cc.CANDIDATES and rt.RET_CHALLENGER_VERSION not in (cc.CTX_VERSION, cc.PROSPECTIVE_VERSION)


def test_app_never_imports_the_research_module():
    backend = pathlib.Path(__file__).resolve().parents[1]
    code = "import sys, app.main; assert 'app.analytics.returning_context' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], cwd=backend, check=True)
    for path in (backend / "app").rglob("*.py"):
        if path.name != "returning_context.py":
            assert "returning_context" not in path.read_text(), path
