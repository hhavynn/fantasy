"""Research: QB- and teammate-context challenger (ifp-ctx, research only; see
docs/research/qb_teammate_context.md). Offline, read-only on both databases.

    cd backend
    uv run python ../scripts/context_research.py build      # feature store + context, coverage, QB-ID accuracy
    uv run python ../scripts/context_research.py develop    # 2021-22 only -> write-once frozen selection
    uv run python ../scripts/context_research.py evaluate   # frozen selection: 2023-24 (once) + 2025-26 replay
    uv run python ../scripts/context_research.py cases      # largest ifp2 -> ctx changes, with their context
    uv run python ../scripts/context_research.py predict --week W   # ifp-ctx-p1 frozen pregame ledger (write-once)
    uv run python ../scripts/context_research.py prospective        # score ledgers of fully final weeks

Outputs: <ifp-dir>/research/context/ (git-ignored). Never writes ifp-1/ifp-2 artifacts, the weekly
ledger, the registry or any production table.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
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
from app.analytics import context_challenger as cc  # noqa: E402
from app.analytics import roster_context as rc  # noqa: E402
from app.analytics.forecast_backtest import (_arr, build_store, metrics, pair_decisions,  # noqa: E402
                                             week_bootstrap_mae_diff)
from app.analytics.forecast_features import POSITIONS  # noqa: E402
from app.config import get_settings  # noqa: E402

SEED = 20261016
CONTEXT_COLUMNS = [*rc.QB_COLUMNS, *rc.TM_COLUMNS, *rc.ENV_COLUMNS, "ctx_report_used"]


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "context"
    p.mkdir(parents=True, exist_ok=True)
    return p


def ctx_sources(args) -> rc.ContextSources:
    parts = []
    for path in (args.history_db, args.live_db):
        if path.exists():
            con = duckdb.connect(str(path), read_only=True)
            try:
                parts.append(rc.load_context_sources(con))
            finally:
                con.close()
    return rc.ContextSources.concat(parts)


def build(args) -> tuple[pl.DataFrame, dict]:
    """The ifp-2 feature store (same builder, same fully-final weeks as the HGB study) plus context
    columns and the evaluation-only actual starter. Cached by an input fingerprint."""
    src, scoring = hr._sources(args)
    ctx = ctx_sources(args)
    weeks = hr.final_weeks(args, src)
    fp = hashlib.sha256(json.dumps({
        "store": hr.input_fingerprint(args, src, scoring.fingerprint, weeks),
        **{n: hr._frame_hash(getattr(ctx, n)) for n in ("starters", "qb_stats", "team_env", "def_targets")}},
        sort_keys=True).encode()).hexdigest()[:16]
    path, meta = _out(args) / "store.parquet", _out(args) / "store.meta.json"
    if path.exists() and meta.exists() and not args.rebuild and json.loads(meta.read_text())["fingerprint"] == fp:
        return pl.read_parquet(path), {"fingerprint": fp, "scoring_fingerprint": scoring.fingerprint}
    t = time.time()
    store = ch.add_last_game(build_store(src, weeks), src.panel)
    parts = []
    for season, week in weeks:
        rows = store.filter((pl.col("season") == season) & (pl.col("week") == week))
        parts.append(rc.context_for_week(src, ctx, season, week, rows.select("gsis_id", "position", "team", "opponent"))
                     .with_columns(pl.lit(season * 100 + week).alias("key")))
    context = pl.concat(parts)
    actual = ctx.starters.select((pl.col("season") * 100 + pl.col("week")).alias("key"), "team",
                                 pl.col("qb_id").alias("actual_qb_id")).unique(["key", "team"])
    store = store.join(context, on=["key", "gsis_id"], how="left").join(actual, on=["key", "team"], how="left")
    store.write_parquet(path)
    meta.write_text(json.dumps({"fingerprint": fp, "rows": store.height, "weeks": len(weeks),
                                "build_seconds": round(time.time() - t, 1)}))
    return store, {"fingerprint": fp, "scoring_fingerprint": scoring.fingerprint}


# ----------------------------------------------------------------------------- build diagnostics


def cmd_build(args) -> dict:
    store, prov = build(args)
    s = store.filter(pl.col("season") >= ch.TRAIN_START)
    out = {"rows": s.height, **prov, "missing_rate_by_season": {}, "qb_identification": {}}
    for season, g in s.group_by("season", maintain_order=True):
        out["missing_rate_by_season"][int(season[0])] = {
            c: round(float(g[c].is_null().mean()), 4) for c in
            ("expected_qb_id", "qb_epa_delta", "tm_tgt_vacated", "tm_tgt_gain_pg", "team_proe", "opp_pressure")}
        out["missing_rate_by_season"][int(season[0])]["report_used"] = round(float(g["ctx_report_used"].mean()), 4)
    # QB identification: one row per team-week (not per player), expected vs the actual starter
    tw = s.unique(["key", "team"]).filter(pl.col("actual_qb_id").is_not_null())
    for conf, g in tw.group_by("qb_confidence"):
        known = g.filter(pl.col("expected_qb_id").is_not_null())
        out["qb_identification"][conf[0]] = {
            "team_weeks": g.height, "with_expected": known.height,
            "expected_started": round(float((known["expected_qb_id"] == known["actual_qb_id"]).mean()), 4)
            if known.height else None}
    by_reason = tw.group_by("qb_reason").agg(pl.len().alias("n"), (pl.col("expected_qb_id") == pl.col("actual_qb_id"))
                                             .mean().round(4).alias("expected_started")).sort("n", descending=True)
    out["qb_identification_by_reason"] = by_reason.to_dicts()
    starter_changes = tw.filter(pl.col("prev_starter_status").is_not_null())
    out["actual_starter_changes"] = {
        "team_weeks": starter_changes.height,
        "actual_differs_from_previous_starter": int((starter_changes["actual_qb_id"] != starter_changes["expected_qb_id"])
                                                    .sum()) if starter_changes.height else 0}
    out["cohort_sizes"] = {c: int(s.filter(e.fill_null(False)).height) for c, e in cc.cohorts().items()
                           if c not in ("starters_pool",)}
    (_out(args) / "build_report.json").write_text(json.dumps(out, indent=1, default=str))
    return out


# ----------------------------------------------------------------------------- comparison helpers


def report(p: pl.DataFrame, models: list[str], exact: bool = False) -> dict:
    """Identical-row comparison of every model against ifp2, per position and pooled, with cohorts."""
    lab = p.filter(pl.col("label").is_not_null())
    cov = {m: round(float(lab[f"pred_{m}"].is_not_null().mean()), 4) for m in models}
    p = lab.filter(pl.all_horizontal([pl.col(f"pred_{m}").is_not_null() for m in models]))
    out = {"population": {"labeled": lab.height, "common": p.height, "coverage": cov,
                          "weeks": p.select("key").n_unique()}}
    flt = cc.cohorts()
    for pos in [*POSITIONS, "ALL"]:
        g = p if pos == "ALL" else p.filter(pl.col("position") == pos)
        a = _arr(g, "label")
        r = {"n": g.height, "metrics": {m: metrics(a, _arr(g, f"pred_{m}"), exact) for m in models},
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
    t = time.time()
    preds = cc.walk_forward(store, weeks, cfg, per_position=per_position)
    print(f"walk-forward {len(weeks)} weeks: {time.time() - t:.0f}s", file=sys.stderr)
    return store.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks])).join(preds, on=["key", "gsis_id"])


def _frozen_path(args) -> Path:
    return _out(args) / f"frozen_selection_{cc.CTX_VERSION}.json"


# ----------------------------------------------------------------------------- develop + freeze


def cmd_develop(args) -> dict:
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    weeks = ch.window_weeks(store, cc.WINDOWS["development"])
    dev = _preds(store, weeks, cfg)
    models = ["ifp2", *cc.CANDIDATES]
    exact = report(dev, models, exact=True)
    chosen, why = {}, {}
    for pos in POSITIONS:
        r = exact[pos]
        elig = {}
        for cand in cc.CANDIDATES:
            d = r["minus_ifp2"][cand]
            bias_ok = abs(r["metrics"][cand]["bias"]) <= abs(r["metrics"]["ifp2"]["bias"]) + cc.BIAS_TOLERANCE
            if d["diff"] <= cc.SELECTION_MAX_DIFF and d["ci95"][1] < 0 and bias_ok:
                elig[cand] = r["metrics"][cand]["mae"]
        chosen[pos] = min(elig, key=lambda c: (elig[c], c)) if elig else "ifp2"
        why[pos] = {"eligible": sorted(elig), "diffs": {c: r["minus_ifp2"][c] for c in cc.CANDIDATES}}
    frozen = {"version": cc.CTX_VERSION, "status": cc.CTX_STATUS, "selected_on": cc.WINDOWS["development"],
              "per_position": chosen, "groups": cc.GROUPS, "candidates": cc.CANDIDATES,
              "rules": cc.DECISION_RULES, "ifp2_config_hash": cfg.hash(), "context_version": rc.CONTEXT_VERSION,
              "scoring_fingerprint": prov["scoring_fingerprint"], "store_fingerprint": prov["fingerprint"]}
    frozen["config_hash"] = hashlib.sha256(json.dumps({k: v for k, v in frozen.items() if k != "rules"},
                                                      sort_keys=True, default=str).encode()).hexdigest()[:16]
    frozen["frozen_at"] = datetime.now(timezone.utc).isoformat()
    path = _frozen_path(args)
    if not hr._write_once(path, frozen):
        prev = json.loads(path.read_text())
        if prev["config_hash"] != frozen["config_hash"]:
            raise SystemExit(f"{path} already frozen with a different selection ({prev['per_position']})")
    rep = report(dev, models)
    (_out(args) / "development.json").write_text(json.dumps({"report": rep, "selection": why, **prov}, indent=1,
                                                            default=str))
    return {"per_position": chosen, "selection": why, "pooled": rep["ALL"]["metrics"],
            "pooled_minus_ifp2": rep["ALL"]["minus_ifp2"]}


# ----------------------------------------------------------------------------- evaluate (once)


def verdict(r: dict, p: pl.DataFrame, chosen: dict) -> dict:
    """The pre-registered confirmation rules (cc.DECISION_RULES), on unrounded values. `r` is the
    exact report of the confirmation window, `p` its identical-row predictions."""
    adopted = [pos for pos, c in chosen.items() if c != "ifp2"]
    if not adopted:
        return {"adopted_positions": [], "outcome": "retain_baseline", "reason": "nothing adopted on development"}
    sub = {pos: r[pos] for pos in adopted}
    rows = p.filter(pl.col("label").is_not_null() & pl.col("position").is_in(adopted)
                    & pl.col("pred_ctx").is_not_null() & pl.col("pred_ifp2").is_not_null())
    pooled = week_bootstrap_mae_diff(rows, "pred_ctx", "pred_ifp2", 1000, SEED, exact=True)
    checks = {
        "c1_pooled_mae": pooled["diff"] < 0 and pooled["ci95"][1] < 0,
        "c2_no_position_significantly_worse": all(s["minus_ifp2"]["ctx"]["ci95"][0] <= 0 for s in sub.values()),
        "c3_rmse": all(s["metrics"]["ctx"]["rmse"] <= s["metrics"]["ifp2"]["rmse"] * 1.005 for s in sub.values()),
        "c4_bias": all(abs(s["metrics"]["ctx"]["bias"]) <= abs(s["metrics"]["ifp2"]["bias"]) + cc.BIAS_TOLERANCE
                       for s in sub.values()),
        "c5_starters_pool": all(s["cohorts"]["starters_pool"]["ctx"]["mae"]
                                <= s["cohorts"]["starters_pool"]["ifp2"]["mae"] * 1.01 for s in sub.values()),
        "c6_pairs": r["pairs"]["all"]["pred_ctx"]["accuracy"] >= r["pairs"]["all"]["pred_ifp2"]["accuracy"] - 0.005,
    }
    mech = {}
    for pos in adopted:
        for g in cc.CANDIDATES[chosen[pos]]:
            for c in cc.MECHANISM_COHORTS[g]:
                co = r[pos]["cohorts"].get(c, {})
                if co.get("n"):
                    mech[f"{pos}:{c}"] = co["ctx"]["mae"] - co["ifp2"]["mae"]
    checks["c7_mechanism"] = bool(mech) and all(v < 0 for v in mech.values())
    return {"adopted_positions": adopted, "pooled_adopted": pooled, "checks": checks, "mechanism_diffs": mech,
            "outcome": "prospective_shadow" if all(checks.values()) else "retain_baseline"}


def cmd_evaluate(args) -> dict:
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    frozen = json.loads(_frozen_path(args).read_text())
    if frozen["ifp2_config_hash"] != cfg.hash() or frozen["scoring_fingerprint"] != prov["scoring_fingerprint"]:
        raise SystemExit("frozen selection was made against a different ifp-2 config or league scoring")
    chosen = frozen["per_position"]
    models = ["ifp2", *cc.CANDIDATES, "ctx"]
    out = {"frozen": {k: frozen[k] for k in ("per_position", "config_hash", "frozen_at")}, **prov}
    for name, seasons in (("confirmation", cc.WINDOWS["confirmation"]), ("exploratory", cc.WINDOWS["exploratory"])):
        weeks = ch.window_weeks(store, seasons)
        p = _preds(store, weeks, cfg, per_position=chosen)
        p.select("key", "gsis_id", "position", "label", *[f"pred_{m}" for m in models]).write_parquet(
            _out(args) / f"{name}_preds.parquet")
        out[name] = report(p, models)
        if name == "confirmation":
            out["verdict"] = verdict(report(p, models, exact=True), p, chosen)
        if name == "exploratory":
            out["exploratory_by_season"] = {int(s): report(p.filter(pl.col("season") == s), models)["ALL"]
                                            for s in seasons if p.filter(pl.col("season") == s).height}
    dev = json.loads((_out(args) / "development.json").read_text())
    out["development"] = dev["report"]
    (_out(args) / "evaluation.json").write_text(json.dumps(out, indent=1, default=str))
    return {"verdict": out["verdict"], "confirmation_pooled": out["confirmation"]["ALL"]["minus_ifp2"],
            "exploratory_pooled": out["exploratory"]["ALL"]["minus_ifp2"]}


# ----------------------------------------------------------------------------- prospective ledger


def _prospective_frozen(args, cfg, scoring_fp: str) -> dict:
    """The pre-registered prospective spec, frozen write-once on first use; later runs must match it."""
    spec = {"version": cc.PROSPECTIVE_VERSION, "status": cc.CTX_STATUS, "per_position": cc.PROSPECTIVE_SPEC,
            "groups": cc.GROUPS, "candidates": cc.CANDIDATES, "rules": cc.PROSPECTIVE_RULES,
            "context_version": rc.CONTEXT_VERSION, "ifp2_config_hash": cfg.hash(), "scoring_fingerprint": scoring_fp}
    spec["config_hash"] = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16]
    path = _out(args) / f"frozen_{cc.PROSPECTIVE_VERSION}.json"
    if not hr._write_once(path, {**spec, "frozen_at": datetime.now(timezone.utc).isoformat()}):
        prev = json.loads(path.read_text())
        if prev["config_hash"] != spec["config_hash"]:
            raise SystemExit(f"{path.name} was frozen with a different spec, ifp-2 config or scoring; refusing")
    return json.loads(path.read_text())


def _frame_sha(df: pl.DataFrame | None) -> str | None:
    return None if df is None else hr._frame_hash(df)


def cmd_predict(args) -> dict:
    """Frozen pregame forecasts (ifp2 and ifp-ctx-p1, same run, same rows) for one week, written
    once. Teams that have kicked off are excluded (never backdated)."""
    from app.analytics.forecast_features import features_for_week
    from app.analytics.participation import live_population
    from app.analytics.weekly_learning import team_kickoffs

    now = datetime.now(timezone.utc)
    store, prov = build(args)
    cfg = ch.ChallengerConfig.load()
    frozen = _prospective_frozen(args, cfg, prov["scoring_fingerprint"])
    season, week = args.season, args.week
    lpath = _out(args) / "ledger" / f"s{season}w{week:02d}-pregame.json"
    if lpath.exists():
        led = json.loads(lpath.read_text())
        return {"ledger": str(lpath), "written": False, "created_at": led["created_at"], "rows": len(led["rows"])}
    src, _ = hr._sources(args)
    ctx = ctx_sources(args)
    kick = team_kickoffs(src, season, week)
    open_teams = sorted(t for t, ko in kick.items() if now < ko)
    if not open_teams:
        raise SystemExit(f"every week-{week} game has kicked off: no pregame forecast possible")
    inj = None if src.injuries is None else src.injuries.filter((pl.col("season") == season) & (pl.col("week") == week))
    if (inj is None or inj.height == 0) and not args.allow_no_report:
        raise SystemExit(f"the week-{week} injury report is not ingested yet (run sync_nfl.py first): a ledger is "
                         "write-once, so forecasts without it would be frozen degraded. --allow-no-report overrides.")
    cutoff = season * 100 + week
    models, n_train = {}, {}
    for pos in POSITIONS:
        train = ch.train_rows(store, cutoff, pos)
        assert train.height == 0 or train["key"].max() < cutoff
        n_train[pos] = train.height
        cand = frozen["per_position"][pos]
        models[pos] = {"ifp2": cc.fit_candidate(train, cfg, pos, None, cutoff),
                       "ctx": cc.fit_candidate(train, cfg, pos, None if cand == "ifp2" else cand, cutoff)}
    art = {"schema": "ifp-ctx-research-artifact/v1", "version": cc.PROSPECTIVE_VERSION, "status": cc.CTX_STATUS,
           "training_cutoff_key": cutoff, "config_hash": frozen["config_hash"], "created_at": now.isoformat(),
           "store_fingerprint": prov["fingerprint"], "training_rows": n_train,
           "models": {p: {k: m.to_dict() for k, m in v.items()} for p, v in models.items()}}
    apath = _out(args) / "artifacts" / f"{cc.PROSPECTIVE_VERSION}-s{season}w{week:02d}.json"
    if not hr._write_once(apath, art):
        art = json.loads(apath.read_text())        # an interrupted earlier run's artifact is reused, never refit
        if art["config_hash"] != frozen["config_hash"] or art["training_cutoff_key"] != cutoff:
            raise SystemExit(f"{apath.name} was built under a different configuration or cutoff")
        models = {p: {k: ch.RidgeModel.from_dict(d) for k, d in v.items()} for p, v in art["models"].items()}
    pop = live_population(src, season, week).filter(pl.col("team").is_in(open_teams))
    feats = features_for_week(src, season, week, pop)
    feats = feats.join(rc.context_for_week(src, ctx, season, week, feats.select("gsis_id", "position", "team",
                                                                                "opponent")), on="gsis_id", how="left")
    rows = []
    for pos in POSITIONS:
        g = feats.filter(pl.col("position") == pos)
        if g.height == 0:
            continue
        cand = frozen["per_position"][pos]
        groups = cc.CANDIDATES.get(cand, ())
        for i in range(g.height):
            r = g.slice(i, 1)
            rec = r.row(0, named=True)
            ok = (rec.get("games_recent") or 0) > 0
            e = cc.explain_change(models[pos]["ifp2"], models[pos]["ctx"], r, groups) if ok else None
            rows.append({"gsis_id": rec["gsis_id"], "position": pos, "team": rec["team"],
                         "kickoff": kick[rec["team"]].isoformat(),
                         "ifp2_points": e["baseline_points"] if e else None,
                         "ctx_points": e["challenger_points"] if e else None,
                         "explanation": e, **{c: rec.get(c) for c in CONTEXT_COLUMNS}})
    ledger = {"schema": "ifp-ctx-research-ledger/v1", "version": cc.PROSPECTIVE_VERSION, "kind": "pregame",
              "season": season, "week": week, "created_at": now.isoformat(), "config_hash": frozen["config_hash"],
              "artifact": apath.name, "training_cutoff_key": cutoff,
              "teams_excluded_already_started": sorted(set(kick) - set(open_teams)),
              "sources": {"injury_report_rows": None if inj is None else inj.height,
                          "injury_report_sha": _frame_sha(inj), "injury_report_timestamped": False,
                          "rosters_sha": _frame_sha(src.rosters), "panel_sha": _frame_sha(src.panel),
                          "starters_sha": _frame_sha(ctx.starters), "qb_stats_sha": _frame_sha(ctx.qb_stats)},
              "rows": rows}
    written = hr._write_once(lpath, ledger)
    return {"ledger": str(lpath), "written": written, "rows": len(rows),
            "forecast_rows": sum(r["ctx_points"] is not None for r in rows), "open_teams": len(open_teams)}


def cmd_prospective(args) -> dict:
    """Score the write-once ledgers of fully final weeks (identical rows, appearances only)."""
    store, prov = build(args)
    ledgers = sorted((_out(args) / "ledger").glob("s*-pregame.json")) if (_out(args) / "ledger").exists() else []
    parts = []
    for path in ledgers:
        led = json.loads(path.read_text())
        k = led["season"] * 100 + led["week"]
        if not store.filter(pl.col("key") == k).height:       # week not fully final yet
            continue
        parts.append(pl.DataFrame([{"key": k, "gsis_id": r["gsis_id"], "pred_ifp2": r["ifp2_points"],
                                    "pred_ctx": r["ctx_points"]} for r in led["rows"]],
                                  schema={"key": pl.Int64, "gsis_id": pl.String, "pred_ifp2": pl.Float64,
                                          "pred_ctx": pl.Float64}))
    if not parts:
        return {"ledgers": len(ledgers), "scored_weeks": 0, "status": "insufficient_data"}
    frozen_rows = pl.concat(parts)
    p = store.join(frozen_rows, on=["key", "gsis_id"], how="inner")
    r = report(p, ["ifp2", "ctx"])
    weeks = p.select("key").n_unique()
    gate = {}
    for pos, cand in cc.PROSPECTIVE_SPEC.items():
        if cand == "ifp2":
            continue
        x = r[pos]
        ci = x["minus_ifp2"]["ctx"]["ci95"]
        checks = {"weeks": weeks >= ch.SHADOW_GATE["min_prospective_weeks"],
                  "appearances": x["n"] >= ch.SHADOW_GATE["min_matched_appearances_per_position"],
                  "mae_ci_upper_below_0": ci is not None and ci[1] < ch.SHADOW_GATE["mae_ci95_upper_below"],
                  "bias": x["n"] > 0 and abs(x["metrics"]["ctx"]["bias"]) <= abs(x["metrics"]["ifp2"]["bias"])
                  + ch.SHADOW_GATE["bias_tolerance_points"],
                  "starters_pool": x["cohorts"]["starters_pool"]["n"] > 0
                  and x["cohorts"]["starters_pool"]["ctx"]["mae"] <= x["cohorts"]["starters_pool"]["ifp2"]["mae"]}
        gate[pos] = {"checks": checks, "status": "meets_gate" if all(checks.values()) else (
            "insufficient_data" if not (checks["weeks"] and checks["appearances"]) else "fails_gate")}
    out = {"scored_weeks": weeks, "report": r, "gate": gate, **prov}
    (_out(args) / "prospective.json").write_text(json.dumps(out, indent=1, default=str))
    return {"scored_weeks": weeks, "gate": gate, "pooled": r["ALL"]["minus_ifp2"]}


def cmd_cases(args) -> dict:
    store, _ = build(args)
    con = duckdb.connect(str(args.live_db), read_only=True)
    names = dict(con.execute("SELECT gsis_id, display_name FROM nfl_players").fetchall())
    con.close()
    p = pl.read_parquet(_out(args) / "confirmation_preds.parquet").join(
        store.drop("label", "position"), on=["key", "gsis_id"])
    p = p.filter(pl.col("pred_C").is_not_null() & pl.col("pred_ifp2").is_not_null()).with_columns(
        (pl.col("pred_C") - pl.col("pred_ifp2")).alias("move"),
        pl.col("gsis_id").replace_strict(names, default=None).alias("name"),
        pl.col("expected_qb_id").replace_strict(names, default=None).alias("expected_qb"),
        pl.col("recent_qb_id").replace_strict(names, default=None).alias("recent_qb"))
    show = ["name", "season", "week", "position", "team", "expected_qb", "recent_qb", "qb_confidence", "qb_epa_delta",
            "tm_tgt_vacated", "tm_tgt_returning", "tm_car_vacated", "pred_ifp2", "pred_C", "label"]
    big = p.sort(pl.col("move").abs(), descending=True).head(args.limit * 4)
    return {"largest_moves": big.select(show).with_columns(pl.col(pl.Float64).round(3)).to_dicts()}


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["build", "develop", "evaluate", "cases", "predict", "prospective"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--week", type=int)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--allow-no-report", action="store_true", help="predict: freeze forecasts without the week's report")
    args = ap.parse_args(argv)
    if args.command == "predict" and args.week is None:
        ap.error("predict needs --week")
    cmds = {"build": cmd_build, "develop": cmd_develop, "evaluate": cmd_evaluate, "cases": cmd_cases,
            "predict": cmd_predict, "prospective": cmd_prospective}
    print(json.dumps(cmds[args.command](args), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
