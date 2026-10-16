"""Research: temporally honest calibration of P(appears) (shadow only; see
docs/research/participation_calibration.md). Offline; prints aggregates only.

    cd backend
    uv run python ../scripts/participation_calibration.py run

Needs the ipm-1.0.0 research outputs in --models (selection.json, participation_selection.json; from
backtest_independent_projections.py select / participation-select). Steps:
  1. out-of-sample base probabilities for every week from OOS_START_SEASON on: the frozen ipm-1.0.0
     penalty refit walk-forward on earlier weeks only (participation.walk_forward), cached by input hash;
  2. every calibration candidate for every week of the development and diagnostic seasons, each map
     fitted only on out-of-sample predictions of EARLIER weeks;
  3. development report (validation seasons) -> pre-registered SELECTION_RULES; diagnostic report
     (2025-2026, already inspected: never used to choose);
  4. a shadow calibration artifact (selected map per position fitted on every out-of-sample week).
Writes only to <models>/research/ (git-ignored). Never touches the production participation artifact.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import time

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import backtest_independent_projections as bip  # noqa: E402

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import participation as pm  # noqa: E402
from app.analytics import participation_calibration as pc  # noqa: E402
from app.analytics import weekly_learning as wl  # noqa: E402
from app.external.identity import TEAM_ALIASES  # noqa: E402
from app.analytics.forecast_backtest import Protocol, completed_weeks  # noqa: E402
from app.config import get_settings  # noqa: E402

OOS_START_SEASON = 2021        # first season with out-of-sample base predictions (training from 2020)


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _frame_hash(df: pl.DataFrame) -> str:
    """Order-independent content hash of every non-nested column (features AND outcomes)."""
    flat = df.select([c for c, t in df.schema.items() if not t.is_nested()])
    return hashlib.sha256(np.sort(flat.hash_rows(seed=0).to_numpy()).tobytes()
                          + ",".join(sorted(flat.columns)).encode()).hexdigest()[:16]


def code_hash() -> str:
    """Hash of every module that can shape a cached result: all of app/ plus this script and the
    backtest script it builds stores with. Broader than the modules imported today, so a change to
    any helper they call (or will call) invalidates the cache."""
    app_dir = Path(pm.__file__).resolve().parents[1]
    files = sorted(app_dir.rglob("*.py")) + [Path(__file__).resolve(), Path(bip.__file__).resolve()]
    h = hashlib.sha256()
    for f in files:
        h.update(str(f.relative_to(app_dir.parent.parent)).encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:16]


def fully_final_weeks(src, weeks: list[tuple[int, int]], fetched_at: dict) -> list[tuple[int, int]]:
    """Only weeks that are fully final by the weekly_learning.week_finality rule: every scheduled game
    presumed final (both scores, fetched long enough after kickoff; `fetched_at`: {season: schedule
    fetch time}) and every scheduled team's box scores and snap counts synced. A partly played or
    partly ingested week would label rostered players with no panel row as non-participants. Team
    codes are compared after alias normalisation (2019 schedules say OAK where box scores say LV)."""
    norm = lambda df: df.with_columns(pl.col("team").replace(TEAM_ALIASES))  # noqa: E731
    view = SimpleNamespace(games=norm(src.games), panel=norm(src.panel))
    return [(s, w) for s, w in weeks if wl.week_finality(view, s, w, fetched_at.get(s))["fully_final"]]


def fetch_times(live, hist, season: int) -> dict:
    """{season: when its schedule was fetched}: the live sync for `season`, the latest successful
    history sync for every earlier season (None when unknown: such weeks never count as final)."""
    from app.analytics.schedule_intelligence import nfl_fetch_times

    h = None
    if hist is not None:
        h = hist.execute("SELECT max(started_at) FROM sync_runs WHERE source = 'nfl_history' AND status = 'ok'"
                         ).fetchone()[0]
        h = h.replace(tzinfo=timezone.utc) if h is not None and h.tzinfo is None else h
    out = {s: h for s in range(2000, season)}
    out[season] = nfl_fetch_times(live, season)["schedules"] if live is not None else None
    return out


def oos_predictions(src, protocol, ifp_sel: dict, psel: dict, cache_dir: Path,
                    fetched_at: dict) -> tuple[pl.DataFrame, str]:
    weeks = fully_final_weeks(src, completed_weeks(src, list(range(protocol.train_start_season,
                                                                     max(protocol.heldout_seasons) + 1))),
                              fetched_at)
    cond_store, store = bip.build_store(src, weeks), pm.build_store(src, weeks)
    data_hash = hashlib.sha256(store.sort("key", "gsis_id").select("key", "gsis_id", "played", "label")
                               .write_csv().encode()).hexdigest()[:16]
    lams = {p: [psel["positions"][p]["lambda"]] for p in pm.POSITIONS}
    spec = pm._ifp_cond_spec(ifp_sel)
    # The cache key covers every input of the prediction: both feature stores in full (any corrected
    # roster, injury, schedule or usage value changes it), the penalties, the conditional spec and the
    # code that builds and fits the models.
    tag = _hash({"data": data_hash, "store": _frame_hash(store), "cond_store": _frame_hash(cond_store),
                 "lams": lams, "cond": spec, "train_start": protocol.train_start_season,
                 "oos_start": OOS_START_SEASON, "version": pm.PARTICIPATION_VERSION, "polars": pl.__version__,
                 "code": code_hash()})
    path = cache_dir / f"oos_base_{tag}.parquet"
    if path.exists():
        return pl.read_parquet(path), data_hash
    test = [w for w in weeks if w[0] >= OOS_START_SEASON]
    preds = pm.walk_forward(store, cond_store, test, lams, spec, protocol.train_start_season)
    preds = preds.with_columns(pl.coalesce([pl.when(pl.col("position") == p).then(pl.col(f"p_{lams[p][0]:g}"))
                                            for p in pm.POSITIONS]).alias("p_model"))
    keep = ["season", "week", "key", "gsis_id", "position", "team", "played", "label", "cond", "p_model", "p_status",
            "p_trail", "inj_out", "inj_doubtful", "inj_questionable", "reserve_since_last_game", "injury_exit_last",
            "games_recent"]
    preds = preds.select(keep)
    cache_dir.mkdir(parents=True, exist_ok=True)
    preds.write_parquet(path)
    return preds, data_hash


def cmd_run(args) -> dict:
    t0 = time.time()
    models = args.models
    ifp_sel = json.loads((models / "selection.json").read_text())
    psel = json.loads((models / "participation_selection.json").read_text())
    live, hist = bip._ro(args.live_db), bip._ro(args.history_db)
    try:
        scoring = bip._scoring(live, args.season)
        for name, sel in (("participation_selection.json", psel), ("selection.json", ifp_sel)):
            if sel.get("scoring_fingerprint") != scoring.fingerprint:
                raise SystemExit(f"{name} was made under different league scoring")
        if psel.get("ifp_selection_sha") != bip._sha(models / "selection.json"):
            raise SystemExit("participation_selection.json was made from a different selection.json")
        src = bip._sources(live, hist, scoring.require_supported())
        fetched = fetch_times(live, hist, args.season)
    finally:
        for c in (live, hist):
            if c is not None:
                c.close()
    protocol = Protocol(heldout_seasons=(2025, args.season) if args.season > 2025 else (2025,))
    out_dir = models / "research"
    oos, data_hash = oos_predictions(src, protocol, ifp_sel, psel, out_dir, fetched)
    t_base = round(time.time() - t0, 1)
    report_teams = None
    if src.injuries is not None:
        report_teams = {(int(s), int(w), t) for s, w, t in src.injuries.select("season", "week", "team").unique().iter_rows()}
    dev_seasons, diag_seasons = list(protocol.validation_seasons), list(protocol.heldout_seasons)
    keys = sorted(oos.filter(pl.col("season").is_in(dev_seasons + diag_seasons))["key"].unique().to_list())
    t1 = time.time()
    preds = pc.walk_forward_calibration(oos, keys)
    preds = pc.with_report_flag(preds, report_teams)
    t_cal = round(time.time() - t1, 1)
    cols = pc.candidate_columns()
    cohorts = pc.report_cohorts(report_teams)
    dev = pc.compare(preds.filter(pl.col("season").is_in(dev_seasons)), cols, protocol.seed, cohorts)
    diag = pc.compare(preds.filter(pl.col("season").is_in(diag_seasons)), cols, protocol.seed, cohorts)
    selection = pc.select(dev, cols)
    fallback_rate = {c: round(float(preds[f"fallback_{c[2:]}"].mean()), 4) for c in cols
                     if c != "p_none" and f"fallback_{c[2:]}" in preds.columns}
    report = {"schema": "participation-calibration-report/v1", "created_at": datetime.now(timezone.utc).isoformat(),
              "base_model": {"participation_version": pm.PARTICIPATION_VERSION,
                             "lambda": {p: psel["positions"][p]["lambda"] for p in pm.POSITIONS},
                             "conditional_model": psel.get("conditional_model")},
              "protocol": {"oos_base_from_season": OOS_START_SEASON, "train_start_season": protocol.train_start_season,
                           "development_seasons": dev_seasons, "diagnostic_seasons_already_inspected": diag_seasons,
                           "methods": list(pc.METHODS), "windows": list(pc.WINDOWS), "rolling_weeks": pc.ROLLING_WEEKS,
                           "min_cal_rows": pc.MIN_CAL_ROWS, "iso_min_block": pc.ISO_MIN_BLOCK,
                           "shrink_prior": pc.SHRINK_PRIOR, "selection_rules": pc.SELECTION_RULES},
              "data_hash": data_hash, "scoring_fingerprint": scoring.fingerprint,
              "oos_rows": oos.height, "calibrated_rows": preds.height, "fallback_rate": fallback_rate,
              "development": dev, "diagnostic": diag, "selection": selection,
              "runtime_seconds": {"base_walk_forward": t_base, "calibration_walk_forward": t_cal,
                                  "total": round(time.time() - t0, 1)}}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "participation_calibration_report.json").write_text(json.dumps(report, indent=1, default=str))
    art = pc.fit_artifact(oos, {p: {"selected": v["selected"]} for p, v in selection.items()},
                          created_at=report["created_at"], base_model=report["base_model"],
                          fingerprints={"scoring": scoring.fingerprint, "data_hash": data_hash,
                                        "participation_selection_sha": bip._sha(models / "participation_selection.json")},
                          validation={"selection": selection, "rules": pc.SELECTION_RULES,
                                      "development_seasons": dev_seasons})
    apath = out_dir / f"participation_calibration_{pc.CAL_VERSION}.json"
    apath.write_text(json.dumps(art.to_dict(), indent=1, default=str))
    return {"report": str(out_dir / "participation_calibration_report.json"), "artifact": str(apath),
            "selected": {p: v["selected"] for p, v in selection.items()},
            "runtime_seconds": report["runtime_seconds"]}


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run"])
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=bip._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--models", type=Path, default=bip._path("INDEPENDENT_MODEL_DIR", "data/models"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    args = ap.parse_args(argv)
    print(json.dumps(cmd_run(args), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
