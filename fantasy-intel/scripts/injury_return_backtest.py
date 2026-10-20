"""Injury-return pair backtest: ifp-2.2.0 plus the shadow's two candidates on identical player-weeks (research only;
read-only on both DBs; docs/research/injury_return_split.md section 4).

    cd backend
    uv run python ../scripts/ifp22_backtest.py build                  # the ifp-2.2.0 guard's store (cached, reused here)
    uv run python ../scripts/injury_return_backtest.py run            # walk-forward, leave-one-out, the guard, slices

This is the engineering check run when the owner considered promoting the pair as ifp-2.3.0 (not shipped: the
prospective shadow decides). It uses the shadow's own definitions (injury_return_shadow.candidate_columns and EXTRA)
on the ifp-2.2.0 guard's store, so nothing here is production code.

Rule (ifp-2.2.0's guard, written down before the run): per position (RB, WR, TE), walk-forward exactly as production
trains (challenger.train_rows / fit_linear with ifp-2.2.0's features, frozen penalties and recency, every week refitted
on strictly earlier weeks), predict ifp-2.2.0 (`base`), both columns (`both`) and each single (`irs` = snap_trend_ret
only, `irb` = ret_ind only). A component whose leave-one-out difference MAE(both) - MAE(both without it) has a
week-clustered 95% CI entirely above 0 makes the position clearly worse and would be left out. Reported alongside:
every model vs `base` on all rows, returners and play-through rows.

The window overlaps the 2023-25 seasons injury_return_snap_trend.md inspected: these numbers are an engineering check,
never evidence for the shadow's pre-registered look.

Outputs: <ifp-dir>/research/injury_return_backtest/ (git-ignored): preds.parquet, report.json.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402
import ifp22_backtest as b22  # noqa: E402
import injury_return_shadow as irs  # noqa: E402

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import context_model as cm  # noqa: E402
from app.analytics.forecast_backtest import week_bootstrap_mae_diff  # noqa: E402
from app.analytics.forecast_features import key  # noqa: E402
from app.config import get_settings  # noqa: E402

SEED = b22.SEED
RESAMPLES = b22.RESAMPLES
EVALUATION = b22.EVALUATION
POSITIONS = irs.POSITIONS
MODELS = {"base": (), "irs": irs.EXTRA[irs.IRS], "irb": irs.EXTRA[irs.IRB], "both": irs.EXTRA[irs.BOTH]}
LEAVE_ONE_OUT = {"return_slope": "irb", "return_bias": "irs"}     # component -> `both` without it


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "injury_return_backtest"
    p.mkdir(parents=True, exist_ok=True)
    return p


def walk_forward(store: pl.DataFrame, weeks: list[tuple[int, int]], cfg) -> pl.DataFrame:
    """pred_<model> for every RB/WR/TE store row of `weeks` (null = abstention); training strictly earlier."""
    out = []
    for season, week in weeks:
        k = key(season, week)
        for pos in POSITIONS:
            rows = store.filter((pl.col("key") == k) & (pl.col("position") == pos))
            tr = ch.train_rows(store, k, pos)
            if rows.height == 0 or tr.height < ch.MIN_TRAIN_ROWS:
                continue
            assert tr["key"].max() < k, "training row at or after the test week"
            sp = cfg.spec(pos)
            ok = rows["games_recent"].fill_null(0).to_numpy() > 0
            cols = {}
            for name, extra in MODELS.items():
                m = ch.fit_linear(tr, pos, [*cm.features(cfg, pos), *extra], sp.lam[pos], loss=sp.loss,
                                  huber_c=sp.huber_c, half_life_weeks=sp.half_life_weeks, target_key=k, name=name)
                cols[f"pred_{name}"] = np.where(ok, m.predict(rows), np.nan)
            out.append(rows.select("key", "season", "week", "gsis_id", "position", "label", "ret_cohort",
                                   "injury_exit_last")
                       .with_columns([pl.Series(c, v).fill_nan(None) for c, v in cols.items()]))
        print(f"  {season} w{week:02d}", file=sys.stderr, flush=True)
    return pl.concat(out, how="diagonal_relaxed")


def _compare(d: pl.DataFrame, a: str, b: str) -> dict:
    r = week_bootstrap_mae_diff(d, f"pred_{a}", f"pred_{b}", RESAMPLES, SEED, exact=True)
    return {"n": d.height, "diff": r["diff"], "ci95": r["ci95"]}


def report(p: pl.DataFrame) -> dict:
    d = p.filter(pl.col("label").is_not_null() & pl.all_horizontal([pl.col(f"pred_{n}").is_not_null()
                                                                      for n in MODELS]))
    slices = {"all": pl.lit(True), "returner": pl.col("ret_cohort"),
              "play_through": (pl.col("injury_exit_last") == 1) & ~pl.col("ret_cohort")}
    out = {"weeks": d.select("key").n_unique(), "leave_one_out": {}, "vs_base": {}}
    for pos in POSITIONS:
        g = d.filter(pl.col("position") == pos)
        out["leave_one_out"][pos] = {c: _compare(g, "both", m) for c, m in LEAVE_ONE_OUT.items()}
    for name in ("both", "irs", "irb"):
        out["vs_base"][name] = {s: {**{pos: _compare(d.filter((pl.col("position") == pos) & e), name, "base")
                                       for pos in POSITIONS},
                                    "pooled": _compare(d.filter(e), name, "base")}
                                for s, e in slices.items()}
    return out


def guard(rep: dict) -> dict[str, list[str]]:
    return {pos: sorted(c for c, v in r.items() if v["ci95"] and v["ci95"][0] > 0)
            for pos, r in rep["leave_one_out"].items()}


def cmd_run(args) -> dict:
    t = time.time()
    store, _, prov = b22.build(SimpleNamespace(**{**vars(args), "rebuild": False}))
    src, _ = b22.hr._sources(args)
    store = irs.candidate_columns(store, src.games)
    cfg = ch.ChallengerConfig.load()
    weeks = b22._weeks(store, EVALUATION)
    preds = walk_forward(store, weeks, cfg)
    preds.write_parquet(_out(args) / "preds.parquet")
    rep = report(preds)
    out = {"schema": "injury-return-backtest/v1", "created_at": datetime.now(timezone.utc).isoformat(),
           "rule": "leave-one-out MAE(both) - MAE(both without c), week-clustered 95% CI (whole weeks resampled, "
                   f"{RESAMPLES} draws, seed {SEED}); CI entirely above 0 -> component left out for the position",
           "evaluation_window": EVALUATION, "weeks": len(weeks),
           "store": {k: prov.get(k) for k in ("fingerprint", "rows", "weeks")}, "report": rep,
           "guard_dropped": guard(rep), "runtime_seconds": round(time.time() - t, 1)}
    (_out(args) / "report.json").write_text(json.dumps(out, indent=1, default=str))
    rnd = lambda v: {"n": v["n"], "diff": round(v["diff"], 4),  # noqa: E731
                     "ci95": [round(x, 4) for x in v["ci95"]]} if v["diff"] is not None else v
    return {"guard_dropped": out["guard_dropped"], "weeks": len(weeks),
            "leave_one_out": {p: {c: rnd(v) for c, v in r.items()} for p, r in rep["leave_one_out"].items()},
            "vs_base": {n: {s: {k: rnd(v) for k, v in r.items()} for s, r in sl.items()}
                        for n, sl in rep["vs_base"].items()},
            "runtime_seconds": out["runtime_seconds"]}


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--reuse", action="store_true", help="reuse the cached ifp-2.2.0 store whatever its fingerprint")
    args = ap.parse_args(argv)
    print(json.dumps({"run": cmd_run}[args.command](args), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
