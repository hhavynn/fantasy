"""Research: ifp-ctx-p2, calibrated two-QB scenario mixture (research only; see
docs/research/qb_teammate_context.md, section 12). Offline, read-only on both databases.

    cd backend
    uv run python ../scripts/qb_scenario_research.py decompose    # where p1's QB bias comes from (all windows)
    uv run python ../scripts/qb_scenario_research.py build        # candidates, realized starters, recent QB env
    uv run python ../scripts/qb_scenario_research.py calibration  # start-probability reliability / Brier (no points)
    uv run python ../scripts/qb_scenario_research.py develop      # 2021-22 only -> write-once frozen p2 selection
    uv run python ../scripts/qb_scenario_research.py evaluate     # frozen p2: 2023-24 verdict + 2025-26 replay

Outputs: <ifp-dir>/research/qb_scenarios/ (git-ignored), including its own copy of the p1 feature
store. It never writes research/context/ (p1's store, frozen selections, artifacts and ledgers),
ifp-1/ifp-2 artifacts, the weekly ledger, the registry or any production table.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402
import context_research as ctxr  # noqa: E402
import hgb_research as hr  # noqa: E402
import qb_scenarios as qs  # noqa: E402

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import context_challenger as cc  # noqa: E402
from app.analytics.forecast_backtest import _arr, metrics, pair_decisions, week_bootstrap_mae_diff  # noqa: E402
from app.analytics.forecast_features import POSITIONS  # noqa: E402
from app.config import get_settings  # noqa: E402

SEED = ctxr.SEED
WINDOWS = cc.WINDOWS
P1_SPEC = cc.PROSPECTIVE_SPEC


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "qb_scenarios"
    p.mkdir(parents=True, exist_ok=True)
    return p


def store(args) -> tuple[pl.DataFrame, dict]:
    """p1's feature store (context_research.build, same code and fingerprint rule), cached under OUR
    directory: pointing build() at <out>/_store keeps p1's research/context/ cache untouched."""
    return ctxr.build(SimpleNamespace(**{**vars(args), "ifp_dir": _out(args) / "_store"}))


def _code_hash() -> str:
    return hashlib.sha256((SCRIPTS / "qb_scenarios.py").read_bytes()).hexdigest()[:16]


def build(args) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    """(store with scenario inputs joined, candidates) for every store week from START_TRAIN_START."""
    st, prov = store(args)
    fp = hashlib.sha256(f"{prov['fingerprint']}:{_code_hash()}".encode()).hexdigest()[:16]
    cpath, tpath, rpath, meta = (_out(args) / f for f in ("candidates.parquet", "teams.parquet", "recent.parquet",
                                                         "build.meta.json"))
    if all(p.exists() for p in (cpath, tpath, rpath, meta)) and not args.rebuild \
            and json.loads(meta.read_text())["fingerprint"] == fp:
        cands, teams, recent = (pl.read_parquet(p) for p in (cpath, tpath, rpath))
    else:
        src, _ = hr._sources(args)
        ctx = ctxr.ctx_sources(args)
        weeks = sorted({(int(s), int(w)) for s, w in st.select("season", "week").unique().iter_rows()
                        if s >= qs.START_TRAIN_START})
        cp, tp, rp = [], [], []
        for season, week in weeks:
            wk = st.filter((pl.col("season") == season) & (pl.col("week") == week))
            cp.append(qs.start_candidates(src, ctx, season, week))
            t, r = qs.realized_and_recent(src, ctx, season, week, wk["gsis_id"].to_list())
            tp.append(t)
            rp.append(r)
        cands, teams, recent = pl.concat(cp), pl.concat(tp), pl.concat(rp)
        for df, p in ((cands, cpath), (teams, tpath), (recent, rpath)):
            df.write_parquet(p)
        meta.write_text(json.dumps({"fingerprint": fp, "weeks": len(weeks), "candidates": cands.height}))
    return st, cands, {**prov, "p2_build_fingerprint": fp}


def with_scenarios(args, st, cands, weeks) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Walk-forward start probabilities for `weeks` and the store with scenario columns (training
    columns on every row, forecast columns on the rows of `weeks`)."""
    teams = pl.read_parquet(_out(args) / "teams.parquet")
    recent = pl.read_parquet(_out(args) / "recent.parquet")
    pc, _ = qs.walk_forward_start(cands, weeks)
    return qs.scenario_columns(st, pc, teams, recent), pc


# ----------------------------------------------------------------------------- decomposition


def _cells(p: pl.DataFrame, models: list[str], by: list[str]) -> list[dict]:
    """Additive bias decomposition: a cell's contribution is sum(actual - pred) / N(window), so the
    contributions sum to the window's bias; same for MAE."""
    n = p.height
    aggs = [pl.len().alias("n"), pl.col("label").mean().alias("mean_points")]
    for m in models:
        r = pl.col("label") - pl.col(f"pred_{m}")
        aggs += [r.mean().alias(f"bias_{m}"), (r.sum() / n).alias(f"bias_contrib_{m}"),
                 r.abs().mean().alias(f"mae_{m}"), (r.abs().sum() / n).alias(f"mae_contrib_{m}")]
    return p.group_by(by).agg(aggs).sort(by).with_columns(pl.col(pl.Float64).round(4)).to_dicts()


def qb_cells(p: pl.DataFrame, models: list[str]) -> dict:
    """QB rows of one window, by the cells that explain p1's bias."""
    q = p.filter((pl.col("position") == "QB") & pl.col("label").is_not_null()
                 & pl.all_horizontal([pl.col(f"pred_{m}").is_not_null() for m in models]))
    q = q.with_columns(
        (pl.col("gsis_id") == pl.col("actual_qb_id")).fill_null(False).alias("started"),
        pl.when(pl.col("week") == 1).then(pl.lit("w1")).when(pl.col("week") <= 4).then(pl.lit("w2-4"))
          .otherwise(pl.lit("w5+")).alias("phase"),
        pl.when(pl.col("qb_is_expected").is_null()).then(pl.lit("unknown"))
          .when(pl.col("qb_is_expected") == 1).then(pl.lit("expected")).otherwise(pl.lit("not_expected"))
          .alias("p1_role"))
    out = {"n": q.height, "overall": _cells(q.with_columns(pl.lit("all").alias("all")), models, ["all"])[0]}
    for name, by in (("by_p1_class", ["qb_confidence"]), ("by_phase", ["phase"]), ("by_season", ["season"]),
                     ("by_p1_role_started", ["p1_role", "started"]),
                     ("by_phase_role_started", ["phase", "p1_role", "started"])):
        out[name] = _cells(q, models, by)
    return out


def cmd_decompose(args) -> dict:
    st, prov = store(args)
    cfg = ch.ChallengerConfig.load()
    out = {**prov, "windows": {}}
    for name, seasons in WINDOWS.items():
        weeks = ch.window_weeks(st, seasons)
        p = ctxr._preds(st, weeks, cfg)
        out["windows"][name] = qb_cells(p, ["ifp2", "A"])
    (_out(args) / "decomposition.json").write_text(json.dumps(out, indent=1, default=str))
    summary = {}
    for name, w in out["windows"].items():
        o = w["overall"]
        summary[name] = {"bias_ifp2": o["bias_ifp2"], "bias_A": o["bias_A"],
                         "delta_by_phase": {c["phase"]: round(c["bias_contrib_A"] - c["bias_contrib_ifp2"], 3)
                                            for c in w["by_phase"]},
                         "delta_by_role": {f"{c['p1_role']}/{'started' if c['started'] else 'not_started'}":
                                           round(c["bias_contrib_A"] - c["bias_contrib_ifp2"], 3)
                                           for c in w["by_p1_role_started"]}}
    return summary


# ----------------------------------------------------------------------------- calibration


RELIABILITY_BINS = [0.0, 0.02, 0.1, 0.3, 0.5, 0.7, 0.9, 0.98, 1.0001]


def _reliability(prob: np.ndarray, y: np.ndarray) -> list[dict]:
    out = []
    for lo, hi in zip(RELIABILITY_BINS[:-1], RELIABILITY_BINS[1:]):
        m = (prob >= lo) & (prob < hi)
        if m.any():
            out.append({"bin": f"[{lo:.2f}, {min(hi, 1.0):.2f}{']' if hi > 1 else ')'}", "n": int(m.sum()),
                        "mean_p": round(float(prob[m].mean()), 4), "observed": round(float(y[m].mean()), 4)})
    return out


def p1_class_baseline(cands: pl.DataFrame, pc: pl.DataFrame) -> pl.DataFrame:
    """p1's evidence classes turned into probabilities walk-forward: the expected QB gets his class's
    start rate in strictly earlier team-weeks, the rest is split equally over the other candidates;
    no expected QB (unknown) -> uniform. The fair 'calibrated p1' comparator."""
    tw = (cands.filter(pl.col("started").is_not_null() & (pl.col("p1_expected") == 1))
          .group_by("key", "p1_confidence").agg(pl.col("started").sum().alias("hit"), pl.len().alias("n")))
    out = []
    for k in sorted(pc["key"].unique().to_list()):
        prior = tw.filter((pl.col("key") < k) & (pl.col("key") >= qs.START_TRAIN_START * 100)).group_by(
            "p1_confidence").agg(pl.col("hit").sum() / pl.col("n").sum())
        rate = dict(prior.iter_rows())
        g = pc.filter(pl.col("key") == k)
        g = g.with_columns(pl.len().over("key", "team").alias("_n"),
                           pl.col("p1_expected").max().over("key", "team").alias("_has"),
                           pl.col("p1_confidence").replace_strict(rate, default=None, return_dtype=pl.Float64)
                           .alias("_r"))
        out.append(g.with_columns(
            pl.when(pl.col("_has") == 0).then(1.0 / pl.col("_n"))
              .when(pl.col("p1_expected") == 1).then(pl.col("_r"))
              .when(pl.col("_n") > 1).then((1.0 - pl.col("_r").max().over("key", "team")) / (pl.col("_n") - 1))
              .otherwise(0.0).alias("p_p1")).drop("_n", "_has", "_r"))
    return pl.concat(out)


def _team_week_scores(pc: pl.DataFrame, col: str) -> dict:
    """Multi-class Brier / log loss per team-week (a realized starter outside the candidate set
    counts as probability 0 on the truth), top-1 accuracy, plus the candidate-level reliability."""
    g = pc.filter(pl.col("started").is_not_null())
    tw = g.group_by("key", "team").agg(
        ((pl.col(col) - pl.col("started")) ** 2).sum().alias("sq"),
        (pl.col(col) * pl.col("started")).sum().alias("p_true"), pl.col("started").sum().alias("covered"),
        pl.col("started").sort_by(col, descending=True).first().alias("top_hit"))
    brier = (tw["sq"] + (1 - tw["covered"])).to_numpy()
    p_true = np.clip(tw["p_true"].to_numpy(), 1e-4, 1.0)
    return {"team_weeks": tw.height, "coverage": round(float(tw["covered"].mean()), 4),
            "top1_accuracy": round(float(tw["top_hit"].mean()), 4), "brier": round(float(brier.mean()), 4),
            "log_loss": round(float(-np.log(p_true).mean()), 4),
            "reliability": _reliability(g[col].to_numpy(), g["started"].to_numpy())}


def cmd_calibration(args) -> dict:
    st, cands, prov = build(args)
    weeks = ch.window_weeks(st, [s for w in WINDOWS.values() for s in w])
    _, pc = with_scenarios(args, st, cands, weeks)
    pc = p1_class_baseline(cands, pc).with_columns(
        pl.col("season").replace_strict({s: n for n, ss in WINDOWS.items() for s in ss}, default=None).alias("window"),
        (pl.col("p1_expected") == 1).cast(pl.Float64).alias("p_p1_hard"))
    out = {**prov, "start_model": {"version": qs.START_MODEL_VERSION, "features": qs.START_FEATURES,
                                   "l2": qs.START_L2}, "windows": {}}
    for name in [*WINDOWS, "all"]:
        g = pc if name == "all" else pc.filter(pl.col("window") == name)
        r = {m: _team_week_scores(g, c) for m, c in (("p2", "p_start"), ("p1_class_rates", "p_p1"),
                                                    ("p1_hard", "p_p1_hard"))}
        fg = g.filter(pl.col("first_game"))
        r["first_game_only"] = {m: {k: v for k, v in _team_week_scores(fg, c).items() if k != "reliability"}
                                for m, c in (("p2", "p_start"), ("p1_class_rates", "p_p1"))} if fg.height else {}
        # by p1 class: p2's mean probability on p1's expected QB vs how often he started
        ex = g.filter((pl.col("p1_expected") == 1) & pl.col("started").is_not_null())
        r["p1_expected_by_class"] = ex.group_by("p1_confidence").agg(
            pl.len().alias("n"), pl.col("p_start").mean().round(4).alias("p2_mean_p"),
            pl.col("p_p1").mean().round(4).alias("p1_class_rate"),
            pl.col("started").mean().round(4).alias("observed")).sort("p1_confidence").to_dicts()
        out["windows"][name] = r
    # pi = P(start | appears) on the label population (QBs who appeared)
    ap = pc.filter((pl.col("appeared") == 1) & pl.col("started").is_not_null())
    out["pi_given_appearance"] = {name: {"n": g.height, "brier": round(float(((g["pi_start"] - g["started"]) ** 2)
                                                                           .mean()), 4),
                                         "reliability": _reliability(g["pi_start"].to_numpy(), g["started"].to_numpy())}
                                  for name, g in [(n, ap.filter(pl.col("window") == n)) for n in WINDOWS]
                                  + [("all", ap)]}
    (_out(args) / "calibration.json").write_text(json.dumps(out, indent=1, default=str))
    return {n: {m: {k: v for k, v in r[m].items() if k != "reliability"} for m in ("p2", "p1_class_rates", "p1_hard")}
            for n, r in out["windows"].items()}


# ----------------------------------------------------------------------------- points backtests


def _preds(args, st, cands, weeks, cfg, per_position=None) -> pl.DataFrame:
    """ifp2, p1's A-D (context_challenger.walk_forward) and p2's S2 / SC2 / S2h (+ ctx2) on the same rows."""
    sc, _ = with_scenarios(args, st, cands, weeks)
    p1 = cc.walk_forward(st, weeks, cfg, per_position=P1_SPEC)
    p2 = qs.walk_forward_p2(sc, weeks, cfg, per_position=per_position)
    rows = sc.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks]))
    clash = (set(p1.columns) | set(p2.columns)) - {"key", "gsis_id"} & set(rows.columns)
    assert not clash, f"prediction columns collide with store columns: {clash}"   # e.g. the store's pred_A2
    return rows.join(p1.rename({"pred_ctx": "pred_p1"}), on=["key", "gsis_id"]).join(p2, on=["key", "gsis_id"])


def _frozen_path(args) -> Path:
    return _out(args) / f"frozen_selection_{qs.P2_VERSION}.json"


def cmd_develop(args) -> dict:
    st, cands, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    weeks = ch.window_weeks(st, WINDOWS["development"])
    dev = _preds(args, st, cands, weeks, cfg)
    models = ["ifp2", "A", "C", "p1", *qs.P2_CANDIDATES, *qs.ABLATIONS]
    exact = ctxr.report(dev, models, exact=True)
    chosen, why = {}, {}
    for pos in POSITIONS:
        r = exact[pos]
        elig = {}
        for cand in qs.P2_CANDIDATES:
            d = r["minus_ifp2"][cand]
            bias_ok = abs(r["metrics"][cand]["bias"]) <= abs(r["metrics"]["ifp2"]["bias"]) + cc.BIAS_TOLERANCE
            if d["diff"] <= cc.SELECTION_MAX_DIFF and d["ci95"][1] < 0 and bias_ok:
                elig[cand] = r["metrics"][cand]["mae"]
        chosen[pos] = min(elig, key=lambda c: (elig[c], c)) if elig else "ifp2"
        why[pos] = {"eligible": sorted(elig), "diffs": {c: r["minus_ifp2"][c] for c in qs.P2_CANDIDATES},
                    "bias": {m: r["metrics"][m]["bias"] for m in ["ifp2", *qs.P2_CANDIDATES]}}
    frozen = {"version": qs.P2_VERSION, "status": qs.P2_STATUS, "selected_on": WINDOWS["development"],
              "per_position": chosen, "candidates": qs.P2_CANDIDATES, "start_model": qs.START_MODEL_VERSION,
              "start_features": qs.START_FEATURES, "rules": qs.P2_RULES, "ifp2_config_hash": cfg.hash(),
              "code_hash": _code_hash(), "scoring_fingerprint": prov["scoring_fingerprint"]}
    frozen["config_hash"] = hashlib.sha256(json.dumps({k: v for k, v in frozen.items() if k != "rules"},
                                                      sort_keys=True, default=str).encode()).hexdigest()[:16]
    frozen["frozen_at"] = datetime.now(timezone.utc).isoformat()
    path = _frozen_path(args)
    if not hr._write_once(path, frozen):
        prev = json.loads(path.read_text())
        if prev["config_hash"] != frozen["config_hash"]:
            raise SystemExit(f"{path} already frozen with a different selection ({prev['per_position']})")
    rep = ctxr.report(dev, models)
    out = {"report": rep, "selection": why, "qb_cells": qb_cells(dev, ["ifp2", "A", "S2", "S2h"]), **prov}
    (_out(args) / "development.json").write_text(json.dumps(out, indent=1, default=str))
    return {"per_position": chosen, "selection": why}


def verdict(r: dict, p: pl.DataFrame, chosen: dict) -> dict:
    """p1's confirmation checks (context_research.verdict) applied to the p2 composite pred_ctx2."""
    adopted = [pos for pos, c in chosen.items() if c != "ifp2"]
    if not adopted:
        return {"adopted_positions": [], "outcome": "retain_baseline", "reason": "nothing adopted on development"}
    sub = {pos: r[pos] for pos in adopted}
    rows = p.filter(pl.col("label").is_not_null() & pl.col("position").is_in(adopted)
                    & pl.col("pred_ctx2").is_not_null() & pl.col("pred_ifp2").is_not_null())
    pooled = week_bootstrap_mae_diff(rows, "pred_ctx2", "pred_ifp2", 1000, SEED, exact=True)
    a = _arr(rows, "label")
    rmse = {m: metrics(a, _arr(rows, f"pred_{m}"), exact=True)["rmse"] for m in ("ctx2", "ifp2")}
    pairs = pair_decisions(rows, ["pred_ctx2", "pred_ifp2"], exact=True)["all"]
    checks = {
        "c1_pooled_mae": pooled["diff"] < 0 and pooled["ci95"][1] < 0,
        "c2_no_position_significantly_worse": all(s["minus_ifp2"]["ctx2"]["ci95"][0] <= 0 for s in sub.values()),
        "c3_rmse": rmse["ctx2"] <= rmse["ifp2"] * 1.005,
        "c4_bias": all(abs(s["metrics"]["ctx2"]["bias"]) <= abs(s["metrics"]["ifp2"]["bias"]) + cc.BIAS_TOLERANCE
                       for s in sub.values()),
        "c5_starters_pool": all(s["cohorts"]["starters_pool"]["ctx2"]["mae"]
                                <= s["cohorts"]["starters_pool"]["ifp2"]["mae"] * 1.01 for s in sub.values()),
        "c6_pairs": pairs["pred_ctx2"]["accuracy"] >= pairs["pred_ifp2"]["accuracy"] - 0.005,
    }
    mech = {}
    for pos in adopted:
        for g in qs.P2_CANDIDATES[chosen[pos]]:
            for c in cc.MECHANISM_COHORTS[qs.MECHANISM_GROUPS[g]]:
                co = r[pos]["cohorts"].get(c, {})
                if co.get("n"):
                    mech[f"{pos}:{c}"] = co["ctx2"]["mae"] - co["ifp2"]["mae"]
    checks["c7_mechanism"] = bool(mech) and all(v < 0 for v in mech.values())
    return {"adopted_positions": adopted, "pooled_adopted": pooled, "pooled_adopted_rmse": rmse,
            "pairs_adopted": {m: pairs[f"pred_{m}"] for m in ("ctx2", "ifp2")}, "checks": checks,
            "mechanism_diffs": mech, "outcome": "prospective_shadow" if all(checks.values()) else "retain_baseline"}


def bias_guards(r: dict, p: pl.DataFrame, models: list[str]) -> dict:
    """Reported, not gating: the |bias| guard per position for every model, overall and weeks 1-4."""
    out = {}
    early = p.filter((pl.col("week") <= 4) & pl.col("label").is_not_null())
    for pos in POSITIONS:
        e = early.filter((pl.col("position") == pos) & pl.all_horizontal(
            [pl.col(f"pred_{m}").is_not_null() for m in ["ifp2", *models]]))
        eb = {m: metrics(_arr(e, "label"), _arr(e, f"pred_{m}"), exact=True)["bias"] for m in ["ifp2", *models]}
        out[pos] = {m: {"bias": r[pos]["metrics"][m]["bias"],
                        "passes": abs(r[pos]["metrics"][m]["bias"]) <= abs(r[pos]["metrics"]["ifp2"]["bias"])
                        + cc.BIAS_TOLERANCE,
                        "early_bias": eb[m], "early_passes": eb[m] is not None
                        and abs(eb[m]) <= abs(eb["ifp2"]) + cc.BIAS_TOLERANCE}
                    for m in models}
        out[pos]["ifp2"] = {"bias": r[pos]["metrics"]["ifp2"]["bias"], "early_bias": eb["ifp2"]}
    return out


def cmd_evaluate(args) -> dict:
    st, cands, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    frozen = json.loads(_frozen_path(args).read_text())
    if frozen["ifp2_config_hash"] != cfg.hash() or frozen["scoring_fingerprint"] != prov["scoring_fingerprint"] \
            or frozen["code_hash"] != _code_hash():
        raise SystemExit("frozen p2 selection was made against a different ifp-2 config, scoring or p2 code")
    chosen = frozen["per_position"]
    models = ["ifp2", "A", "C", "p1", *qs.P2_CANDIDATES, *qs.ABLATIONS, "ctx2"]
    out = {"frozen": {k: frozen[k] for k in ("per_position", "config_hash", "frozen_at")}, **prov}
    for name in ("confirmation", "exploratory"):
        weeks = ch.window_weeks(st, WINDOWS[name])
        p = _preds(args, st, cands, weeks, cfg, per_position=chosen)
        p.select("key", "gsis_id", "position", "label", *[f"pred_{m}" for m in models]).write_parquet(
            _out(args) / f"{name}_preds.parquet")
        exact = ctxr.report(p, models, exact=True)
        out[name] = ctxr.report(p, models)
        out[name]["qb_cells"] = qb_cells(p, ["ifp2", "A", "S2", "S2h"])
        out[name]["bias_guards"] = bias_guards(exact, p, ["A", "C", "p1", *qs.P2_CANDIDATES, *qs.ABLATIONS, "ctx2"])
        if name == "confirmation":
            out["verdict"] = verdict(exact, p, chosen)
        else:
            out["exploratory_by_season"] = {int(s): ctxr.report(p.filter(pl.col("season") == s), models)["ALL"]
                                            for s in WINDOWS[name] if p.filter(pl.col("season") == s).height}
    dev = json.loads((_out(args) / "development.json").read_text())
    out["development"] = dev["report"]
    (_out(args) / "evaluation.json").write_text(json.dumps(out, indent=1, default=str))
    return {"verdict": out["verdict"], "confirmation_pooled": out["confirmation"]["ALL"]["minus_ifp2"],
            "exploratory_pooled": out["exploratory"]["ALL"]["minus_ifp2"]}


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["decompose", "build", "calibration", "develop", "evaluate"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args(argv)
    cmds = {"decompose": cmd_decompose, "build": lambda a: build(a)[2], "calibration": cmd_calibration,
            "develop": cmd_develop, "evaluate": cmd_evaluate}
    print(json.dumps(cmds[args.command](args), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
