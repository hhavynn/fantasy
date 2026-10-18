"""Research: returning-teammate challenger (ifp-ret, research only; see
docs/research/qb_teammate_context.md, section 12). Offline, read-only on both databases.

    cd backend
    uv run python ../scripts/returning_research.py build      # store + returning features, cohort sizes
    uv run python ../scripts/returning_research.py develop    # 2021-22 only -> write-once frozen selection
    uv run python ../scripts/returning_research.py evaluate   # frozen selection: 2023-24 (once) + 2025-26 replay

Reuses the ifp-ctx feature store builder (context_research.build) into its OWN cache directory, so the
ifp-ctx / ifp-ctx-p1 outputs are never touched. Outputs: <ifp-dir>/research/returning/ (git-ignored).
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

import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import context_challenger as cc  # noqa: E402
from app.analytics import returning_context as rt  # noqa: E402
from app.analytics.forecast_backtest import _arr, metrics, pair_decisions, week_bootstrap_mae_diff  # noqa: E402
from app.analytics.forecast_features import POSITIONS  # noqa: E402
from app.config import get_settings  # noqa: E402

SEED = 20261017


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "returning"
    p.mkdir(parents=True, exist_ok=True)
    return p


def build(args) -> tuple[pl.DataFrame, dict]:
    """The ifp-ctx store (ifp-2 features + ctx columns, same fully-final weeks) plus the returning
    columns, cached by the ctx store's fingerprint (which already covers all code and sources)."""
    work = SimpleNamespace(**{**vars(args), "ifp_dir": _out(args) / "work"})
    store, prov = ctxr.build(work)
    path, meta = _out(args) / "store.parquet", _out(args) / "store.meta.json"
    if path.exists() and meta.exists() and not args.rebuild and json.loads(meta.read_text())["fingerprint"] == prov["fingerprint"]:
        return pl.read_parquet(path), prov
    t = time.time()
    src, _ = hr._sources(args)
    parts = []
    for (season, week), rows in store.group_by("season", "week", maintain_order=True):
        parts.append(rt.returning_for_week(src, int(season), int(week), rows.select("gsis_id", "team"))
                     .with_columns(pl.lit(int(season) * 100 + int(week)).alias("key")))
    store = store.join(pl.concat(parts), on=["key", "gsis_id"], how="left")
    store.write_parquet(path)
    meta.write_text(json.dumps({"fingerprint": prov["fingerprint"], "rows": store.height,
                                "build_seconds": round(time.time() - t, 1)}))
    return store, prov


def cmd_build(args) -> dict:
    store, prov = build(args)
    s = store.filter(pl.col("season") >= ch.TRAIN_START)
    out = {"rows": s.height, **prov, "missing_rate_by_season": {}, "cohort_sizes": {}}
    for season, g in s.group_by("season", maintain_order=True):
        out["missing_rate_by_season"][int(season[0])] = {c: round(float(g[c].is_null().mean()), 4)
                                                         for c in rt.FEATURE_COLUMNS}
    for pos in [*POSITIONS, "ALL"]:
        g = s if pos == "ALL" else s.filter(pl.col("position") == pos)
        out["cohort_sizes"][pos] = {c: int(g.filter(e.fill_null(False)).height) for c, e in rt.cohorts().items()
                                    if c != "starters_pool"}
    ret = s.filter(rt.cohorts()["ret_any"].fill_null(False))
    out["overlap_with_ctx_top_tgt_competitor_returning"] = {
        "ret_any": ret.height, "also_top_tgt_comp_return": int((ret["top_tgt_comp_return"] == 1).sum()),
        "top_tgt_comp_return": int((s["top_tgt_comp_return"] == 1).sum())}
    (_out(args) / "build_report.json").write_text(json.dumps(out, indent=1, default=str))
    return out


# ----------------------------------------------------------------------------- comparison


def report(p: pl.DataFrame, models: list[str], exact: bool = False) -> dict:
    """Identical-row comparison of every model against ifp2, per position and pooled, with cohorts."""
    lab = p.filter(pl.col("label").is_not_null())
    cov = {m: round(float(lab[f"pred_{m}"].is_not_null().mean()), 4) for m in models}
    p = lab.filter(pl.all_horizontal([pl.col(f"pred_{m}").is_not_null() for m in models]))
    out = {"population": {"labeled": lab.height, "common": p.height, "coverage": cov,
                          "weeks": p.select("key").n_unique()}}
    flt = rt.cohorts()
    for pos in [*POSITIONS, "ALL"]:
        g = p if pos == "ALL" else p.filter(pl.col("position") == pos)
        r = {"n": g.height, "metrics": {m: metrics(_arr(g, "label"), _arr(g, f"pred_{m}"), exact) for m in models},
             "minus_ifp2": {m: week_bootstrap_mae_diff(g, f"pred_{m}", "pred_ifp2", 1000, SEED, exact)
                            for m in models if m != "ifp2"},
             "cohorts": {}}
        for c, e in flt.items():
            cg = g.filter(e.fill_null(False))
            rr = {"n": cg.height, **{m: metrics(_arr(cg, "label"), _arr(cg, f"pred_{m}"), exact) for m in models}}
            if cg.height >= 30 and cg.select("key").n_unique() >= 5:
                rr["minus_ifp2"] = {m: week_bootstrap_mae_diff(cg, f"pred_{m}", "pred_ifp2", 1000, SEED, exact)
                                    for m in models if m != "ifp2"}
            r["cohorts"][c] = rr
        out[pos] = r
    out["pairs"] = pair_decisions(p, [f"pred_{m}" for m in models], exact=exact)
    return out


def _preds(store: pl.DataFrame, weeks, cfg, per_position=None) -> pl.DataFrame:
    """pred_ifp2, pred_R (+ pred_ret) and the descriptive ifp-ctx comparator pred_B on the same rows."""
    t = time.time()
    preds = rt.walk_forward(store, weeks, cfg, per_position=per_position)
    b = cc.walk_forward(store, weeks, cfg, candidates=("B",)).rename({"pred_ifp2": "_ifp2_cc"})
    preds = preds.join(b, on=["key", "gsis_id"], how="left")
    # both walk-forwards fit ifp-2 itself: they must agree exactly
    d = preds.filter(pl.col("pred_ifp2").is_not_null() | pl.col("_ifp2_cc").is_not_null())
    assert d.filter((pl.col("pred_ifp2") - pl.col("_ifp2_cc")).abs().fill_null(1.0) > 1e-9).height == 0
    print(f"walk-forward {len(weeks)} weeks: {time.time() - t:.0f}s", file=sys.stderr)
    return (store.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks]))
            .join(preds.drop("_ifp2_cc"), on=["key", "gsis_id"]))


def _frozen_path(args) -> Path:
    return _out(args) / f"frozen_selection_{rt.RET_CHALLENGER_VERSION}.json"


def cmd_develop(args) -> dict:
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    dev = _preds(store, ch.window_weeks(store, rt.WINDOWS["development"]), cfg)
    models = ["ifp2", rt.CANDIDATE, "B"]
    exact = report(dev, models, exact=True)
    chosen, why = {}, {}
    for pos in POSITIONS:
        r = exact[pos]
        d = r["minus_ifp2"][rt.CANDIDATE]
        bias_ok = abs(r["metrics"][rt.CANDIDATE]["bias"]) <= abs(r["metrics"]["ifp2"]["bias"]) + rt.BIAS_TOLERANCE
        ok = d["diff"] <= rt.SELECTION_MAX_DIFF and d["ci95"][1] < 0 and bias_ok
        chosen[pos] = rt.CANDIDATE if ok else "ifp2"
        why[pos] = {"diff": d, "bias_ok": bias_ok}
    frozen = {"version": rt.RET_CHALLENGER_VERSION, "status": rt.RET_STATUS, "selected_on": rt.WINDOWS["development"],
              "per_position": chosen, "group": rt.GROUP, "rules": rt.DECISION_RULES, "ifp2_config_hash": cfg.hash(),
              "feature_version": rt.RET_VERSION, "scoring_fingerprint": prov["scoring_fingerprint"],
              "store_fingerprint": prov["fingerprint"]}
    frozen["config_hash"] = hashlib.sha256(json.dumps({k: v for k, v in frozen.items() if k != "rules"},
                                                      sort_keys=True, default=str).encode()).hexdigest()[:16]
    frozen["frozen_at"] = datetime.now(timezone.utc).isoformat()
    path = _frozen_path(args)
    if not hr._write_once(path, frozen):
        prev = json.loads(path.read_text())
        if prev["per_position"] != chosen:
            raise SystemExit(f"{path} already frozen with a different selection ({prev['per_position']})")
    rep = report(dev, models)
    (_out(args) / "development.json").write_text(json.dumps({"report": rep, "selection": why, **prov}, indent=1,
                                                            default=str))
    return {"per_position": chosen, "selection": why, "pooled_minus_ifp2": rep["ALL"]["minus_ifp2"]}


def verdict(r: dict, p: pl.DataFrame, chosen: dict) -> dict:
    """The pre-registered confirmation rules (rt.DECISION_RULES), on unrounded values."""
    adopted = [pos for pos, c in chosen.items() if c != "ifp2"]
    if not adopted:
        return {"adopted_positions": [], "outcome": "retain_baseline", "reason": "nothing adopted on development"}
    sub = {pos: r[pos] for pos in adopted}
    rows = p.filter(pl.col("label").is_not_null() & pl.col("position").is_in(adopted)
                    & pl.col("pred_ret").is_not_null() & pl.col("pred_ifp2").is_not_null())
    pooled = week_bootstrap_mae_diff(rows, "pred_ret", "pred_ifp2", 1000, SEED, exact=True)
    a = _arr(rows, "label")
    rmse = {m: metrics(a, _arr(rows, f"pred_{m}"), exact=True)["rmse"] for m in ("ret", "ifp2")}
    pairs = pair_decisions(rows, ["pred_ret", "pred_ifp2"], exact=True)["all"]
    checks = {
        "c1_pooled_mae": pooled["diff"] < 0 and pooled["ci95"][1] < 0,
        "c2_no_position_significantly_worse": all(s["minus_ifp2"]["ret"]["ci95"][0] <= 0 for s in sub.values()),
        "c3_rmse": rmse["ret"] <= rmse["ifp2"] * 1.005,
        "c4_bias": all(abs(s["metrics"]["ret"]["bias"]) <= abs(s["metrics"]["ifp2"]["bias"]) + rt.BIAS_TOLERANCE
                       for s in sub.values()),
        "c5_starters_pool": all(s["cohorts"]["starters_pool"]["ret"]["mae"]
                                <= s["cohorts"]["starters_pool"]["ifp2"]["mae"] * 1.01 for s in sub.values()),
        "c6_pairs": pairs["pred_ret"]["accuracy"] >= pairs["pred_ifp2"]["accuracy"] - 0.005,
    }
    mech = {f"{pos}:{rt.MECHANISM_COHORT}": r[pos]["cohorts"][rt.MECHANISM_COHORT]["ret"]["mae"]
            - r[pos]["cohorts"][rt.MECHANISM_COHORT]["ifp2"]["mae"]
            for pos in adopted if r[pos]["cohorts"][rt.MECHANISM_COHORT]["n"]}
    checks["c7_mechanism"] = bool(mech) and all(v < 0 for v in mech.values())
    return {"adopted_positions": adopted, "pooled_adopted": pooled, "pooled_adopted_rmse": rmse,
            "pairs_adopted": {m: pairs[f"pred_{m}"] for m in ("ret", "ifp2")}, "checks": checks,
            "mechanism_diffs": mech,
            "outcome": "prospective_shadow" if all(checks.values()) else "retain_baseline"}


def cmd_evaluate(args) -> dict:
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    frozen = json.loads(_frozen_path(args).read_text())
    if frozen["ifp2_config_hash"] != cfg.hash() or frozen["scoring_fingerprint"] != prov["scoring_fingerprint"]:
        raise SystemExit("frozen selection was made against a different ifp-2 config or league scoring")
    chosen = frozen["per_position"]
    models = ["ifp2", rt.CANDIDATE, "B", "ret"]
    out = {"frozen": {k: frozen[k] for k in ("per_position", "config_hash", "frozen_at")}, **prov}
    for name, seasons in (("confirmation", rt.WINDOWS["confirmation"]), ("exploratory", rt.WINDOWS["exploratory"])):
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
    return {"verdict": out["verdict"], "confirmation_pooled": out["confirmation"]["ALL"]["minus_ifp2"],
            "exploratory_pooled": out["exploratory"]["ALL"]["minus_ifp2"]}


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["build", "develop", "evaluate"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args(argv)
    cmds = {"build": cmd_build, "develop": cmd_develop, "evaluate": cmd_evaluate}
    print(json.dumps(cmds[args.command](args), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
