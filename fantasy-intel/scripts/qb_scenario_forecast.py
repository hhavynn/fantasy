"""The ifp-ctx-p2q pregame forecast for one week (research only; see docs/research/qb_scenario_mixture.md section 8).

Everything that turns the data into the frozen numbers lives here, so the frozen spec can bind it: together with
qb_scenarios.py (the p2 model) and qb_scenario_research.py (the study's store and candidate build), the sha256 of this
file is the spec's `forecast_code_hash`, and every ledger records it. qb_scenario_shadow.py only decides when to call
this and where the result goes; editing it does not invalidate the spec.

The forecast is the study's S2 at QB, unchanged: week-W start candidates on pregame inputs only, the start model fitted
on strictly earlier team-weeks (qb_scenarios.walk_forward_start), and S2 / the ifp-2 refit from the same training rows
through qb_scenarios.walk_forward_p2.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
FORECAST_CODE = ("qb_scenarios.py", "qb_scenario_research.py", "qb_scenario_forecast.py")


def forecast_code_hash() -> str:
    """sha256 over the forecast-building code (FORECAST_CODE, name and content)."""
    h = hashlib.sha256()
    for name in FORECAST_CODE:
        h.update(name.encode() + b"\0" + (SCRIPTS / name).read_bytes())
    return h.hexdigest()


def forecast(ns, st, cands, cfg, src, ctx, season: int, week: int, open_teams: list[str], kick: dict):
    """(QB rows, start-model candidates, fitted start model) for the open teams of week (S, W). `ns`: the
    qb_scenario_research namespace whose cache holds teams/recent; `st`, `cands`: its build() output (fully final
    weeks only)."""
    import polars as pl

    import qb_scenario_research as qsr
    import qb_scenarios as qs
    from app.analytics import roster_context as rc
    from app.analytics.forecast_features import features_for_week, key
    from app.analytics.participation import live_population

    cutoff = key(season, week)
    # week-W candidates (pregame inputs only; the postgame columns are blanked) and a start model fitted on strictly
    # earlier team-weeks (walk_forward_start asserts it)
    cw = qs.start_candidates(src, ctx, season, week, teams=open_teams).with_columns(
        pl.lit(None, pl.Float64).alias("started"), pl.lit(None, pl.Float64).alias("appeared"))
    pc, models = qs.walk_forward_start(pl.concat([cands.filter(pl.col("key") < cutoff), cw], how="vertical_relaxed"),
                                       [(season, week)])
    teams_all = pl.read_parquet(qsr._out(ns) / "teams.parquet")
    recent_all = pl.read_parquet(qsr._out(ns) / "recent.parquet")
    # forecast rows: QB only (RB/WR/TE are ifp-2 in this candidate); realized-starter columns stay empty
    pop = live_population(src, season, week).filter(pl.col("team").is_in(open_teams))
    feats = features_for_week(src, season, week, pop).filter(pl.col("position") == "QB")
    feats = feats.join(rc.context_for_week(src, ctx, season, week, feats.select("gsis_id", "position", "team",
                                                                                "opponent")), on="gsis_id", how="left")
    feats = feats.with_columns(pl.lit(season).alias("season"), pl.lit(week).alias("week"), pl.lit(cutoff).alias("key"))
    rows_w = qs.scenario_columns(feats, pc, teams_all.head(0), recent_all.head(0))
    train = qs.scenario_columns(st.filter((pl.col("position") == "QB") & (pl.col("key") < cutoff)), pc, teams_all,
                                recent_all)
    both = pl.concat([train, rows_w], how="diagonal_relaxed")
    # S2 and the ifp-2 refit through the study's own walk_forward_p2 (pred_ctx2 = ifp-2 base for QB here)
    pred = qs.walk_forward_p2(both, [(season, week)], cfg, candidates=("S2",), ablations=(), per_position={"QB": "ifp2"})
    pred = pred.select("key", "gsis_id", "pred_S2", pl.col("pred_ctx2").alias("pred_ifp2")) if pred.height else \
        pl.DataFrame(schema={"key": pl.Int64, "gsis_id": pl.String, "pred_S2": pl.Float64, "pred_ifp2": pl.Float64})
    fr = rows_w.join(pred, on=["key", "gsis_id"], how="left")
    rows = [{"gsis_id": r["gsis_id"], "position": "QB", "team": r["team"], "kickoff": kick[r["team"]].isoformat(),
             "ifp2_points": r["pred_ifp2"], "s2_points": r["pred_S2"], "pi_start": r["pi_start"],
             "pi_hard": r["pi_hard"], "qb_self_epa": r.get("qb_self_epa"), "games_recent": r.get("games_recent"),
             "scenario_fallback_ifp2": r["pi_start"] is None}
            for r in fr.sort("team", "gsis_id").iter_rows(named=True)]
    cand_rows = [{k: r[k] for k in ("team", "gsis_id", "p_start", "pi_start", "rank", "first_game", "p1_expected",
                                    "p1_confidence", "prev_start", "questionable", "doubtful", "unavailable")}
                 for r in pc.filter(pl.col("key") == cutoff).sort("team", "p_start", descending=[False, True])
                 .iter_rows(named=True)]
    return rows, cand_rows, models.get(cutoff)
