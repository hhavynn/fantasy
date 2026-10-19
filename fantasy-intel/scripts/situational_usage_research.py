"""Research: situational usage splits and RB workload components (ifp-su, research only; see
docs/research/situational_usage.md). Offline, read-only on both databases; prints aggregates.

    cd backend
    uv run python ../scripts/situational_usage_research.py pbp        # nflverse play-by-play -> slim parquet (cached)
    uv run python ../scripts/situational_usage_research.py build      # ifp-2 store + su_* + handoff cohort columns
    uv run python ../scripts/situational_usage_research.py describe   # development feature distributions (no outcomes)
    uv run python ../scripts/situational_usage_research.py develop    # 2021-22 adoption, frozen once
    uv run python ../scripts/situational_usage_research.py confirm    # 2023-24 once + 2025-26 exploratory (write-once)

Inputs as in injury_workload_research.py (play-by-play exits in --exits-dir/pbp_exits). Outputs go to
<ifp-dir>/research/situational_usage/ (git-ignored). Never writes ifp-1/ifp-2 artifacts, the weekly ledger, the
registry or any production table.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402
import hgb_research as hr  # noqa: E402

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import injury_workload as iw  # noqa: E402
from app.analytics import situational_usage as su  # noqa: E402
from app.analytics.forecast_backtest import build_store, pair_decisions  # noqa: E402
from app.analytics.forecast_features import POSITIONS  # noqa: E402
from app.config import get_settings  # noqa: E402

DOC = SCRIPTS.parent / "docs" / "research" / "situational_usage.md"
TL_COLS = ["designation", "status_class", "missed_team_games", "missed_injured", "practice_status", "return_evidence"]


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "situational_usage"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pbp_dir(args) -> Path:
    return args.pbp_dir or (_out(args) / "pbp")


def prereg_hash() -> str:
    """SHA-256 of the preregistration block of the research doc (between the PREREG markers)."""
    text = DOC.read_text()
    m = re.search(r"<!-- PREREG-START -->(.*)<!-- PREREG-END -->", text, re.S)
    if not m:
        raise SystemExit(f"no preregistration block in {DOC}")
    return hashlib.sha256(m.group(1).encode()).hexdigest()


def candidate_code_hash() -> str:
    return hashlib.sha256(Path(su.__file__).read_bytes()).hexdigest()[:16]


# ----------------------------------------------------------------------------- inputs


def cmd_pbp(args) -> dict:
    """nflverse play-by-play (the release app.nfl.sync uses) reduced to su.PBP_COLUMNS, one parquet per season."""
    import nflreadpy as nfl

    d = _pbp_dir(args)
    d.mkdir(parents=True, exist_ok=True)
    out = {}
    for s in range(args.first_season, args.season + 1):
        p = nfl.load_pbp([s])
        p = p.select([c for c in su.PBP_COLUMNS if c in p.columns])
        p.write_parquet(d / f"pbp_{s}.parquet")
        out[s] = {"plays": p.height, "max_week": int(p["week"].max())}
    return out


def _splits(args) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    files = sorted(_pbp_dir(args).glob("pbp_*.parquet"))
    if not files:
        raise SystemExit(f"no play-by-play in {_pbp_dir(args)}: run `pbp` first")
    players, teams, h = [], [], hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + f.read_bytes())
        p, t = su.week_splits(pl.read_parquet(f))
        players.append(p)
        teams.append(t)
    return pl.concat(players), pl.concat(teams), {"files": [f.name for f in files], "sha256": h.hexdigest()[:16]}


def _codes(args) -> pl.DataFrame | None:
    parts = []
    for path in (args.history_db, args.live_db):
        if path.exists():
            con = duckdb.connect(str(path), read_only=True)
            try:
                c = iw.load_roster_codes(con)
            finally:
                con.close()
            if c is not None:
                parts.append(c)
    return pl.concat(parts).unique(["season", "week", "gsis_id"]) if parts else None


def build(args) -> tuple[pl.DataFrame, dict]:
    """ifp-2 feature store (forecast_backtest.build_store over the fully final weeks, as every earlier study) +
    su_* features + component labels + the injury-handoff cohort columns (ho_*, tl_*). Cached by a fingerprint of
    the inputs and the code."""
    src, scoring = hr._sources(args)
    weeks = hr.final_weeks(args, src)
    psplit, tsplit, pinfo = _splits(args)
    fp = hashlib.sha256(json.dumps({"store": hr.input_fingerprint(args, src, scoring.fingerprint, weeks),
                                    "pbp": pinfo["sha256"]}, sort_keys=True).encode()).hexdigest()[:16]
    path, meta = _out(args) / "store.parquet", _out(args) / "store.meta.json"
    if path.exists() and meta.exists() and not args.rebuild and json.loads(meta.read_text())["fingerprint"] == fp:
        return pl.read_parquet(path), json.loads(meta.read_text())
    t = time.time()
    store = ch.add_last_game(build_store(src, weeks), src.panel)
    store = su.attach(store, src.panel, src.games, psplit, tsplit)
    print(f"  store + su features: {time.time() - t:.0f}s", file=sys.stderr)
    codes = _codes(args)
    parts = []
    for i, (season, week) in enumerate(weeks):
        tl = iw.injury_timeline(src, season, week, roster_codes=codes)
        ho = iw.workload_handoff(src, season, week, timeline=tl, roster_codes=codes)
        parts.append(tl.select("gsis_id", *[pl.col(c).alias(f"tl_{c}") for c in TL_COLS])
                     .join(ho, on="gsis_id", how="full", coalesce=True).with_columns(pl.lit(season * 100 + week).alias("key")))
        if i % 20 == 0:
            print(f"  handoff {season} w{week}: {time.time() - t:.0f}s", file=sys.stderr)
    store = store.join(pl.concat(parts, how="diagonal_relaxed"), on=["key", "gsis_id"], how="left")
    store.write_parquet(path)
    info = {"fingerprint": fp, "rows": store.height, "weeks": len(weeks), "scoring_fingerprint": scoring.fingerprint,
            "pbp": pinfo, "handoff_version": iw.WORKLOAD_VERSION, "roster_codes": codes is not None,
            "build_seconds": round(time.time() - t, 1)}
    meta.write_text(json.dumps(info))
    return store, info


def cmd_build(args) -> dict:
    store, info = build(args)
    s = store.filter(pl.col("season") >= ch.TRAIN_START)
    cov = {}
    for (season,), g in s.group_by("season", maintain_order=True):
        cov[int(season)] = {"rows": g.height, "su_games_zero": round(float((g["su_games"] == 0).mean()), 4),
                            **{f"null_{c}": round(float(g[c].is_null().mean()), 4)
                               for c in ("su_car_i5_share", "su_tgt_pd_share", "su_opp_per_snap", "su_conv_success",
                                         "y_gl", "ho_exp_xfpl")}}
    sizes = {c: int(s.filter(e.fill_null(False)).height) for c, e in su.cohorts().items()}
    return {**info, "coverage": cov, "cohort_sizes_all_seasons": sizes}


def cmd_describe(args) -> dict:
    """Development-window (2021-22) distributions of the situational features and of the cohort sizes. Uses no
    outcome; it is what the archetype thresholds in su.cohorts() were read from."""
    store, _ = build(args)
    dev = store.filter(pl.col("season").is_between(*su.WINDOWS["development"]))
    out = {"rows": dev.height}
    for pos in ("RB", "WR", "TE"):
        g = dev.filter((pl.col("position") == pos) & (pl.col("games_recent") > 0))
        out[pos] = {f: {q: (None if (v := g[f].quantile(q / 100)) is None else round(float(v), 4))
                        for q in (10, 25, 50, 75, 90, 95)} for f in su.FEATURE_DOCS}
        out[pos]["corr_with_volume"] = {
            f: round(float(np.corrcoef(*g.select(f, "rush_share" if pos == "RB" else "target_share").drop_nulls()
                                       .to_numpy().T)[0, 1]), 3) for f in su.FEATURE_DOCS}
    out["cohort_sizes_dev"] = {c: int(dev.filter(e.fill_null(False)).height) for c, e in su.cohorts().items()}
    return out


# ----------------------------------------------------------------------------- develop / confirm


def _preds(store: pl.DataFrame, weeks, cfg, adopted=None) -> pl.DataFrame:
    t = time.time()
    preds = su.walk_forward(store, weeks, cfg, adopted)
    print(f"walk-forward {len(weeks)} weeks: {time.time() - t:.0f}s", file=sys.stderr)
    keep = [c for c in store.columns if not c.startswith("pred_") or c in ("pred_A1", "pred_A2")]
    return (store.filter(pl.col("key").is_in([s * 100 + w for s, w in weeks])).select(keep)
            .join(preds, on=["key", "gsis_id"]))


def _weeks(store, seasons) -> list[tuple[int, int]]:
    return sorted({(int(s), int(w)) for s, w in store.filter(pl.col("season").is_between(*seasons))
                   .select("season", "week").unique().iter_rows()})


def _frozen_path(args) -> Path:
    return _out(args) / f"frozen_adoption_{su.SU_VERSION}.json"


def _summary(p: pl.DataFrame) -> dict:
    """Identical-row point metrics per position for every model + baselines, and every cohort."""
    models = ["pred_A1", "pred_A2", "pred_ifp2", *[f"pred_{c}" for c in su.CANDIDATES]]
    lab = p.filter(pl.col("label").is_not_null() & pl.all_horizontal([pl.col(m).is_not_null() for m in models]))
    out = {"common_rows": lab.height, "weeks": lab.select("key").n_unique(), "positions": {}}
    flt = su.cohorts()
    for pos in [*POSITIONS, "ALL"]:
        g = lab if pos == "ALL" else lab.filter(pl.col("position") == pos)
        r = {m: su.point_metrics(g, m) for m in models}
        r["diff_vs_ifp2"] = {m: {"mae": su.week_bootstrap(g, m, "pred_ifp2"),
                                 "mse": su.week_bootstrap(g, m, "pred_ifp2", loss="squared")}
                             for m in models if m != "pred_ifp2"}
        r["cohorts"] = {}
        for c, e in flt.items():
            cg = g.filter(e.fill_null(False))
            if cg.height == 0:
                continue
            rr = {"n": cg.height, **{m: su.point_metrics(cg, m) for m in models}}
            if cg.height >= 30 and cg.select("key").n_unique() >= 5:
                rr["diff_vs_ifp2"] = {m: {"mae": su.week_bootstrap(cg, m, "pred_ifp2"),
                                          "mse": su.week_bootstrap(cg, m, "pred_ifp2", loss="squared")}
                                      for m in models if m.startswith("pred_") and m not in ("pred_ifp2",)}
            r["cohorts"][c] = rr
        out["positions"][pos] = r
    out["pairs"] = pair_decisions(lab, models)
    return out


def cmd_develop(args) -> dict:
    path = _frozen_path(args)
    if path.exists():
        raise SystemExit(f"{path} exists: the development adoption is frozen (write-once)")
    store, info = build(args)
    cfg = ch.ChallengerConfig.load()
    weeks = _weeks(store, su.WINDOWS["development"])
    p = _preds(store, weeks, cfg)
    adoption = su.development_adoption(p)
    frozen = {"version": su.SU_VERSION, "status": su.SU_STATUS, "created": datetime.now(timezone.utc).isoformat(),
              "adoption": {c: {pos: v["adopt"] for pos, v in d.items()} for c, d in adoption.items()},
              "prereg_sha256": prereg_hash(), "candidate_code_sha256": candidate_code_hash(),
              "store_fingerprint": info["fingerprint"], "ifp2_config": ch.ChallengerConfig.load().version,
              "decision_rules": su.DECISION_RULES}
    report = {"frozen": frozen, "adoption_detail": adoption, "summary": _summary(p),
              "workload": su.workload_report(p)}
    (_out(args) / "development.json").write_text(json.dumps(report, indent=1, default=str))
    p.write_parquet(_out(args) / "development_preds.parquet")
    with open(path, "x") as f:
        json.dump(frozen, f, indent=1, default=str)
    return {"adoption": frozen["adoption"], "detail": {c: {pos: {"mae": v["mae"], "mse": v["mse"]["diff"],
                                                               "checks": v["checks"]} for pos, v in d.items()}
                                                       for c, d in adoption.items()}}


def cmd_confirm(args) -> dict:
    report_path = _out(args) / "confirmation.json"
    if report_path.exists():
        raise SystemExit(f"{report_path} exists: the confirmation ran once (write-once)")
    frozen = json.loads(_frozen_path(args).read_text())
    if frozen["prereg_sha256"] != prereg_hash():
        raise SystemExit("the preregistration block changed since `develop`")
    if frozen["candidate_code_sha256"] != candidate_code_hash():
        raise SystemExit("situational_usage.py changed since `develop`")
    store, info = build(args)
    if frozen["store_fingerprint"] != info["fingerprint"]:
        raise SystemExit("the input store changed since `develop`")
    cfg = ch.ChallengerConfig.load()
    out = {"frozen": frozen}
    for name in ("confirmation", "exploratory"):
        weeks = _weeks(store, su.WINDOWS[name])
        # every candidate runs on every position here so the tables can show non-adopted pairs too; the verdict
        # reads only the adopted pairs
        p = _preds(store, weeks, cfg)
        p.write_parquet(_out(args) / f"{name}_preds.parquet")
        out[name] = {"verdict": su.verdict(p, frozen["adoption"]), "summary": _summary(p),
                     "workload": su.workload_report(p)}
    with open(report_path, "x") as f:
        json.dump(out, f, indent=1, default=str)
    return {n: out[n]["verdict"].get("outcome") for n in ("confirmation", "exploratory")}


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["pbp", "build", "describe", "develop", "confirm"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--pbp-dir", type=Path, default=None, help="default: <ifp-dir>/research/situational_usage/pbp")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--first-season", type=int, default=2019)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args(argv)
    cmds = {"pbp": cmd_pbp, "build": cmd_build, "describe": cmd_describe, "develop": cmd_develop,
            "confirm": cmd_confirm}
    print(json.dumps(cmds[args.command](args), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
