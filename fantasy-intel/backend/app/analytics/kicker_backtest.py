"""Walk-forward selection, evaluation and final fit for the kicker model (offline research; polars/numpy).

Protocol (docs/kicker_projection_engine.md, "Evaluation"):
  * 2019 is burn-in only: it has no previous season, so its point-in-time priors would need a league mean from
    the future. Its games are used as HISTORY (features, kicker skill, league curves), never as rows.
  * Validation seasons (VALIDATION, default 2021-2022): every hyperparameter, the feature groups and the blend
    weight are chosen here, each season predicted by models fitted on earlier seasons only.
  * Test seasons (TEST, default 2023-2025): reported once with the frozen choices; nothing is tuned on them.
  * Prospective season (the live season, weeks completed so far): same frozen choices, fitted on every
    finished season. ESPN numbers appear only here, labeled by how they were recorded.
Every target row is a (team-game, kicker who kicked) pair; predictions are conditional on him kicking.
"""

from __future__ import annotations

import math
from dataclasses import asdict, replace

import numpy as np
import polars as pl

from . import kicker_model as km
from .kicker_scoring import KickerScoring

VALIDATION = (2021, 2022)
TEST = (2023, 2024, 2025)
FIRST_ROW_SEASON = 2020
ABLATIONS = {
    "ability_only": (),
    "+offense": ("offense",),
    "+defense": ("offense", "defense"),
    "+stadium": ("offense", "defense", "stadium"),
    "+weather (retrospective)": ("offense", "defense", "stadium", "weather"),
    "+script (full, no weather)": ("offense", "defense", "stadium", "script"),
    "full + weather (retrospective)": ("offense", "defense", "stadium", "weather", "script"),
}


# ----------------------------------------------------------------------------- dataset


def build_dataset(frames: dict, scoring: KickerScoring, cfg: km.KConfig, targets: pl.DataFrame | None = None) -> dict:
    """{rows, conv, venues}: point-in-time features for every completed team-game (and `targets`, unplayed rows)."""
    tg = km.team_games(frames, scoring)
    if targets is not None and targets.height:
        tg = pl.concat([tg, targets], how="diagonal_relaxed")
    venues = km.venue_table(frames["schedule"])
    feats = km.add_context(km.team_features(tg, cfg), venues)
    feats = feats.join(km.line_table(frames["schedule"]), on=["game_id", "team"], how="left")
    feats = feats.with_columns(pl.col("game_kicker_id").alias("kicker_player_id"))
    khist = km.kicker_history_features(tg)
    feats = km.kicker_points_asof(khist, feats)
    conv = km.conversion_frame(frames["kicks"], venues)
    return {"rows": feats, "conv": conv, "venues": venues}


def lambda_columns(groups) -> list[str]:
    return [c for g in groups for c in km.LAMBDA_FEATURES.get(g, [])]


def _attach_skill(rows: pl.DataFrame, conv: pl.DataFrame, cfg: km.KConfig, season: int) -> tuple[pl.DataFrame, km.Conversion]:
    model = km.fit_conversion(conv.filter(pl.col("season") < season), cfg)
    sk = km.kicker_skill(conv, model, season, cfg)
    r = rows.filter(pl.col("season") == season).with_columns(pl.col("week").cast(pl.Int32))
    r = r.join(sk, on=["kicker_player_id", "week"], how="left").with_columns(
        pl.col("k_skill").fill_null(0.0), pl.col("k_long_share").fill_null(model.league_long_share),
        pl.col("k_kicks").fill_null(0.0))
    return r, model


def season_frames(data: dict, cfg: km.KConfig, seasons) -> dict[int, tuple[pl.DataFrame, km.Conversion]]:
    return {s: _attach_skill(data["rows"], data["conv"], cfg, s) for s in seasons}


def fit_components(train: pl.DataFrame, cfg: km.KConfig) -> dict:
    cols = lambda_columns(cfg.groups)
    w = km.season_weights(train["season"].to_numpy(), cfg.train_decay)
    lam_fg = km.fit_glm(train, lambda_columns(cfg.lambda_fg_groups), train["fg_att"].cast(pl.Float64).to_numpy(),
                        "poisson", cfg.ridge_lambda, weights=w)
    lam_pat = km.fit_glm(train, cols, train["pat_att"].cast(pl.Float64).to_numpy(), "poisson", cfg.ridge_lambda, weights=w)
    dcols = cols + km.DIRECT_KICKER
    direct = km.fit_glm(train, dcols, train["label"].cast(pl.Float64).to_numpy(), "gaussian", cfg.ridge_direct / 100.0,
                        weights=w)
    return {"lam_fg": lam_fg, "lam_pat": lam_pat, "direct": direct}


def predict_rows(rows: pl.DataFrame, comps: dict, conv: km.Conversion, scoring: KickerScoring, cfg: km.KConfig,
                 with_lines: bool = False) -> pl.DataFrame:
    lf = comps["lam_fg"].predict(rows)
    lp = comps["lam_pat"].predict(rows)
    direct = comps["direct"].predict(rows) if comps.get("direct") is not None else np.full(rows.height, np.nan)
    pts, lines = [], []
    for i, r in enumerate(rows.select("k_skill", "k_long_share", "venue_fixed", "venue_retractable", "elev_km",
                                      "wx_wind", "wx_cold", "wx_missing").iter_rows(named=True)):
        e = km.expected_kicking(conv, scoring, float(lf[i]), float(lp[i]), float(r["k_skill"]), float(r["k_long_share"]),
                                km.env_of(r, cfg.groups))
        pts.append(e["points"])
        if with_lines:
            lines.append(e["stat_line"])
    structural = np.array(pts)
    out = rows.with_columns(pl.Series("pred_lam_fg", lf), pl.Series("pred_lam_pat", lp),
                            pl.Series("pred_struct", structural), pl.Series("pred_direct", direct),
                            pl.Series("pred", cfg.blend * structural + (1 - cfg.blend) * np.nan_to_num(direct, nan=0.0)
                                      if cfg.blend < 1 else structural))
    if with_lines:
        out = out.with_columns(pl.Series("stat_line", lines, dtype=pl.Object))
    return out


def baseline_columns(rows: pl.DataFrame, comps_a3: dict, conv: km.Conversion, scoring: KickerScoring) -> pl.DataFrame:
    """A1 season-to-date mean (previous season if none), A2 last-4 mean, A3 team-rate x kicker-conversion."""
    a3 = []
    for r in rows.select("o_fg_att", "o_pat_att", "k_skill", "k_long_share").iter_rows(named=True):
        a3.append(km.expected_kicking(conv, scoring, float(r["o_fg_att"]), float(r["o_pat_att"]), float(r["k_skill"]),
                                      float(r["k_long_share"]), {})["points"])
    return rows.with_columns(
        pl.when(pl.col("k_games") > 0).then(pl.col("k_pts_season")).otherwise(pl.col("k_pts_prev")).alias("pred_A1"),
        pl.col("k_pts_l4").alias("pred_A2"), pl.Series("pred_A3", a3))


# ----------------------------------------------------------------------------- metrics


def _metrics(pred: np.ndarray, y: np.ndarray) -> dict:
    e = y - pred
    return {"n": int(len(y)), "mae": round(float(np.mean(np.abs(e))), 4), "rmse": round(float(np.sqrt(np.mean(e ** 2))), 4),
            "bias_actual_minus_pred": round(float(np.mean(e)), 4), "mean_pred": round(float(np.mean(pred)), 3),
            "mean_actual": round(float(np.mean(y)), 3)}


def _paired_ci(a_err: np.ndarray, b_err: np.ndarray, clusters: np.ndarray, reps: int = 2000, seed: int = 7) -> dict:
    """MAE(a) - MAE(b) with a 95% cluster-bootstrap CI (clusters = season-week)."""
    d = np.abs(a_err) - np.abs(b_err)
    uniq = np.unique(clusters)
    idx = {c: np.where(clusters == c)[0] for c in uniq}
    rng = np.random.default_rng(seed)
    sums = np.array([d[idx[c]].sum() for c in uniq])
    cnts = np.array([len(idx[c]) for c in uniq])
    boots = []
    for _ in range(reps):
        pick = rng.integers(0, len(uniq), len(uniq))
        boots.append(sums[pick].sum() / cnts[pick].sum())
    return {"mae_diff": round(float(d.mean()), 4), "ci95": [round(float(np.quantile(boots, 0.025)), 4),
                                                           round(float(np.quantile(boots, 0.975)), 4)],
            "share_rows_a_better": round(float(np.mean(np.abs(a_err) < np.abs(b_err))), 4)}


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    if len(a) < 3 or ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def ranking_metrics(df: pl.DataFrame, col: str) -> dict:
    """Weekly start/sit ranking: mean Spearman per week, top-12 precision (predicted top 12 that finished top 12),
    and AUC of the prediction for a top-12 finish."""
    sp, prec, auc_num, auc_den = [], [], 0.0, 0.0
    for (_, _), g in df.group_by(["season", "week"]):
        if g.height < 16:
            continue
        p, y = g[col].to_numpy(), g["label"].to_numpy()
        sp.append(_spearman(p, y))
        top_y = set(np.argsort(-y, kind="stable")[:12])
        top_p = set(np.argsort(-p, kind="stable")[:12])
        prec.append(len(top_y & top_p) / 12)
        pos = np.array([i in top_y for i in range(len(y))])
        ps, ns = p[pos], p[~pos]
        auc_num += float((ps[:, None] > ns[None, :]).sum() + 0.5 * (ps[:, None] == ns[None, :]).sum())
        auc_den += len(ps) * len(ns)
    return {"weeks": len(sp), "spearman_mean": round(float(np.nanmean(sp)), 4) if sp else None,
            "top12_precision": round(float(np.mean(prec)), 4) if prec else None,
            "top12_auc": round(auc_num / auc_den, 4) if auc_den else None}


def calibration_table(pred: np.ndarray, y: np.ndarray, bins: int = 5) -> list[dict]:
    order = np.argsort(pred)
    out = []
    for chunk in np.array_split(order, bins):
        if len(chunk):
            out.append({"n": int(len(chunk)), "mean_pred": round(float(pred[chunk].mean()), 3),
                        "mean_actual": round(float(y[chunk].mean()), 3)})
    return out


def conversion_calibration(conv_rows: pl.DataFrame, p: np.ndarray) -> list[dict]:
    d = conv_rows["kick_distance"].to_numpy()
    y = conv_rows["y"].to_numpy()
    out = []
    for name, lo, hi in (("18-29", 0, 30), ("30-39", 30, 40), ("40-49", 40, 50), ("50-54", 50, 55), ("55+", 55, 99)):
        m = (d >= lo) & (d < hi)
        if m.sum():
            out.append({"distance": name, "n": int(m.sum()), "mean_p": round(float(p[m].mean()), 4),
                        "make_rate": round(float(y[m].mean()), 4)})
    return out


def _logloss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def predict_kicks(conv_rows: pl.DataFrame, model: km.Conversion, skills: pl.DataFrame) -> np.ndarray:
    """Point-in-time make probability for each FG try of a season (skill as of the kick's week)."""
    r = conv_rows.with_columns(pl.col("week").cast(pl.Int32)).join(skills, on=["kicker_player_id", "week"], how="left") \
        .with_columns(pl.col("k_skill").fill_null(0.0))
    dist = r["kick_distance"].to_numpy().astype(float)
    base = km.fg_design(dist)
    cols = [base[:, 0], base[:, 1]] + [r[c].fill_null(0.0).to_numpy().astype(float) for c in model.env_columns]
    eta = model.fg.linear(np.column_stack(cols)) + r["k_skill"].to_numpy()
    return 1 / (1 + np.exp(-eta))


# ----------------------------------------------------------------------------- selection


def tune_conversion(data: dict, base: km.KConfig, seasons=VALIDATION) -> dict:
    """tau2 / decay / long_k / env groups by validation FG log loss (point-in-time skill)."""
    results = []
    for groups in ((), ("stadium",), ("stadium", "weather")):
        for tau2 in (0.05, 0.1, 0.2, 0.4):
            for decay in (0.5, 0.7, 0.9):
                cfg = replace(base, tau2=tau2, decay=decay, groups=tuple(groups))
                ll, n = 0.0, 0
                for s in seasons:
                    model = km.fit_conversion(data["conv"].filter(pl.col("season") < s), cfg)
                    sk = km.kicker_skill(data["conv"], model, s, cfg)
                    rows = data["conv"].filter((pl.col("season") == s) & (pl.col("kick_type") == "FG"))
                    p = predict_kicks(rows, model, sk)
                    ll += _logloss(p, rows["y"].to_numpy()) * rows.height
                    n += rows.height
                results.append({"groups": list(groups), "tau2": tau2, "decay": decay, "logloss": round(ll / n, 5), "n": n})
    # league-only reference (no kicker skill)
    ref = []
    for s in seasons:
        model = km.fit_conversion(data["conv"].filter(pl.col("season") < s), base.with_groups(()))
        rows = data["conv"].filter((pl.col("season") == s) & (pl.col("kick_type") == "FG"))
        p = predict_kicks(rows, model, pl.DataFrame(schema={"kicker_player_id": pl.String, "week": pl.Int32,
                                                            "k_skill": pl.Float64}))
        ref.append((_logloss(p, rows["y"].to_numpy()), rows.height))
    results.sort(key=lambda r: r["logloss"])
    return {"grid": results, "best": results[0],
            "league_curve_only_logloss": round(sum(a * b for a, b in ref) / sum(b for _, b in ref), 5)}


def tune_opportunity(frames: dict, scoring: KickerScoring, base: km.KConfig, seasons=VALIDATION) -> dict:
    """team_k / prior_k / ridge by validation Poisson deviance of FG and PAT tries (all groups except weather)."""
    def dev(y, mu):
        mu = np.maximum(mu, 1e-9)
        return float(2 * np.sum(np.where(y > 0, y * np.log(np.maximum(y, 1e-9) / mu), 0) - (y - mu)))

    results = []
    for team_k in (1.0, 2.0, 4.0, 8.0):
        for prior_k in (2.0, 4.0, 8.0, 16.0):
            cfg = replace(base, team_k=team_k, prior_k=prior_k)
            data = build_dataset(frames, scoring, cfg)
            rows = data["rows"].filter(pl.col("completed") & (pl.col("season") >= FIRST_ROW_SEASON))
            for ridge in (0.1, 0.3, 1.0, 3.0, 10.0, 30.0):
                c2 = replace(cfg, ridge_lambda=ridge)
                d_fg = d_pat = 0.0
                n = 0
                for s in seasons:
                    tr, te = rows.filter(pl.col("season") < s), rows.filter(pl.col("season") == s)
                    comps = fit_components(tr, c2)
                    d_fg += dev(te["fg_att"].cast(pl.Float64).to_numpy(), comps["lam_fg"].predict(te))
                    d_pat += dev(te["pat_att"].cast(pl.Float64).to_numpy(), comps["lam_pat"].predict(te))
                    n += te.height
                results.append({"team_k": team_k, "prior_k": prior_k, "ridge": ridge,
                                "dev_fg": round(d_fg / n, 5), "dev_pat": round(d_pat / n, 5),
                                "dev_total": round((d_fg + d_pat) / n, 5)})
    results.sort(key=lambda r: r["dev_total"])
    return {"grid": results, "best": results[0]}


# ----------------------------------------------------------------------------- evaluation


def evaluate_season(data: dict, cfg: km.KConfig, scoring: KickerScoring, season: int, sf=None) -> pl.DataFrame:
    rows = data["rows"].filter(pl.col("completed") & (pl.col("season") >= FIRST_ROW_SEASON))
    sf = sf or season_frames(data, cfg, range(FIRST_ROW_SEASON, season + 1))
    train = pl.concat([sf[s][0] for s in range(FIRST_ROW_SEASON, season) if s in sf], how="diagonal_relaxed")
    train = train.filter(pl.col("completed"))
    comps = fit_components(train, cfg)
    test, conv = sf[season]
    test = test.filter(pl.col("completed") & pl.col("kicker_player_id").is_not_null())
    out = predict_rows(test, comps, conv, scoring, cfg)
    del rows
    return out


def run_ablations(data: dict, base: km.KConfig, scoring: KickerScoring, seasons) -> dict:
    sf = season_frames(data, base, range(FIRST_ROW_SEASON, max(seasons) + 1))
    out = {}
    for name, groups in ABLATIONS.items():
        cfg = base.with_groups(groups)
        # the conversion env follows the groups too, so refit season frames only when it changes
        conv_groups = tuple(g for g in groups if g in ("stadium", "weather"))
        key_ = conv_groups
        if key_ != tuple(g for g in base.groups if g in ("stadium", "weather")):
            sfa = season_frames(data, cfg, range(FIRST_ROW_SEASON, max(seasons) + 1))
        else:
            sfa = sf
        preds = pl.concat([evaluate_season(data, cfg, scoring, s, sfa) for s in seasons], how="diagonal_relaxed")
        y = preds["label"].to_numpy()
        per_season = {}
        for s in seasons:
            g = preds.filter(pl.col("season") == s)
            per_season[str(s)] = _metrics(g["pred_struct"].to_numpy(), g["label"].to_numpy())["mae"]
        out[name] = {"groups": list(groups), **_metrics(preds["pred_struct"].to_numpy(), y),
                     "per_season_mae": per_season,
                     "direct_mae": _metrics(preds["pred_direct"].to_numpy(), y)["mae"]}
    return out


def full_report(data: dict, cfg: km.KConfig, scoring: KickerScoring, seasons, label: str,
                espn: pl.DataFrame | None = None) -> dict:
    sf = season_frames(data, cfg, range(FIRST_ROW_SEASON, max(seasons) + 1))
    preds = []
    for s in seasons:
        p = evaluate_season(data, cfg, scoring, s, sf)
        train = pl.concat([sf[x][0] for x in range(FIRST_ROW_SEASON, s)], how="diagonal_relaxed").filter(pl.col("completed"))
        a3 = fit_components(train, cfg.with_groups(()))
        preds.append(baseline_columns(p, a3, sf[s][1], scoring))
    df = pl.concat(preds, how="diagonal_relaxed")
    y = df["label"].to_numpy()
    support = df.filter(pl.col("pred_A1").is_not_null() & pl.col("pred_A2").is_not_null())
    ys = support["label"].to_numpy()
    models = {"model (kfp structural)": "pred_struct", "direct ridge": "pred_direct", "blend (production)": "pred",
              "A1 season-to-date avg": "pred_A1", "A2 last-4 avg": "pred_A2", "A3 team rates x kicker conversion": "pred_A3"}
    common = {k: _metrics(support[c].to_numpy(), ys) for k, c in models.items()}
    clusters = (support["season"] * 100 + support["week"]).to_numpy()
    h2h = {f"model vs {k}": _paired_ci(ys - support["pred"].to_numpy(), ys - support[c].to_numpy(), clusters)
           for k, c in models.items() if c not in ("pred",)}
    rep = {"label": label, "seasons": list(seasons), "rows": df.height, "common_support_rows": support.height,
           "all_rows_model": _metrics(df["pred"].to_numpy(), y),
           "common_support": common, "head_to_head_mae (negative = model better)": h2h,
           "ranking": {k: ranking_metrics(support, c) for k, c in models.items()},
           "points_calibration": calibration_table(df["pred"].to_numpy(), y),
           "fg_attempt_calibration": calibration_table(df["pred_lam_fg"].to_numpy(), df["fg_att"].cast(pl.Float64).to_numpy()),
           "pat_attempt_calibration": calibration_table(df["pred_lam_pat"].to_numpy(), df["pat_att"].cast(pl.Float64).to_numpy()),
           "per_season": {str(s): _metrics(df.filter(pl.col("season") == s)["pred"].to_numpy(),
                                           df.filter(pl.col("season") == s)["label"].to_numpy()) for s in seasons},
           "cohorts": cohorts(df)}
    # FG make-probability calibration by distance (point-in-time skill)
    conv_cal = []
    for s in seasons:
        rows = data["conv"].filter((pl.col("season") == s) & (pl.col("kick_type") == "FG"))
        model = km.fit_conversion(data["conv"].filter(pl.col("season") < s), cfg)
        sk = km.kicker_skill(data["conv"], model, s, cfg)
        conv_cal.append((rows, predict_kicks(rows, model, sk)))
    allrows = pl.concat([r for r, _ in conv_cal], how="diagonal_relaxed")
    allp = np.concatenate([p for _, p in conv_cal])
    rep["fg_make_calibration"] = conversion_calibration(allrows, allp)
    rep["fg_make_logloss"] = round(_logloss(allp, allrows["y"].to_numpy()), 5)
    if espn is not None and espn.height:
        rep["espn"] = espn_comparison(df, espn)
    rep["_predictions"] = df
    return rep


def cohorts(df: pl.DataFrame) -> dict:
    y = df["label"].to_numpy()
    p = df["pred"].to_numpy()
    def m(mask):
        mask = np.asarray(mask, dtype=bool)
        return _metrics(p[mask], y[mask]) if mask.sum() >= 30 else {"n": int(mask.sum())}
    roof = df["roof_class"].fill_null("unknown").to_numpy()
    wind = df["obs_wind"].fill_null(-1).to_numpy()
    temp = df["obs_temp"].fill_null(999).to_numpy()
    kicks = df["k_kicks"].fill_null(0).to_numpy()
    lam = df["pred_lam_fg"].to_numpy() + df["pred_lam_pat"].to_numpy()
    t1, t2 = np.quantile(lam, [1 / 3, 2 / 3])
    return {"open_air": m(roof == "open_air"), "fixed_roof": m(roof == "fixed"), "retractable": m(roof == "retractable"),
            "observed_wind_15plus": m(wind >= 15), "observed_wind_lt10": m((wind >= 0) & (wind < 10)),
            "observed_temp_lt40": m(temp < 40), "kicker_history_lt60_kicks": m(kicks < 60),
            "kicker_history_ge60_kicks": m(kicks >= 60), "opportunity_low_tercile": m(lam < t1),
            "opportunity_mid_tercile": m((lam >= t1) & (lam < t2)), "opportunity_high_tercile": m(lam >= t2)}


def espn_comparison(df: pl.DataFrame, espn: pl.DataFrame) -> dict:
    """espn: rows (gsis_id, season, week, espn_points, espn_label). Matched on the kicker and week."""
    j = df.join(espn.rename({"gsis_id": "kicker_player_id"}), on=["kicker_player_id", "season", "week"], how="inner")
    out = {}
    for lab, g in j.group_by("espn_label"):
        y = g["label"].to_numpy()
        out[lab[0]] = {"model": _metrics(g["pred"].to_numpy(), y), "espn": _metrics(g["espn_points"].to_numpy(), y),
                       "head_to_head": _paired_ci(y - g["pred"].to_numpy(), y - g["espn_points"].to_numpy(),
                                                  (g["season"] * 100 + g["week"]).to_numpy())}
    return out


def cfg_dict(cfg: km.KConfig) -> dict:
    d = asdict(cfg)
    d["groups"] = list(cfg.groups)
    return d


def finite(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


# ----------------------------------------------------------------------------- production configuration

# Frozen kfp-1.1.0 choices, made on the VALIDATION seasons only (scripts/kicker_projections.py evaluate re-derives
# them and reports any drift): FG-try volume uses the league rate (team/opponent context did not reduce validation
# deviance), PAT volume uses offense + defense + stadium, the conversion curve adds venue terms, observed weather and
# game-script features are excluded (no validated gain), structural model only (blending with the direct ridge did
# not help), no season-recency weighting.
# kfp-1.1.0 adds the "line" group (the team's implied total) to both FG and PAT volume, chosen on validation
# (scripts/kicker_feature_tests.py: MAE 3.593 -> 3.575). An earlier gate also looked at TEST, so 2023-25 is no longer a
# clean held-out estimate for it; 2026 prospective weeks are.
PRODUCTION = km.KConfig(groups=("offense", "defense", "stadium", "line"), fg_groups=("line",), team_k=2.0, prior_k=2.0,
                        ridge_lambda=0.3, tau2=0.1, decay=0.7, blend=1.0, train_decay=1.0)


def participation_table(con_list, frames: dict, scoring: KickerScoring, seasons=range(2020, 2026),
                        prior_n: float = 3.0) -> dict:
    """P(the team's previous-game kicker kicks this game | he is still on the team's weekly roster as ACT/INA, and
    his pregame injury designation), from history. Beta-shrunk toward the undesignated rate."""
    from ..db.connection import table_exists

    inj, ros = [], []
    for con in con_list:
        if table_exists(con, "nfl_injuries"):
            inj.append(pl.from_arrow(con.execute("""SELECT season::INT AS season, week::INT AS week, gsis_id,
                any_value(report_status) AS report_status FROM nfl_injuries WHERE game_type = 'REG' GROUP BY 1, 2, 3""").arrow()))
        if table_exists(con, "nfl_rosters_weekly"):
            ros.append(pl.from_arrow(con.execute("""SELECT season::INT AS season, week::INT AS week, gsis_id, team, status
                FROM nfl_rosters_weekly WHERE gsis_id IS NOT NULL AND (game_type = 'REG' OR game_type IS NULL)""").arrow()))
    if not inj or not ros:
        return {"none": {"p": 0.97, "n": 0, "note": "no history: default"}}
    inj_df, ros_df = pl.concat(inj, how="diagonal_relaxed"), pl.concat(ros, how="diagonal_relaxed").unique(
        ["season", "week", "gsis_id"], keep="last")
    tg = km.team_games(frames, scoring).filter(pl.col("season").is_in(list(seasons))).sort(["team", "season", "week"])
    tg = tg.with_columns(pl.col("game_kicker_id").shift(1).over(["team", "season"]).alias("prev_kicker"))
    t = (tg.filter(pl.col("prev_kicker").is_not_null())
         .join(ros_df.rename({"gsis_id": "prev_kicker", "team": "r_team"}), on=["season", "week", "prev_kicker"], how="left")
         .filter((pl.col("r_team") == pl.col("team")) & pl.col("status").is_in(["ACT", "INA"]))
         .join(inj_df.rename({"gsis_id": "prev_kicker"}), on=["season", "week", "prev_kicker"], how="left")
         .with_columns(pl.col("report_status").fill_null("none").str.to_lowercase().alias("d"),
                       (pl.col("game_kicker_id") == pl.col("prev_kicker")).cast(pl.Float64).alias("kicked")))
    base = float(t.filter(pl.col("d") == "none")["kicked"].mean() or 0.97)
    out = {}
    for (d,), g in t.group_by(["d"]):
        k, n = float(g["kicked"].sum()), g.height
        out[d] = {"p": round((k + prior_n * base) / (n + prior_n), 4) if d != "none" else round(k / n, 4),
                  "n": n, "raw": round(k / n, 4)}
    out.setdefault("none", {"p": round(base, 4), "n": 0})
    return out
