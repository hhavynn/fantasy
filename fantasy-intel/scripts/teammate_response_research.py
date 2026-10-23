"""Research: the production model's teammate response against two baselines (issue #213 v1, research only; see
docs/research/teammate_response.md and app/analytics/teammate_response.py, whose DECISION_RULE is fixed before the run).

    cd backend
    uv run python ../scripts/sync_nfl_history.py --seasons 2020-2025 --db HIST
    uv run python ../scripts/weekly_learning.py sync-exits --history-seasons 2020-2025 --history-db HIST --out OUT
    uv run python ../scripts/weekly_learning.py sync-depth --history-db HIST --out OUT
    uv run python ../scripts/teammate_response_research.py --live-db LIVE --history-db HIST --exits-dir OUT --ifp-dir IFP

The store is ifp22_backtest's (ifp-2.1.0's training rows plus every ifp-2.2.0 component input, cached by input
fingerprint). Walk-forward exactly as production trains (context_model.fit with the SHIPPED components, refitted every
evaluation week on strictly earlier weeks). For every RB and WR row of an evaluation week the same fitted model
predicts twice: with the row's teammate inputs (`pred_model`) and with them neutral (`pred_zero`). Writes
<ifp-dir>/research/teammate_response/{preds.parquet,report.json} (git-ignored) and prints the report. Never writes an
artifact, a run, the ledger, the registry or any production table.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import ifp22_backtest as ib  # noqa: E402

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import context_model as cm  # noqa: E402
from app.analytics import teammate_response as tr  # noqa: E402
from app.analytics.forecast_features import key  # noqa: E402
from app.config import get_settings  # noqa: E402

EVALUATION = ((2023, 1), (2025, 18))      # finished seasons only (the history file)


def walk(store: pl.DataFrame, weeks: list[tuple[int, int]], cfg) -> pl.DataFrame:
    for pos in tr.POSITIONS:
        assert cm.TEAMMATES in cm.SHIPPED[pos], f"{pos}: the production model ships no teammate group"
    out = []
    for season, week in weeks:
        k = key(season, week)
        rows_k = store.filter(pl.col("key") == k)
        for pos in tr.POSITIONS:
            rows = rows_k.filter(pl.col("position") == pos)
            if rows.height == 0:
                continue
            models, n, fb = cm.fit(store, season, week, cfg, positions=(pos,))
            ok = rows["games_recent"].fill_null(0).to_numpy() > 0
            pm = np.where(ok, cm.predict_points(models, fb, rows, pos), np.nan)
            pz = np.where(ok, cm.predict_points(models, fb, tr.neutral(rows, pos), pos), np.nan)
            # Diagnostic (not deciding; added after the first results): the shipped model retrained WITHOUT the
            # teammate group, as the ifp-2.2.0 promotion's leave-one-out did. Setting the inputs to 0 inside a
            # model trained with them can sit off its training distribution; this baseline cannot.
            drop = tuple(c for c in cm.SHIPPED[pos] if c != cm.TEAMMATES)
            md, _, fbd = cm.fit(store, season, week, cfg, {pos: drop}, positions=(pos,))
            pd_ = np.where(ok, cm.predict_points(md, fbd, rows, pos, drop), np.nan)
            out.append(rows.select("season", "week", "gsis_id", "position", "label").with_columns(
                pl.Series("is_event", tr.event_mask(rows, pos)),
                pl.Series("pred_model", pm).fill_nan(None), pl.Series("pred_zero", pz).fill_nan(None),
                pl.Series("pred_retrained", pd_).fill_nan(None)))
        print(f"  {season} w{week:02d}", file=sys.stderr, flush=True)
    return pl.concat(out, how="diagonal_relaxed")


def history(store: pl.DataFrame, seasons: list[int]) -> pl.DataFrame:
    """Every RB / WR store row of the seasons, with its event flag (the split baseline's history)."""
    parts = []
    for pos in tr.POSITIONS:
        rows = store.filter(pl.col("position").eq(pos) & pl.col("season").is_in(seasons))
        parts.append(rows.select("season", "week", "gsis_id", "label").with_columns(
            pl.Series("is_event", tr.event_mask(rows, pos))))
    return pl.concat(parts)


def git_sha() -> str | None:
    """The checked-out commit, plus "-dirty" when tracked files differ from it (pre-registration evidence)."""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                               text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return sha + ("-dirty" if dirty else "")


def main() -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ifp-dir", type=Path, required=True)
    ap.add_argument("--exits-dir", type=Path, required=True)
    ap.add_argument("--live-db", type=Path, required=True)
    ap.add_argument("--history-db", type=Path, required=True)
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--from-preds", action="store_true",
                    help="re-evaluate the stored preds.parquet (no walk-forward); the report says so")
    args = ap.parse_args()
    if args.live_db.resolve() == Path(settings.db_path).resolve():
        raise SystemExit("pass a research live file (league scoring only), not the production database")
    t = time.time()
    out = args.ifp_dir / "research" / "teammate_response"
    if args.from_preds:
        preds = pl.read_parquet(out / "preds.parquet")
        report = {"evaluation": EVALUATION, "re_evaluated_from": str(out / "preds.parquet"), "git_sha": git_sha(),
                  "model_version": cm.MODEL_VERSION, **tr.evaluate(preds), "seconds": round(time.time() - t, 1)}
        (out / "report_reevaluated.json").write_text(json.dumps(report, indent=1, default=str))
        print(json.dumps(report, indent=1, default=str))
        return 0
    store, _, prov = ib.build(SimpleNamespace(**vars(args), reuse=False))
    cfg = ch.ChallengerConfig.load()
    weeks = ib._weeks(store, EVALUATION)
    preds = walk(store, weeks, cfg)
    seasons = sorted({s for s, _ in weeks})
    splits = tr.split_deltas(history(store, seasons).with_columns(pl.col("season").cast(pl.Int64),
                                                                  pl.col("week").cast(pl.Int64)))
    preds = preds.with_columns(pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64)).join(
        splits, on=["season", "gsis_id", "week"], how="left")
    out.mkdir(parents=True, exist_ok=True)
    preds.write_parquet(out / "preds.parquet")
    report = {"evaluation": EVALUATION, "weeks": len(weeks), "store": {k: prov.get(k) for k in ("rows", "weeks",
              "scoring_fingerprint", "depth_note", "depth_coverage")}, "components": {p: list(cm.SHIPPED[p]) for p in tr.POSITIONS},
              "model_version": cm.MODEL_VERSION, **tr.evaluate(preds), "seconds": round(time.time() - t, 1)}
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
