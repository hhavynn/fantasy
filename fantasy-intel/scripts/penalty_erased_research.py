"""Research: penalty-erased targets and carries (ifp-pe, research only; see
docs/research/penalty_erased_opportunities.md). Offline, read-only on both databases.

    cd backend
    uv run python ../scripts/penalty_erased_research.py identify   # parse no_play snaps, id validation report
    uv run python ../scripts/penalty_erased_research.py build      # ifp-2 store + erased-opportunity features
    uv run python ../scripts/penalty_erased_research.py develop    # 2021-22 only -> write-once frozen selection
    uv run python ../scripts/penalty_erased_research.py evaluate   # frozen selection: 2023-24 (once) + 2025-26 replay

Play-by-play comes from nflverse (nflreadpy.load_pbp), cached per season under the output directory so
a rerun uses the same plays (--refresh-pbp re-downloads). Builds the ifp-2 feature store with the HGB
study's builder into its OWN cache directory. Outputs: <ifp-dir>/research/penalty_erased/ (git-ignored).
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
import hgb_research as hr  # noqa: E402

import duckdb  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import penalty_erased as pe  # noqa: E402
from app.analytics.forecast_backtest import (_arr, espn_pregame, metrics, pair_decisions,  # noqa: E402
                                             week_bootstrap_mae_diff)
from app.analytics.forecast_features import POSITIONS  # noqa: E402
from app.analytics.independent_projections import baseline_predictions  # noqa: E402
from app.config import get_settings  # noqa: E402

SEED = 20261018
FIRST_SEASON = ch.TRAIN_START - 1      # 2019: the feature window of 2020 week 1 reaches back one season


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "penalty_erased"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ----------------------------------------------------------------------------- play-by-play + identification


def load_pbp(args, season: int) -> pl.DataFrame:
    path = _out(args) / "pbp" / f"{season}.parquet"
    if not path.exists() or args.refresh_pbp:
        import nflreadpy as nfl

        path.parent.mkdir(parents=True, exist_ok=True)
        nfl.load_pbp([season]).write_parquet(path)
    return pl.read_parquet(path)


def rosters(args) -> pl.DataFrame:
    parts = []
    for db in (args.history_db, args.live_db):
        if db.exists():
            con = duckdb.connect(str(db), read_only=True)
            try:
                parts.append(pl.from_arrow(con.execute(
                    "SELECT season, week, team, jersey_number, gsis_id, last_name FROM nfl_rosters_weekly").arrow()))
            finally:
                con.close()
    # a season present in both databases is kept from the live one (as forecast_features.Sources.concat)
    live_seasons = set(parts[-1]["season"].unique().to_list()) if len(parts) == 2 else set()
    return pl.concat([parts[0].filter(~pl.col("season").is_in(sorted(live_seasons))), *parts[1:]]) if parts else \
        pl.DataFrame()


def plays(args) -> tuple[pl.DataFrame, pl.DataFrame, str]:
    """Erased plays and official team air yards for FIRST_SEASON..season, cached by the pbp files'
    and the module's hashes."""
    seasons = list(range(FIRST_SEASON, args.season + 1))
    pbp_paths = []
    for s in seasons:
        load_pbp(args, s)
        pbp_paths.append(_out(args) / "pbp" / f"{s}.parquet")
    ros = rosters(args)
    fp = hashlib.sha256(json.dumps({"pbp": [_hash_file(p) for p in pbp_paths], "module": _hash_file(Path(pe.__file__)),
                                    "rosters": hr._frame_hash(ros)}, sort_keys=True).encode()).hexdigest()[:16]
    path, air_path, meta = (_out(args) / "plays.parquet", _out(args) / "team_air.parquet",
                            _out(args) / "plays.meta.json")
    if path.exists() and air_path.exists() and meta.exists() and json.loads(meta.read_text())["fingerprint"] == fp:
        return pl.read_parquet(path), pl.read_parquet(air_path), fp
    parts, air = [], []
    for s, pth in zip(seasons, pbp_paths):
        p = pl.read_parquet(pth)
        parts.append(pe.derive(p, ros.filter(pl.col("season") == s)))
        air.append(pe.team_air(p))
    out, air_df = pl.concat(parts), pl.concat(air)
    out.write_parquet(path)
    air_df.write_parquet(air_path)
    meta.write_text(json.dumps({"fingerprint": fp, "plays": out.height}))
    return out, air_df, fp


def cmd_identify(args) -> dict:
    pl_, _, fp = plays(args)
    ros = rosters(args)
    rep = {"fingerprint": fp, "identification": pe.identification_report(pl_), "validation_on_official": {},
           "dpi_coverage": {}}
    for s in range(FIRST_SEASON, args.season + 1):
        p = load_pbp(args, s)
        rep["validation_on_official"][s] = pe.validate_on_official(p, ros.filter(pl.col("season") == s))
        reg = p.filter((pl.col("season_type") == "REG") & (pl.col("play_type") == "no_play")
                       & pl.col("desc").str.contains(pe.DPI, literal=True)
                       & ~pl.col("desc").str.contains("TWO-POINT CONVERSION ATTEMPT", literal=True))
        got = pl_.filter((pl.col("season") == s) & (pl.col("kind") == "target") & (pl.col("dpi") == 1))
        rep["dpi_coverage"][s] = {"no_play_dpi_snaps": reg.height, "parsed_dpi_targets": got.height}
    sample = (pl_.filter(pl.col("kind").is_in(["target", "carry"])).sample(40, seed=SEED)
              .select("season", "week", "team", "kind", "jersey", "name", "gsis_id", "dpi", "penalty_side",
                      "air_proxy", "desc"))
    rep["spot_check_sample"] = sample.to_dicts()
    (_out(args) / "identification.json").write_text(json.dumps(rep, indent=1, default=str))
    v = rep["validation_on_official"]
    return {"identification": {k: rep["identification"][k] for k in ("opportunities", "confident_rate", "by_status")},
            "validation": {s: {k: v[s][k] for k in ("n", "combined_precision", "combined_recall")} for s in v},
            "dpi_coverage": rep["dpi_coverage"]}


# ----------------------------------------------------------------------------- store


def build(args) -> tuple[pl.DataFrame, dict]:
    """The ifp-2 feature store (HGB study builder, same fully-final weeks, in this study's own cache)
    plus the penalty-erased columns, cached by both fingerprints."""
    src, scoring = hr._sources(args)
    work = SimpleNamespace(**{**vars(args), "ifp_dir": _out(args) / "work"})
    store = hr.full_store(work, src, scoring.fingerprint)
    base_fp = hr.store_fingerprint(work)
    erased, air, plays_fp = plays(args)
    fp = hashlib.sha256(f"{base_fp}:{plays_fp}".encode()).hexdigest()[:16]
    prov = {"fingerprint": fp, "base_store_fingerprint": base_fp, "plays_fingerprint": plays_fp,
            "scoring_fingerprint": scoring.fingerprint}
    path, meta = _out(args) / "store.parquet", _out(args) / "store.meta.json"
    if path.exists() and meta.exists() and not args.rebuild and json.loads(meta.read_text())["fingerprint"] == fp:
        return pl.read_parquet(path), prov
    t = time.time()
    pw, tw = pe.aggregate(erased, air)
    parts = []
    for (season, week), rows in store.group_by("season", "week", maintain_order=True):
        parts.append(pe.features_for_week(src.panel, pw, tw, int(season), int(week), rows.select("gsis_id"))
                     .with_columns(pl.lit(int(season) * 100 + int(week)).alias("key")))
    store = store.join(pl.concat(parts), on=["key", "gsis_id"], how="left")
    store.write_parquet(path)
    # join coverage: panel rows whose team-week has parsed play-by-play
    pj = src.panel.filter(pl.col("season") >= FIRST_SEASON).join(tw.select("season", "week", "team", "team_air"),
                                                                 on=["season", "week", "team"], how="left")
    prov["panel_team_week_coverage"] = round(float(pj["team_air"].is_not_null().mean()), 5)
    meta.write_text(json.dumps({**prov, "rows": store.height, "build_seconds": round(time.time() - t, 1)}))
    return store, prov


def cmd_build(args) -> dict:
    store, prov = build(args)
    s = store.filter(pl.col("season") >= ch.TRAIN_START)
    prov = {**prov, **json.loads((_out(args) / "store.meta.json").read_text())}
    out = {"rows": s.height, **prov, "missing_rate_by_season": {}, "cohort_sizes": {}, "feature_summary": {}}
    for season, g in s.group_by("season", maintain_order=True):
        out["missing_rate_by_season"][int(season[0])] = {c: round(float(g[c].is_null().mean()), 4)
                                                         for c in ("pe_tgt_pg", "pe_adj_target_share",
                                                                   "pe_adj_rush_share", "pe_adj_air_share")}
    for pos in [*POSITIONS, "ALL"]:
        g = s if pos == "ALL" else s.filter(pl.col("position") == pos)
        out["cohort_sizes"][pos] = {c: int(g.filter(e.fill_null(False)).height) for c, e in pe.cohorts().items()
                                    if c != "starters_pool"}
        if pos != "ALL":
            out["feature_summary"][pos] = {
                c: {"mean": round(float(g[c].mean() or 0), 4), "p90": round(float(g[c].quantile(0.9) or 0), 4)}
                for c in ("pe_tgt_pg", "pe_car_pg", "pe_rate")}
            # how far the adjusted share moves from the official one
            d = g.select((pl.col("pe_adj_target_share") - pl.col("target_share")).alias("dt"),
                         (pl.col("pe_adj_rush_share") - pl.col("rush_share")).alias("dr")).drop_nulls()
            out["feature_summary"][pos]["share_shift"] = {
                "target_mean": round(float(d["dt"].mean() or 0), 5), "target_abs_p90": round(float(d["dt"].abs().quantile(0.9) or 0), 5),
                "rush_mean": round(float(d["dr"].mean() or 0), 5), "rush_abs_p90": round(float(d["dr"].abs().quantile(0.9) or 0), 5),
                "corr_adj_vs_official_target_share": round(float(g.select(pl.corr("pe_adj_target_share", "target_share")).item() or 0), 5),
                "corr_adj_vs_official_rush_share": round(float(g.select(pl.corr("pe_adj_rush_share", "rush_share")).item() or 0), 5)}
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
    flt = pe.cohorts()
    for pos in [*POSITIONS, "RB_WR_TE", "ALL"]:
        g = (p if pos == "ALL" else p.filter(pl.col("position").is_in(pe.EVAL_POSITIONS)) if pos == "RB_WR_TE"
             else p.filter(pl.col("position") == pos))
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
    """pred_ifp2, pred_A, pred_V (+ pred_pe), and the trailing baselines A1/A2, on store rows of `weeks`."""
    t = time.time()
    preds = pe.walk_forward(store, weeks, cfg, per_position=per_position)
    # ifp-2 fitted here must equal the frozen challenger's own walk-forward exactly
    ref = ch.challenger_predictions(store, weeks, cfg, col="_ifp2_ref")
    chk = preds.join(ref, on=["key", "gsis_id"], how="full", coalesce=True).filter(
        pl.col("pred_ifp2").is_not_null() | pl.col("_ifp2_ref").is_not_null())
    assert chk.filter((pl.col("pred_ifp2") - pl.col("_ifp2_ref")).abs().fill_null(1.0) > 1e-9).height == 0
    print(f"walk-forward {len(weeks)} weeks: {time.time() - t:.0f}s", file=sys.stderr)
    rows = store.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks]))
    base = baseline_predictions(rows).select("key", "gsis_id", "pred_A1", "pred_A2")
    return rows.join(preds, on=["key", "gsis_id"]).join(base, on=["key", "gsis_id"], how="left")


def _frozen_path(args) -> Path:
    return _out(args) / f"frozen_selection_{pe.PE_CHALLENGER_VERSION}.json"


def cmd_develop(args) -> dict:
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    dev = _preds(store, ch.window_weeks(store, pe.WINDOWS["development"]), cfg)
    models = ["ifp2", *pe.CANDIDATES]
    exact = report(dev, models, exact=True)
    chosen, why = {"QB": "ifp2"}, {}
    for pos in pe.EVAL_POSITIONS:
        r = exact[pos]
        qual = {}
        for c in pe.CANDIDATES:
            d = r["minus_ifp2"][c]
            bias_ok = abs(r["metrics"][c]["bias"]) <= abs(r["metrics"]["ifp2"]["bias"]) + pe.BIAS_TOLERANCE
            ok = d["diff"] <= pe.SELECTION_MAX_DIFF and d["ci95"][1] < 0 and bias_ok
            why[f"{pos}:{c}"] = {"diff": d, "bias_ok": bias_ok, "qualifies": ok}
            if ok:
                qual[c] = r["metrics"][c]["mae"]
        chosen[pos] = min(qual, key=qual.get) if qual else "ifp2"
    frozen = {"version": pe.PE_CHALLENGER_VERSION, "status": pe.PE_STATUS, "selected_on": pe.WINDOWS["development"],
              "per_position": chosen, "replace": pe.REPLACE, "add": pe.ADD, "rules": pe.DECISION_RULES,
              "ifp2_config_hash": cfg.hash(), "feature_version": pe.PE_VERSION,
              "scoring_fingerprint": prov["scoring_fingerprint"], "store_fingerprint": prov["fingerprint"]}
    frozen["config_hash"] = hashlib.sha256(json.dumps({k: v for k, v in frozen.items() if k != "rules"},
                                                      sort_keys=True, default=str).encode()).hexdigest()[:16]
    frozen["frozen_at"] = datetime.now(timezone.utc).isoformat()
    path = _frozen_path(args)
    if not hr._write_once(path, frozen):
        prev = json.loads(path.read_text())
        if prev["per_position"] != chosen:
            raise SystemExit(f"{path} already frozen with a different selection ({prev['per_position']})")
    rep = report(dev, [*models, "A1", "A2"])
    (_out(args) / "development.json").write_text(json.dumps({"report": rep, "selection": why, **prov}, indent=1,
                                                            default=str))
    return {"per_position": chosen, "selection": why,
            "by_position_minus_ifp2": {pos: rep[pos]["minus_ifp2"] for pos in [*pe.EVAL_POSITIONS, "RB_WR_TE"]}}


def verdict(r: dict, p: pl.DataFrame, chosen: dict) -> dict:
    """The pre-registered confirmation rules (pe.DECISION_RULES), on unrounded values."""
    adopted = [pos for pos, c in chosen.items() if c != "ifp2"]
    if not adopted:
        return {"adopted_positions": [], "outcome": "retain_baseline", "reason": "nothing adopted on development"}
    sub = {pos: r[pos] for pos in adopted}
    rows = p.filter(pl.col("label").is_not_null() & pl.col("position").is_in(adopted)
                    & pl.col("pred_pe").is_not_null() & pl.col("pred_ifp2").is_not_null())
    pooled = week_bootstrap_mae_diff(rows, "pred_pe", "pred_ifp2", 1000, SEED, exact=True)
    a = _arr(rows, "label")
    rmse = {m: metrics(a, _arr(rows, f"pred_{m}"), exact=True)["rmse"] for m in ("pe", "ifp2")}
    pairs = pair_decisions(rows, ["pred_pe", "pred_ifp2"], exact=True)["all"]
    checks = {
        "c1_pooled_mae": pooled["diff"] < 0 and pooled["ci95"][1] < 0,
        "c2_no_position_significantly_worse": all(s["minus_ifp2"]["pe"]["ci95"][0] <= 0 for s in sub.values()),
        "c3_rmse": rmse["pe"] <= rmse["ifp2"] * 1.005,
        "c4_bias": all(abs(s["metrics"]["pe"]["bias"]) <= abs(s["metrics"]["ifp2"]["bias"]) + pe.BIAS_TOLERANCE
                       for s in sub.values()),
        "c5_starters_pool": all(s["cohorts"]["starters_pool"]["pe"]["mae"]
                                <= s["cohorts"]["starters_pool"]["ifp2"]["mae"] * 1.01 for s in sub.values()),
        "c6_pairs": pairs["pred_pe"]["accuracy"] >= pairs["pred_ifp2"]["accuracy"] - 0.005,
    }
    mech = {}
    for pos in adopted:
        co = r[pos]["cohorts"][pe.MECHANISM_COHORT]
        mech[f"{pos}:{pe.MECHANISM_COHORT}"] = co["pe"]["mae"] - co["ifp2"]["mae"] if co["n"] else None
    checks["c7_mechanism"] = all(v is not None and v < 0 for v in mech.values())
    return {"adopted_positions": adopted, "pooled_adopted": pooled, "pooled_adopted_rmse": rmse,
            "pairs_adopted": {m: pairs[f"pred_{m}"] for m in ("pe", "ifp2")}, "checks": checks,
            "mechanism_diffs": mech,
            "outcome": "adopt_candidate" if all(checks.values()) else "retain_baseline"}


def espn_rows(args, p: pl.DataFrame, models: list[str]) -> dict:
    """Descriptive: every model vs ESPN on rows with a pregame ESPN snapshot (live season only)."""
    if not args.live_db.exists():
        return {"n": 0}
    con = duckdb.connect(str(args.live_db), read_only=True)
    try:
        espn = espn_pregame(con, args.season)
    finally:
        con.close()
    j = p.join(espn, on=["season", "week", "gsis_id"], how="inner").filter(
        pl.col("label").is_not_null() & pl.all_horizontal([pl.col(f"pred_{m}").is_not_null() for m in models]))
    out = {"n": j.height, "weeks": sorted(j["week"].unique().to_list()) if j.height else []}
    for pos in [*pe.EVAL_POSITIONS, "RB_WR_TE"]:
        g = j.filter(pl.col("position").is_in(pe.EVAL_POSITIONS)) if pos == "RB_WR_TE" else \
            j.filter(pl.col("position") == pos)
        if g.height == 0:
            continue
        out[pos] = {"n": g.height, **{m: metrics(_arr(g, "label"), _arr(g, f"pred_{m}")) for m in [*models, "espn"]}}
        if g.select("key").n_unique() >= 3:
            out[pos]["espn_minus_ifp2"] = week_bootstrap_mae_diff(g, "espn", "pred_ifp2", 1000, SEED)
            for m in models:
                if m != "ifp2":
                    out[pos][f"{m}_minus_espn"] = week_bootstrap_mae_diff(g, f"pred_{m}", "espn", 1000, SEED)
    return out


def cmd_evaluate(args) -> dict:
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    frozen = json.loads(_frozen_path(args).read_text())
    if frozen["ifp2_config_hash"] != cfg.hash() or frozen["scoring_fingerprint"] != prov["scoring_fingerprint"]:
        raise SystemExit("frozen selection was made against a different ifp-2 config or league scoring")
    if frozen.get("feature_version") != pe.PE_VERSION or frozen.get("replace") != pe.REPLACE \
            or frozen.get("add") != pe.ADD or frozen.get("rules") != pe.DECISION_RULES:
        raise SystemExit(f"{_frozen_path(args).name} was frozen with a different feature version, candidates or rules; "
                         "bump PE_VERSION / PE_CHALLENGER_VERSION instead of re-confirming")
    chosen = frozen["per_position"]
    models = ["ifp2", *pe.CANDIDATES, "pe", "A1", "A2"]
    out = {"frozen": {k: frozen[k] for k in ("per_position", "config_hash", "frozen_at", "store_fingerprint")},
           "store_matches_frozen": frozen["store_fingerprint"] == prov["fingerprint"], **prov}
    for name, seasons in (("confirmation", pe.WINDOWS["confirmation"]), ("exploratory", pe.WINDOWS["exploratory"])):
        p = _preds(store, ch.window_weeks(store, seasons), cfg, per_position=chosen)
        p.select("key", "season", "week", "gsis_id", "position", "label", *[f"pred_{m}" for m in models]).write_parquet(
            _out(args) / f"{name}_preds.parquet")
        out[name] = report(p, models)
        if name == "confirmation":
            out["verdict"] = verdict(report(p, models, exact=True), p, chosen)
        else:
            out["exploratory_by_season"] = {int(s): report(p.filter(pl.col("season") == s), models)["RB_WR_TE"]
                                            for s in seasons if p.filter(pl.col("season") == s).height}
            out["espn"] = espn_rows(args, p, ["ifp2", *pe.CANDIDATES, "pe"])
    out["development"] = json.loads((_out(args) / "development.json").read_text())["report"]
    (_out(args) / "evaluation.json").write_text(json.dumps(out, indent=1, default=str))
    return {"verdict": out["verdict"],
            "confirmation_minus_ifp2": {p: out["confirmation"][p]["minus_ifp2"] for p in [*pe.EVAL_POSITIONS, "RB_WR_TE"]},
            "exploratory_minus_ifp2": {p: out["exploratory"][p]["minus_ifp2"] for p in [*pe.EVAL_POSITIONS, "RB_WR_TE"]},
            "espn": out["espn"]}


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["identify", "build", "develop", "evaluate"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--refresh-pbp", action="store_true")
    args = ap.parse_args(argv)
    cmds = {"identify": cmd_identify, "build": cmd_build, "develop": cmd_develop, "evaluate": cmd_evaluate}
    print(json.dumps(cmds[args.command](args), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
