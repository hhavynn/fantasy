"""Receiver role x QB passing quality (ifp-qbr, research only): play-by-play aggregation, shrinkage
toward the replacement / position prior, point-in-time safety, null handling, the challenger plumbing
and isolation from production and from ifp-ctx-p1. Hand-built frames with invented values: these
tests check behaviour, not forecasting accuracy."""

from __future__ import annotations

import pathlib
import subprocess
import sys

import numpy as np
import polars as pl
import pytest

from app.analytics import challenger as ch
from app.analytics import context_challenger as cc
from app.analytics import qb_receiver_compat as qr
from app.analytics.roster_context import STARTER_SCHEMA
from tests.test_roster_context import _store

S, W = 2025, 6
STAR, BACKUP, DEEP, SHORT, TE1, RB1 = "00-STAR", "00-BACKUP", "00-DEEP", "00-SHORT", "00-TE1", "00-RB1"
POS = {STAR: "QB", BACKUP: "QB", DEEP: "WR", SHORT: "WR", TE1: "TE", RB1: "RB"}


def play(week, passer=STAR, receiver=None, air=None, cpoe=None, epa=0.1, sack=0, scramble=0, two=0, season=S,
         team="CHI", rusher=None):
    att = 0 if (sack or scramble) else 1
    return {"season": season, "week": week, "season_type": "REG", "game_id": f"{season}_{week}_{team}",
            "play_id": float(len(_ids)), "posteam": team, "play_type": "pass" if not scramble else "run",
            "qb_dropback": 1.0, "pass_attempt": float(att), "sack": float(sack), "qb_scramble": float(scramble),
            "two_point_attempt": float(two), "air_yards": air, "cpoe": cpoe, "epa": epa, "qb_epa": epa,
            "passer_player_id": None if scramble else passer, "rusher_player_id": rusher, "receiver_player_id": receiver}


_ids: list = []


def pbp(rows) -> pl.DataFrame:
    schema = {c: pl.Float64 for c in qr.PBP_COLUMNS}
    schema.update(season=pl.Int64, week=pl.Int64, season_type=pl.String, game_id=pl.String, posteam=pl.String,
                  play_type=pl.String, passer_player_id=pl.String, rusher_player_id=pl.String,
                  receiver_player_id=pl.String)
    return pl.DataFrame([{**r, "play_id": float(i)} for i, r in enumerate(rows)], schema=schema)


def season_of_plays(weeks=range(1, W), qb=STAR, deep_cpoe=10.0, short_cpoe=2.0, team="CHI", season=S):
    """Per week: DEEP gets 3 deep targets (air 25) + 2 short, SHORT gets 6 short (air 4), TE1 3 (air 6),
    RB1 2 (air -2), one sack, one scramble."""
    rows = []
    for w in weeks:
        rows += [play(w, qb, DEEP, 25.0, deep_cpoe, 0.6, season=season, team=team) for _ in range(3)]
        rows += [play(w, qb, DEEP, 8.0, short_cpoe, 0.2, season=season, team=team) for _ in range(2)]
        rows += [play(w, qb, SHORT, 4.0, short_cpoe, 0.1, season=season, team=team) for _ in range(6)]
        rows += [play(w, qb, TE1, 6.0, short_cpoe, 0.1, season=season, team=team) for _ in range(3)]
        rows += [play(w, qb, RB1, -2.0, short_cpoe, 0.0, season=season, team=team) for _ in range(2)]
        rows += [play(w, qb, sack=1, epa=-1.5, season=season, team=team),
                 play(w, qb, scramble=1, rusher=qb, epa=0.4, season=season, team=team)]
    return rows


def test_pass_game_tables_count_dropbacks_attempts_and_deep_plays():
    rows = season_of_plays(weeks=[1]) + [play(1, STAR, DEEP, 30.0, 50.0, two=1),        # 2-pt try: ignored
                                         play(1, STAR, None, None, None, 0.0)]             # throwaway: attempt, no target
    qb, rec = qr.pass_game_tables(pbp(rows))
    q = qb.row(0, named=True)
    assert (q["gsis_id"], q["dropbacks"], q["sacks"], q["attempts"]) == (STAR, 19.0, 1.0, 17.0)
    assert q["deep_att"] == 3 and q["deep_cpoe_n"] == 3 and q["deep_cpoe_sum"] == pytest.approx(30.0)
    assert q["ay_n"] == 16 and q["ay_sum"] == pytest.approx(3 * 25 + 2 * 8 + 6 * 4 + 3 * 6 - 4)
    d = rec.filter(pl.col("gsis_id") == DEEP).row(0, named=True)
    assert (d["targets"], d["deep_tgt"], d["team_targets"]) == (5.0, 3.0, 16.0)
    assert d["team_ay"] == pytest.approx(q["ay_sum"])


def _tables(rows):
    return qr.pass_game_tables(pbp(rows))


def test_qb_profile_shrinks_a_backup_toward_the_replacement_prior_and_unknown_is_none():
    # last season: STAR a full starter, BACKUP a low-volume QB-season (the replacement prior); this season STAR only
    rows = season_of_plays(range(1, 18), STAR, 10.0, season=S - 1) + \
        season_of_plays(range(1, 3), BACKUP, -20.0, team="CHI", season=S - 1) + season_of_plays(range(1, W), STAR, 10.0)
    qb, _ = _tables(rows)
    prof, prior = qr.qb_pass_profiles(qb, S, W)
    assert prior["deep_cpoe"] == pytest.approx(-20.0)          # BACKUP's 38-dropback season is the replacement level
    n = 3 * (17 + 5)                                            # STAR's deep attempts with a cpoe value
    assert prof[STAR]["deep_cpoe"] == pytest.approx((10.0 * n - 20.0 * 250) / (n + 250))
    assert prof[BACKUP]["deep_cpoe"] == pytest.approx((-20.0 * 6 - 20.0 * 250) / (6 + 250))
    assert qr.pass_profile_of(prof, prior, None) is None
    new = qr.pass_profile_of(prof, prior, "00-NEW")
    assert new["attempts"] == 0 and new["deep_cpoe"] == prior["deep_cpoe"]


def test_receiver_role_is_shrunk_toward_the_position_and_shares_are_of_team_air_yards():
    _, rec = _tables(season_of_plays())
    roles = qr.receiver_roles(rec, POS, S, W)
    d, s = roles[DEEP], roles[SHORT]
    lg_deep = 15 / 55                                            # WR league deep share: 15 of 55 WR targets
    assert d["rec_deep_share"] == pytest.approx((15 + lg_deep * 20) / (25 + 20))
    assert s["rec_deep_share"] == pytest.approx((0 + lg_deep * 20) / (30 + 20))
    assert d["rec_deep_share"] > s["rec_deep_share"] and d["rec_adot"] > s["rec_adot"]
    team_ay = 3 * 25 + 2 * 8 + 6 * 4 + 3 * 6 - 4
    assert d["rec_ay_share"] == pytest.approx(91 / team_ay) and d["rec_tgt_share"] == pytest.approx(5 / 16)
    assert roles[TE1]["rec_deep_share"] == pytest.approx(0.0)   # no TE deep target anywhere: the prior is 0


def _world(backup_deep=-20.0, extra=()):
    rows = season_of_plays(range(1, 18), STAR, 10.0, season=S - 1) + \
        season_of_plays(range(1, 3), BACKUP, backup_deep, season=S - 1) + season_of_plays(range(1, W), STAR, 10.0)
    qb, rec = _tables([*rows, *extra])
    panel = pl.DataFrame([{"season": S, "week": w, "gsis_id": g, "team": "CHI", "position": p}
                          for w in range(1, W) for g, p in POS.items() if p != "QB"])
    starters = pl.DataFrame([{"season": S, "week": w, "team": "CHI", "qb_id": STAR} for w in range(1, W)],
                            schema=STARTER_SCHEMA)
    return qb, rec, panel, starters


def _rows(expected=BACKUP, epa_delta=-0.2, world=None):
    qb, rec, panel, starters = world or _world()
    t = pl.DataFrame({"gsis_id": [DEEP, SHORT, TE1, RB1], "position": ["WR", "WR", "TE", "RB"],
                      "expected_qb_id": [expected] * 4, "qb_epa_delta": [epa_delta] * 4},
                     schema={"gsis_id": pl.String, "position": pl.String, "expected_qb_id": pl.String,
                             "qb_epa_delta": pl.Float64})
    out = qr.qbr_for_week(qb, rec, panel, starters, POS, S, W, t)
    return {r["gsis_id"]: r for r in out.iter_rows(named=True)}


def test_backup_hits_the_deep_threat_harder_than_the_short_route_receiver():
    r = _rows()
    assert r[DEEP]["qb_deep_cpoe_delta"] < 0 and r[DEEP]["qb_deep_cpoe_delta"] == r[SHORT]["qb_deep_cpoe_delta"]
    assert r[DEEP]["qbr_deep_x"] < r[SHORT]["qbr_deep_x"] < 0
    assert r[DEEP]["qbr_deep_x"] == pytest.approx(r[DEEP]["rec_deep_share"] * r[DEEP]["qb_deep_cpoe_delta"])
    assert r[DEEP]["qbr_epa_x"] == pytest.approx(r[DEEP]["rec_ay_share"] * -0.2)
    assert r[DEEP]["qbr_epa_x"] < r[SHORT]["qbr_epa_x"]
    assert all(r[RB1][c] is None for c in qr.FEATURE_COLUMNS)        # RB rows: out of scope, no interaction
    assert r[RB1]["qb_deep_cpoe_delta"] is not None                    # the QB delta itself is still reported


def test_same_qb_gives_zero_interactions_and_unknown_qb_gives_null():
    r = _rows(expected=STAR, epa_delta=0.0)
    assert all(r[DEEP][c] == pytest.approx(0.0) for c in ("qb_deep_cpoe_delta", "qb_deep_rate_delta",
                                                           *qr.FEATURE_COLUMNS))
    r = _rows(expected=None, epa_delta=None)
    assert all(r[DEEP][c] is None for c in ("exp_qb_deep_cpoe", "qb_deep_cpoe_delta", *qr.FEATURE_COLUMNS))
    assert r[DEEP]["rec_deep_share"] is not None                       # the role is still known


def test_week_w_and_later_plays_never_change_features():
    before = pl.DataFrame(list(_rows().values()))
    late = [*season_of_plays([W, W + 1], BACKUP, 60.0), *season_of_plays([1], STAR, -80.0, season=S + 1)]
    qb, rec, panel, starters = _world(extra=late)
    late_panel = pl.DataFrame([{"season": S, "week": W, "gsis_id": DEEP, "team": "CHI", "position": "WR"}])
    late_start = pl.DataFrame([{"season": S, "week": W, "team": "CHI", "qb_id": BACKUP}], schema=STARTER_SCHEMA)
    after = pl.DataFrame(list(_rows(world=(qb, rec, pl.concat([panel, late_panel]),
                                           pl.concat([starters, late_start]))).values()))
    assert before.equals(after)


# ----------------------------------------------------------------------------- challenger plumbing


def _qbr_store():
    rng = np.random.default_rng(7)
    n = _store().height
    return _store().with_columns(*[pl.Series(c, rng.normal(0, 1, n)) for c in qr.FEATURE_COLUMNS],
                                 pl.Series("tm_tgt_gain_pg", rng.normal(0, 1, n)),
                                 pl.Series("tm_tgt_vacated", rng.uniform(0, .3, n)),
                                 pl.Series("tm_tgt_returning", rng.uniform(0, .3, n)))


def test_candidates_extend_ifp_ctx_a_and_c_with_only_the_interactions():
    cfg = ch.ChallengerConfig.load()
    assert qr.candidate_features(cfg, "WR", None) == cfg.features("WR")
    for pos in qr.POSITIONS:
        assert qr.candidate_features(cfg, pos, "A") == cc.candidate_features(cfg, pos, "A")
        assert qr.candidate_features(cfg, pos, "Q") == [*cc.candidate_features(cfg, pos, "A"), *qr.FEATURE_COLUMNS]
        assert qr.candidate_features(cfg, pos, "CQ") == [*cc.candidate_features(cfg, pos, "C"), *qr.FEATURE_COLUMNS]


def test_baseline_and_comparators_match_the_existing_challengers_and_composite_follows_selection():
    cfg = ch.ChallengerConfig.load()
    store = _qbr_store()
    weeks = [(2025, w) for w in (7, 8)]
    ours = qr.walk_forward(store, weeks, cfg, per_position={"WR": "Q"})
    prod = ch.challenger_predictions(store, weeks, cfg).rename({"pred_ifp2": "prod"})
    ref = cc.walk_forward(store, weeks, cfg, candidates=("A", "C")).rename(
        {"pred_ifp2": "cc_ifp2", "pred_A": "cc_A", "pred_C": "cc_C"})
    j = ours.join(prod, on=["key", "gsis_id"]).join(ref, on=["key", "gsis_id"]).join(
        store.select("key", "gsis_id", "position"), on=["key", "gsis_id"])
    assert set(j["position"].unique()) == {"WR", "TE"}
    for a, b in (("pred_ifp2", "prod"), ("pred_A", "cc_A"), ("pred_C", "cc_C")):
        assert np.allclose(j[a].cast(pl.Float64).fill_null(np.nan).to_numpy(),
                           j[b].cast(pl.Float64).fill_null(np.nan).to_numpy(), equal_nan=True), a
    wr, te = j.filter(pl.col("position") == "WR"), j.filter(pl.col("position") == "TE")
    assert wr["pred_qbr"].equals(wr["pred_Q"]) and te["pred_qbr"].equals(te["pred_ifp2"])
    assert not wr["pred_Q"].equals(wr["pred_A"])


def test_ifp_ctx_and_its_prospective_candidate_are_untouched():
    assert cc.PROSPECTIVE_VERSION == "ifp-ctx-p1"
    assert cc.PROSPECTIVE_SPEC == {"QB": "A", "RB": "C", "WR": "C", "TE": "ifp2"}
    assert set(cc.CANDIDATES) == {"A", "B", "C", "D"} and set(cc.GROUPS) == {"qb", "tm", "env"}
    assert qr.QBR_CHALLENGER_VERSION not in (cc.CTX_VERSION, cc.PROSPECTIVE_VERSION)
    assert not any(f.startswith("qbr_") for g in cc.GROUPS.values() for fs in g.values() for f in fs)


def test_decision_rules_require_role_awareness_and_keep_the_bias_guard():
    rules = qr.DECISION_RULES
    assert "MAE(Q) - MAE(A) < 0" in rules["selection"] and "bias" in rules["selection"]
    assert "MAE(qbr) < MAE(A)" in rules["confirmation"] and qr.MECHANISM_COHORT == "changed_qb"
    assert qr.BIAS_TOLERANCE == cc.BIAS_TOLERANCE and qr.SELECTION_MAX_DIFF == cc.SELECTION_MAX_DIFF


def test_app_never_imports_the_research_module():
    backend = pathlib.Path(__file__).resolve().parents[1]
    code = "import sys, app.main; assert 'app.analytics.qb_receiver_compat' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], cwd=backend, check=True)
    for path in (backend / "app").rglob("*.py"):
        if path.name != "qb_receiver_compat.py":
            assert "qb_receiver_compat" not in path.read_text(), path
