"""RB projection diagnostics and pre-registered candidates. Offline, read-only on every database;
prints aggregates only; outputs go to --out (default data/models/research/rb, git-ignored).

    cd backend
    uv run python ../scripts/rb_research.py prepare    # feature store (cached by complete input fingerprint)
    uv run python ../scripts/rb_research.py diagnose   # development window 2021-2022 only
    uv run python ../scripts/rb_research.py develop    # grids on 2021-2022 only; writes the frozen config ONCE
    uv run python ../scripts/rb_research.py confirm    # frozen candidates on 2023-2026 wk 1-5 (write-once report)
    uv run python ../scripts/rb_research.py posthoc    # descriptive follow-up on the same window (selects nothing)

Inputs: the nflverse history database (sync_nfl_history.py), optionally a second nflverse-only database
holding the 2026 weeks played (sync_nfl.py with FANTASY_DB_PATH pointing at a scratch file; no league needed)
and the play-by-play injury evidence (weekly_learning.py sync-exits). Nothing is written to either database.
No ESPN projection is read. Research only: nothing here promotes a model or touches a consumer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401
import polars as pl

from app.analytics import rb_research as rb
from app.config import REPO_ROOT


def _path(env: str, default: str) -> Path:
    raw = Path(os.getenv(env) or default)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def _round(obj, nd: int = 4):
    if isinstance(obj, float):
        return None if obj != obj else round(obj, nd)
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(v, nd) for v in obj]
    return obj


def load(args):
    src = rb.load_all_sources(args.history_db, args.live_db, args.exits_dir)
    store, fp = rb.cached_store(src, args.out / "cache")
    return src, store, fp, rb.data_fingerprint(src)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_round(obj), indent=1, sort_keys=True, default=str))


def cmd_prepare(args) -> dict:
    t = time.time()
    src, store, store_fp, data_fp = load(args)
    sel = rb.verify_c_lambda(store)
    st = rb.rb_store(store)
    return {"store_rows": store.height, "rb_rows": st.height, "rb_rows_by_season": {
                int(s): int(n) for s, n in st.group_by("season").len().sort("season").iter_rows()},
            "store_fingerprint": store_fp, "data_fingerprint": data_fp, "ifp1_rb_selection": sel,
            "scoring_fingerprint": rb.PRODUCTION_SCORING_FINGERPRINT, "runtime_seconds": round(time.time() - t, 1)}


def cmd_diagnose(args) -> dict:
    """Development window only (2021-2022): where does ridge C lose to the season average?"""
    _, store, _, data_fp = load(args)
    models = list(rb.REFERENCE_MODELS)
    preds = rb.reference_predictions(store, rb.weeks_in(store, rb.DEVELOPMENT))
    d = rb.common_support(preds, models)
    seg = {**rb.rb_segments(), **rb.outcome_segments()}
    rep = {"window": list(rb.DEVELOPMENT), "n_rows": preds.height, "common_support_n": d.height,
           "pooled": rb.pooled_metrics(d, models),
           "by_season": {int(s): rb.pooled_metrics(g, models) for (s,), g in d.group_by(["season"], maintain_order=True)},
           "diffs": {f"{a}-{b}": rb.boot_diff(d, f"pred_{a}", f"pred_{b}")
                     for a, b in (("C", "A2"), ("C", "A1"), ("ifp2", "A2"), ("ifp2", "C"), ("A2", "A1"))},
           "segments": rb.segment_table(d, models, seg, vs=(("C", "A2"), ("ifp2", "A2"), ("ifp2", "C"))),
           "td_dependence": rb.error_decomposition(d, models),
           "volume_tiers": rb.volume_tier_table(d, models),
           "data_fingerprint": data_fp}
    write_json(args.out / "diagnostics.json", rep)
    return rep


def cmd_develop(args) -> dict:
    """Candidate grids on the development window only; freezes the choice. Refuses a second, different freeze."""
    src, store, _, data_fp = load(args)
    doc = args.doc.read_text()
    prereg = rb.preregistration_hash(doc)
    weeks = rb.weeks_in(store, rb.DEVELOPMENT)
    ref = rb.reference_predictions(store, weeks)
    st = rb.rb_store(store)
    selected, dev_preds = {}, ref
    for name in rb.CANDIDATE_GRIDS:
        selected[name] = rb.grid_search(st, ref, weeks, name)
        pred = rb.candidate_predictions(st, ref, weeks, name, {selected[name]["param"]: selected[name]["value"]})
        dev_preds = dev_preds.join(pred, on=["key", "gsis_id"], how="left")
    cfg = rb.frozen_config(selected, prereg, data_fp)
    frozen = args.out / "frozen.json"
    if frozen.exists() and json.loads(frozen.read_text()) != json.loads(json.dumps(cfg, default=str)):
        raise SystemExit(f"{frozen} already holds a different frozen configuration; candidates are frozen once")
    write_json_exact(frozen, cfg)
    report = rb.evaluate_window(dev_preds, list(rb.CANDIDATE_GRIDS))
    write_json(args.out / "develop.json", {"selected": selected, "development_report": report,
                                           "note": "development window: informational, cannot confirm anything"})
    return {"selected": selected, "development_report": report}


def write_json_exact(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str))


def cmd_confirm(args) -> dict:
    """One run of the frozen candidates on the confirmation window. The report is write-once."""
    report_path = args.out / "confirmation_report.json"
    if report_path.exists():
        raise SystemExit(f"{report_path} exists: the confirmation window is used once. Read it instead.")
    src, store, _, data_fp = load(args)
    cfg_path = args.out / "frozen.json"
    if not cfg_path.exists():
        raise SystemExit("no frozen.json: run `develop` first (candidates are frozen before confirmation)")
    cfg = json.loads(cfg_path.read_text())
    rb.check_frozen(cfg, rb.preregistration_hash(args.doc.read_text()), data_fp)
    weeks = [w for w in rb.weeks_in(store, rb.CONFIRMATION)]
    ref = rb.reference_predictions(store, weeks)
    st = rb.rb_store(store)
    preds = ref
    for name, sel in cfg["selected"].items():
        preds = preds.join(rb.candidate_predictions(st, ref, weeks, name, {sel["param"]: sel["value"]}),
                           on=["key", "gsis_id"], how="left")
    report = rb.evaluate_window(preds, list(cfg["selected"]))
    report["coverage"] = {"test_rows": preds.height, "common_support_rows": report["n"],
                          "abstained": preds.height - report["n"]}
    report["frozen"] = cfg
    report["timestamp_caveat"] = ("2025 and 2026 nflverse injury rows carry no date_modified: the pregame filter of the "
                                  "teammate-unavailability feature cannot be verified there; 2023-24 can")
    write_json_exact(report_path, _round(report))
    return report


def cmd_posthoc(args) -> dict:
    """After `confirm`: descriptive checks on the same window (placebo control for the teammate features,
    timestamp-verified slice, whether the development diagnostics replicate). Selects nothing; read-only."""
    src, store, _, data_fp = load(args)
    cfg_path = args.out / "frozen.json"
    if not (cfg_path.exists() and (args.out / "confirmation_report.json").exists()):
        raise SystemExit("run `develop` and `confirm` first")
    cfg = json.loads(cfg_path.read_text())
    rb.check_frozen(cfg, rb.preregistration_hash(args.doc.read_text()), data_fp)
    weeks = rb.weeks_in(store, rb.CONFIRMATION)
    ref = rb.reference_predictions(store, weeks)
    st = rb.rb_store(store)
    preds = ref
    for name, sel in cfg["selected"].items():
        preds = preds.join(rb.candidate_predictions(st, ref, weeks, name, {sel["param"]: sel["value"]}),
                           on=["key", "gsis_id"], how="left")
    sel = cfg["selected"]["tm_transfer"]
    placebo = rb.candidate_predictions(rb.placebo_teammate_store(st), ref, weeks, "tm_transfer",
                                       {sel["param"]: sel["value"]}).rename({"pred_tm_transfer": "pred_placebo"})
    report = rb.posthoc_report(preds, placebo)
    write_json(args.out / "posthoc.json", report)
    return report


def summarize(name: str, rep) -> str:
    return json.dumps(_round(rep), indent=1, default=str)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["prepare", "diagnose", "develop", "confirm", "posthoc"])
    ap.add_argument("--history-db", type=Path, default=_path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--live-db", type=Path, default=None,
                    help="optional nflverse-only database holding the 2026 weeks played (read-only)")
    ap.add_argument("--exits-dir", type=Path, default=_path("WEEKLY_MODEL_DIR", "data/models/weekly") / "pbp_exits")
    ap.add_argument("--out", type=Path, default=_path("RB_RESEARCH_DIR", "data/models/research/rb"))
    ap.add_argument("--doc", type=Path, default=REPO_ROOT / "docs" / "research" / "rb_projection_diagnostics.md")
    args = ap.parse_args(argv)
    result = {"prepare": cmd_prepare, "diagnose": cmd_diagnose, "develop": cmd_develop, "confirm": cmd_confirm,
              "posthoc": cmd_posthoc}[args.command](args)
    print(summarize(args.command, result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
