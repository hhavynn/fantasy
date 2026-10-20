"""ifp-2.3.0 backtest guard: the injury-return pair on top of ifp-2.2.0, on identical player-weeks (read-only on both DBs).

    cd backend
    uv run python ../scripts/ifp22_backtest.py build                  # the ifp-2.2.0 guard's store (cached, reused here)
    uv run python ../scripts/ifp23_backtest.py run                    # walk-forward, leave-one-out, the guard

Rule (docs/research/ifp23_promotion.md, fixed before the run; the ifp-2.2.0 guard's owner-approved safeguard): per
position (RB, WR, TE), walk-forward exactly as production trains (injury_return_model.fit: ifp-2.2.0's training rows,
frozen penalties and recency, refitted every week on strictly earlier weeks), predict ifp-2.2.0 (`b22`), the full
pair (`full`) and each leave-one-out (`drop_<component>`). A component whose leave-one-out difference MAE(full) -
MAE(drop_c) has a week-clustered 95% CI entirely above 0 on the evaluation window makes the position clearly worse and
is left out for that position; every neutral or better component ships. A second pass re-checks the shipped set the
same way. QB is ifp-2.2.0 unchanged and is not compared.

Outputs: <ifp-dir>/research/ifp23/ (git-ignored): preds.parquet, report.json.
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

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import injury_return_model as rm  # noqa: E402
from app.analytics.forecast_backtest import week_bootstrap_mae_diff  # noqa: E402
from app.analytics.forecast_features import key  # noqa: E402
from app.config import get_settings  # noqa: E402

SEED = b22.SEED
RESAMPLES = b22.RESAMPLES
EVALUATION = b22.EVALUATION
POSITIONS = rm.RETURN_POSITIONS


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "ifp23"
    p.mkdir(parents=True, exist_ok=True)
    return p


def variants(position: str, comps: tuple[str, ...] | None = None) -> dict[str, tuple[str, ...]]:
    """name -> components of every model compared at the position."""
    cand = tuple(rm.CANDIDATE[position]) if comps is None else tuple(comps)
    return {"b22": (), "full": cand, **{f"drop_{c}": tuple(x for x in cand if x != c) for c in cand}}


def walk_forward(store: pl.DataFrame, weeks: list[tuple[int, int]], cfg,
                 var: dict[str, dict[str, tuple[str, ...]]] | None = None) -> pl.DataFrame:
    """pred_<variant> for every RB/WR/TE store row of `weeks` (null = abstention); training strictly earlier."""
    var = var or {p: variants(p) for p in POSITIONS}
    out = []
    for season, week in weeks:
        k = key(season, week)
        for pos in POSITIONS:
            rows = store.filter((pl.col("key") == k) & (pl.col("position") == pos))
            if rows.height == 0:
                continue
            ok = rows["games_recent"].fill_null(0).to_numpy() > 0
            cols = {}
            for name, comps in var[pos].items():
                models, _, fb = rm.fit(store, season, week, cfg, {pos: comps}, positions=(pos,))
                cols[f"pred_{name}"] = np.where(ok, rm.predict_points(models, fb, rows, pos), np.nan)
            out.append(rows.select("key", "season", "week", "gsis_id", "position", "label", "ret_cohort")
                       .with_columns([pl.Series(c, v).fill_nan(None) for c, v in cols.items()]))
        print(f"  {season} w{week:02d}", file=sys.stderr, flush=True)
    return pl.concat(out, how="diagonal_relaxed")


def _compare(d: pl.DataFrame, a: str, b: str) -> dict:
    r = week_bootstrap_mae_diff(d, f"pred_{a}", f"pred_{b}", RESAMPLES, SEED, exact=True)
    return {"diff": None if r["diff"] is None else round(r["diff"], 4),
            "ci95": None if r["ci95"] is None else [round(v, 4) for v in r["ci95"]],
            "ci95_exact": r["ci95"]}


def position_report(p: pl.DataFrame, pos: str, names: list[str]) -> dict:
    d = p.filter((pl.col("position") == pos) & pl.col("label").is_not_null()
                 & pl.all_horizontal([pl.col(f"pred_{n}").is_not_null() for n in names]))
    y = d["label"].to_numpy()
    out = {"n": d.height, "weeks": d.select("key").n_unique(), "returner_rows": int(d["ret_cohort"].sum()),
           "models": {n: {"mae": round(float(np.abs(d[f"pred_{n}"].to_numpy() - y).mean()), 4),
                          "bias": round(float((d[f"pred_{n}"].to_numpy() - y).mean()), 4)} for n in names}}
    out["full_vs_b22"] = _compare(d, "full", "b22")
    out["full_vs_b22_returners"] = _compare(d.filter(pl.col("ret_cohort")), "full", "b22")
    out["leave_one_out"] = {n[5:]: _compare(d, "full", n) for n in names if n.startswith("drop_")}
    return out


def guard(rep: dict) -> dict[str, list[str]]:
    """Components whose leave-one-out CI lies entirely above 0 (unrounded): the full model is clearly worse with them."""
    return {pos: sorted(c for c, v in r["leave_one_out"].items() if v["ci95_exact"] and v["ci95_exact"][0] > 0)
            for pos, r in rep.items()}


def cmd_run(args) -> dict:
    t = time.time()
    store, _, prov = b22.build(SimpleNamespace(**{**vars(args), "rebuild": False}))
    src, _ = b22.hr._sources(args)
    store = rm.return_columns(store, src.games)
    cfg = ch.ChallengerConfig.load()
    weeks = b22._weeks(store, EVALUATION)
    preds = walk_forward(store, weeks, cfg)
    preds.write_parquet(_out(args) / "preds.parquet")
    rep = {pos: position_report(preds, pos, list(variants(pos))) for pos in POSITIONS}
    dropped = guard(rep)
    shipped = {pos: [c for c in rm.CANDIDATE[pos] if c not in dropped[pos]] for pos in POSITIONS}
    second = {}
    if any(dropped.values()):
        var = {pos: variants(pos, tuple(shipped[pos])) for pos in POSITIONS}
        p2 = walk_forward(store, weeks, cfg, var)
        second = {pos: position_report(p2, pos, list(var[pos])) for pos in POSITIONS}
        dropped2 = guard(second)
        shipped = {pos: [c for c in shipped[pos] if c not in dropped2[pos]] for pos in POSITIONS}
        dropped = {pos: sorted(set(dropped[pos]) | set(dropped2[pos])) for pos in POSITIONS}
    out = {"schema": "ifp23-backtest/v1", "created_at": datetime.now(timezone.utc).isoformat(),
           "rule": "leave-one-out MAE(full) - MAE(drop_c), week-clustered 95% CI (whole weeks resampled, "
                   f"{RESAMPLES} draws, seed {SEED}); CI entirely above 0 -> component left out for the position",
           "evaluation_window": EVALUATION, "weeks": len(weeks), "store": {k: prov.get(k) for k in ("fingerprint",
                                                                                                   "rows", "weeks")},
           "positions": rep, "dropped": dropped, "shipped": shipped, "second_pass": second,
           "empty": not any(shipped.values()), "runtime_seconds": round(time.time() - t, 1)}
    (_out(args) / "report.json").write_text(json.dumps(out, indent=1, default=str))
    return {"dropped": dropped, "shipped": shipped, "empty": out["empty"],
            "full_vs_b22": {p: rep[p]["full_vs_b22"] for p in POSITIONS},
            "full_vs_b22_returners": {p: rep[p]["full_vs_b22_returners"] for p in POSITIONS},
            "leave_one_out": {p: rep[p]["leave_one_out"] for p in POSITIONS},
            "n": {p: [rep[p]["n"], rep[p]["returner_rows"], rep[p]["weeks"]] for p in POSITIONS},
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
    res = {"run": cmd_run}[args.command](args)
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
