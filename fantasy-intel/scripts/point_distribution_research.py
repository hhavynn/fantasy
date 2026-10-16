"""Research: calibrated fantasy-point distributions around the existing mean projections
(ifpd, research only; see docs/research/fantasy_point_distributions.md). Offline, read-only on
both databases; prints aggregates.

    cd backend
    uv run python ../scripts/point_distribution_research.py run        # everything below, in order
    uv run python ../scripts/point_distribution_research.py artifact   # frozen research artifact only

`run`: (1) out-of-sample means (frozen ifp-1.0.0 model C, walk-forward) and participation
probabilities (frozen ipm-1.0.0, walk-forward), cached under an input fingerprint; (2) every
candidate on development 2021-22 and the pre-registered choice, frozen write-once in
distribution_selection.json BEFORE any later window is scored; (3) confirmation 2023-24 (statuses)
and exploratory 2025-26; (4) unconditional mixtures, thresholds, dependence, pairwise and decision
scenarios; (5) the report (atomic write) carrying the fingerprint. `artifact` refuses a report or a
selection made from other inputs and writes a write-once artifact named by its content hash.

Inputs as in challenger_research.py: the live DB (2026 + league scoring), the history DB, the
play-by-play exit files in --exits-dir/pbp_exits, and the frozen selection.json /
participation_selection.json in --ifp-dir. Outputs go to <ifp-dir>/research/point_distributions/
(git-ignored). Never writes a production artifact, config or ledger.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import _bootstrap  # noqa: F401

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import participation as pm  # noqa: E402
from app.analytics import point_distributions as pd_  # noqa: E402
from app.analytics.forecast_backtest import build_store, completed_weeks, walk_forward  # noqa: E402
from app.analytics.forecast_features import POSITIONS, Sources, load_sources  # noqa: E402
from app.analytics.independent_projection_store import league_scoring  # noqa: E402
from app.analytics.independent_projections import interval_for  # noqa: E402
from app.config import REPO_ROOT, get_settings  # noqa: E402
from app.nfl.pbp_exits import load_dir  # noqa: E402

CODE_MODULES = ["app.analytics.point_distributions", "app.analytics.forecast_features",
                "app.analytics.forecast_backtest", "app.analytics.participation",
                "app.analytics.independent_projections"]


def _path(env: str, default: str) -> Path:
    raw = Path(os.getenv(env) or default)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _atomic_parquet(path: Path, df: pl.DataFrame) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    df.write_parquet(tmp)
    os.replace(tmp, path)


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "point_distributions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _frame_hash(df: pl.DataFrame | None) -> str | None:
    """Order-independent content hash of a source frame."""
    if df is None:
        return None
    return hashlib.sha256(np.sort(df.hash_rows(seed=0).to_numpy()).tobytes()).hexdigest()[:16]


def load_inputs(args) -> dict:
    """Sources, scoring and the frozen selections, with every consistency check."""
    live = duckdb.connect(str(args.live_db), read_only=True)
    hist = duckdb.connect(str(args.history_db), read_only=True) if args.history_db.exists() else None
    _, scoring = league_scoring(live, args.season)
    parts = [load_sources(c, scoring.require_supported()) for c in (hist, live) if c is not None]
    src = Sources.concat(parts).with_exits(load_dir(args.exits_dir / "pbp_exits"))
    sel_path, psel_path = args.ifp_dir / "selection.json", args.ifp_dir / "participation_selection.json"
    sel, psel = json.loads(sel_path.read_text()), json.loads(psel_path.read_text())
    try:
        pd_.check_frozen_inputs(sel, psel, scoring.fingerprint, _sha(sel_path))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    weeks = pd_.fully_final_weeks(src.games, src.panel,
                                  completed_weeks(src, list(range(pd_.POOL_START, args.season + 1))))
    mods = [sys.modules[m].__file__ for m in CODE_MODULES]
    parts_fp = {"season": args.season, "weeks": weeks, "scoring": scoring.fingerprint, "polars": pl.__version__,
                "numpy": np.__version__, "code": hashlib.sha256(b"".join(Path(m).read_bytes() for m in mods)).hexdigest()[:16],
                "selection_sha": _sha(sel_path), "participation_selection_sha": _sha(psel_path),
                **{n: _frame_hash(getattr(src, n)) for n in ("panel", "games", "injuries", "exits", "rosters", "defense")}}
    fp = hashlib.sha256(json.dumps(parts_fp, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {"src": src, "scoring": scoring, "sel": sel, "psel": psel, "sel_sha": _sha(sel_path),
            "psel_sha": _sha(psel_path), "weeks": weeks, "fingerprint": fp, "fingerprint_parts": parts_fp}


def cached_frames(args, inp: dict) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(appearance rows with the out-of-sample mean, roster rows with P(appears) and the mean),
    reused only when their recorded input fingerprint matches."""
    out = _out(args)
    fp = inp["fingerprint"]
    a_path, r_path, meta = out / "oos_appearances.parquet", out / "oos_roster.parquet", out / "oos.meta.json"
    if (a_path.exists() and r_path.exists() and meta.exists() and not args.rebuild
            and json.loads(meta.read_text()).get("fingerprint") == fp):
        return pl.read_parquet(a_path), pl.read_parquet(r_path)
    src, sel, psel, weeks = inp["src"], inp["sel"], inp["psel"], inp["weeks"]
    t = time.time()
    store = build_store(src, weeks)
    pens = {p: {s["primary"]: [s["lambda"][s["primary"]]]} for p, s in sel["positions"].items()}
    test_weeks = [w for w in weeks if w[0] >= pd_.POOL_START]
    wf = walk_forward(store, test_weeks, pens, pd_.POOL_START)
    col = {p: f"pred_{s['primary']}_{s['lambda'][s['primary']]:g}" for p, s in sel["positions"].items()}
    app = pd_.add_inputs(wf.with_columns(pl.coalesce([pl.when(pl.col("position") == p).then(pl.col(c))
                                                      for p, c in col.items()]).alias("mean"))
                         .drop([c for c in wf.columns if c.startswith("pred_") and c not in ("pred_A1", "pred_A2")]))
    print(f"appearance walk-forward {time.time() - t:.0f}s", file=sys.stderr)
    t = time.time()
    pstore = pm.build_store(src, weeks)
    keys_eval = [w for w in weeks if w[0] >= pd_.WINDOWS["confirmation"][0]]
    lams = {p: [psel["positions"][p]["lambda"]] for p in POSITIONS}
    cond_spec = {p: (s["primary"], s["lambda"][s["primary"]]) for p, s in sel["positions"].items()}
    pw = pm.walk_forward(pstore, store, keys_eval, lams, cond_spec, pd_.POOL_START)
    roster = pd_.add_inputs(pw).with_columns(
        pl.coalesce([pl.when(pl.col("position") == p).then(pl.col(f"p_{psel['positions'][p]['lambda']:g}"))
                     for p in POSITIONS]).alias("p"),
        pl.col("cond").alias("mean"),
        pl.when(pl.col("games_recent") > 0).then(pl.col("pts_recent_raw")).otherwise(None).alias("pred_A1"),
        pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64)).select(
        "key", "season", "week", "gsis_id", "position", "team", "opponent", "p", "mean", "played", "label", "pred_A1",
        "pts_std", "carries_pg", "targets_pg", "air_yards_pg", "adot", "games_recent", "weeks_since_last",
        "team_changed", "snap_trend")
    print(f"participation walk-forward {time.time() - t:.0f}s", file=sys.stderr)
    _atomic_parquet(a_path, app)
    _atomic_parquet(r_path, roster)
    _atomic_write(meta, json.dumps({"fingerprint": fp, "parts": inp["fingerprint_parts"], "appearance_rows": app.height,
                                    "roster_rows": roster.height}, default=str))
    return app, roster


def _keys(df: pl.DataFrame, seasons) -> list[int]:
    return sorted(df.filter(pl.col("season").is_in(list(range(seasons[0], seasons[1] + 1))))["key"].unique().to_list())


def _blocks(rows: pl.DataFrame, mats: dict[str, np.ndarray], keys: list[int], with_cohorts: bool = True) -> dict:
    """evaluate_block per position (+ pooled) on the window's rows, every method on the same rows."""
    mask = rows["key"].is_in(keys).to_numpy()
    sub = rows.filter(pl.Series(mask))
    qs = {c: pd_.outcome_quantiles(sub, m[mask], floor_zero=(c == "A")) for c, m in mats.items()}
    out = {}
    for pos in [*POSITIONS, "ALL"]:
        pm_ = np.ones(sub.height, bool) if pos == "ALL" else (sub["position"] == pos).to_numpy()
        out[pos] = pd_.evaluate_block(sub.filter(pl.Series(pm_)), {c: q[pm_] for c, q in qs.items()},
                                      with_cohorts=with_cohorts and pos != "ALL")
    return out


def _frozen_bands(rows: pl.DataFrame, sel: dict, keys: list[int]) -> dict:
    """The published ifp-1.0.0 bands (selection.json, validation 2023-24 residuals) as they are."""
    out = {}
    for pos in POSITIONS:
        g = rows.filter(pl.col("key").is_in(keys) & (pl.col("position") == pos))
        bands = sel["positions"][pos].get("intervals") or {}
        if not bands or g.height == 0:
            out[pos] = None
            continue
        lo_hi = [interval_for({pos: bands}, pos, float(m)) for m in g["mean"].to_numpy()]
        y = g["label"].to_numpy()
        lo = np.array([a for a, _ in lo_hi])
        hi = np.array([b for _, b in lo_hi])
        out[pos] = {"n": g.height, "coverage80": float(np.mean((y >= lo) & (y <= hi))),
                    "p10_below_rate": float(np.mean(y < lo)), "p90_above_rate": float(np.mean(y > hi)),
                    "width80": float(np.mean(hi - lo)), "pinball_p10": float(pd_.pinball(y, lo, 0.1).mean()),
                    "pinball_p90": float(pd_.pinball(y, hi, 0.9).mean())}
    return out


def freeze_selection(args, inp: dict, choice: dict, dev_summary: dict) -> dict:
    """Write-once: an existing selection made from the same inputs must agree; one made from other
    inputs is refused (pass --refreeze to discard it deliberately)."""
    path = _out(args) / "distribution_selection.json"
    body = {"version": pd_.DISTRIBUTION_VERSION, "input_fingerprint": inp["fingerprint"],
            "scoring_fingerprint": inp["scoring"].fingerprint, "ifp_selection_sha": inp["sel_sha"],
            "participation_selection_sha": inp["psel_sha"], "selection_rule": pd_.SELECTION_RULE,
            "choice": choice, "development": dev_summary}
    if path.exists() and not args.refreeze:
        old = json.loads(path.read_text())
        if old.get("input_fingerprint") != inp["fingerprint"]:
            raise SystemExit("distribution_selection.json was frozen from other inputs; it is never silently refit. "
                             "Pass --refreeze to discard it deliberately.")
        if {p: v.get("method") for p, v in old["choice"].items()} != {p: v.get("method") for p, v in choice.items()}:
            raise SystemExit("recomputed development choice differs from the frozen selection (non-determinism?)")
        return old
    if path.exists():                              # --refreeze: keep the record of what it replaces
        old = json.loads(path.read_text())
        body["replaces"] = {"input_fingerprint": old.get("input_fingerprint"), "created_at": old.get("created_at"),
                            "choice": {p: v.get("method") for p, v in old.get("choice", {}).items()}}
        body["choice_changed"] = body["replaces"]["choice"] != {p: v.get("method") for p, v in choice.items()}
    body["created_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(path, json.dumps(_clean(body), indent=1))
    return body


def _per_position(rows: pl.DataFrame, mats: dict[str, np.ndarray], choice: dict) -> np.ndarray:
    """Residual quantiles of each row's frozen per-position method."""
    out = np.full((rows.height, len(pd_.TAUS)), np.nan)
    pos = rows["position"].to_numpy()
    for p in POSITIONS:
        col = choice[p]["method"]
        if col is not None:
            out[pos == p] = mats[col][pos == p]
    return out


def cmd_run(args) -> dict:
    t0 = time.time()
    inp = load_inputs(args)
    app, roster = cached_frames(args, inp)
    cols = pd_.method_columns()
    dev_keys = _keys(app, pd_.WINDOWS["development"])
    later_keys = _keys(app, (pd_.WINDOWS["confirmation"][0], pd_.WINDOWS["exploratory"][1]))
    # (2) development only, then freeze the choice before any later window is scored
    t = time.time()
    dev_rows, dev_mats = pd_.walk_forward_quantiles(app, dev_keys, cols)
    dev = _blocks(dev_rows, dev_mats, dev_keys)
    choice = pd_.choose({p: dev[p] for p in POSITIONS})
    print(f"development {time.time() - t:.0f}s", file=sys.stderr)
    frozen = freeze_selection(args, inp, choice, {p: {c: {k: dev[p]["methods"][c][k] for k in
                                                          ("crps", "coverage80", "p10_below_rate", "p90_above_rate")}
                                                      for c in dev[p]["methods"]} for p in POSITIONS})
    choice = frozen["choice"]
    if args.development_only:
        return {"selection": {p: v.get("method") for p, v in choice.items()},
                "development": {p: {c: {k: round(m[k], 4) for k in ("crps", "coverage80", "inclusive_coverage80",
                                                                    "p10_below_rate", "p90_above_rate", "p50_mid_rate",
                                                                    "width80", "implied_mean_minus_published")}
                                    for c, m in dev[p]["methods"].items()} for p in [*POSITIONS, "ALL"]}}
    # (3) confirmation + exploratory
    t = time.time()
    rows, mats = pd_.walk_forward_quantiles(app, later_keys, cols)
    conf_keys, expl_keys = _keys(rows, pd_.WINDOWS["confirmation"]), _keys(rows, pd_.WINDOWS["exploratory"])
    conf, expl = _blocks(rows, mats, conf_keys), _blocks(rows, mats, expl_keys)
    print(f"confirmation + exploratory {time.time() - t:.0f}s", file=sys.stderr)
    status = {p: pd_.validation_status(conf[p], choice[p]["method"]) if choice[p]["method"] else
              {"status": "withheld", "reasons": [choice[p]["reason"]]} for p in POSITIONS}
    status_expl = {p: pd_.validation_status(expl[p], choice[p]["method"]) if choice[p]["method"] else None
                   for p in POSITIONS}
    chosen = _per_position(rows, mats, choice)
    q_chosen = pd_.outcome_quantiles(rows, chosen)
    thresholds = {}
    for name, keys in (("confirmation", conf_keys), ("exploratory", expl_keys)):
        m = rows["key"].is_in(keys).to_numpy()
        thresholds[name] = {p: pd_.threshold_calibration(rows["label"].to_numpy()[m & (rows["position"] == p).to_numpy()],
                                                         q_chosen[m & (rows["position"] == p).to_numpy()])
                            for p in POSITIONS}
    # centering cost: the chosen method without centring (diagnostic only)
    # (4) unconditional mixtures on roster rows (participation held-out 2025-26 is the gate)
    t = time.time()
    needed = sorted({c["method"] for c in choice.values() if c["method"]} | {"D"})
    rr, rmats = pd_.walk_forward_quantiles(app, sorted(roster["key"].unique().to_list()),
                                           [c for c in cols if c[0] in needed], targets=roster)
    rr = rr.filter(pl.col("p").is_not_null())
    rch = _per_position(rr, {c: v for c, v in rmats.items()}, choice)
    rq_cond = pd_.outcome_quantiles(rr, rch)
    uncond = {}
    for name, keys in (("confirmation", conf_keys), ("exploratory", expl_keys)):
        uncond[name] = {}
        for p in POSITIONS:
            m = (rr["key"].is_in(keys) & (rr["position"] == p)).to_numpy()
            uncond[name][p] = pd_.unconditional_block(rr.filter(pl.Series(m)), rq_cond[m])
    part_status = dict(pd_.PARTICIPATION_STATUS)
    uncond_status = {}
    for p in POSITIONS:
        u = uncond["exploratory"][p]
        c = pd_.UNCONDITIONAL_CRITERIA
        reasons = []
        if status[p]["status"] != "validated":
            reasons.append("conditional distribution withheld")
        if part_status[p] != "validated":
            reasons.append(f"ipm-1.0.0 participation is {part_status[p]} for {p}")
        if u.get("n", 0) and not c["coverage80"][0] <= u["coverage80"] <= c["coverage80"][1]:
            reasons.append(f"unconditional 80% coverage {u['coverage80']:.3f} outside {c['coverage80']}")
        if u.get("n", 0) and abs(u["absence"]["predicted"] - u["absence"]["observed"]) > c["absence_calibration_tolerance"]:
            reasons.append("absence rate miscalibrated")
        uncond_status[p] = {"status": "validated" if not reasons else "withheld", "reasons": reasons}
    print(f"unconditional {time.time() - t:.0f}s", file=sys.stderr)
    # (5) dependence and pairwise probabilities (appearance rows, confirmation + exploratory)
    t = time.time()
    sp = rows.with_columns(pd_.starters_pool_expr().alias("_sp"))["_sp"].to_numpy()
    dependence = pd_.residual_dependence(rows.filter(pl.Series(sp)))
    valid = ~np.isnan(chosen).any(1)
    pairwise = pd_.pairwise_calibration(rows.filter(pl.Series(valid)), q_chosen[valid])
    print(f"dependence + pairs {time.time() - t:.0f}s", file=sys.stderr)
    # (6) decision scenarios (roster rows, unconditional; needs from 2020-22 appearances)
    needs = pd_.scenario_needs(app.filter(pl.col("season") <= 2022))
    decisions = {"chosen": pd_.decision_scenarios(rr, rq_cond, needs)}
    if "D" in rmats:
        decisions["D_sensitivity"] = pd_.decision_scenarios(rr, pd_.outcome_quantiles(rr, rmats["D"]), needs)
    volatility = {w: pd_.volatility_signal(rows.filter(pl.col("key").is_in(k)))
                  for w, k in (("confirmation", conf_keys), ("exploratory", expl_keys))}
    report = {"version": pd_.DISTRIBUTION_VERSION, "input_fingerprint": inp["fingerprint"],
              "input_fingerprint_parts": inp["fingerprint_parts"], "scoring_fingerprint": inp["scoring"].fingerprint,
              "ifp_selection_sha": inp["sel_sha"], "participation_selection_sha": inp["psel_sha"],
              "data": {"appearance_rows": app.height, "roster_rows": roster.height, "fully_final_weeks": len(inp["weeks"]),
                       "last_week": inp["weeks"][-1]},
              "selection": choice, "development": dev, "confirmation": conf, "exploratory": expl,
              "frozen_bands_as_published": {"confirmation_in_sample": _frozen_bands(rows, inp["sel"], conf_keys),
                                            "exploratory_heldout": _frozen_bands(rows, inp["sel"], expl_keys)},
              "status": status, "status_exploratory_replay": status_expl, "thresholds": thresholds,
              "unconditional": uncond, "unconditional_status": uncond_status, "participation_status": part_status,
              "dependence": dependence, "pairwise": pairwise, "decisions": decisions, "volatility_signal": volatility,
              "criteria": {"selection": pd_.SELECTION_RULE, "validation": pd_.VALIDATION_CRITERIA,
                           "unconditional": pd_.UNCONDITIONAL_CRITERIA},
              "runtime_seconds": round(time.time() - t0, 1), "created_at": datetime.now(timezone.utc).isoformat()}
    _atomic_write(_out(args) / "distribution_report.json", json.dumps(_clean(report), indent=1, default=str))
    art = write_artifact(args, inp, app, choice, status, uncond_status)
    return summary(report) | {"artifact": art}


def _clean(o):
    if isinstance(o, float):
        return None if (np.isnan(o) or np.isinf(o)) else round(o, 6)
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.generic):
        return _clean(o.item())
    return o


def write_artifact(args, inp: dict, app: pl.DataFrame, choice: dict, status: dict, uncond_status: dict) -> str:
    """Write-once research artifact named by its content hash (identical inputs -> identical file)."""
    part = {p: "validated" if uncond_status[p]["status"] == "validated" else "not_approved" for p in POSITIONS}
    through = int(app.filter(pl.col("label").is_not_null())["key"].max())
    art = pd_.fit_artifact(app, choice, status, through_key=through, fingerprint=inp["fingerprint"],
                           scoring_fingerprint=inp["scoring"].fingerprint, selection_sha=inp["sel_sha"],
                           participation_status=part)
    path = _out(args) / f"point_distributions_{pd_.DISTRIBUTION_VERSION}_{art['content_hash']}.json"
    text = json.dumps(art, sort_keys=True)
    if path.exists():
        if path.read_text() != text:
            raise SystemExit(f"{path.name} exists with different content; refusing to overwrite")
        return str(path)
    _atomic_write(path, text)
    return str(path)


def cmd_artifact(args) -> dict:
    inp = load_inputs(args)
    rep_path, sel_path = _out(args) / "distribution_report.json", _out(args) / "distribution_selection.json"
    if not rep_path.exists() or not sel_path.exists():
        raise SystemExit("run `run` first")
    rep, sel = json.loads(rep_path.read_text()), json.loads(sel_path.read_text())
    for name, d in (("distribution_report.json", rep), ("distribution_selection.json", sel)):
        if d.get("input_fingerprint") != inp["fingerprint"]:
            raise SystemExit(f"{name} was made from other inputs (stale); rerun `run`")
    app, _ = cached_frames(args, inp)
    return {"artifact": write_artifact(args, inp, app, sel["choice"], rep["status"], rep["unconditional_status"])}


EXAMPLE_TARGETS = {"QB": (18.0,), "RB": (12.0,), "WR": (13.0, 15.0), "TE": (9.0,)}


def cmd_examples(args) -> dict:
    """Illustrations from the frozen artifact on the last fully final week's roster rows (pregame
    inputs only; the realized score is shown afterwards for context, not as evidence)."""
    inp = load_inputs(args)
    _, roster = cached_frames(args, inp)
    arts = sorted(_out(args).glob(f"point_distributions_{pd_.DISTRIBUTION_VERSION}_*.json"))
    art = next((a for a in map(lambda p: json.loads(p.read_text()), arts)
                if a["input_fingerprint"] == inp["fingerprint"]), None)
    if art is None:
        raise SystemExit("no artifact for the current inputs; run `run` first")
    live = duckdb.connect(str(args.live_db), read_only=True)
    names = dict(live.execute("SELECT gsis_id, display_name FROM nfl_players").fetchall())
    last = int(roster["key"].max())
    wk = roster.filter((pl.col("key") == last) & pl.col("p").is_not_null() & pl.col("mean").is_not_null())
    out = {"week": last, "rows": []}
    for pos, targets in EXAMPLE_TARGETS.items():
        g = wk.filter(pl.col("position") == pos)
        for t in targets:
            for r in g.with_columns((pl.col("mean") - t).abs().alias("_d")).sort("_d").head(3).iter_rows(named=True):
                s = pd_.artifact_summary(art, pos, r["mean"], r["p"], {f: r.get(f) for f in pd_.QR_FEATURES[pos]})
                out["rows"].append({"name": names.get(r["gsis_id"], r["gsis_id"]), "position": pos,
                                    "conditional_mean": round(r["mean"], 2), "p_play": round(r["p"], 3),
                                    "expected_points": round(r["p"] * r["mean"], 2),
                                    "conditional": s["conditional"], "unconditional": s["unconditional"],
                                    "withheld": s.get("unconditional_withheld_reason") or s.get("withheld_reason"),
                                    "realized_after_the_fact": r["label"]})
    return out


def summary(rep: dict) -> dict:
    out = {"selection": {p: v.get("method") for p, v in rep["selection"].items()},
           "status": {p: (v["status"], v.get("reasons")) for p, v in rep["status"].items()},
           "unconditional_status": rep["unconditional_status"]}
    for w in ("development", "confirmation", "exploratory"):
        out[w] = {p: {c: {k: round(m[k], 4) for k in ("crps", "coverage80", "p10_below_rate", "p90_above_rate",
                                                         "p50_mid_rate", "width80")}
                      for c, m in rep[w][p]["methods"].items()} | {"n": rep[w][p]["n"]}
                  for p in [*POSITIONS, "ALL"]}
    return out


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "artifact", "examples"])
    ap.add_argument("--ifp-dir", type=Path, default=_path("IFP_MODEL_DIR", "data/models"),
                    help="directory with the frozen selection.json and participation_selection.json (read-only)")
    ap.add_argument("--exits-dir", type=Path, default=_path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                    help="directory holding pbp_exits/ (weekly_learning.py sync-exits)")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=_path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--rebuild", action="store_true", help="ignore the cached out-of-sample frames")
    ap.add_argument("--refreeze", action="store_true", help="discard a frozen selection made from other inputs")
    ap.add_argument("--development-only", action="store_true",
                    help="stop after scoring development and freezing the selection (no later window is scored)")
    args = ap.parse_args(argv)
    result = {"run": cmd_run, "artifact": cmd_artifact, "examples": cmd_examples}[args.command](args)
    print(json.dumps(_clean(result), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
