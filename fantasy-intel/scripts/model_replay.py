"""Weekly model replay: today's production model re-run on every finished week of the season (offline).

    cd backend
    uv run python ../scripts/model_replay.py show    [--season 2026] [--json]   # replay and print, writes nothing
    uv run python ../scripts/model_replay.py publish [--season 2026]            # replay, store one snapshot row
    uv run python ../scripts/model_replay.py history [--season 2026]            # stored snapshots, week by week

For every fully final week W of the season, the production model (context_model, its SHIPPED components) and the
previous production version (ifp-2.1.0's frozen spec, `b21` in scripts/ifp22_backtest.py) are refitted on rows of
weeks strictly before W, exactly as production trains, and predict W; the recent-average baseline A1 rides along.
Scored on identical player-weeks (players who played and had a recent game), MAE per week and position.

`publish` stores the snapshot as one write-once row of `model_replays` (app/analytics/model_replay.py) unless the
newest snapshot already has the same model and data fingerprints. scripts/refresh_all.py runs it after the
projection-performance report, so every refresh re-scores the current model on all finished weeks: a model change shows
up on the page the next refresh, week by week, without waiting for new games.

This is hindsight: the features are rebuilt from today's data, the outcome is conditional on playing, and it is never
prospective evidence, never a promotion input, never mixed into the scorecard. Inputs: the cached walk-forward store
of scripts/ifp22_backtest.py (`build`, rebuilt when its inputs change), read-only on both databases except the one
INSERT of `publish`.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402
import ifp22_backtest as ib  # noqa: E402

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import context_model as cm  # noqa: E402
from app.analytics import model_replay as mr  # noqa: E402
from app.analytics.forecast_features import POSITIONS  # noqa: E402
from app.analytics.injury_model import MODEL_VERSION as PRIOR_VERSION  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.connection import connect as db_connect  # noqa: E402

# Model identity: the model version and config plus the source of every repo module the replay actually loaded
# (context_model and everything it imports transitively, the store builders, the frozen QB scenario script). Modules
# that only package or serve the replay are left out, so editing the page plumbing is not "a new model".
NOT_MODEL = {"__main__", "model_replay", "app.analytics.model_replay", "app.analytics.performance_report", "_bootstrap"}
NOT_MODEL_PREFIXES = ("app.db", "app.api", "app.config")
PRED = {"model": "pred_model", "prior_version": "pred_prior", "recent_avg": "pred_A1"}
NOTES = [
    "Hindsight replay: for each finished week the model is refitted on earlier weeks only and predicts that week, "
    "but the inputs are today's data (stat corrections, depth charts and injury reports as synced now). It is not "
    "what the site showed that week and never counts as prospective evidence.",
    "Scored on players who played and had at least one recent game (rookies' debuts are left out); every forecast is "
    "scored on exactly the same player-weeks.",
    "A new snapshot is stored when the model, its inputs or the replayed forecasts change; the page compares the newest "
    "snapshot with the newest one of the previous model, week by week.",
]


def model_files() -> list[Path]:
    """Repo source files of every module loaded so far, minus NOT_MODEL. Call after the replay ran, so every lazily
    imported model module (e.g. context_model's _qs) is in sys.modules; config JSON next to app/analytics is added."""
    root = SCRIPTS.parent.resolve()
    files = set()
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if not f or name in NOT_MODEL or name.startswith(NOT_MODEL_PREFIXES):
            continue
        path = Path(f).resolve()
        if path == Path(__file__).resolve():        # this script, whatever name it was loaded under (__main__)
            continue
        if path.is_relative_to(root / "backend" / "app") or path.parent == root / "scripts":
            files.add(path)
    files.add((root / "backend" / "app" / "analytics" / "challenger_config.json").resolve())
    return sorted(files)


def model_fingerprint(cfg) -> str:
    """Which model: version, config, shipped components and the source of every module the replay loaded."""
    root = SCRIPTS.parent.resolve()
    h = hashlib.sha256(json.dumps({"version": cm.MODEL_VERSION, "config": cm.config_hash(cfg),
                                   "shipped": {p: list(cm.SHIPPED[p]) for p in POSITIONS}}, sort_keys=True).encode())
    for f in model_files():
        h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:16]


def data_fingerprint(store: pl.DataFrame, cands: pl.DataFrame, store_fp: str | None) -> str:
    """The replay's inputs: the labeled rows, every feature column of the store, the QB start candidates and the
    store's own provenance fingerprint (its source frames). A corrected injury, roster or depth input changes it."""
    h = hashlib.sha256((store_fp or "").encode())
    for df in (store, cands):
        h.update(",".join(sorted(df.columns)).encode())
        h.update(np.sort(df.select(sorted(df.columns)).hash_rows(seed=0).to_numpy()).tobytes())
    return h.hexdigest()[:16]


def output_fingerprint(preds: pl.DataFrame) -> str:
    """What the replay produced: every scored row's forecasts and outcome (rounded)."""
    if preds.height == 0:
        return hashlib.sha256(b"").hexdigest()[:16]
    rows = preds.select("key", "gsis_id", *[pl.col(c).round(4) for c in (*PRED.values(), "label")]).sort("key", "gsis_id")
    return hashlib.sha256(rows.write_csv().encode()).hexdigest()[:16]


def replay(store: pl.DataFrame, cands: pl.DataFrame, weeks: list[tuple[int, int]], cfg) -> pl.DataFrame:
    """pred_model / pred_prior / pred_A1 for every store row of `weeks` (walk-forward; training strictly earlier)."""
    var = {p: {"model": tuple(cm.SHIPPED[p]), "prior": ()} for p in POSITIONS}
    preds = ib.walk_forward(store, cands, weeks, cfg, var)
    return preds.join(store.select("key", "gsis_id", "pred_A1"), on=["key", "gsis_id"], how="left")


def _cell(d: pl.DataFrame) -> dict:
    if d.height == 0:
        return {"n": 0, "mae": {m: None for m in mr.METRICS}}
    y = d["label"].to_numpy()
    return {"n": d.height, "mae": {m: round(float(np.abs(d[c].to_numpy() - y).mean()), 4) for m, c in PRED.items()}}


def summarize(preds: pl.DataFrame) -> tuple[list[dict], dict]:
    """(weeks, totals) on paired rows: label and all three forecasts present."""
    d = preds.filter(pl.col("label").is_not_null() & pl.all_horizontal([pl.col(c).is_not_null() for c in PRED.values()]))
    weeks = []
    for wk in sorted(d["week"].unique().to_list()):
        w = d.filter(pl.col("week") == wk)
        weeks.append({"week": int(wk), "groups": {**{p: _cell(w.filter(pl.col("position") == p)) for p in POSITIONS},
                                                  "ALL": _cell(w)}})
    totals = {**{p: _cell(d.filter(pl.col("position") == p)) for p in POSITIONS}, "ALL": _cell(d)}
    return weeks, totals


def build_payload(args) -> dict:
    store, cands, prov = ib.build(args)
    cfg = ch.ChallengerConfig.load()
    weeks = sorted({(int(s), int(w)) for s, w in store.filter(pl.col("season") == args.season)
                    .select("season", "week").unique().iter_rows()})
    preds = replay(store, cands, weeks, cfg) if weeks else pl.DataFrame()
    wk, totals = summarize(preds) if weeks else ([], {g: {"n": 0, "mae": {m: None for m in mr.METRICS}}
                                                      for g in mr.GROUPS})
    return {"schema_version": mr.SCHEMA_VERSION, "season": args.season,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": {"version": cm.MODEL_VERSION, "fingerprint": model_fingerprint(cfg),
                      "components": {p: list(cm.SHIPPED[p]) for p in POSITIONS}},
            "prior_version": {"version": PRIOR_VERSION},
            "data": {"fingerprint": data_fingerprint(store, cands, prov.get("fingerprint")),
                     "store_fingerprint": prov.get("fingerprint"), "output_fingerprint": output_fingerprint(preds),
                     "last_week": max((w for _, w in weeks), default=None)},
            "population": "players who played and had a recent game; identical player-weeks for every forecast",
            "weeks": wk, "totals": totals, "notes": NOTES}


def _fmt(v) -> str:
    return "  —  " if v is None else f"{v:5.2f}"


def print_payload(p: dict) -> None:
    print(f"Replay {p['season']} · {p['model']['version']} ({p['model']['fingerprint']}) vs {p['prior_version']['version']}"
          f" vs recent average · built {p['generated_at']}")
    print(f"{'week':>4}  " + "  ".join(f"{g:^19}" for g in mr.GROUPS))
    print(f"{'':>4}  " + "  ".join(f"{'model prior  avg':^19}" for _ in mr.GROUPS))
    for w in [*p["weeks"], {"week": "all", "groups": p["totals"]}]:
        cells = ["".join(f" {_fmt(w['groups'][g]['mae'][m])}" for m in mr.METRICS) for g in mr.GROUPS]
        print(f"{w['week']:>4}  " + "  ".join(cells))


def print_comparison(cmp: dict, title: str) -> None:
    print(f"\n{title}  (negative = closer; |diff| < {mr.SAME_WITHIN} reads as same)")
    for w in cmp["weeks"]:
        parts = [f"{g} {c['diff']:+.2f} {c['direction']}" if c else f"{g} —" for g, c in w["groups"].items()]
        print(f"  week {w['week']:>2}: " + " · ".join(parts))
    t = cmp["tally"]["ALL"]
    print(f"  all positions: better in {t['better']} weeks, worse in {t['worse']}, about the same in {t['same']}")


def cmd_show(args) -> dict:
    p = build_payload(args)
    if args.json:
        print(json.dumps(p, indent=1))
    else:
        print_payload(p)
        print_comparison(mr.vs_prior(p), f"{p['model']['version']} minus {p['prior_version']['version']}")
    return p


def cmd_publish(args) -> dict:
    p = build_payload(args)
    if not p["weeks"]:
        print(f"no fully final {args.season} week yet: nothing stored")
        return p
    from app.db.connection import open_db

    with open_db(args.live_db) as con:       # one INSERT, after the read-only build has closed its connections
        sha, created = mr.store_replay(con, p)
    print(f"{'stored' if created else 'unchanged (same model, inputs and forecasts as the newest snapshot)'}: {sha[:16]} · "
          f"weeks {[w['week'] for w in p['weeks']]} · MAE {p['totals']['ALL']['mae']['model']}")
    return p


def cmd_history(args) -> dict:
    con = db_connect(args.live_db, read_only=True)
    try:
        res = mr.response(con, args.season)
    finally:
        con.close()
    if not res["available"]:
        print(res["unavailable_reason"])
        return res
    for h in res["history"]:
        print(f"  {h['generated_at']}  {h['model']['version']} {h['model']['fingerprint']}  weeks {h['weeks']}  "
              f"MAE {h['mae']} (n {h['n']})")
    if res["vs_previous"]:
        print_comparison(res["vs_previous"], f"newest model minus previous model ({res['previous']['model']['fingerprint']})")
    else:
        print("\nOnly one model fingerprint stored so far: the next model change will show week-by-week differences.")
    return res


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["show", "publish", "history"])
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--rebuild", action="store_true", help="rebuild the cached store even if its inputs are unchanged")
    args = ap.parse_args(argv)
    args.reuse = False
    {"show": cmd_show, "publish": cmd_publish, "history": cmd_history}[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
