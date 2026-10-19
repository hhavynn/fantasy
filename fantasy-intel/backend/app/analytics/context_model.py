"""ifp-2.2.0: the production "Ours" model. ifp-2.1.0 plus the prospective research shadows, by owner decision.

ifp-2.1.0 (app.analytics.injury_model: ifp-2.0.0 + the injury workload handoff + return-rate P(plays)) is kept whole.
On top of it each position adds the frozen spec of the research shadows that collect prospective evidence from 2026
week 7 (docs/independent_projection_engine.md, "ifp-2.2.0"; per-component evidence in docs/research/ifp22_promotion.md):

=================  ============  =====================================================================  =============
component          shadow        what it adds (each shadow's own frozen definition, no refit choices)   positions
=================  ============  =====================================================================  =============
qb_scenarios       ifp-ctx-p2q   start probability of every plausible QB (conditional logit, refitted   QB
                                 on earlier team-weeks), s_start + qb_self_epa, and the two-scenario
                                 mixture pi x f(start) + (1 - pi) x f(no start), pi = P(start | appears)
qb_context         ifp-ctx-p1    the expected starter's EPA / CPOE vs the player's recent QBs, QB change RB, WR
teammates          ifp-ctx-p1    vacated and returning target / carry claims of unavailable teammates     RB, WR
depth_tier         ifp-dct-p1    static pregame depth-chart tier (nflverse modern charts, 1.5 h point-in  QB, RB, WR
                                 time, at most 8 days old)
decline_role       ifp-awd-p1    the decline-only adaptive role aw_role_d in place of xfpl_eb            RB, WR, TE
qb_participation   ipm-cal-0.1.0 the frozen QB isotonic map on the participation model's P(plays)       QB (P(plays))
                                 (left out by the guard: GUARD_DROPPED; the code path stays tested)
=================  ============  =====================================================================  =============

Overlaps, resolved once (docs/research/ifp22_promotion.md, "Overlaps"): ifp-ctx-p1 and ifp-ctx-p2q both change the
QB model. ifp-ctx-p2q was pre-registered after ifp-ctx-p1 as its QB successor (qb_scenario_mixture.md section 8) and has
the better development result (QB MAE -0.264 vs -0.146), so QB uses the scenario mixture and ifp-ctx-p1 contributes its
receiver groups only. ifp-fr-p1 (FTN first reads) is not part of ifp-2.2.0: FTN data is CC-BY-SA 4.0 and publishing a
projection derived from it waits on the owner's licensing decision.

SHIPPED is the backtest guard's outcome (scripts/ifp22_backtest.py): a component whose leave-one-out difference makes a
position clearly worse (week-clustered MAE CI entirely above 0) is left out for that position; every neutral or better
component ships. The shadows keep running unchanged and are the post-hoc evidence.

The QB scenario code is the shadow's own file (scripts/qb_scenarios.py, sha256-bound into the ifp-ctx-p2q spec, so it
cannot change under either). It is imported lazily by the training and projection scripts only; the hosted API reads
stored runs and never imports this module (projection_source keeps version strings).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import polars as pl

from . import adaptive_workload as aw
from . import context_challenger as cc
from . import injury_model as im
from . import roster_context as rc
from . import usage_signals as us
from .forecast_features import POSITIONS, Sources, key
from .independent_projections import RidgeModel, explain_row, interval_for
from .independent_projections import predict_frame as base_predict_frame

MODEL_VERSION = "ifp-2.2.0"
BASE_VERSION = im.MODEL_VERSION               # ifp-2.1.0: kept whole, this model extends it
MODEL_NAME = "ifp22"                         # RidgeModel.model of every position
CONFIG_KEY = "context_model"                 # Artifact.config key that marks an ifp-2.2.0 artifact
FEATURE_DECIMALS = 9                         # added features are rounded (reproducible data hash, as the handoff)

QB_SCENARIOS, QB_CONTEXT, TEAMMATES, DEPTH_TIER, DECLINE_ROLE, QB_PARTICIPATION = (
    "qb_scenarios", "qb_context", "teammates", "depth_tier", "decline_role", "qb_participation")
COMPONENTS = {
    QB_SCENARIOS: {"shadow": "ifp-ctx-p2q", "positions": ("QB",), "kind": "points",
                   "label": "starting-QB probabilities and the two-scenario mixture"},
    QB_CONTEXT: {"shadow": "ifp-ctx-p1", "positions": ("RB", "WR"), "kind": "points",
                 "label": "expected starting QB vs the player's recent QBs"},
    TEAMMATES: {"shadow": "ifp-ctx-p1", "positions": ("RB", "WR"), "kind": "points",
                "label": "unavailable and returning teammates' target / carry claims"},
    DEPTH_TIER: {"shadow": "ifp-dct-p1", "positions": tuple(us.PROSPECTIVE["ifp-dct-p1"]["positions"]), "kind": "points",
                 "label": "pregame depth-chart tier"},
    DECLINE_ROLE: {"shadow": aw.AWD_VERSION, "positions": tuple(aw.AWD_POSITIONS), "kind": "points",
                   "label": "decline-only adaptive role"},
    QB_PARTICIPATION: {"shadow": "ipm-cal-0.1.0", "positions": ("QB",), "kind": "p_play",
                       "label": "calibrated QB P(plays)"},
}
FEATURE_COMPONENTS = (QB_SCENARIOS, QB_CONTEXT, TEAMMATES, DEPTH_TIER, DECLINE_ROLE)
CANDIDATE = {p: tuple(c for c, d in COMPONENTS.items() if p in d["positions"]) for p in POSITIONS}

# The backtest guard's outcome (docs/research/ifp22_promotion.md): what ships per position. Filled from
# scripts/ifp22_backtest.py's report; a component missing here was left out for that position (GUARD_DROPPED says why).
GUARD_DROPPED: dict[str, dict[str, str]] = {
    "QB": {QB_PARTICIPATION: "unconditional QB MAE +0.016 [+0.003, +0.031] with the walk-forward map (2023 - 2026 week 6, "
                             "4,038 QB-weeks; Brier 0.118 -> 0.115 improves, but the CI is entirely above 0)"},
    "RB": {}, "WR": {}, "TE": {}}
SHIPPED: dict[str, tuple[str, ...]] = {p: tuple(c for c in CANDIDATE[p] if c not in GUARD_DROPPED[p]) for p in POSITIONS}
EXCLUDED = {"ifp-fr-p1": "FTN first-read data is CC-BY-SA 4.0; serving a projection derived from it waits on the "
                         "owner's licensing decision (attribution and ShareAlike)"}

QB2_GROUP = ["s_start", "qb_self_epa"]       # qb_scenarios.QB2_GROUP["QB"] (asserted when the shadow code loads)
DEPTH_FEATURE = "dc_tier"
DEPTH_CUTOFF_HOURS = float(us.PROSPECTIVE["ifp-dct-p1"]["cutoff_hours"])
DEPTH_FROM = us.MODERN_FROM                  # modern (timestamped) charts only: 2025 on
ROLE_FEATURE = f"aw_role{aw.DECLINE_SUFFIX}"
CAL_VERSION = "ipm-cal-0.1.0"
CAL_POSITION = "QB"
P_CLIP = 1e-4                                # participation_calibration.P_CLIP

CONTEXT_COLUMNS = ("expected_qb_id", "qb_confidence", "qb_reason", "qb_self_epa", "qb_epa_delta", "qb_cpoe_delta",
                   "qb_changed", "tm_tgt_vacated", "tm_tgt_returning", "tm_car_vacated", "tm_car_returning",
                   "tm_tgt_gain_pg", "tm_car_gain_pg", "top_tgt_comp_out", "top_tgt_comp_return", "rb1_out")
ADDED_FLOATS = ("qb_self_epa", "qb_epa_delta", "qb_cpoe_delta", "tm_tgt_vacated", "tm_tgt_returning",
                "tm_car_vacated", "tm_car_returning", "tm_tgt_gain_pg", "tm_car_gain_pg", ROLE_FEATURE, DEPTH_FEATURE)
REPO_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _qs():
    """The ifp-ctx-p2q model code (scripts/qb_scenarios.py, the shadow's frozen file)."""
    if str(REPO_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(REPO_SCRIPTS))
    import qb_scenarios as qs

    assert qs.QB2_GROUP["QB"] == QB2_GROUP, "qb_scenarios QB group changed"
    return qs


def qb_scenario_code_sha() -> str:
    return hashlib.sha256((REPO_SCRIPTS / "qb_scenarios.py").read_bytes()).hexdigest()


# ----------------------------------------------------------------------------- definition


def features(cfg, position: str, components: tuple[str, ...] | None = None) -> list[str]:
    """ifp-2.1.0's features for the position plus the components' groups (default: SHIPPED)."""
    comps = SHIPPED[position] if components is None else components
    feats = list(im.features(cfg, position))
    if DECLINE_ROLE in comps and position in aw.AWD_POSITIONS:
        assert "xfpl_eb" in feats, f"{position} has no xfpl_eb to replace"
        feats = [ROLE_FEATURE if f == "xfpl_eb" else f for f in feats]
    add: list[str] = []
    if QB_SCENARIOS in comps and position == "QB":
        add += QB2_GROUP
    if QB_CONTEXT in comps:
        add += cc.GROUPS["qb"][position]
    if TEAMMATES in comps:
        add += cc.GROUPS["tm"][position]
    if DEPTH_TIER in comps:
        add.append(DEPTH_FEATURE)
    return feats + [f for f in add if f not in feats]


def uses_scenarios(position: str, components: tuple[str, ...] | None = None) -> bool:
    return position == "QB" and QB_SCENARIOS in (SHIPPED[position] if components is None else components)


def config(cfg) -> dict:
    """The frozen model definition stored in every artifact (and hashed into its config_hash)."""
    return {"version": MODEL_VERSION, "base": BASE_VERSION, "base_config_hash": im.config_hash(cfg),
            "components": {p: list(SHIPPED[p]) for p in POSITIONS},
            "shadows": {c: COMPONENTS[c]["shadow"] for c in COMPONENTS},
            "guard_dropped": GUARD_DROPPED, "excluded": EXCLUDED,
            "context_version": rc.CONTEXT_VERSION, "qb_scenario_code_sha256": qb_scenario_code_sha(),
            "depth": {"cutoff_hours": DEPTH_CUTOFF_HOURS, "max_stale_days": us.MAX_STALE_DAYS, "from_season": DEPTH_FROM},
            "decline_role": {"config": aw.DECLINE_CONFIG.tag(), "version": aw.AWD_VERSION},
            "participation_calibration": CAL_VERSION,
            "features": {p: features(cfg, p) for p in POSITIONS}}


def config_hash(cfg) -> str:
    from .challenger import config_hash as h

    return h(config(cfg))


def is_context_artifact(artifact) -> bool:
    return isinstance((artifact.config or {}).get(CONFIG_KEY), dict)


# ----------------------------------------------------------------------------- inputs


@dataclass
class Inputs:
    """Data the ifp-2.2.0 components read beyond forecast_features.Sources."""
    ctx: rc.ContextSources                    # starters, QB stats, team environment (history + live DB)
    depth: pl.DataFrame | None                # normalized modern depth snapshots (usage_signals.normalize_modern)
    depth_note: str | None = None             # why depth is missing, when it is


def load_inputs(cons: list, depth_dir: Path | None, season: int) -> Inputs:
    """ContextSources from the open connections (history first, the live DB last wins) and the depth snapshots stored
    under `depth_dir` (usage_signals content-addressed raw snapshots, family "depth", DEPTH_FROM..season)."""
    ctx = rc.ContextSources.concat([rc.load_context_sources(c) for c in cons if c is not None])
    depth, note = None, None
    if depth_dir is None:
        note = "no depth snapshot directory"
    else:
        frames = []
        for s in range(DEPTH_FROM, season + 1):
            df = us.read_snapshot(Path(depth_dir), "depth", s)
            if df is None:
                note = f"no stored depth snapshot for {s} in {depth_dir}"
                frames = []
                break
            frames.append(df)
        if frames:
            depth, _ = us.normalize_modern(pl.concat(frames, how="diagonal_relaxed"))
    return Inputs(ctx, depth, note)


def _games(sources: Sources) -> pl.DataFrame:
    from .projection_backtest import _kickoff

    return sources.games.with_columns(pl.struct("gameday", "gametime").map_elements(
        lambda r: _kickoff(r), return_dtype=pl.Datetime("us", "UTC")).alias("kickoff"))


def _round(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns([pl.col(c).cast(pl.Float64).round(FEATURE_DECIMALS) for c in ADDED_FLOATS if c in df.columns])


def _week_report_free(sources: Sources, season: int, week: int) -> Sources:
    """`sources` without the week's injury report (rest-of-season rows: the cutoff week's next-game designations do
    not describe a later week)."""
    if sources.injuries is None:
        return sources
    inj = sources.injuries.filter(~((pl.col("season") == season) & (pl.col("week") == week)))
    return Sources(sources.panel, sources.games, sources.defense, sources.rosters, inj, sources.exits)


# ----------------------------------------------------------------------------- per-week component features


def context_columns(sources: Sources, inputs: Inputs, season: int, week: int, targets: pl.DataFrame,
                    report_mode: str = "week") -> pl.DataFrame:
    """ifp-ctx-p1's QB and teammate columns (roster_context.context_for_week) for `targets` (gsis_id, position, team,
    opponent) of week (season, week)."""
    out = rc.context_for_week(sources, inputs.ctx, season, week, targets.select("gsis_id", "position", "team", "opponent"),
                              report_mode)
    return out.select("gsis_id", *[c for c in CONTEXT_COLUMNS if c in out.columns])


def depth_columns(sources: Sources, inputs: Inputs, targets: pl.DataFrame, as_of: datetime | None = None,
                  games: pl.DataFrame | None = None) -> pl.DataFrame:
    """ifp-dct-p1's dc_tier for `targets` (season, week, gsis_id, position, team): the team's latest modern snapshot
    strictly before kickoff - 1.5 h (and before `as_of`, the forecast time), at most 8 days old; null otherwise."""
    schema = {"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, DEPTH_FEATURE: pl.Float64,
              "dc_age_hours": pl.Float64}
    t = targets.filter(pl.col("season") >= DEPTH_FROM)
    if inputs.depth is None or t.height == 0:
        return pl.DataFrame(schema=schema)
    if as_of is not None and as_of.tzinfo is None:      # db.connection.utcnow is naive UTC
        as_of = as_of.replace(tzinfo=timezone.utc)
    snaps = inputs.depth if as_of is None else inputs.depth.filter(pl.col("dt") < as_of)
    g = _games(sources) if games is None else games
    dc, _ = us.depth_features(snaps, g, t.select("season", "week", "gsis_id", "position", "team"), DEPTH_CUTOFF_HOURS)
    return dc.select("season", "week", "gsis_id", DEPTH_FEATURE, "dc_age_hours").cast(
        {"season": pl.Int64, "week": pl.Int64})


def role_columns(sources: Sources, season: int, week: int, targets: pl.DataFrame) -> pl.DataFrame:
    """ifp-awd-p1's decline-only role (adaptive_workload.DECLINE_CONFIG) for `targets` (gsis_id, position, team)."""
    r = aw.role_features_for_week(sources, season, week, targets.select("gsis_id", "position", "team"), aw.DECLINE_CONFIG)
    return r.select("gsis_id", pl.col("aw_role").alias(ROLE_FEATURE), pl.col("aw_streak").alias(f"aw_streak{aw.DECLINE_SUFFIX}"))


def realized_starters(inputs: Inputs) -> pl.DataFrame:
    """(key, team, act_qb_id): the starting QB of every past game (training labels of s_start only)."""
    s = inputs.ctx.starters
    return (s.filter(pl.col("qb_id").is_not_null())
            .select((pl.col("season") * 100 + pl.col("week")).alias("key"), "team", pl.col("qb_id").alias("act_qb_id"))
            .unique(["key", "team"], keep="first"))


# ----------------------------------------------------------------------------- training


def with_training_inputs(sources: Sources, store: pl.DataFrame, inputs: Inputs,
                         roster_codes: pl.DataFrame | None = None, *, handoff: bool = True) -> pl.DataFrame:
    """`store` (forecast_backtest.build_store rows) plus every column the components read, each week computed from
    data before that week only: the ifp-2.1.0 handoff, ifp-ctx-p1's context, the realized start (s_start, QB rows),
    the depth tier and the decline-only role."""
    if handoff:
        store = im.with_training_handoff(sources, store, roster_codes)
    if store.height == 0:
        return store.with_columns(*[pl.lit(None, dtype=pl.Float64).alias(c) for c in ADDED_FLOATS + ("s_start",)])
    games = _games(sources)
    ctx_parts, dc_parts, role_parts = [], [], []
    for (s, w), rows in store.group_by("season", "week", maintain_order=True):
        s, w = int(s), int(w)
        k = pl.lit(key(s, w)).cast(store["key"].dtype).alias("key")
        ctx_parts.append(context_columns(sources, inputs, s, w, rows).with_columns(k))
        role_parts.append(role_columns(sources, s, w, rows).with_columns(k))
    dc = depth_columns(sources, inputs, store.select("season", "week", "gsis_id", "position", "team"), games=games)
    keep = [c for c in store.columns if c not in set(CONTEXT_COLUMNS) | {ROLE_FEATURE, f"aw_streak{aw.DECLINE_SUFFIX}",
                                                                         DEPTH_FEATURE, "dc_age_hours", "s_start",
                                                                         "act_qb_id"}]
    out = (store.select(keep)
           .join(pl.concat(ctx_parts, how="diagonal_relaxed"), on=["key", "gsis_id"], how="left")
           .join(pl.concat(role_parts, how="diagonal_relaxed"), on=["key", "gsis_id"], how="left")
           .join(dc.cast({"season": store["season"].dtype, "week": store["week"].dtype}),
                 on=["season", "week", "gsis_id"], how="left")
           .join(realized_starters(inputs).cast({"key": store["key"].dtype}), on=["key", "team"], how="left"))
    out = out.with_columns(
        pl.when(pl.col("position") != "QB").then(None).when(pl.col("act_qb_id").is_null()).then(None)
          .otherwise((pl.col("gsis_id") == pl.col("act_qb_id")).cast(pl.Float64)).alias("s_start")).drop("act_qb_id")
    return _round(out)


def source_inputs_hash(inputs: Inputs, sources: Sources, season: int, week: int) -> str:
    """Digest of what ifp-2.2.0's training reads beyond the training store and forecast_features.Sources, for target
    week W: the context tables before W and the depth snapshots made before W's first kickoff (later rows never
    enter the artifact, so a mid-week sync or a fresh depth download does not force a retrain)."""
    k = key(season, week)
    before = (pl.col("season") * 100 + pl.col("week")) < k
    h = hashlib.sha256()
    for name in ("starters", "qb_stats", "team_env", "def_targets"):
        df = getattr(inputs.ctx, name)
        h.update(name.encode())
        if df is not None and df.height:
            df = df.filter(before)
            h.update(df.sort(sorted(df.columns)).write_csv().encode())
    if inputs.depth is not None:
        kick = _games(sources).filter((pl.col("season") == season) & (pl.col("week") == week))["kickoff"].min()
        d = inputs.depth if kick is None else inputs.depth.filter(pl.col("dt") < kick)
        h.update(b"depth" + d.sort(sorted(d.columns)).write_csv().encode())
    return h.hexdigest()[:16]


def inputs_hash(store: pl.DataFrame) -> str:
    """Digest of the component columns of a training store (part of the artifact's data hash)."""
    cols = ["key", "gsis_id", "s_start", *[c for c in ADDED_FLOATS if c in store.columns]]
    return hashlib.sha256(store.select(cols).sort("key", "gsis_id").write_csv().encode()).hexdigest()[:16]


def start_candidates(sources: Sources, inputs: Inputs, weeks: list[tuple[int, int]],
                     teams: dict[tuple[int, int], list[str]] | None = None) -> pl.DataFrame:
    """ifp-ctx-p2q start candidates (qb_scenarios.start_candidates) for every week, from the shadow's own code."""
    qs = _qs()
    parts = [qs.start_candidates(sources, inputs.ctx, s, w, teams=None if teams is None else teams.get((s, w)))
             for s, w in weeks]
    full = [p for p in parts if p.height]
    if full:
        return pl.concat(full, how="vertical_relaxed")
    return parts[0] if parts else pl.DataFrame(schema={
        "season": pl.Int64, "week": pl.Int64, "key": pl.Int64, "team": pl.String, "gsis_id": pl.String,
        **{f: pl.Float64 for f in qs.START_FEATURES}, "started": pl.Float64, "appeared": pl.Float64})


def fit_start(cands: pl.DataFrame, season: int, week: int) -> dict:
    """The start model for a forecast of week (season, week): fitted on candidate team-weeks strictly before it from
    START_TRAIN_START (qb_scenarios.walk_forward_start's rule), with its relief-appearance rates."""
    qs = _qs()
    k = key(season, week)
    train = cands.filter((pl.col("key") < k) & (pl.col("season") >= qs.START_TRAIN_START)) if cands.height else cands
    assert train.height == 0 or train["key"].max() < k, "start-model training row at or after the forecast week"
    m = qs.fit_start_model(train)
    rates = (qs.relief_rates(qs.predict_start(m, train)) if train.height
             else qs.relief_rates(train.with_columns(pl.lit(0.0).alias("p_start"))))
    return {**m, "relief_rates": {str(r): float(v) for r, v in rates.items()}, "train_team_weeks": m["groups"]}


def start_probabilities(start_model: dict, cands: pl.DataFrame) -> pl.DataFrame:
    """(key, team, gsis_id, p_start, pi_start, rank) of the week's candidates under a fitted start model."""
    qs = _qs()
    if cands.height == 0:
        return pl.DataFrame(schema={"key": pl.Int64, "team": pl.String, "gsis_id": pl.String, "p_start": pl.Float64,
                                    "pi_start": pl.Float64, "rank": pl.Int64})
    rates = {int(r): float(v) for r, v in start_model["relief_rates"].items()}
    pc = qs.with_pi(qs.predict_start(start_model, cands), rates)
    return pc.select("key", "team", "gsis_id", "p_start", "pi_start", "rank")


def with_pi(rows: pl.DataFrame, probs: pl.DataFrame) -> pl.DataFrame:
    """QB rows get pi_start (qb_scenarios.scenario_columns' rule): the candidate's P(start | appears), 0 for a QB who
    is not a candidate of a team-week that has candidates, null when the team-week has none (fallback model)."""
    has = probs.select("key", "team").unique().with_columns(pl.lit(True).alias("_has"))
    out = (rows.drop([c for c in ("pi_start", "p_start") if c in rows.columns])
           .join(has.cast({"key": rows["key"].dtype}), on=["key", "team"], how="left")
           .join(probs.select("key", "team", "gsis_id", "p_start", "pi_start").cast({"key": rows["key"].dtype}),
                 on=["key", "team", "gsis_id"], how="left"))
    return out.with_columns(
        pl.when(pl.col("_has").is_null() | (pl.col("position") != "QB")).then(None)
          .otherwise(pl.col("pi_start").fill_null(0.0)).alias("pi_start")).drop("_has")


def fit(store: pl.DataFrame, season: int, week: int, cfg, components: dict[str, tuple[str, ...]] | None = None,
        positions: tuple[str, ...] = POSITIONS) -> tuple[dict, dict, dict]:
    """({position: RidgeModel}, {position: training rows}, {position: fallback RidgeModel}) for target week (season,
    week): ifp-2.1.0's training rows and fitting rule (challenger.train_rows / fit_linear, frozen penalty and recency)
    with the components' features. QB with the scenario group also gets a fallback model without it (a team-week with
    no start candidate). `store` must carry with_training_inputs' columns."""
    from . import challenger as ch

    comps = SHIPPED if components is None else components
    k = key(season, week)
    models, rows, fallback = {}, {}, {}
    for pos in positions:
        tr = ch.train_rows(store, k, pos)
        if tr.height < ch.MIN_TRAIN_ROWS:
            continue
        spec = cfg.spec(pos)
        fitp = lambda feats: ch.fit_linear(tr, pos, feats, spec.lam[pos], loss=spec.loss, huber_c=spec.huber_c,  # noqa: E731
                                           half_life_weeks=spec.half_life_weeks, target_key=k, name=MODEL_NAME)
        models[pos] = fitp(features(cfg, pos, comps[pos]))
        rows[pos] = tr.height
        if uses_scenarios(pos, comps[pos]):
            fallback[pos] = fitp(features(cfg, pos, tuple(c for c in comps[pos] if c != QB_SCENARIOS)))
    return models, rows, fallback


def mix_points(model: RidgeModel, rows: pl.DataFrame) -> np.ndarray:
    """QB scenario mixture pi x f(start) + (1 - pi) x f(no start) (qb_scenarios.mix_predict); NaN where pi is null."""
    pi = rows["pi_start"].cast(pl.Float64).fill_null(np.nan).to_numpy()
    f1 = model.predict(rows.with_columns(pl.lit(1.0).alias("s_start")))
    f0 = model.predict(rows.with_columns(pl.lit(0.0).alias("s_start")))
    return pi * f1 + (1.0 - pi) * f0


def predict_points(models: dict, fallback: dict, rows: pl.DataFrame, position: str,
                   components: tuple[str, ...] | None = None) -> np.ndarray:
    """Conditional points of one position's rows (NaN = no model). QB with the scenario group: the mixture, the
    fallback model where the team-week has no start candidate."""
    m = models.get(position)
    if m is None or rows.height == 0:
        return np.full(rows.height, np.nan)
    if not uses_scenarios(position, components):
        return m.predict(rows)
    p = mix_points(m, rows)
    fb = fallback.get(position)
    return np.where(np.isnan(p), fb.predict(rows) if fb is not None else np.nan, p)


# ----------------------------------------------------------------------------- live (independent_projection_store)


def augment(artifact, sources: Sources, inputs: Inputs, season: int, week: int, feature_week: int,
            feats: pl.DataFrame, *, as_of: datetime) -> pl.DataFrame:
    """Live features (build_live, after injury_model.augment) plus every column the artifact's components read, all
    as known at the feature week W (the cutoff). Horizon 0 reads W's injury report (`sources` carries the ESPN status
    fallback, as for P(plays)); a later target week drops W's report (next-game designations) and keeps the roster
    status. Depth: W's game, snapshots strictly before `as_of`."""
    if not is_context_artifact(artifact) or feats.height == 0:
        return feats
    fw = feature_week
    src = sources if week == fw else _week_report_free(sources, season, fw)
    mode = "week"     # with W's report removed above for a later week, "week" reads no designation for W
    tg = feats.select("gsis_id", "position", "team", "opponent")
    drop = set(CONTEXT_COLUMNS) | {ROLE_FEATURE, f"aw_streak{aw.DECLINE_SUFFIX}", DEPTH_FEATURE, "dc_age_hours",
                                   "s_start", "pi_start", "p_start"}
    out = feats.drop([c for c in feats.columns if c in drop])
    out = out.join(context_columns(src, inputs, season, fw, tg, mode), on="gsis_id", how="left")
    out = out.join(role_columns(src, season, fw, tg), on="gsis_id", how="left")
    dt = tg.select(pl.lit(season, dtype=pl.Int64).alias("season"), pl.lit(fw, dtype=pl.Int64).alias("week"),
                   "gsis_id", "position", "team")
    out = out.join(depth_columns(src, inputs, dt, as_of=as_of).select("gsis_id", DEPTH_FEATURE, "dc_age_hours"),
                   on="gsis_id", how="left")
    k = key(season, fw)
    out = out.with_columns(pl.lit(k, dtype=pl.Int64).alias("key"))
    start = (artifact.config.get(CONFIG_KEY) or {}).get("start_model")
    if start is not None:
        teams = sorted(set(out.filter(pl.col("position") == "QB")["team"].drop_nulls().to_list()))
        cands = start_candidates(src, inputs, [(season, fw)] if teams else [], {(season, fw): teams})
        probs = start_probabilities(start, cands)
        out = with_pi(out, probs)
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Float64).alias("pi_start"))
    # s_start for the explanation and the plain prediction: the expected value pi (mixture is computed explicitly)
    return _round(out.with_columns(pl.col("pi_start").alias("s_start")))


def predict_frame(artifact, feats: pl.DataFrame) -> list[dict]:
    """independent_projections.predict_frame with the QB scenario mixture: QB rows' model_points are
    pi x f(start) + (1 - pi) x f(no start) (the fallback model's points when the team-week has no start candidate),
    their interval and explanation follow the point."""
    out = base_predict_frame(artifact, feats)
    cfgk = artifact.config.get(CONFIG_KEY) or {}
    comps = tuple((cfgk.get("components") or {}).get("QB") or ())
    m = artifact.models.get("QB")
    if m is None or not uses_scenarios("QB", comps):
        return out
    fb = {p: RidgeModel.from_dict(d) for p, d in (cfgk.get("fallback_models") or {}).items()}
    qb = feats.filter(pl.col("position") == "QB")
    idx = {r["gsis_id"]: i for i, r in enumerate(qb.iter_rows(named=True))}
    pts = predict_points(artifact.models, fb, qb, "QB", comps)
    for item in out:
        if item.get("position") != "QB" or item.get("model_points") is None:
            continue
        i = idx[item["gsis_id"]]
        p = round(float(pts[i]), 2)
        row = qb.slice(i, 1)
        used = m if row["pi_start"][0] is not None else fb.get("QB", m)
        iv = interval_for(artifact.intervals, "QB", p)
        item.update(model_points=p, explanation=explain_row(used, row),
                    interval_low=None if iv is None else round(iv[0], 2),
                    interval_high=None if iv is None else round(iv[1], 2))
    return out


# ----------------------------------------------------------------------------- QB P(plays) (ipm-cal-0.1.0)


def load_participation_map(models_dir: Path) -> tuple[dict | None, str]:
    """(the frozen ipm-cal-0.1.0 QB isotonic map {x, y, source, sha256}, note). Reads the participation-calibration
    shadow's frozen copy (research/participation_cal_shadow/frozen_ipm-cal-0.1.0.json, content hash verified), else
    the study's artifact (research/participation_calibration_ipm-cal-0.1.0.json, status shadow). None when neither
    exists: the component then keeps the participation model's P(plays)."""
    from .participation_calibration import CalibrationArtifact

    root = Path(models_dir) / "research"
    frozen = root / "participation_cal_shadow" / f"frozen_{CAL_VERSION}.json"
    source = root / f"participation_calibration_{CAL_VERSION}.json"
    art, origin = None, None
    if frozen.exists():
        rec = json.loads(frozen.read_text())
        body = {k: v for k, v in rec.items() if k not in ("config_hash", "frozen_at")}
        if hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16] != rec.get("config_hash"):
            raise ValueError(f"{frozen.name} does not match its config_hash: it was changed after the freeze")
        art, origin = rec["artifact"], str(frozen)
    elif source.exists():
        art, origin = json.loads(source.read_text()), str(source)
    if art is None:
        return None, f"no {CAL_VERSION} map in {root} (participation_cal_shadow.py cycle freezes it)"
    CalibrationArtifact.from_dict(art)
    cal = (art.get("calibrators") or {}).get(CAL_POSITION) or {}
    params = cal.get("params") or {}
    if cal.get("method") != "isotonic" or cal.get("fallback") or not params.get("x"):
        return None, f"the {CAL_VERSION} QB map is {cal.get('method')!r}, not a fitted isotonic map"
    knots = {"x": [float(v) for v in params["x"]], "y": [float(v) for v in params["y"]]}
    return {**knots, "version": art.get("version"), "training_cutoff_key": art.get("training_cutoff_key"),
            "source": origin, "sha256": hashlib.sha256(json.dumps(knots, sort_keys=True).encode()).hexdigest()[:16]}, "ok"


def calibrate(pmap: dict, p: float) -> float:
    """The isotonic map applied to one P(plays) (participation_calibration.Calibrator.apply)."""
    return float(np.clip(np.interp(np.clip(p, P_CLIP, 1 - P_CLIP), pmap["x"], pmap["y"]), P_CLIP, 1 - P_CLIP))


def calibrate_rows(artifact, rows: list[dict], horizon: int) -> int:
    """Horizon 0 only: every projected QB row whose P(plays) is the participation model's gets the frozen QB map
    (ifp-2.1.0's return-rate rows at a later horizon are never touched). Mutates `rows`; returns how many changed."""
    pmap = ((artifact.config.get(CONFIG_KEY) or {}).get("participation_map"))
    comps = ((artifact.config.get(CONFIG_KEY) or {}).get("components") or {}).get(CAL_POSITION) or ()
    if horizon or pmap is None or QB_PARTICIPATION not in comps:
        return 0
    n = 0
    for rec in rows:
        q = rec.get("participation") or {}
        if (rec.get("position") != CAL_POSITION or rec.get("coverage_status") != "projected" or rec.get("p_play") is None
                or q.get("model_version") == im.RETURN_RATE_MODEL):
            continue
        p = calibrate(pmap, float(rec["p_play"]))
        rec.update(p_play=round(p, 4), expected_points=round(p * rec["projected_points"], 2),
                   participation={**q, "p_play": round(p, 4), "uncalibrated_p_play": q.get("p_play"),
                                  "calibration": CAL_VERSION})
        n += 1
    return n


# ----------------------------------------------------------------------------- artifact


def artifact_config(cfg, *, start_model: dict | None, fallback: dict, participation_map: dict | None,
                    participation_note: str, depth_note: str | None) -> dict:
    return {**config(cfg), "start_model": start_model,
            "fallback_models": {p: m.to_dict() for p, m in fallback.items()},
            "participation_map": participation_map, "participation_map_note": participation_note,
            "depth_note": depth_note}


def describe(artifact) -> dict:
    """Short, JSON-safe summary of an ifp-2.2.0 artifact (CLI output)."""
    c = artifact.config.get(CONFIG_KEY) or {}
    sm = c.get("start_model") or {}
    pm_ = c.get("participation_map") or {}
    return {"version": artifact.version, "components": c.get("components"), "guard_dropped": c.get("guard_dropped"),
            "start_model_team_weeks": sm.get("train_team_weeks"), "participation_map": pm_.get("sha256"),
            "participation_map_note": c.get("participation_map_note"), "depth_note": c.get("depth_note")}


__all__ = ["MODEL_VERSION", "BASE_VERSION", "COMPONENTS", "SHIPPED", "EXCLUDED", "features", "config", "config_hash",
           "is_context_artifact", "Inputs", "load_inputs", "with_training_inputs", "start_candidates", "fit_start",
           "fit", "predict_points", "augment", "predict_frame", "load_participation_map", "calibrate_rows",
           "artifact_config", "describe"]
