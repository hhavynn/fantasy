"""RB projection diagnostics and pre-registered RB-specific candidates (research only, shadow).

Nothing here is read by lineups, Start/Sit, trades, waivers or any consumer, and nothing here is
promoted: it never touches MODEL_VERSION, the production artifacts or the weekly pipeline. The
production champion (ifp-1.0.0 context ridge "C") and the ifp-2 challenger are only re-fitted
walk-forward, exactly as their own protocols do, to be compared with.

Protocol (docs/research/rb_projection_diagnostics.md):
  * development window 2021-2022: all diagnostics and every hyperparameter choice;
  * confirmation window 2023-2026 weeks 1-5: candidates frozen on development are run ONCE;
  * every model is scored on the identical test player-weeks (common support);
  * chronological walk-forward only: a test week's models see strictly earlier weeks;
  * ESPN projections are never an input;
  * caches are keyed by a fingerprint of every input frame and the full configuration.

Columns whose names start with ``o_`` describe the TARGET game (its box score). They exist only to
define outcome-based diagnostic segments and are never features (tested).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

from . import challenger as ch
from .forecast_backtest import (PAIR_POOL, _arr, metrics, pair_decisions, starters_pool_expr)
from .forecast_features import Sources, key
from .independent_projections import CONTEXT, MIN_TRAIN_ROWS, MODEL_VERSION

POSITION = "RB"
DEVELOPMENT = (2021, 2022)
CONFIRMATION = (2023, 2024, 2025, 2026)
TRAIN_START = ch.TRAIN_START
SEED = 20261016
RESAMPLES = 1000
BASELINES = ("A1", "A2")
REFERENCE_MODELS = ("A1", "A2", "C", "ifp2")
# ifp-1.0.0 frozen selection for RB (validation 2023-24, forecast_backtest.select): lambda of model C.
# Reproduced from the data by `scripts/rb_research.py prepare` and asserted against this constant.
C_LAMBDA_RB = 0.03

OUTCOME_COLUMNS = {   # target-game attributes (outcome side): panel column -> o_ column
    "td_points": "o_td_points", "xfpl": "o_xfpl", "xfpl_td": "o_xfpl_td", "carries": "o_carries",
    "targets": "o_targets", "offense_pct": "o_snap_pct", "gl_carries": "o_gl_carries",
}


# ----------------------------------------------------------------------------- fingerprints and caching


def frame_hash(df: pl.DataFrame | None) -> str:
    """Content hash of a frame (column names, dtypes, every row), independent of row order."""
    if df is None:
        return "none"
    h = hashlib.sha256(json.dumps([(c, str(t)) for c, t in df.schema.items()]).encode())
    if df.height:
        cols = sorted(df.columns)
        rows = df.select(cols).with_columns(pl.all().cast(pl.String)).hash_rows(seed=0)
        h.update(rows.sort().to_numpy().tobytes())
    return h.hexdigest()[:20]


def fingerprint(frames: dict[str, pl.DataFrame | None], config: dict) -> str:
    """Complete source fingerprint: a hash of every input frame plus the configuration. A cache
    entry is valid only for this exact value; player-week ids matching is never enough."""
    parts = {name: frame_hash(df) for name, df in sorted(frames.items())}
    blob = json.dumps({"frames": parts, "config": config}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def sources_frames(src: Sources) -> dict[str, pl.DataFrame | None]:
    return {"panel": src.panel, "games": src.games, "defense": src.defense, "rosters": src.rosters,
            "injuries": src.injuries, "exits": src.exits}


def cache_load(path: Path, expected: str) -> pl.DataFrame | None:
    """Parquet written by cache_save, or None unless its recorded fingerprint equals `expected`."""
    meta, data = Path(str(path) + ".meta.json"), Path(path)
    if not (meta.exists() and data.exists()):
        return None
    try:
        if json.loads(meta.read_text()).get("fingerprint") != expected:
            return None
        return pl.read_parquet(data)
    except Exception:
        return None


def cache_save(path: Path, df: pl.DataFrame, fp: str, config: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    Path(str(path) + ".meta.json").write_text(json.dumps({"fingerprint": fp, "config": config}, sort_keys=True, default=str))


# ----------------------------------------------------------------------------- scoring and sources

# The league's ESPN scoring items (full PPR, 4-point passing TD, -2 INT / fumble lost, 2-point conversions,
# return TDs). Same items as tests/synthetic_forecast.SCORING_ITEMS; the resolved fingerprint equals the
# production fingerprint recorded in model_registry.json, which `prepare` asserts. No live league database
# is needed (and none exists in the research container).
SCORING_ITEMS = [
    {"statId": 3, "points": 0.04, "pointsOverrides": {}}, {"statId": 4, "points": 4.0, "pointsOverrides": {}},
    {"statId": 19, "points": 2.0, "pointsOverrides": {}}, {"statId": 20, "points": -2.0, "pointsOverrides": {}},
    {"statId": 24, "points": 0.1, "pointsOverrides": {}}, {"statId": 25, "points": 6.0, "pointsOverrides": {}},
    {"statId": 26, "points": 2.0, "pointsOverrides": {}}, {"statId": 42, "points": 0.1, "pointsOverrides": {}},
    {"statId": 43, "points": 6.0, "pointsOverrides": {}}, {"statId": 44, "points": 2.0, "pointsOverrides": {}},
    {"statId": 53, "points": 1.0, "pointsOverrides": {}}, {"statId": 63, "points": 6.0, "pointsOverrides": {}},
    {"statId": 72, "points": -2.0, "pointsOverrides": {}}, {"statId": 101, "points": 6.0, "pointsOverrides": {"16": 6.0}},
    {"statId": 102, "points": 6.0, "pointsOverrides": {"16": 6.0}}, {"statId": 80, "points": 3.0, "pointsOverrides": {}},
    {"statId": 86, "points": 1.0, "pointsOverrides": {}}, {"statId": 89, "points": 0.0, "pointsOverrides": {"16": 5.0}},
    {"statId": 103, "points": 6.0, "pointsOverrides": {"16": 6.0}},
]
PRODUCTION_SCORING_FINGERPRINT = "e165b59686110c7d"


def league_scoring_for_research():
    from .league_scoring import resolve_skill_scoring

    scoring = resolve_skill_scoring(SCORING_ITEMS).require_supported()
    if scoring.fingerprint != PRODUCTION_SCORING_FINGERPRINT:
        raise ValueError(f"research scoring {scoring.fingerprint} differs from production {PRODUCTION_SCORING_FINGERPRINT}")
    return scoring


def code_fingerprint(include_self: bool = False) -> str:
    """Hash of the source files that define features, scoring and the compared models: a change to any
    of them invalidates every cache. `include_self` adds this module (store cache only; the frozen
    configuration binds to the data and the production code, not to this file)."""
    here = Path(__file__).parent
    h = hashlib.sha256()
    names = ["forecast_features.py", "independent_projections.py", "league_scoring.py", "challenger.py",
             "challenger_config.json", "forecast_backtest.py"] + (["rb_research.py"] if include_self else [])
    for name in names:
        h.update(name.encode())
        h.update((here / name).read_bytes())
    return h.hexdigest()[:20]


def data_fingerprint(src: Sources) -> str:
    """Fingerprint of every input frame plus the production code and scoring that turn them into features."""
    return fingerprint(sources_frames(src), {"code": code_fingerprint(), "scoring": PRODUCTION_SCORING_FINGERPRINT,
                                             "train_start": TRAIN_START})


def load_all_sources(history_db: Path, live_db: Path | None, exits_dir: Path | None) -> Sources:
    """History seasons from the history DB (read-only), 2026 weeks 1-5 from a separate nflverse-only
    database if given, plus play-by-play injury evidence."""
    import duckdb

    from ..nfl.pbp_exits import load_dir
    from .forecast_features import load_sources

    scoring = league_scoring_for_research()
    parts = []
    for path in (history_db, live_db):
        if path is not None and Path(path).exists():
            con = duckdb.connect(str(path), read_only=True)
            try:
                parts.append(load_sources(con, scoring))
            finally:
                con.close()
    if not parts:
        raise FileNotFoundError(f"no nflverse database at {history_db}")
    src = Sources.concat(parts)
    return src.with_exits(load_dir(exits_dir)) if exits_dir is not None else src


# ----------------------------------------------------------------------------- store


def build_research_store(src: Sources) -> pl.DataFrame:
    """Walk-forward feature store 2020..latest (features from strictly earlier weeks), plus the player's
    last game, outcome-side ``o_*`` columns and, for RBs, the teammate-unavailability features."""
    from .forecast_backtest import build_store, completed_weeks

    seasons = sorted(int(s) for s in src.panel["season"].unique().to_list() if int(s) >= TRAIN_START)
    store = ch.add_last_game(build_store(src, completed_weeks(src, seasons)), src.panel)
    store = attach_outcomes(store, src.panel)
    return attach_teammate_features(store, src)


def cached_store(src: Sources, cache_dir: Path) -> tuple[pl.DataFrame, str]:
    """RB research store, rebuilt unless a cache with the identical complete fingerprint exists."""
    config = {"code": code_fingerprint(include_self=True), "scoring": PRODUCTION_SCORING_FINGERPRINT,
              "train_start": TRAIN_START, "teammate": [TEAMMATE_WINDOW_WEEKS, list(UNAVAILABLE_STATUSES)]}
    fp = fingerprint(sources_frames(src), config)
    path = Path(cache_dir) / f"store-{fp}.parquet"
    hit = cache_load(path, fp)
    if hit is not None:
        return hit, fp
    store = build_research_store(src)
    cache_save(path, store, fp, config)
    return store, fp


def attach_outcomes(store: pl.DataFrame, panel: pl.DataFrame) -> pl.DataFrame:
    """Add the target game's box-score attributes as ``o_*`` columns (outcome side, diagnostics only)."""
    o = panel.select("season", "week", "gsis_id",
                     *[pl.col(src).alias(dst) for src, dst in OUTCOME_COLUMNS.items()])
    return store.join(o, on=["season", "week", "gsis_id"], how="left")


def rb_store(store: pl.DataFrame) -> pl.DataFrame:
    return store.filter(pl.col("position") == POSITION)


def weeks_in(store: pl.DataFrame, seasons) -> list[tuple[int, int]]:
    return ch.window_weeks(store, seasons)


# ----------------------------------------------------------------------------- reference models


def verify_c_lambda(store: pl.DataFrame) -> dict:
    """Re-run ifp-1.0.0's own validation selection (2023-24) on this store and require the RB result the
    research assumes: primary model C at C_LAMBDA_RB. Fails loudly if the data or code drifted."""
    from .forecast_backtest import Protocol, select

    sel = select(store, Protocol())["positions"][POSITION]
    if sel["primary"] != "C" or sel["lambda"]["C"] != C_LAMBDA_RB:
        raise ValueError(f"ifp-1.0.0 RB selection is now {sel['primary']}@{sel['lambda']}; expected C@{C_LAMBDA_RB}")
    return {"primary": sel["primary"], "lambda": sel["lambda"], "validation_mae": sel["validation_mae"]}


def reference_predictions(store: pl.DataFrame, weeks: list[tuple[int, int]], c_lambda: float = C_LAMBDA_RB) -> pl.DataFrame:
    """RB rows of `weeks` with pred_A1, pred_A2 (from the store), pred_C (ifp-1.0.0 context ridge
    at its frozen penalty) and pred_ifp2 (frozen ifp-2.0.0 RB spec), all walk-forward."""
    st = rb_store(store)
    cfg = ch.ChallengerConfig.load()
    spec_c = ch.Spec("C", lam={POSITION: c_lambda})
    spec_2 = cfg.spec(POSITION)
    preds = ch.walk_forward(st, weeks, [spec_c, spec_2])
    return preds.rename({"pred_C": "pred_C", f"pred_{spec_2.name}": "pred_ifp2"})


# ----------------------------------------------------------------------------- segments


def rb_segments() -> dict[str, pl.Expr]:
    """Pregame RB segments (every input is a feature available before kickoff). Thresholds are
    fixed in advance of any candidate result."""
    c, t = pl.col("carries_pg"), pl.col("targets_pg")
    return {
        # rushing volume
        "rush_volume: carries/g < 5": c < 5,
        "rush_volume: carries/g 5-10": (c >= 5) & (c < 10),
        "rush_volume: carries/g 10-15": (c >= 10) & (c < 15),
        "rush_volume: carries/g >= 15": c >= 15,
        # target volume
        "target_volume: targets/g < 1.5": t < 1.5,
        "target_volume: targets/g 1.5-3.5": (t >= 1.5) & (t < 3.5),
        "target_volume: targets/g >= 3.5": t >= 3.5,
        # team rush share
        "team_rush_share < 0.15": pl.col("rush_share") < 0.15,
        "team_rush_share 0.15-0.35": (pl.col("rush_share") >= 0.15) & (pl.col("rush_share") < 0.35),
        "team_rush_share >= 0.35": pl.col("rush_share") >= 0.35,
        # healthy snap share
        "snap_share < 0.30": pl.col("snap_pct") < 0.30,
        "snap_share 0.30-0.60": (pl.col("snap_pct") >= 0.30) & (pl.col("snap_pct") < 0.60),
        "snap_share >= 0.60": pl.col("snap_pct") >= 0.60,
        # goal-line / red-zone opportunity
        "goal_line: no inside-5 carries": pl.col("gl_carries_pg").fill_null(0) == 0,
        "goal_line: inside-5 carries/g > 0": pl.col("gl_carries_pg").fill_null(0) > 0,
        "red_zone: opps/g >= 3": pl.col("rz_opps_pg").fill_null(0) >= 3,
        # back type
        "receiving_back (targets/g >= 3, carries/g < 10)": (t >= 3) & (c < 10),
        "early_down_back (carries/g >= 10, targets/g < 3)": (c >= 10) & (t < 3),
        # role and team changes
        "role_change: last game a role drop": pl.col("last_role_low") == 1,
        "role_change: snap trend <= -0.15": pl.col("snap_trend") <= -0.15,
        "role_change: snap trend >= +0.15": pl.col("snap_trend") >= 0.15,
        "team_change (this or last season)": (pl.col("team_changed") == 1) | (pl.col("prev_team_changed") == 1),
        # injury-shortened games and returns
        "injury: last game an injury exit": pl.col("last_injury_exit") == 1,
        "injury: any exit in last 5": pl.col("inj_exits_recent") >= 1,
        "injury_return: >= 3 weeks since last game": pl.col("weeks_since_last") >= 3,
        # sample size
        "sample: < 3 recent games": pl.col("games_recent") < 3,
        "sample: early season (weeks 1-4)": pl.col("week") <= 4,
        "sample: mid season (weeks 5-12)": (pl.col("week") >= 5) & (pl.col("week") <= 12),
        "sample: late season (weeks 13+)": pl.col("week") >= 13,
        # touchdown dependence (pregame)
        "td_history: recent TDs above expectation (td_over_exp >= 0.5)": pl.col("td_over_exp") >= 0.5,
        "td_history: recent TDs below expectation (td_over_exp <= -0.5)": pl.col("td_over_exp") <= -0.5,
        # reference cohorts
        "starters_pool": starters_pool_expr(),
        "high_volume_back (carries/g + targets/g >= 15)": (c + t) >= 15,
    }


def outcome_segments() -> dict[str, pl.Expr]:
    """Segments defined by the TARGET game itself (outcome side). They describe where error comes
    from (touchdown luck, a game cut short); they can never be used by a forecast."""
    return {
        "outcome: scored a touchdown": pl.col("o_td_points").fill_null(0) > 0,
        "outcome: no touchdown": pl.col("o_td_points").fill_null(0) == 0,
        "outcome: <= 40% snap share (cut short or demoted)": pl.col("o_snap_pct") <= 0.40,
        "outcome: zero opportunities (no carry or target)": (pl.col("o_carries").fill_null(0) + pl.col("o_targets").fill_null(0)) == 0,
    }


def td_neutral_label() -> pl.Expr:
    """Realized points with the touchdown component replaced by its expectation (nflverse expected
    touchdown points): what the game would have scored without touchdown variance. Outcome side."""
    return pl.col("label") - pl.col("o_td_points").fill_null(0) + pl.col("o_xfpl_td").fill_null(0)


# ----------------------------------------------------------------------------- metrics


N_CANDIDATES = 3
ADJUSTED_ALPHA = 0.05 / N_CANDIDATES      # Bonferroni across the three pre-registered candidates


def boot_diff(df: pl.DataFrame, a: str, b: str, *, resamples: int = RESAMPLES, seed: int = SEED,
              label: str = "label") -> dict:
    """Week-clustered bootstrap of MAE(a) - MAE(b) on rows where both exist (unrounded). Same
    algorithm as forecast_backtest.week_bootstrap_mae_diff, but unrounded and with a settable label.
    `ci95` is the plain 95% interval; `ci_adj` is the Bonferroni interval for ADJUSTED_ALPHA (three
    candidates), computed from the same resamples."""
    d = df.filter(pl.col(a).is_not_null() & pl.col(b).is_not_null())
    if d.height == 0:
        return {"n": 0, "diff": None, "ci95": None, "ci_adj": None}
    d = d.with_columns((pl.col(label) - pl.col(a)).abs().alias("_ea"), (pl.col(label) - pl.col(b)).abs().alias("_eb"))
    by = d.group_by("key").agg(pl.col("_ea").sum(), pl.col("_eb").sum(), pl.len().alias("n")).sort("key")
    ea, eb, n = by["_ea"].to_numpy(), by["_eb"].to_numpy(), by["n"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(n), size=(resamples, len(n)))
    diffs = (ea[idx].sum(1) - eb[idx].sum(1)) / n[idx].sum(1)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    alo, ahi = np.percentile(diffs, [50 * ADJUSTED_ALPHA, 100 - 50 * ADJUSTED_ALPHA])
    return {"n": int(n.sum()), "diff": float((ea.sum() - eb.sum()) / n.sum()), "ci95": [float(lo), float(hi)],
            "ci_adj": [float(alo), float(ahi)]}


def common_support(preds: pl.DataFrame, models: list[str]) -> pl.DataFrame:
    """Rows on which every named model produced a forecast (identical test population)."""
    cols = [f"pred_{m}" for m in models]
    return preds.filter(pl.all_horizontal([pl.col(c).is_not_null() for c in cols]))


def segment_table(df: pl.DataFrame, models: list[str], segments: dict[str, pl.Expr], *, label: str = "label",
                  vs: tuple[tuple[str, str], ...] = (("C", "A2"),), min_n: int = 1) -> list[dict]:
    """Per segment: n, MAE and bias of each model and the bootstrapped MAE difference for each pair in `vs`."""
    rows = []
    for name, expr in segments.items():
        g = df.filter(expr.fill_null(False))
        if g.height < min_n:
            continue
        a = _arr(g, label)
        row = {"segment": name, "n": g.height,
               "mae": {m: _round(metrics(a, _arr(g, f"pred_{m}"), )["mae"]) for m in models},
               "bias": {m: _round(metrics(a, _arr(g, f"pred_{m}"))["bias"]) for m in models}}
        for x, y in vs:
            row[f"{x}-{y}"] = boot_diff(g, f"pred_{x}", f"pred_{y}", label=label)
        rows.append(row)
    return rows


def _round(x, nd: int = 3):
    return None if x is None else round(float(x), nd)


def pooled_metrics(df: pl.DataFrame, models: list[str], *, label: str = "label") -> dict:
    a = _arr(df, label)
    return {m: metrics(a, _arr(df, f"pred_{m}")) for m in models}


VOLUME_TIERS = ((0, 2), (2, 4), (4, 6), (6, 9), (9, 12), (12, 16), (16, 1000))


def volume_tier_table(df: pl.DataFrame, models: list[str]) -> list[dict]:
    """By pregame opportunities per game (carries + targets): calibration of each model (mean forecast
    vs mean outcome, slope of outcome on forecast), the share of appearances without a single carry or
    target, and C against the season average. Slope < 1 means the forecast spreads players more than
    their outcomes spread."""
    rows = []
    opp = opportunities_per_game()
    for lo, hi in VOLUME_TIERS:
        g = df.filter((opp >= lo) & (opp < hi))
        if g.height < 5:
            continue
        y = _arr(g, "label")
        row = {"opp_per_game": f"{lo}-{hi if hi < 1000 else '+'}", "n": g.height, "mean_outcome": _round(y.mean()),
               "median_outcome": _round(float(np.median(y))),
               "zero_opportunity_share": _round(float(((g["o_carries"].fill_null(0) + g["o_targets"].fill_null(0)) == 0).mean())),
               "mean_forecast": {}, "slope": {}, "mae": {}}
        for m in models:
            p = _arr(g, f"pred_{m}")
            row["mean_forecast"][m] = _round(float(np.nanmean(p)))
            row["slope"][m] = _round(float(np.polyfit(p, y, 1)[0])) if np.nanstd(p) > 0 else None
            row["mae"][m] = _round(float(np.abs(y - p).mean()))
        row["C-A2"] = boot_diff(g, "pred_C", "pred_A2")
        rows.append(row)
    return rows


def error_decomposition(df: pl.DataFrame, models: list[str]) -> dict:
    """How much of each model's absolute error is touchdown variance? MAE against realized points,
    against touchdown-neutralized points (outcome side), and split by whether the game had a touchdown."""
    d = df.with_columns(td_neutral_label().alias("label_td_neutral"))
    out = {}
    for m in models:
        col = f"pred_{m}"
        a, y0 = _arr(d, "label"), _arr(d, "label_td_neutral")
        p = _arr(d, col)
        e_real, e_neutral = np.abs(a - p), np.abs(y0 - p)
        td = (d["o_td_points"].fill_null(0) > 0).to_numpy()
        out[m] = {"mae_realized": _round(e_real.mean()), "mae_td_neutral_label": _round(e_neutral.mean()),
                  "td_share_of_mae": _round(1 - e_neutral.mean() / e_real.mean()),
                  "mae_td_games": _round(e_real[td].mean()) if td.any() else None,
                  "mae_no_td_games": _round(e_real[~td].mean()) if (~td).any() else None,
                  "bias_td_games": _round((a - p)[td].mean()) if td.any() else None,
                  "bias_no_td_games": _round((a - p)[~td].mean()) if (~td).any() else None}
    td = d["o_td_points"].fill_null(0).to_numpy()
    out["_target"] = {"share_games_with_td": _round((td > 0).mean()),
                      "var_label": _round(float(np.var(_arr(d, "label")))),
                      "var_td_points": _round(float(np.var(td))),
                      "share_of_label_variance_from_td_points": _round(float(np.var(td) / np.var(_arr(d, "label"))))}
    return out


# ----------------------------------------------------------------------------- teammate availability (pregame)

TEAMMATE_WINDOW_WEEKS = 6      # a teammate counts if his last game for the team was at most this many NFL weeks earlier
UNAVAILABLE_STATUSES = ("Out", "Doubtful")
TEAMMATE_COLUMNS = ["tm_out_carries_pg", "tm_out_targets_pg", "tm_out_xfpl_pg", "tm_out_n"]


def teammate_out_features(src: Sources, season: int, week: int, targets: pl.DataFrame) -> pl.DataFrame:
    """For each target RB, the recent usage (last <= 5 games) of OTHER running backs on his team who
    are designated Out or Doubtful on the week-W injury report. Point-in-time: usage comes from games
    strictly before (season, week); the designation is the week-W pregame report (rows modified at or
    after kickoff were dropped at load time where `date_modified` exists). Without any report rows the
    features are 0 (no teammate known to be unavailable), never filled from outcomes.

    `targets`: gsis_id, team. Returns gsis_id + TEAMMATE_COLUMNS."""
    empty = targets.select("gsis_id").with_columns(*[pl.lit(0.0).alias(c) for c in TEAMMATE_COLUMNS])
    inj = src.injuries
    if inj is None or inj.height == 0 or targets.height == 0:
        return empty
    cutoff = key(season, week)
    panel = src.panel.filter((pl.col("position") == POSITION) & (pl.col("season") * 100 + pl.col("week") < cutoff)
                             & (pl.col("season") >= season - 1))
    desc = panel.sort(["season", "week"], descending=True).group_by("gsis_id", maintain_order=True).head(5)
    usage = desc.group_by("gsis_id", maintain_order=True).agg(
        pl.col("team").first().alias("tm_team"), pl.col("season").first().alias("_ls"), pl.col("week").first().alias("_lw"),
        pl.col("carries").mean().alias("_c"), pl.col("targets").mean().alias("_t"),
        pl.col("xfpl").fill_null(0.0).mean().alias("_x"))
    usage = usage.with_columns(
        pl.when(pl.col("_ls") == season).then(week - pl.col("_lw"))
          .otherwise(week + 18 * (season - pl.col("_ls")) - pl.col("_lw")).alias("_age")
    ).filter(pl.col("_age") <= TEAMMATE_WINDOW_WEEKS)
    out_ids = inj.filter((pl.col("season") == season) & (pl.col("week") == week)
                         & pl.col("report_status").is_in(list(UNAVAILABLE_STATUSES))).select("gsis_id").unique()
    unavailable = usage.join(out_ids, on="gsis_id", how="inner")
    gone = unavailable.group_by("tm_team").agg(
        pl.col("_c").sum().alias("tm_out_carries_pg"), pl.col("_t").sum().alias("tm_out_targets_pg"),
        pl.col("_x").sum().alias("tm_out_xfpl_pg"), pl.len().cast(pl.Float64).alias("tm_out_n"))
    j = targets.select("gsis_id", "team").join(gone, left_on="team", right_on="tm_team", how="left")
    # the target himself is never his own unavailable teammate (he appears, so he is not Out; guard anyway)
    me = unavailable.select("gsis_id", pl.col("tm_team").alias("team"), pl.col("_c").alias("_mc"),
                            pl.col("_t").alias("_mt"), pl.col("_x").alias("_mx"))
    j = j.join(me, on=["gsis_id", "team"], how="left")
    return j.select(
        "gsis_id",
        (pl.col("tm_out_carries_pg").fill_null(0.0) - pl.col("_mc").fill_null(0.0)).alias("tm_out_carries_pg"),
        (pl.col("tm_out_targets_pg").fill_null(0.0) - pl.col("_mt").fill_null(0.0)).alias("tm_out_targets_pg"),
        (pl.col("tm_out_xfpl_pg").fill_null(0.0) - pl.col("_mx").fill_null(0.0)).alias("tm_out_xfpl_pg"),
        (pl.col("tm_out_n").fill_null(0.0) - pl.col("_mc").is_not_null().cast(pl.Float64)).alias("tm_out_n"))


def attach_teammate_features(store: pl.DataFrame, src: Sources) -> pl.DataFrame:
    """`store` plus TEAMMATE_COLUMNS (filled for RB rows, null for other positions), one week at a time:
    each week's features see only earlier games and that week's pregame report."""
    parts = []
    for (season, week), g in rb_store(store).group_by(["season", "week"], maintain_order=True):
        f = teammate_out_features(src, int(season), int(week), g.select("gsis_id", "team"))
        parts.append(f.with_columns(pl.lit(int(season)).alias("season"), pl.lit(int(week)).alias("week")))
    if not parts:
        return store.with_columns(*[pl.lit(None, dtype=pl.Float64).alias(c) for c in TEAMMATE_COLUMNS])
    tm = pl.concat(parts).with_columns(pl.col("season").cast(store.schema["season"]), pl.col("week").cast(store.schema["week"]))
    return store.join(tm, on=["season", "week", "gsis_id"], how="left")


# ----------------------------------------------------------------------------- pre-registered candidates

# Each candidate has ONE distinct hypothesis and a fixed small grid chosen on the development window only.
# See docs/research/rb_projection_diagnostics.md ("Preregistration"); the text between the PREREG markers
# there is hashed into the frozen configuration, and `confirm` refuses to run if the hash changes.
CANDIDATE_GRIDS: dict[str, dict[str, list[float]]] = {
    "vol_shrink": {"tau": [4.0, 8.0, 12.0]},            # opportunities/game at which C gets full weight
    "tm_transfer": {"lam": [0.01, 0.03, 0.1]},          # ridge penalty of C + teammate-unavailability features
    "hurdle": {"logit_lam": [0.001, 0.01, 0.1]},        # penalty of the P(touch) logistic
}
HURDLE_RIDGE_LAM = C_LAMBDA_RB                          # positive-part ridge uses C's frozen penalty
CANDIDATE_FEATURES = {
    "vol_shrink": ["carries_pg", "targets_pg"],
    "tm_transfer": list(CONTEXT[POSITION]) + TEAMMATE_COLUMNS[:3],
    "hurdle": list(CONTEXT[POSITION]),
}
for _name, _feats in CANDIDATE_FEATURES.items():     # no target-game quantity can be a feature
    assert not any(f.startswith("o_") or f == "label" for f in _feats), _name


def opportunities_per_game() -> pl.Expr:
    return pl.col("carries_pg") + pl.col("targets_pg")


def shrink_toward_season_average(c: np.ndarray, a2: np.ndarray, opp_pg: np.ndarray, tau: float) -> np.ndarray:
    """w = clip(opportunities per game / tau, 0, 1); forecast = w * C + (1 - w) * A2. Where the
    season-average baseline abstains (NaN) or there is no volume evidence, the forecast is C."""
    w = np.clip(np.nan_to_num(opp_pg, nan=tau) / tau, 0.0, 1.0)
    return np.where(np.isnan(a2), c, w * c + (1.0 - w) * np.nan_to_num(a2))


def _fit_rows(store: pl.DataFrame, k: int) -> pl.DataFrame:
    train = ch.train_rows(store, k, POSITION)
    assert train.height == 0 or train["key"].max() < k, "training row at or after the test week"
    return train


def candidate_predictions(store: pl.DataFrame, ref: pl.DataFrame, weeks: list[tuple[int, int]], name: str,
                          params: dict) -> pl.DataFrame:
    """Walk-forward forecasts of one candidate: columns key, gsis_id, pred_<name>. `store` carries the RB
    rows with TEAMMATE_COLUMNS; `ref` holds pred_C / pred_A2 for the same rows (vol_shrink only reads
    them). Every refit uses store rows strictly before the test week; test rows without a recent game
    abstain exactly like C."""
    from .participation import fit_logit

    col = f"pred_{name}"
    parts = []
    for season, week in weeks:
        k = key(season, week)
        test = store.filter((pl.col("key") == k) & (pl.col("position") == POSITION))
        if test.height == 0:
            continue
        recent = test["games_recent"].to_numpy() > 0
        if name == "vol_shrink":
            r = ref.filter(pl.col("key") == k).select("gsis_id", "pred_C", "pred_A2")
            t = test.join(r, on="gsis_id", how="left")
            c, a2 = _arr(t, "pred_C"), _arr(t, "pred_A2")
            p = shrink_toward_season_average(c, a2, _arr(t, "carries_pg") + _arr(t, "targets_pg"), params["tau"])
            test = t
        else:
            train = _fit_rows(store, k)
            if train.height < MIN_TRAIN_ROWS:
                p = np.full(test.height, np.nan)
            elif name == "tm_transfer":
                m = ch.fit_linear(train, POSITION, CANDIDATE_FEATURES[name], params["lam"], target_key=k)
                p = np.where(recent, m.predict(test), np.nan)
            elif name == "hurdle":
                touched = ((train["o_carries"].fill_null(0) + train["o_targets"].fill_null(0)) > 0)
                tr = train.with_columns(touched.cast(pl.Float64).alias("touched"))
                pos_rows, zero_rows = tr.filter(pl.col("touched") == 1.0), tr.filter(pl.col("touched") == 0.0)
                if pos_rows.height < MIN_TRAIN_ROWS or zero_rows.height == 0:
                    p = np.full(test.height, np.nan)
                else:
                    lg = fit_logit(tr, POSITION, [params["logit_lam"]], label="touched",
                                   features=CANDIDATE_FEATURES[name])[params["logit_lam"]]
                    rg = ch.fit_linear(pos_rows, POSITION, CANDIDATE_FEATURES[name], HURDLE_RIDGE_LAM, target_key=k)
                    p_touch = lg.predict(test)
                    z0 = float(zero_rows["label"].mean())
                    p = np.where(recent, p_touch * rg.predict(test) + (1.0 - p_touch) * z0, np.nan)
            else:
                raise ValueError(f"unknown candidate {name!r}")
        parts.append(pl.DataFrame({"key": test["key"], "gsis_id": test["gsis_id"], col: p}).with_columns(pl.col(col).fill_nan(None)))
    return pl.concat(parts) if parts else pl.DataFrame(schema={"key": pl.Int64, "gsis_id": pl.String, col: pl.Float64})


def grid_search(store: pl.DataFrame, ref: pl.DataFrame, weeks: list[tuple[int, int]], name: str) -> dict:
    """Evaluate every grid point of candidate `name` on `weeks` (development window) and pick the one
    with the lowest MAE on the rows where every grid point and C and A2 produced a forecast. Ties go
    to the smaller parameter (the more conservative setting)."""
    (pname, values), = CANDIDATE_GRIDS[name].items()
    cols, base = {}, ref
    for v in values:
        pred = candidate_predictions(store, ref, weeks, name, {pname: v}).rename({f"pred_{name}": f"pred_{name}@{v:g}"})
        base = base.join(pred, on=["key", "gsis_id"], how="left")
        cols[v] = f"pred_{name}@{v:g}"
    d = base.filter(pl.all_horizontal([pl.col(c).is_not_null() for c in cols.values()] + [pl.col("pred_C").is_not_null()]))
    a = _arr(d, "label")
    scores = {v: float(np.abs(a - _arr(d, c)).mean()) for v, c in cols.items()}
    best = min(scores, key=lambda v: (round(scores[v], 12), v))
    return {"param": pname, "value": best, "dev_mae_by_value": {f"{v:g}": round(s, 4) for v, s in scores.items()},
            "dev_mae_C": round(float(np.abs(a - _arr(d, "pred_C")).mean()), 4), "n": d.height}


# ----------------------------------------------------------------------------- evaluation and decision rule

# Cohorts fixed in advance (all pregame). Injury return = the last game was an injury exit or the
# player is back after >= 3 weeks away.
EVAL_COHORTS: dict[str, pl.Expr] = {
    "starters_pool (top-48 RB by A1)": starters_pool_expr(),
    "high_volume_back (carries/g + targets/g >= 15)": (pl.col("carries_pg") + pl.col("targets_pg")) >= 15,
    "injury_return (last game injury exit or >= 3 weeks away)":
        (pl.col("last_injury_exit") == 1) | (pl.col("weeks_since_last") >= 3),
    "low_volume (carries/g + targets/g < 4)": (pl.col("carries_pg") + pl.col("targets_pg")) < 4,
    "team_change (this or last season)": (pl.col("team_changed") == 1) | (pl.col("prev_team_changed") == 1),
    "early_season (weeks 1-4)": pl.col("week") <= 4,
    "teammate_unavailable (a teammate RB Out/Doubtful)": pl.col("tm_out_n").fill_null(0) > 0,
}
GUARD_COHORTS = ("high_volume_back (carries/g + targets/g >= 15)",
                 "injury_return (last game injury exit or >= 3 weeks away)",
                 "team_change (this or last season)", "low_volume (carries/g + targets/g < 4)")

DECISION_RULE = {
    "window": "confirmation: 2023, 2024, 2025 and 2026 weeks 1-5, one run of the frozen candidates",
    "primary_comparison": "candidate - ridge C (ifp-1.0.0 context model at its frozen penalty), MAE, identical rows",
    "c1_effect": "pooled MAE difference <= -0.02 points AND the Bonferroni-adjusted (3 candidates) week-clustered "
                 "bootstrap CI upper bound < 0",
    "c2_consistency": "MAE difference < 0 in at least 3 of the 4 sub-windows (2023, 2024, 2025, 2026 weeks 1-5)",
    "c3_rmse": "RMSE not worse than C by more than 0.5%",
    "c4_bias": "|bias| not larger than C's by more than 0.15 points",
    "c5_starters": "starters-pool MAE not worse than C",
    "c6_pairs": "Start/Sit pair accuracy not lower than C's by more than 0.5 percentage points",
    "c7_guard_cohorts": "no guard cohort (high-volume backs, injury return, team change, low volume) where the "
                        "candidate is significantly worse than C (adjusted CI lower bound > 0)",
    "verdict": "all of c1-c7 -> 'supported_for_shadow_tracking' (never a promotion; promotion needs prospective "
               "frozen pregame weeks and explicit approval); otherwise 'not_supported' with the failed checks listed",
    "also_reported": "candidate - season average (A2) and candidate - ifp-2; a candidate is described as beating the "
                     "season average only if the adjusted CI of candidate - A2 is below 0",
}


def season_windows(d: pl.DataFrame) -> dict[str, pl.DataFrame]:
    return {str(s): d.filter(pl.col("season") == s) for s in sorted(d["season"].unique().to_list())}


def evaluate_window(d: pl.DataFrame, candidates: list[str], *, resamples: int = RESAMPLES, seed: int = SEED) -> dict:
    """Full report on rows where every model (references and candidates) produced a forecast."""
    models = [*REFERENCE_MODELS, *candidates]
    d = common_support(d, models)
    report: dict = {"n": d.height, "weeks": d.select("key").n_unique(), "models": models,
                    "pooled": pooled_metrics(d, models)}
    report["by_season"] = {s: {"n": g.height, **pooled_metrics(g, models)} for s, g in season_windows(d).items()}
    diffs = {}
    for m in models:
        for ref in ("C", "A2"):
            if m != ref:
                diffs[f"{m}-{ref}"] = boot_diff(d, f"pred_{m}", f"pred_{ref}", resamples=resamples, seed=seed)
    for c in candidates:
        diffs[f"{c}-ifp2"] = boot_diff(d, f"pred_{c}", "pred_ifp2", resamples=resamples, seed=seed)
    report["diffs"] = diffs
    report["diffs_by_season"] = {s: {f"{c}-C": boot_diff(g, f"pred_{c}", "pred_C", resamples=resamples, seed=seed)
                                     for c in candidates} for s, g in season_windows(d).items()}
    report["cohorts"] = {}
    for name, expr in EVAL_COHORTS.items():
        g = d.filter(expr.fill_null(False))
        a = _arr(g, "label")
        report["cohorts"][name] = {
            "n": g.height, "mae": {m: _round(metrics(a, _arr(g, f"pred_{m}"))["mae"]) for m in models},
            "bias": {m: _round(metrics(a, _arr(g, f"pred_{m}"))["bias"]) for m in models},
            "diffs": {f"{c}-C": boot_diff(g, f"pred_{c}", "pred_C", resamples=resamples, seed=seed) for c in candidates}
            if g.height else {}}
    report["pairs"] = pair_decisions(d, [f"pred_{m}" for m in models])
    report["decisions"] = {c: decide(report, c) for c in candidates}
    return report


def decide(report: dict, name: str) -> dict:
    """Apply DECISION_RULE to an evaluate_window report."""
    pooled = report["pooled"]
    d_c = report["diffs"][f"{name}-C"]
    checks = {}

    def check(k, ok, detail):
        checks[k] = {"pass": bool(ok), "detail": detail}

    check("c1_effect", d_c["diff"] is not None and d_c["diff"] <= -0.02 and d_c["ci_adj"][1] < 0,
          f"diff {_round(d_c['diff'], 4)}, adjusted CI {[_round(x, 4) for x in (d_c['ci_adj'] or [])]}")
    subs = [v[f"{name}-C"]["diff"] for v in report["diffs_by_season"].values() if v[f"{name}-C"]["diff"] is not None]
    check("c2_consistency", sum(1 for x in subs if x < 0) >= 3 and len(subs) >= 3,
          f"{sum(1 for x in subs if x < 0)} of {len(subs)} sub-windows improve")
    rm, rc = pooled[name]["rmse"], pooled["C"]["rmse"]
    check("c3_rmse", rm <= rc * 1.005, f"RMSE {rm} vs C {rc}")
    bm, bc = pooled[name]["bias"], pooled["C"]["bias"]
    check("c4_bias", abs(bm) <= abs(bc) + 0.15, f"bias {bm} vs C {bc}")
    sp = report["cohorts"]["starters_pool (top-48 RB by A1)"]
    check("c5_starters", sp["n"] > 0 and sp["mae"][name] <= sp["mae"]["C"], f"starters MAE {sp['mae'][name]} vs C {sp['mae']['C']}")
    pa = report["pairs"]["all"]
    check("c6_pairs", pa[f"pred_{name}"]["accuracy"] >= pa["pred_C"]["accuracy"] - 0.005,
          f"pair accuracy {pa[f'pred_{name}']['accuracy']} vs C {pa['pred_C']['accuracy']}")
    worse = [g for g in GUARD_COHORTS if report["cohorts"][g]["diffs"].get(f"{name}-C", {}).get("ci_adj")
             and report["cohorts"][g]["diffs"][f"{name}-C"]["ci_adj"][0] > 0]
    check("c7_guard_cohorts", not worse, "significantly worse in: " + ", ".join(worse) if worse else "none significantly worse")
    d_a2 = report["diffs"][f"{name}-A2"]
    ok = all(v["pass"] for v in checks.values())
    return {"verdict": "supported_for_shadow_tracking" if ok else "not_supported", "checks": checks,
            "beats_season_average": bool(d_a2["ci_adj"] and d_a2["ci_adj"][1] < 0),
            "vs_season_average": d_a2, "vs_C": d_c}


# ----------------------------------------------------------------------------- freezing and preregistration

PREREG_START, PREREG_END = "<!-- PREREG-START -->", "<!-- PREREG-END -->"


def preregistration_hash(doc_text: str) -> str:
    """sha256 of the text between the PREREG markers of the research document."""
    if PREREG_START not in doc_text or PREREG_END not in doc_text:
        raise ValueError("document has no preregistration markers")
    body = doc_text.split(PREREG_START, 1)[1].split(PREREG_END, 1)[0]
    return hashlib.sha256(body.strip().encode()).hexdigest()


def frozen_config(selected: dict[str, dict], prereg_hash: str, source_fp: str) -> dict:
    """What `confirm` consumes: the grid points chosen on the development window, bound to the
    preregistration text and the input data. Written once; `confirm` re-checks both bindings."""
    return {"version": "rb-research-0.1.0", "development_window": list(DEVELOPMENT),
            "confirmation_window": list(CONFIRMATION), "c_lambda": C_LAMBDA_RB, "selected": selected,
            "preregistration_sha256": prereg_hash, "source_fingerprint": source_fp, "model_version_compared": MODEL_VERSION,
            "grids": CANDIDATE_GRIDS, "seed": SEED, "resamples": RESAMPLES}


def check_frozen(cfg: dict, prereg_hash: str, source_fp: str) -> None:
    if cfg.get("preregistration_sha256") != prereg_hash:
        raise ValueError("the preregistration text changed after the candidates were frozen")
    if cfg.get("source_fingerprint") != source_fp:
        raise ValueError("the input data changed after the candidates were frozen")
    if set(cfg.get("selected", {})) != set(CANDIDATE_GRIDS):
        raise ValueError("frozen configuration does not name exactly the preregistered candidates")
    for name, sel in cfg["selected"].items():
        if sel["value"] not in CANDIDATE_GRIDS[name][sel["param"]]:
            raise ValueError(f"{name}: frozen value is not on the preregistered grid")
