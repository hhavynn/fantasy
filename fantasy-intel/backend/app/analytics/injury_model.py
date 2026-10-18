"""ifp-2.1.0: the production "Ours" model. ifp-2.0.0's frozen spec plus the injury workload handoff.

What changes against ifp-2.0.0 (docs/independent_projection_engine.md, "ifp-2.1.0"; evidence in
docs/research/injury_workload_handoff.md):

1. Conditional points (RB/WR/TE). Each position's ridge is ifp-2.0.0's (same features, frozen penalty, 36-week
   recency weights, fitted on the same training rows by challenger.fit_linear) plus the two handoff features of
   injury_challenger.GROUP: `ho_exp_xfpl` (expected league points of opportunity under who is actually available
   this week) and `ho_shift_pg` (expected carries + targets minus the raw last-5 average). QB keeps ifp-2.0.0's
   features. The handoff is iwh-0.1.1: iwh-0.1.0 (the evaluated research version) with the pre-registered fix of
   section 7.3, so a Questionable-and-inactive game counts as an injury absence, not a role absence
   (injury_workload._Cut.injured_absence).
2. Rest-of-season P(plays) for a player designated Out or Doubtful at the cutoff week W, ros-avail-0.1.1
   (docs/research/ros_availability.md, its pre-registered next candidate). A run for a later week T used to give
   him an undesignated P(plays) (about 0.91 on 2023-24 while 41% played the next game). For his team's next
   PPLAY_HORIZONS games after W it is now the empirical return rate P(appears in the team's h-th game after W |
   Out/Doubtful at W, on the active roster at W), by position and games already missed
   (injury_workload.fit_return_rates(active_roster_only=True)). Later games keep the participation model's
   estimate: the 0.1.0 table lost to it at h >= 3. The table is refitted with every weekly artifact on episodes
   whose outcome game precedes W (2019 through the cutoff) and stored in it, so a run never reads an outcome at or
   after its cutoff.
3. Rest-of-season handoff scenarios: at horizon >= 1 a teammate who is injured at the cutoff is enumerated as
   present/absent scenarios weighted by the same table (a temporary starter does not keep the role all season).

Nothing else changes: candidates, withholding, the participation model at horizon 0, intervals (ifp-2.0.0's
residual bands), storage, the artifact gate and the integrity checks are the existing ones. polars/numpy: imported
by the training and projection scripts only, never by the hosted API (projection_source reads version strings).
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib

import polars as pl

from . import challenger as ch
from . import injury_challenger as ic
from . import injury_workload as iw
from .forecast_features import POSITIONS, Sources, key

MODEL_VERSION = "ifp-2.1.0"
BASE_VERSION = ch.CHALLENGER_VERSION          # ifp-2.0.0: the frozen spec this model extends
MODEL_NAME = "ifp21"                         # RidgeModel.model of every position
WORKLOAD_VERSION = iw.WORKLOAD_VERSION        # iwh-0.1.1
GROUP = {p: list(ic.GROUP[p]) for p in POSITIONS}
HANDOFF_FEATURES = ("ho_exp_xfpl", "ho_shift_pg")
FEATURE_DECIMALS = 9                         # handoff features are rounded (see handoff)
RETURN_HORIZON = 8                           # return-rate table horizons (team games after the cutoff; the handoff's
                                             # teammate-return scenarios use all of them)
PPLAY_HORIZONS = 2                           # P(plays) from the table only for the team's next 2 games (ros-avail-0.1.1)
ROS_AVAILABILITY_VERSION = "ros-avail-0.1.1"
UNAVAILABLE = iw.UNAVAILABLE_DESIGNATIONS    # ("out", "doubtful"): the designations the table conditions on
CONFIG_KEY = "injury_model"                  # Artifact.config key that marks an ifp-2.1.0 artifact
RETURN_RATE_MODEL = "return-rate-table"      # participation.model_version of a row whose P(plays) is the table's


def config(cfg: ch.ChallengerConfig) -> dict:
    """The frozen model definition stored in every artifact (and hashed into its config_hash)."""
    return {"version": MODEL_VERSION, "base": BASE_VERSION, "base_config_hash": cfg.hash(),
            "workload_version": WORKLOAD_VERSION, "group": GROUP, "listing_absence": True,   # iwh-0.1.1 rule
            "return_rate_horizons": RETURN_HORIZON, "return_rate_min_n": iw.RETURN_MIN_N,
            "return_rate_population": "Out/Doubtful at the cutoff, latest roster row <= cutoff ACT/INA",
            "ros_availability": ROS_AVAILABILITY_VERSION, "p_play_horizons": PPLAY_HORIZONS,
            "features": {p: features(cfg, p) for p in POSITIONS}}


def config_hash(cfg: ch.ChallengerConfig) -> str:
    return ch.config_hash(config(cfg))


def features(cfg: ch.ChallengerConfig, position: str) -> list[str]:
    """ifp-2.0.0's features for the position, plus the handoff group (RB/WR/TE)."""
    return ic.candidate_features(cfg, position, bool(GROUP[position]))


def is_injury_artifact(artifact) -> bool:
    return isinstance((artifact.config or {}).get(CONFIG_KEY), dict)


# ----------------------------------------------------------------------------- handoff features


def handoff(sources: Sources, season: int, target_week: int, cutoff_week: int | None = None, *,
            return_rates: dict | None = None, roster_codes: pl.DataFrame | None = None) -> pl.DataFrame:
    """The iwh-0.1.1 handoff of every RB/WR/TE for (season, target week) as known at the cutoff week. The two model
    features are rounded to FEATURE_DECIMALS: polars' parallel sums differ by ~1e-15 between identical calls, and the
    training data hash and content-addressed artifact id must reproduce on identical inputs."""
    ho = iw.workload_handoff(sources, season, target_week, cutoff_week, return_rates=return_rates,
                             roster_codes=roster_codes)
    return ho.with_columns(*[pl.col(c).round(FEATURE_DECIMALS) for c in HANDOFF_FEATURES])


def with_training_handoff(sources: Sources, store: pl.DataFrame, roster_codes: pl.DataFrame | None = None) -> pl.DataFrame:
    """`store` (forecast_backtest.build_store rows) plus ho_exp_xfpl / ho_shift_pg, each week's handoff computed at
    horizon 0 from data before that week only (the research `build`, with the 0.1.1 participation rule)."""
    if store.height == 0:
        return store.with_columns(*[pl.lit(None, dtype=pl.Float64).alias(c) for c in HANDOFF_FEATURES])
    parts = []
    for season, week in sorted({(int(s), int(w)) for s, w in store.select("season", "week").unique().iter_rows()}):
        ho = handoff(sources, season, week, roster_codes=roster_codes)
        parts.append(ho.select("gsis_id", *HANDOFF_FEATURES).with_columns(pl.lit(key(season, week)).alias("key")))
    extra = pl.concat(parts).cast({"key": store.schema["key"]})
    return store.drop([c for c in HANDOFF_FEATURES if c in store.columns]).join(extra, on=["key", "gsis_id"], how="left")


def handoff_hash(store: pl.DataFrame) -> str:
    """Digest of the handoff columns of a training store (part of the artifact's data hash)."""
    cols = ["key", "gsis_id", *HANDOFF_FEATURES]
    return hashlib.sha256(store.select(cols).sort("key", "gsis_id").write_csv().encode()).hexdigest()[:16]


def augment(artifact, sources: Sources, season: int, week: int, feature_week: int, feats: pl.DataFrame, *,
            roster_codes: pl.DataFrame | None = None) -> pl.DataFrame:
    """Live features (independent_projection_store.build_live) plus the handoff columns the artifact's models read.
    `sources` must already carry the cutoff week's designations the run uses (the ESPN status fallback for teams
    without a synced nflverse report, as for P(plays)). Horizon >= 1 mixes injured teammates' returns with the
    artifact's own return-rate table."""
    if not is_injury_artifact(artifact) or feats.height == 0:
        return feats
    rates = return_table(artifact) if week > feature_week else None
    ho = handoff(sources, season, week, feature_week, return_rates=rates, roster_codes=roster_codes)
    ho = ho.select("gsis_id", *HANDOFF_FEATURES)
    return feats.drop([c for c in HANDOFF_FEATURES if c in feats.columns]).join(ho, on="gsis_id", how="left")


# ----------------------------------------------------------------------------- training


def fit(store: pl.DataFrame, season: int, week: int, cfg: ch.ChallengerConfig) -> tuple[dict, dict]:
    """({position: RidgeModel}, {position: training rows}) for target week (season, week): ifp-2.0.0's training rows
    (challenger.train_rows) and fitting rule, with the handoff group added for RB/WR/TE. `store` must carry the
    handoff columns (with_training_handoff)."""
    missing = [c for c in HANDOFF_FEATURES if c not in store.columns]
    if missing:
        raise ValueError(f"training store has no {', '.join(missing)} (run with_training_handoff first)")
    k = key(season, week)
    models, rows = {}, {}
    for pos in POSITIONS:
        tr = ch.train_rows(store, k, pos)
        if tr.height < ch.MIN_TRAIN_ROWS:
            continue
        spec = cfg.spec(pos)
        models[pos] = ch.fit_linear(tr, pos, features(cfg, pos), spec.lam[pos], loss=spec.loss, huber_c=spec.huber_c,
                                    half_life_weeks=spec.half_life_weeks, target_key=k, name=MODEL_NAME)
        rows[pos] = tr.height
    return models, rows


def fit_return_table(sources: Sources, season: int, week: int) -> list[dict]:
    """The return-rate table for a forecast made at (season, week): every Out/Doubtful episode in the sources whose
    outcome game precedes the cutoff, as JSON cells [{position, missed, h, rate, n}] (sorted)."""
    rates = iw.fit_return_rates(sources, key(season, week), max_horizon=RETURN_HORIZON, active_roster_only=True)
    return [{"position": p, "missed": b, "h": h, "rate": round(v["rate"], 6), "n": v["n"]}
            for (p, b, h), v in sorted(rates.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]), kv[0][2]))]


def return_table(artifact) -> dict:
    """The artifact's table as injury_workload expects it: {(position, missed bucket, h): {rate, n}}."""
    cells = ((artifact.config or {}).get(CONFIG_KEY) or {}).get("return_rates") or []
    return {(c["position"], c["missed"], int(c["h"])): {"rate": float(c["rate"]), "n": int(c["n"])} for c in cells}


def return_table_summary(cells: list[dict]) -> dict:
    """Pooled rate and episode count per horizon (the artifact's evaluation summary)."""
    return {str(c["h"]): {"rate": c["rate"], "n": c["n"]} for c in cells if c["position"] == "ALL" and c["missed"] == "ALL"}


# ----------------------------------------------------------------------------- rest-of-season P(plays)


def team_games_after(sources: Sources, season: int, team: str, cutoff_week: int, week: int) -> int:
    """h: the team's scheduled games in weeks cutoff_week+1 .. week (byes skipped), the table's horizon."""
    g = sources.games.filter((pl.col("season") == season) & (pl.col("team") == team)
                             & (pl.col("week") > cutoff_week) & (pl.col("week") <= week))
    return g.select("week").n_unique()


def cutoff_injured(sources: Sources, season: int, cutoff_week: int, roster_codes: pl.DataFrame | None = None) -> dict:
    """{gsis_id: timeline row} of every player designated Out or Doubtful for the cutoff week's game (the cutoff
    report, including the ESPN status fallback the caller merged into `sources`)."""
    tl = iw.injury_timeline(sources, season, cutoff_week, roster_codes=roster_codes)
    return {r["gsis_id"]: r for r in tl.filter(pl.col("designation").is_in(list(UNAVAILABLE))).iter_rows(named=True)}


def ros_p_play(artifact, sources: Sources, season: int, cutoff_week: int, week: int, rows: list[dict], *,
               injured: dict) -> dict[str, dict]:
    """{gsis_id: participation dict} replacing the undesignated P(plays) of a rest-of-season row (week > cutoff)
    whose player was Out or Doubtful at the cutoff, for h (his team's games after the cutoff) <= PPLAY_HORIZONS: the
    artifact's empirical return rate for his position, games already missed and h. Every other row keeps the
    participation model's estimate (it is not returned)."""
    rates = return_table(artifact)
    if week <= cutoff_week or not rates:
        return {}
    out = {}
    for r in rows:
        t = injured.get(r.get("gsis_id"))
        if t is None or r.get("coverage_status") != "projected":
            continue
        h = team_games_after(sources, season, r["nfl_team"], cutoff_week, week)
        if h > PPLAY_HORIZONS:
            continue
        hit = iw.return_rate_cell(rates, t["position"], t["missed_team_games"], h)
        if hit is None:
            continue
        (cell_pos, bucket, cell_h), cell = hit
        designation = t["designation"].capitalize()
        out[r["gsis_id"]] = {
            "p_play": round(cell["rate"], 4), "model_version": RETURN_RATE_MODEL,
            "ros_availability": ROS_AVAILABILITY_VERSION, "status": "production",
            "expected_points_status": "production",
            "return_rate": {"position": cell_pos, "missed_games_bucket": bucket, "h": h, "table_h": cell_h,
                            "episodes": cell["n"], "cutoff_week": cutoff_week, "designation": t["designation"]},
            "flags": [f"{designation} for week {cutoff_week} (the cutoff): P(plays) in week {week} is the historical "
                      f"return rate {cell['rate']:.0%} ({cell['n']} {cell_pos if cell_pos != 'ALL' else 'skill-player'} "
                      f"episodes, {h} team game(s) later, {bucket} game(s) already missed)"]}
    return out


def artifact_config(cfg: ch.ChallengerConfig, return_cells: list[dict]) -> dict:
    return {**config(cfg), "return_rates": return_cells, "base_config": asdict(cfg)}


def describe(artifact) -> dict:
    """Short, JSON-safe summary of an ifp-2.1.0 artifact (CLI output)."""
    c = artifact.config.get(CONFIG_KEY) or {}
    return {"version": artifact.version, "workload_version": c.get("workload_version"),
            "return_rates": return_table_summary(c.get("return_rates") or []),
            "features": {p: [f for f in c.get("features", {}).get(p, []) if f in HANDOFF_FEATURES] for p in POSITIONS}}


__all__ = ["MODEL_VERSION", "BASE_VERSION", "WORKLOAD_VERSION", "GROUP", "HANDOFF_FEATURES", "config", "config_hash",
           "features", "is_injury_artifact", "handoff", "with_training_handoff", "augment", "fit", "fit_return_table",
           "return_table", "cutoff_injured", "ros_p_play", "artifact_config", "describe"]
