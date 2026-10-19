"""Research: receiver role x expected-QB passing quality (ifp-qbr, research only; see
docs/research/qb_receiver_compatibility.md). Offline except `pbp`, read-only on both databases.

    cd backend
    uv run python ../scripts/qbr_research.py pbp        # cache nflverse play-by-play pass plays (2018+), once
    uv run python ../scripts/qbr_research.py build      # ifp-ctx store + role x QB columns, coverage, diagnostics
    uv run python ../scripts/qbr_research.py develop    # 2021-22 only -> write-once frozen selection
    uv run python ../scripts/qbr_research.py evaluate   # frozen selection: 2023-24 (once) + 2025-26 replay
    uv run python ../scripts/qbr_research.py hypothesis # descriptive: role-dependent residuals, all windows

Reuses the ifp-ctx feature store builder (context_research.build) into its OWN cache directory, so the
ifp-ctx / ifp-ctx-p1 outputs are never touched. Outputs: <ifp-dir>/research/qbr/ (git-ignored).
Never writes ifp-1/ifp-2 artifacts, the weekly ledger, the registry or any production table.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import time

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402
import context_research as ctxr  # noqa: E402
import hgb_research as hr  # noqa: E402

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import context_challenger as cc  # noqa: E402
from app.analytics import qb_receiver_compat as qr  # noqa: E402
from app.analytics.forecast_backtest import _arr, metrics, pair_decisions, week_bootstrap_mae_diff  # noqa: E402
from app.config import get_settings  # noqa: E402

SEED = 20261018
PBP_FIRST_SEASON = 2018        # QB profiles look back two seasons from the first store season (2020)


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "qbr"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ----------------------------------------------------------------------------- play-by-play cache


def _pbp_path(args, season: int) -> Path:
    return _out(args) / "pbp" / f"pbp_pass_{season}.parquet"


def cmd_pbp(args) -> dict:
    """Caches the REG-season pass plays of nflverse play-by-play (the columns qr.PBP_COLUMNS) per season.
    Past seasons are fetched once; the current season is refreshed on every call."""
    import nflreadpy as nfl

    (_out(args) / "pbp").mkdir(exist_ok=True)
    out = {}
    for s in range(PBP_FIRST_SEASON, args.season + 1):
        path = _pbp_path(args, s)
        if path.exists() and s < args.season and not args.rebuild:
            out[s] = "cached"
            continue
        p = nfl.load_pbp([s])
        p = p.filter((pl.col("season_type") == "REG") & ((pl.col("qb_dropback") == 1) | (pl.col("play_type") == "pass")))
        p.select(list(qr.PBP_COLUMNS)).write_parquet(path)
        out[s] = p.height
    return out


def pbp_tables(args) -> tuple[pl.DataFrame, pl.DataFrame, str]:
    paths = [_pbp_path(args, s) for s in range(PBP_FIRST_SEASON, args.season + 1)]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise SystemExit(f"play-by-play cache missing {missing}: run `qbr_research.py pbp` first")
    pbp = pl.concat([pl.read_parquet(p, columns=list(qr.PBP_COLUMNS)) for p in paths], how="vertical_relaxed")
    qb, rec = qr.pass_game_tables(pbp)
    return qb, rec, hashlib.sha256((hr._frame_hash(qb) + hr._frame_hash(rec)).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------- store


def build(args) -> tuple[pl.DataFrame, dict]:
    """The ifp-ctx store (ifp-2 features + ctx columns, same fully-final weeks) plus the role x QB
    columns, cached by the ctx store's fingerprint (which covers all code and sources) and the pbp hash."""
    work = SimpleNamespace(**{**vars(args), "ifp_dir": _out(args) / "work"})
    store, prov = ctxr.build(work)
    qb, rec, pbp_hash = pbp_tables(args)
    fp = hashlib.sha256(f"{prov['fingerprint']}|{pbp_hash}|{qr.QBR_VERSION}".encode()).hexdigest()[:16]
    prov = {**prov, "ctx_fingerprint": prov["fingerprint"], "pbp_hash": pbp_hash, "fingerprint": fp}
    path, meta = _out(args) / "store.parquet", _out(args) / "store.meta.json"
    if path.exists() and meta.exists() and not args.rebuild and json.loads(meta.read_text())["fingerprint"] == fp:
        return pl.read_parquet(path), prov
    t = time.time()
    src, _ = hr._sources(args)
    ctx = ctxr.ctx_sources(args)
    parts = []
    for (season, week), rows in store.group_by("season", "week", maintain_order=True):
        parts.append(qr.qbr_for_week(qb, rec, src.panel, ctx.starters, int(season), int(week),
                                     rows.select("gsis_id", "position", "expected_qb_id", "qb_epa_delta"))
                     .with_columns(pl.lit(int(season) * 100 + int(week)).alias("key")))
    store = store.join(pl.concat(parts), on=["key", "gsis_id"], how="left")
    store.write_parquet(path)
    meta.write_text(json.dumps({"fingerprint": fp, "rows": store.height, "build_seconds": round(time.time() - t, 1)}))
    return store, prov


def cmd_build(args) -> dict:
    store, prov = build(args)
    s = store.filter((pl.col("season") >= ch.TRAIN_START) & pl.col("position").is_in(list(qr.POSITIONS)))
    out = {"rows": s.height, **prov, "missing_rate_by_season": {}, "cohort_sizes": {}, "role_quantiles": {}}
    for season, g in s.group_by("season", maintain_order=True):
        out["missing_rate_by_season"][int(season[0])] = {c: round(float(g[c].is_null().mean()), 4)
                                                         for c in (*qr.FEATURE_COLUMNS, "rec_deep_share",
                                                                   "qb_deep_cpoe_delta", "qb_epa_delta")}
    for pos in [*qr.POSITIONS, "ALL"]:
        g = s if pos == "ALL" else s.filter(pl.col("position") == pos)
        out["cohort_sizes"][pos] = {c: int(g.filter(e.fill_null(False)).height) for c, e in qr.cohorts().items()
                                    if c != "starters_pool"}
        r = g.filter(pl.col("rec_deep_share").is_not_null())
        out["role_quantiles"][pos] = {c: [round(float(r[c].quantile(q)), 4) for q in (0.1, 0.25, 0.5, 0.75, 0.9)]
                                      for c in ("rec_deep_share", "rec_adot", "rec_ay_share", "rec_epa_tgt")}
    ch_ = s.filter(pl.col("qb_changed") == 1)
    out["changed_qb_delta_quantiles"] = {
        c: [round(float(ch_[c].quantile(q)), 4) for q in (0.1, 0.25, 0.5, 0.75, 0.9)]
        for c in ("qb_epa_delta", "qb_cpoe_delta", "qb_pbp_cpoe_delta", "qb_deep_cpoe_delta", "qb_deep_rate_delta",
                  "qb_adot_delta", "qb_sack_delta")}
    both = ch_.filter(pl.col("qb_cpoe_delta").is_not_null() & pl.col("qb_pbp_cpoe_delta").is_not_null())
    out["pbp_vs_weekly_cpoe_delta_corr"] = round(float(np.corrcoef(both["qb_cpoe_delta"], both["qb_pbp_cpoe_delta"])[0, 1]),
                                                 4) if both.height > 2 else None
    out["changed_qb_feature_corr"] = {
        f"{a}~{b}": round(float(np.corrcoef(*[ch_.drop_nulls([a, b])[c].to_numpy() for c in (a, b)])[0, 1]), 4)
        for a, b in (("qb_deep_cpoe_delta", "qb_epa_delta"), ("qb_deep_rate_delta", "qb_epa_delta"),
                     ("qbr_epa_x", "qb_epa_delta"), ("qbr_deep_x", "qb_cpoe_delta"))}
    (_out(args) / "build_report.json").write_text(json.dumps(out, indent=1, default=str))
    return out


# ----------------------------------------------------------------------------- comparison


def report(p: pl.DataFrame, models: list[str], exact: bool = False) -> dict:
    """Identical-row comparison of every model against ifp2 (and Q/CQ against their role-blind base),
    per position and pooled over WR + TE, with cohorts."""
    lab = p.filter(pl.col("label").is_not_null())
    cov = {m: round(float(lab[f"pred_{m}"].is_not_null().mean()), 4) for m in models}
    p = lab.filter(pl.all_horizontal([pl.col(f"pred_{m}").is_not_null() for m in models]))
    out = {"population": {"labeled": lab.height, "common": p.height, "coverage": cov,
                          "weeks": p.select("key").n_unique()}}
    flt = qr.cohorts()
    vs_base = {m: b for m, b in (("Q", "A"), ("CQ", "C"), ("qbr", "A")) if m in models and b in models}

    def diffs(g):
        d = {f"{m}-ifp2": week_bootstrap_mae_diff(g, f"pred_{m}", "pred_ifp2", 1000, SEED, exact)
             for m in models if m != "ifp2"}
        d.update({f"{m}-{b}": week_bootstrap_mae_diff(g, f"pred_{m}", f"pred_{b}", 1000, SEED, exact)
                  for m, b in vs_base.items()})
        return d

    for pos in [*qr.POSITIONS, "ALL"]:
        g = p if pos == "ALL" else p.filter(pl.col("position") == pos)
        r = {"n": g.height, "metrics": {m: metrics(_arr(g, "label"), _arr(g, f"pred_{m}"), exact) for m in models},
             "diffs": diffs(g), "cohorts": {}}
        for c, e in flt.items():
            cg = g.filter(e.fill_null(False))
            rr = {"n": cg.height, **{m: metrics(_arr(cg, "label"), _arr(cg, f"pred_{m}"), exact) for m in models}}
            if cg.height >= 30 and cg.select("key").n_unique() >= 5:
                rr["diffs"] = diffs(cg)
            r["cohorts"][c] = rr
        out[pos] = r
    out["pairs"] = pair_decisions(p, [f"pred_{m}" for m in models], exact=exact)
    return out


def _preds(store: pl.DataFrame, weeks, cfg, per_position=None) -> pl.DataFrame:
    """pred_ifp2, A, Q, C, CQ (+ pred_qbr) on WR/TE rows; ifp2, A and C cross-checked against
    context_challenger.walk_forward on the same rows (they must agree exactly)."""
    t = time.time()
    preds = qr.walk_forward(store, weeks, cfg, per_position=per_position)
    ref = cc.walk_forward(store.filter(pl.col("position").is_in(list(qr.POSITIONS))), weeks, cfg,
                          candidates=("A", "C"))
    j = preds.join(ref, on=["key", "gsis_id"], how="left", suffix="_cc")
    for m in ("ifp2", "A", "C"):
        bad = j.filter(((pl.col(f"pred_{m}") - pl.col(f"pred_{m}_cc")).abs().fill_null(0.0) > 1e-9)
                       | (pl.col(f"pred_{m}").is_null() != pl.col(f"pred_{m}_cc").is_null()))
        assert bad.height == 0, f"pred_{m} differs from context_challenger.walk_forward"
    print(f"walk-forward {len(weeks)} weeks: {time.time() - t:.0f}s", file=sys.stderr)
    return (store.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks]))
            .join(preds, on=["key", "gsis_id"]))


def _frozen_path(args) -> Path:
    return _out(args) / f"frozen_selection_{qr.QBR_CHALLENGER_VERSION}.json"


MODELS = ["ifp2", *qr.CANDIDATES]


def select(exact: dict) -> tuple[dict, dict]:
    """The pre-registered development rule (qr.DECISION_RULES['selection']) on an exact report."""
    chosen, why = {}, {}
    for pos in qr.POSITIONS:
        r = exact[pos]
        d, inc = r["diffs"]["Q-ifp2"], r["diffs"]["Q-A"]
        bias_ok = abs(r["metrics"]["Q"]["bias"]) <= abs(r["metrics"]["ifp2"]["bias"]) + qr.BIAS_TOLERANCE
        vs_ifp2 = d["diff"] <= qr.SELECTION_MAX_DIFF and d["ci95"][1] < 0
        vs_a = inc["diff"] < 0 and inc["ci95"][1] < 0
        chosen[pos] = qr.CANDIDATE if vs_ifp2 and bias_ok and vs_a else "ifp2"
        why[pos] = {"Q-ifp2": d, "Q-A": inc, "vs_ifp2_ok": vs_ifp2, "bias_ok": bias_ok, "vs_A_ok": vs_a}
    return chosen, why


def cmd_develop(args) -> dict:
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    dev = _preds(store, ch.window_weeks(store, qr.WINDOWS["development"]), cfg)
    chosen, why = select(report(dev, MODELS, exact=True))
    frozen = {"version": qr.QBR_CHALLENGER_VERSION, "status": qr.QBR_STATUS, "selected_on": qr.WINDOWS["development"],
              "per_position": chosen, "group": qr.GROUP, "candidates": qr.CANDIDATES, "rules": qr.DECISION_RULES,
              "ifp2_config_hash": cfg.hash(), "feature_version": qr.QBR_VERSION, "ctx_groups": cc.GROUPS,
              "scoring_fingerprint": prov["scoring_fingerprint"], "store_fingerprint": prov["fingerprint"]}
    frozen["config_hash"] = hashlib.sha256(json.dumps({k: v for k, v in frozen.items() if k != "rules"},
                                                      sort_keys=True, default=str).encode()).hexdigest()[:16]
    frozen["frozen_at"] = datetime.now(timezone.utc).isoformat()
    path = _frozen_path(args)
    if not hr._write_once(path, frozen):
        prev = json.loads(path.read_text())
        if prev["per_position"] != chosen:
            raise SystemExit(f"{path} already frozen with a different selection ({prev['per_position']})")
    rep = report(dev, MODELS)
    (_out(args) / "development.json").write_text(json.dumps({"report": rep, "selection": why, **prov}, indent=1,
                                                            default=str))
    return {"per_position": chosen, "selection": why, "pooled_diffs": rep["ALL"]["diffs"]}


def verdict(r: dict, p: pl.DataFrame, chosen: dict) -> dict:
    """The pre-registered confirmation rules (qr.DECISION_RULES), on unrounded values."""
    adopted = [pos for pos, c in chosen.items() if c != "ifp2"]
    if not adopted:
        return {"adopted_positions": [], "outcome": "retain_baseline", "reason": "nothing adopted on development"}
    sub = {pos: r[pos] for pos in adopted}
    rows = p.filter(pl.col("label").is_not_null() & pl.col("position").is_in(adopted)
                    & pl.col("pred_qbr").is_not_null() & pl.col("pred_ifp2").is_not_null())
    pooled = week_bootstrap_mae_diff(rows, "pred_qbr", "pred_ifp2", 1000, SEED, exact=True)
    a = _arr(rows, "label")
    rmse = {m: metrics(a, _arr(rows, f"pred_{m}"), exact=True)["rmse"] for m in ("qbr", "ifp2")}
    pairs = pair_decisions(rows, ["pred_qbr", "pred_ifp2"], exact=True)["all"]
    checks = {
        "c1_pooled_mae": pooled["diff"] < 0 and pooled["ci95"][1] < 0,
        "c2_no_position_significantly_worse": all(s["diffs"]["qbr-ifp2"]["ci95"][0] <= 0 for s in sub.values()),
        "c3_rmse": rmse["qbr"] <= rmse["ifp2"] * 1.005,
        "c4_bias": all(abs(s["metrics"]["qbr"]["bias"]) <= abs(s["metrics"]["ifp2"]["bias"]) + qr.BIAS_TOLERANCE
                       for s in sub.values()),
        "c5_starters_pool": all(s["cohorts"]["starters_pool"]["qbr"]["mae"]
                                <= s["cohorts"]["starters_pool"]["ifp2"]["mae"] * 1.01 for s in sub.values()),
        "c6_pairs": pairs["pred_qbr"]["accuracy"] >= pairs["pred_ifp2"]["accuracy"] - 0.005,
    }
    mech = {}
    for pos in adopted:
        co = r[pos]["cohorts"][qr.MECHANISM_COHORT]
        mech[pos] = ({"vs_ifp2": co["qbr"]["mae"] - co["ifp2"]["mae"], "vs_A": co["qbr"]["mae"] - co["A"]["mae"]}
                     if co["n"] else None)
    checks["c7_mechanism"] = all(v is not None and v["vs_ifp2"] < 0 and v["vs_A"] < 0 for v in mech.values())
    return {"adopted_positions": adopted, "pooled_adopted": pooled, "pooled_adopted_rmse": rmse,
            "pairs_adopted": {m: pairs[f"pred_{m}"] for m in ("qbr", "ifp2")}, "checks": checks,
            "mechanism_diffs": mech, "outcome": "prospective_shadow" if all(checks.values()) else "retain_baseline"}


def cmd_evaluate(args) -> dict:
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    frozen = json.loads(_frozen_path(args).read_text())
    if frozen["ifp2_config_hash"] != cfg.hash() or frozen["scoring_fingerprint"] != prov["scoring_fingerprint"]:
        raise SystemExit("frozen selection was made against a different ifp-2 config or league scoring")
    if frozen.get("feature_version") != qr.QBR_VERSION or frozen.get("group") != qr.GROUP \
            or frozen.get("rules") != qr.DECISION_RULES or frozen.get("ctx_groups") != json.loads(json.dumps(cc.GROUPS)):
        raise SystemExit(f"{_frozen_path(args).name} was frozen with a different feature version, group or rules; "
                         "bump QBR_VERSION / QBR_CHALLENGER_VERSION instead of re-confirming")
    chosen = frozen["per_position"]
    models = [*MODELS, "qbr"]
    out = {"frozen": {k: frozen[k] for k in ("per_position", "config_hash", "frozen_at", "store_fingerprint")},
           "store_matches_frozen": frozen["store_fingerprint"] == prov["fingerprint"], **prov}
    for name, seasons in (("confirmation", qr.WINDOWS["confirmation"]), ("exploratory", qr.WINDOWS["exploratory"])):
        p = _preds(store, ch.window_weeks(store, seasons), cfg, per_position=chosen)
        p.select("key", "gsis_id", "position", "label", *[f"pred_{m}" for m in models]).write_parquet(
            _out(args) / f"{name}_preds.parquet")
        out[name] = report(p, models)
        if name == "confirmation":
            out["verdict"] = verdict(report(p, models, exact=True), p, chosen)
        else:
            out["exploratory_by_season"] = {int(s): report(p.filter(pl.col("season") == s), models)["ALL"]
                                            for s in seasons if p.filter(pl.col("season") == s).height}
    out["development"] = json.loads((_out(args) / "development.json").read_text())["report"]
    (_out(args) / "evaluation.json").write_text(json.dumps(out, indent=1, default=str))
    return {"verdict": out["verdict"], "confirmation_pooled": out["confirmation"]["ALL"]["diffs"],
            "exploratory_pooled": out["exploratory"]["ALL"]["diffs"]}


# ----------------------------------------------------------------------------- the hypothesis, directly (descriptive)


def _boot(values: np.ndarray, n: int = 2000, seed: int = SEED) -> list[float]:
    rng = np.random.default_rng(seed)
    b = [float(values[rng.integers(0, len(values), len(values))].mean()) for _ in range(n)]
    return [round(float(np.percentile(b, 2.5)), 3), round(float(np.percentile(b, 97.5)), 3)]


def _slope(df: pl.DataFrame, x: str, y: str, n: int = 1000, seed: int = SEED) -> dict:
    """OLS slope of y on x with a week-clustered bootstrap 95% CI."""
    def fit(d):
        xs, ys = d[x].to_numpy(), d[y].to_numpy()
        xs = xs - xs.mean()
        return float((xs * (ys - ys.mean())).sum() / (xs * xs).sum())

    parts = df.partition_by("key")
    rng = np.random.default_rng(seed)
    b = [fit(pl.concat([parts[i] for i in rng.integers(0, len(parts), len(parts))])) for _ in range(n)]
    return {"slope": round(fit(df), 3), "ci95": [round(float(np.percentile(b, 2.5)), 3),
                                                 round(float(np.percentile(b, 97.5)), 3)], "n": df.height}


def cmd_hypothesis(args) -> dict:
    """Descriptive, selects nothing: on WR rows with a changed expected QB, does the role-blind error
    depend on the receiver's role? (1) mean residual (label - prediction) of ifp2 and A by role when the
    expected QB is a downgrade; (2) deep-threat minus short-route residual of ifp2 on the SAME team-week;
    (3) slope of A's residual on (deep share - mean) x QB delta; (4) Q's fitted WR coefficients (per raw
    unit) at the start of each window and the points they imply for a deep threat vs a short-route WR."""
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    out = {"fingerprint": prov["fingerprint"]}
    for name, seasons in qr.WINDOWS.items():
        weeks = ch.window_weeks(store, seasons)
        p = (store.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks]))
             .join(qr.walk_forward(store, weeks, cfg, candidates=("A",)), on=["key", "gsis_id"])
             .filter(pl.col("label").is_not_null() & pl.col("pred_A").is_not_null() & (pl.col("position") == "WR")
                     & (pl.col("qb_changed") == 1))
             .drop_nulls(["rec_deep_share", "qb_epa_delta", "qb_deep_cpoe_delta"]))
        p = p.with_columns((pl.col("label") - pl.col("pred_ifp2")).alias("res_ifp2"),
                           (pl.col("label") - pl.col("pred_A")).alias("res_A"),
                           (pl.col("rec_deep_share") - pl.col("rec_deep_share").mean()).alias("_ds"))
        p = p.with_columns((pl.col("_ds") * pl.col("qb_epa_delta")).alias("ds_x_epa"),
                           (pl.col("_ds") * pl.col("qb_deep_cpoe_delta")).alias("ds_x_deep_cpoe"))
        down = p.filter(pl.col("qb_epa_delta") < qr.DOWNGRADE)
        roles = {"deep_threat": pl.col("rec_deep_share") >= qr.DEEP_THREAT,
                 "middle": (pl.col("rec_deep_share") > qr.SHORT_ROUTE) & (pl.col("rec_deep_share") < qr.DEEP_THREAT),
                 "short_route": pl.col("rec_deep_share") <= qr.SHORT_ROUTE}
        r = {"changed_qb_wr_rows": p.height, "downgrade_rows": down.height, "downgrade_residual_by_role": {}}
        for lab, e in roles.items():
            g = down.filter(e)
            r["downgrade_residual_by_role"][lab] = {"n": g.height, "ifp2": round(float(g["res_ifp2"].mean()), 3),
                                                    "A": round(float(g["res_A"].mean()), 3)}
        tw = lambda e, c: down.filter(e).group_by("key", "team").agg(pl.col("res_ifp2").mean().alias(c))  # noqa: E731
        pair = tw(roles["deep_threat"], "deep").join(tw(roles["short_route"], "short"), on=["key", "team"])
        d = (pair["deep"] - pair["short"]).to_numpy()
        r["same_team_week_deep_minus_short_residual_ifp2"] = {"team_weeks": pair.height,
                                                              "mean": round(float(d.mean()), 3) if len(d) else None,
                                                              "ci95": _boot(d) if len(d) > 3 else None}
        r["slope_res_A_on_deep_share_x_epa_delta"] = _slope(p, "ds_x_epa", "res_A")
        r["slope_res_A_on_deep_share_x_deep_cpoe_delta"] = _slope(p, "ds_x_deep_cpoe", "res_A")
        k = weeks[0][0] * 100 + weeks[0][1]
        m = qr.fit_candidate(ch.train_rows(store, k, "WR"), cfg, "WR", "Q", k)
        coef = {c: b / s for c, b, s in zip(m.columns, m.coef, m.stds)
                if c in ("qb_epa_delta", "qb_cpoe_delta", "qb_changed", *qr.FEATURE_COLUMNS)}
        # a 10th-percentile changed-QB downgrade (build_report changed_qb_delta_quantiles) for two WR roles
        q = {"qb_epa_delta": -0.17, "qb_cpoe_delta": -4.3, "qb_deep_cpoe_delta": -2.6, "qb_deep_rate_delta": -0.02}

        def pts(deep_share, ay_share):
            x = {"qb_epa_delta": q["qb_epa_delta"], "qb_cpoe_delta": q["qb_cpoe_delta"], "qb_changed": 0.0,
                 "qbr_deep_x": deep_share * q["qb_deep_cpoe_delta"], "qbr_vol_x": deep_share * q["qb_deep_rate_delta"],
                 "qbr_epa_x": ay_share * q["qb_epa_delta"]}
            return sum(coef[c] * x[c] for c in coef)

        r["Q_wr_coef_per_unit_at_window_start"] = {c: round(v, 4) for c, v in coef.items()}
        r["Q_implied_points_10th_pct_downgrade"] = {"deep_threat (deep 0.25, ay 0.35)": round(pts(0.25, 0.35), 2),
                                                    "short_route (deep 0.10, ay 0.10)": round(pts(0.10, 0.10), 2)}
        out[name] = r
    (_out(args) / "hypothesis.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["pbp", "build", "develop", "evaluate", "hypothesis"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args(argv)
    cmds = {"pbp": cmd_pbp, "build": cmd_build, "develop": cmd_develop, "evaluate": cmd_evaluate,
            "hypothesis": cmd_hypothesis}
    print(json.dumps(cmds[args.command](args), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
