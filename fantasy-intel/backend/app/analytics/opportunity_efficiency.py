"""Research only: two-target (opportunity + efficiency) projections (ifp-oe research challenger).

Nothing here changes ifp-1.0.0 or ifp-2.0.0, nothing outside research scripts imports it
(tests/test_opportunity_efficiency.py), and its artifact cannot be loaded as a production model.
See docs/research/opportunity_efficiency.md.

Targets (outcome side, known only after the game, same league scoring as the label):
  t_opp  realized expected league points of the target game: the league's own weights applied to
         nflverse's expected counts for that game's realized opportunities (`xfpl`). nflverse has no
         row for a player with no opportunity (no carry, target or pass attempt); when his team's
         game IS covered by nflverse, that is a measured zero (t_opp = 0, source "zero_opportunity",
         the same rule load_panel applies to red-zone counts). Any other absence - opportunities but
         no expected counts, or a team-week nflverse does not cover - stays null (never zero).
  t_eff  label - t_opp: actual league points minus that expectation (null when t_opp is null). It
         also absorbs scoring that has no expected counterpart (fumbles lost, return and fumble-
         recovery touchdowns; see scoring_compatibility).

Forecast = opportunity model + efficiency model, each a weighted ridge (challenger.fit_linear,
squared loss, so each is a conditional mean and their sum is the conditional mean of the label)
trained on strictly earlier weeks whose target component is known.

Why this is not a re-run of the rejected opportunity-first variant: ifp-2's O_XHPE kept ONE target
(points) and only dropped raw-production features. Here the target itself is split. Ridge is linear
in the target, so with identical features, penalty, weights and training rows the two components
sum EXACTLY to the direct model (tested): a decomposition can only differ through (a) its training
population (rows without target xFP cannot train either component), (b) a different penalty per
component, or (c) different features per component. Candidates C, D and E isolate those three.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json

import numpy as np
import polars as pl

from .challenger import ChallengerConfig, cohorts as ch_cohorts, fit_linear, train_rows
from .forecast_backtest import _arr, metrics, pair_decisions, week_bootstrap_mae_diff
from .forecast_features import EXPECTED_COMPONENTS, POSITIONS, key
from .independent_projections import LAMBDA_GRID, MIN_TRAIN_ROWS, RidgeModel

OE_VERSION = "ifp-oe-0.1.0"
OE_SCHEMA = "opportunity-efficiency-research/v1"
OE_STATUS = "research_only"
TARGETS = {"opp": "t_opp", "eff": "t_eff"}
EFF_GRID = (0.3, 1.0, 3.0, 10.0, 30.0)          # "stronger" efficiency penalties (candidates D, E)

# Candidate E: usage-only opportunity features (raw production removed) and a small efficiency set
# (the player's shrunk efficiency history, volume, and context that can change efficiency).
PRODUCTION = ("pts_recent", "pts_l3", "pts_season", "pts_prev_season", "pts_std", "pts_minus_xfp", "pts_median")
EFFICIENCY_FEATURES = {
    "QB": ["yd_over_exp", "td_over_exp", "xfpl_recent", "xfpl_missing", "games_recent", "opp_pos_allowed",
           "opp_pass_epa", "opp_rush_epa", "team_pts_pg", "is_home"],
    "RB": ["yd_over_exp", "td_over_exp", "xfpl_recent", "xfpl_missing", "games_recent", "opp_pos_allowed",
           "opp_rush_epa", "team_pts_pg", "is_home"],
    "WR": ["yd_over_exp", "td_over_exp", "xfpl_recent", "xfpl_missing", "games_recent", "opp_pos_allowed",
           "opp_pass_epa", "team_pts_pg", "is_home"],
    "TE": ["yd_over_exp", "td_over_exp", "xfpl_recent", "xfpl_missing", "games_recent", "opp_pos_allowed",
           "opp_pass_epa", "team_pts_pg", "is_home"],
}


@dataclass(frozen=True)
class Candidate:
    name: str
    features: str            # "matching" (ifp-2's features for both components) | "restrained"
    shared_lambda: bool      # True: one penalty for both components (C)
    lam_grid_opp: tuple = LAMBDA_GRID
    lam_grid_eff: tuple = LAMBDA_GRID


# Pre-registered before any development result.
CANDIDATES = (
    Candidate("OE_C", "matching", True),
    Candidate("OE_D", "matching", False, LAMBDA_GRID, EFF_GRID),
    Candidate("OE_E", "restrained", False, LAMBDA_GRID, EFF_GRID),
)
DEVELOPMENT, DIAGNOSTIC = (2021, 2022), ((2023, 2024), (2025, 2026))

OE_DECISION_RULES = {
    "selection": "development 2021-2022 only: per candidate and position the penalty (pair) with the lowest MAE "
                 "of the summed forecast; then the candidate with the lowest pooled development MAE",
    "continue_if": "development: selected - ifp2 pooled MAE week-clustered 95% CI upper bound < 0, OR mean Start/Sit "
                   "regret lower than ifp2 by >= 1% with MAE not worse; AND on the already-inspected 2023-2026 "
                   "replay none of: pooled MAE worse, RMSE worse by > 0.5%, |bias| worse by > 0.15, pair accuracy "
                   "lower by > 0.5 pp, coverage lower by > 0.5 pp",
    "otherwise": "negative result: documented, no artifact",
    "never": "no promotion, no 'Ours' selector, no weekly-pipeline integration, no change to ifp-1.0.0 / ifp-2.0.0",
}


# ----------------------------------------------------------------------------- targets


def scoring_compatibility(weights: dict[str, float]) -> dict:
    """Which scoring items have an nflverse expected-count counterpart (enter t_opp) and which do
    not (their points land in t_eff)."""
    scored = {k: v for k, v in weights.items() if v}
    covered = {k: v for k, v in scored.items() if k in EXPECTED_COMPONENTS}
    return {"covered": covered, "uncovered": {k: v for k, v in scored.items() if k not in EXPECTED_COMPONENTS},
            "expected_columns": {k: EXPECTED_COMPONENTS[k] for k in covered}}


def attach_targets(store: pl.DataFrame, panel: pl.DataFrame) -> pl.DataFrame:
    """Add the outcome-side targets t_opp, its source and t_eff = label - t_opp. Expected points
    that are unknown stay null in both targets; only a zero-opportunity game in an nflverse-covered
    team-week is a measured zero."""
    keys = ["season", "week", "gsis_id"]
    covered = (panel.filter(pl.col("xfpl").is_not_null()).select("season", "week", "team").unique()
               .with_columns(pl.lit(True).alias("_team_cov")))
    opps = pl.col("carries").fill_null(0) + pl.col("targets").fill_null(0) + pl.col("pass_attempts").fill_null(0)
    tgt = (panel.join(covered, on=["season", "week", "team"], how="left")
           .select(*keys, pl.col("xfpl"), (opps == 0).alias("_no_opp"), pl.col("_team_cov").fill_null(False))
           .unique(keys, keep="first")
           .with_columns(pl.when(pl.col("xfpl").is_not_null()).then(pl.lit("nflverse"))
                         .when(pl.col("_no_opp") & pl.col("_team_cov")).then(pl.lit("zero_opportunity"))
                         .otherwise(None).alias("t_opp_source"))
           .with_columns(pl.when(pl.col("t_opp_source") == "nflverse").then(pl.col("xfpl"))
                         .when(pl.col("t_opp_source") == "zero_opportunity").then(0.0)
                         .otherwise(None).alias("t_opp"))
           .select(*keys, "t_opp", "t_opp_source"))
    out = store.join(tgt, on=keys, how="left")
    return out.with_columns(pl.when(pl.col("t_opp").is_not_null() & pl.col("label").is_not_null())
                            .then(pl.col("label") - pl.col("t_opp")).otherwise(None).alias("t_eff"))


def coverage(store: pl.DataFrame) -> list[dict]:
    """Target-xFP coverage by season and position (labeled rows)."""
    return (store.filter(pl.col("label").is_not_null()).group_by("season", "position")
            .agg(pl.len().alias("rows"), pl.col("t_opp").is_not_null().sum().alias("with_target_xfp"),
                 (pl.col("t_opp_source") == "nflverse").sum().alias("nflverse_xfp"),
                 (pl.col("t_opp_source") == "zero_opportunity").sum().alias("zero_opportunity"),
                 pl.col("t_opp").is_null().mean().round(4).alias("missing_rate"),
                 pl.col("label").filter(pl.col("t_opp").is_null()).mean().round(3).alias("mean_points_when_missing"),
                 pl.col("label").filter(pl.col("t_opp").is_not_null()).mean().round(3).alias("mean_points_when_present"))
            .sort("season", "position").to_dicts())


# ----------------------------------------------------------------------------- features


def component_features(cand: Candidate, position: str, ifp2: ChallengerConfig) -> dict[str, list[str]]:
    base = ifp2.features(position)
    if cand.features == "matching":
        return {"opp": list(base), "eff": list(base)}
    return {"opp": [f for f in base if f not in PRODUCTION], "eff": list(EFFICIENCY_FEATURES[position])}


def half_life(position: str, ifp2: ChallengerConfig) -> float | None:
    return ifp2.positions[position].get("half_life_weeks")


def component_train_rows(store: pl.DataFrame, k: int, position: str, component: str) -> pl.DataFrame:
    """Training rows of one component: strictly earlier weeks, labeled, with a known target."""
    rows = train_rows(store, k, position)
    return rows.filter(pl.col(TARGETS[component]).is_not_null())


# ----------------------------------------------------------------------------- walk-forward


def raw_predict(model: RidgeModel, rows: pl.DataFrame) -> np.ndarray:
    """Unfloored ridge prediction. RidgeModel.predict floors at 0, which is right for points but
    would clip every negative efficiency forecast; components stay unfloored and only the summed
    forecast is floored at 0, exactly as the direct model floors its own prediction."""
    return model.contributions(rows).sum(axis=1) + model.intercept


def walk_forward(store: pl.DataFrame, weeks: list[tuple[int, int]], tag: str, features: dict[str, list[str]],
                 component: str, lams: dict[str, tuple], half_lives: dict[str, float | None]) -> pl.DataFrame:
    """One component, one feature set: columns pred_<tag>_<component>@<lam>. Null (abstention) when
    the component has fewer than MIN_TRAIN_ROWS training rows. Training rows strictly earlier
    (asserted)."""
    out = []
    for season, week in weeks:
        k = key(season, week)
        for pos in POSITIONS:
            rows = store.filter((pl.col("key") == k) & (pl.col("position") == pos))
            if rows.height == 0:
                continue
            train = component_train_rows(store, k, pos, component)
            assert train.height == 0 or train["key"].max() < k, "training row at or after the test week"
            cols = {}
            for lam in lams[pos]:
                col = f"pred_{tag}_{component}@{lam:g}"
                if train.height < MIN_TRAIN_ROWS:
                    cols[col] = np.full(rows.height, np.nan)
                    continue
                m = fit_linear(train, pos, features[pos], lam, half_life_weeks=half_lives[pos], target_key=k,
                               label=TARGETS[component], name=f"{tag}_{component}")
                cols[col] = np.where(rows["games_recent"].to_numpy() > 0, raw_predict(m, rows), np.nan)
            out.append(rows.select("key", "gsis_id").with_columns(
                [pl.Series(c, v).fill_nan(None) for c, v in cols.items()]))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


def combine(df: pl.DataFrame, tag: str, lam_opp: float, lam_eff: float) -> pl.Expr:
    """Summed forecast floored at 0 (as the direct model); null (explicit abstention) when either
    component is missing - never a fallback to the other component or to another model."""
    o, e = pl.col(f"pred_{tag}_opp@{lam_opp:g}"), pl.col(f"pred_{tag}_eff@{lam_eff:g}")
    return pl.when(o.is_not_null() & e.is_not_null()).then((o + e).clip(lower_bound=0.0)).otherwise(None)


def lambda_pairs(cand: Candidate) -> list[tuple[float, float]]:
    if cand.shared_lambda:
        return [(lam, lam) for lam in cand.lam_grid_opp]
    return [(lo, le) for lo in cand.lam_grid_opp for le in cand.lam_grid_eff if le >= lo]


def tag_of(cand: Candidate) -> str:
    return "M" if cand.features == "matching" else "R"


# ----------------------------------------------------------------------------- evaluation


def cohorts(df: pl.DataFrame) -> dict[str, pl.Expr]:
    base = ch_cohorts(df)
    out = {k: v for k, v in base.items() if k != "high_projection_ge15"}
    out.update({
        "rb_starters_pool": (pl.col("position") == "RB") & base["starters_pool"],
        "injury_return": (pl.col("last_injury_exit") == 1) | ((pl.col("inj_exits_recent") >= 1)
                                                                & (pl.col("weeks_since_last") >= 2)),
        "role_change": (pl.col("last_role_low") == 1) | (pl.col("team_changed") == 1) | (pl.col("prev_team_changed") == 1),
        "early_season_wk1_4": pl.col("week") <= 4,
        "target_xfp_missing": pl.col("t_opp").is_null(),
    })
    return out


def compare(preds: pl.DataFrame, models: dict[str, str], baseline: str = "ifp2", seed: int = 20261015,
            resamples: int = 1000, exact: bool = False) -> dict:
    """Coverage of every model on the full labeled population, then identical-row metrics (every
    model non-null): per position and pooled, week-clustered CIs vs baseline, cohorts, pairs."""
    lab = preds.filter(pl.col("label").is_not_null())
    cov = {n: (lambda v: v if exact else round(v, 4))(float(lab[c].is_not_null().mean())) if lab.height else None
           for n, c in models.items()}
    p = lab.with_columns([pl.col(c).alias(f"pred_{n}") for n, c in models.items() if c != f"pred_{n}"])
    cols = [f"pred_{n}" for n in models]
    p = p.filter(pl.all_horizontal([pl.col(c).is_not_null() for c in cols]))
    out = {"population": {"labeled_rows": lab.height, "common_rows": p.height, "coverage": cov}}
    flt = cohorts(p)
    for pos in [*POSITIONS, "ALL"]:
        g = p if pos == "ALL" else p.filter(pl.col("position") == pos)
        a = _arr(g, "label")
        rep = {"n": g.height, "metrics": {n: metrics(a, _arr(g, f"pred_{n}"), exact) for n in models},
               "minus_baseline_mae": {n: week_bootstrap_mae_diff(g, f"pred_{n}", f"pred_{baseline}", resamples, seed, exact)
                                      for n in models if n != baseline}}
        if pos == "ALL":
            rep["cohorts"] = {}
            for cname, expr in flt.items():
                c = g.filter(expr.fill_null(False))
                ac = _arr(c, "label")
                rep["cohorts"][cname] = {"n": c.height, **{n: metrics(ac, _arr(c, f"pred_{n}"), exact) for n in models}}
                if c.height >= 30:
                    rep["cohorts"][cname]["minus_baseline_mae"] = {
                        n: week_bootstrap_mae_diff(c, f"pred_{n}", f"pred_{baseline}", resamples, seed, exact)
                        for n in models if n != baseline}
        out[pos] = rep
    out["pairs"] = pair_decisions(p, cols, exact=exact)
    return out


def _naive_opportunity(d: pl.DataFrame, opp: str) -> dict:
    """Naive opportunity forecasts (the established and recent xFP features) against the model, all on
    the rows where both naive features exist (players without covered xFP history have none)."""
    s = d.filter(pl.col("xfpl_eb").is_not_null() & pl.col("xfpl_recent").is_not_null())
    t = s["t_opp"].to_numpy()
    return {"naive_n": s.height, "model_mae_naive_rows": metrics(t, _arr(s, opp))["mae"],
            "naive_xfpl_eb_mae": metrics(t, _arr(s, "xfpl_eb"))["mae"],
            "naive_xfpl_recent_mae": metrics(t, _arr(s, "xfpl_recent"))["mae"]}


def component_validity(df: pl.DataFrame, opp: str, eff: str, direct: str) -> dict:
    """On rows with a known target xFP: component errors, bias, correlation of the component errors
    and whether summing them amplifies error. `direct` is the direct model's points column."""
    d = df.filter(pl.col("t_opp").is_not_null() & pl.col(opp).is_not_null() & pl.col(eff).is_not_null()
                  & pl.col(direct).is_not_null())
    if d.height < 2:
        return {"n": d.height}
    eo = d["t_opp"].to_numpy() - d[opp].to_numpy()
    ee = d["t_eff"].to_numpy() - d[eff].to_numpy()
    tot = eo + ee
    teff = d["t_eff"].to_numpy()

    def r2(err, y):
        v = float(np.var(y))
        return round(1 - float(np.mean(err ** 2)) / v, 4) if v > 0 else None

    return {
        "n": d.height,
        "opportunity": {"mae": round(float(np.abs(eo).mean()), 3), "bias": round(float(eo.mean()), 3),
                        "r2": r2(eo, d["t_opp"].to_numpy()),
                        **_naive_opportunity(d, opp)},
        "efficiency": {"mae": round(float(np.abs(ee).mean()), 3), "bias": round(float(ee.mean()), 3),
                       "r2": r2(ee, teff), "zero_mae": round(float(np.abs(teff).mean()), 3),
                       "sd_target": round(float(np.std(teff)), 3), "sd_prediction": round(float(np.std(d[eff].to_numpy())), 3)},
        "error_correlation": round(float(np.corrcoef(eo, ee)[0, 1]), 4),
        "amplification": round(float(np.var(tot) / (np.var(eo) + np.var(ee))), 4),
        "summed_mae": round(float(np.abs(tot).mean()), 3),
        "direct_mae_same_rows": metrics(d["label"].to_numpy(), _arr(d, direct))["mae"],
    }


def decision(dev: dict, diag: dict, cand: str, base: str = "ifp2") -> dict:
    d = dev["ALL"]["minus_baseline_mae"][cand]
    md, mb = dev["ALL"]["metrics"][cand], dev["ALL"]["metrics"][base]
    rd = dev["pairs"]["all"][f"pred_{cand}"]["mean_regret"]
    rb = dev["pairs"]["all"][f"pred_{base}"]["mean_regret"]
    dev_gain = d["ci95"] is not None and d["ci95"][1] < 0
    decision_gain = bool(rd <= rb * 0.99 and md["mae"] <= mb["mae"])
    a = diag["ALL"]
    m, b = a["metrics"][cand], a["metrics"][base]
    cov = diag["population"]["coverage"]
    rejects = {
        "pooled_mae_worse": a["minus_baseline_mae"][cand]["diff"] > 0,
        "rmse_worse": m["rmse"] > b["rmse"] * 1.005,
        "bias_worse": abs(m["bias"]) > abs(b["bias"]) + 0.15,
        "pairs_worse": diag["pairs"]["all"][f"pred_{cand}"]["accuracy"]
                       < diag["pairs"]["all"][f"pred_{base}"]["accuracy"] - 0.005,
        "coverage_lower": cov[cand] < cov[base] - 0.005,
    }
    ok = (dev_gain or decision_gain) and not any(rejects.values())
    return {"candidate": cand, "development_gain": dev_gain, "decision_gain": decision_gain,
            "development_diff": d, "development_regret": {"candidate": rd, "baseline": rb},
            "rejections": rejects, "outcome": "continue_research" if ok else "negative"}


# ----------------------------------------------------------------------------- research artifact


@dataclass
class ResearchArtifact:
    """Research-only two-component model. Its schema differs from the production artifact
    (independent_projections.ARTIFACT_SCHEMA), so the production loader refuses it, and
    `from_dict` refuses anything that is not research-only."""
    version: str
    created_at: str
    scoring_fingerprint: str
    training_cutoff_key: int                     # trained on weeks strictly before this (season*100+week)
    candidate: dict
    lam: dict[str, dict[str, float]]             # position -> {"opp": lam, "eff": lam}
    features: dict[str, dict[str, list[str]]]    # position -> component -> features
    models: dict[str, dict[str, dict]]           # position -> component -> RidgeModel dict
    targets: dict = field(default_factory=lambda: {
        "t_opp": "realized expected league points of the game (league weights x nflverse expected counts)",
        "t_eff": "league points - t_opp; null when t_opp is null"})
    coverage: list = field(default_factory=list)
    backtest: dict = field(default_factory=dict)
    status: str = OE_STATUS
    schema: str = OE_SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ResearchArtifact":
        if d.get("schema") != OE_SCHEMA:
            raise ValueError(f"not an opportunity-efficiency research artifact: {d.get('schema')!r}")
        if d.get("status") != OE_STATUS:
            raise ValueError(f"research artifact with status {d.get('status')!r} is not loadable")
        return ResearchArtifact(**d)

    def predict(self, rows: pl.DataFrame) -> np.ndarray:
        """Summed forecast per row; NaN (abstention) when the position has no fitted component."""
        out = np.full(rows.height, np.nan)
        for pos, comps in self.models.items():
            idx = np.flatnonzero((rows["position"] == pos).to_numpy())
            if len(idx) == 0 or set(comps) != {"opp", "eff"}:
                continue
            sub = rows[idx]
            out[idx] = np.maximum(raw_predict(RidgeModel.from_dict(comps["opp"]), sub)
                                  + raw_predict(RidgeModel.from_dict(comps["eff"]), sub), 0.0)
        if "games_recent" in rows.columns:    # the backtest's abstention rule: no recent game, no forecast
            out[~(rows["games_recent"].fill_null(0).to_numpy() > 0)] = np.nan
        return out

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, default=str).encode()).hexdigest()[:16]


def fit_artifact(store: pl.DataFrame, cutoff_key: int, cand: Candidate, lams: dict[str, dict[str, float]],
                 ifp2: ChallengerConfig, created_at: str, scoring_fingerprint: str, cov: list | None = None,
                 backtest: dict | None = None) -> ResearchArtifact:
    models, feats = {}, {}
    for pos in POSITIONS:
        f = component_features(cand, pos, ifp2)
        feats[pos], models[pos] = f, {}
        for comp in ("opp", "eff"):
            train = component_train_rows(store, cutoff_key, pos, comp)
            if train.height < MIN_TRAIN_ROWS:
                continue
            models[pos][comp] = fit_linear(train, pos, f[comp], lams[pos][comp], half_life_weeks=half_life(pos, ifp2),
                                           target_key=cutoff_key, label=TARGETS[comp], name=f"oe_{comp}").to_dict()
    return ResearchArtifact(OE_VERSION, created_at, scoring_fingerprint, cutoff_key, asdict(cand), lams, feats, models,
                            coverage=cov or [], backtest=backtest or {})


__all__ = ["OE_VERSION", "CANDIDATES", "OE_DECISION_RULES", "attach_targets", "coverage", "scoring_compatibility",
           "walk_forward", "combine", "compare", "component_validity", "decision", "ResearchArtifact", "fit_artifact"]
