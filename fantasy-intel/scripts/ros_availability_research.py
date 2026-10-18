"""Research: rest-of-season P(plays) for players injured at the cutoff (ros-avail-0.1.0, research only; see
docs/research/ros_availability.md). Offline, read-only on both databases.

    cd backend
    uv run python ../scripts/ros_availability_research.py evaluate   # confirmation 2023-24 (gated) + 2025 replay
    uv run python ../scripts/ros_availability_research.py live       # 2026 week-6 cutoff: current vs candidate

The gates are app.analytics.ros_availability.DECISION_RULES, committed before the first run. Outputs:
<ifp-dir>/research/ros_availability/ (git-ignored). Never writes ifp-1/ifp-2 artifacts, the registry, production
runs or any database table.
"""

from __future__ import annotations

import argparse
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
from app.analytics import injury_challenger as ic  # noqa: E402
from app.analytics import injury_workload as iw  # noqa: E402
from app.analytics import ros_availability as ra  # noqa: E402
from app.analytics.forecast_backtest import build_store, completed_weeks  # noqa: E402
from app.analytics.forecast_features import POSITIONS, features_for_week, key  # noqa: E402
from app.analytics.participation import ParticipationArtifact, predict_week  # noqa: E402
from app.config import get_settings  # noqa: E402

PARTICIPATION = "participation_ipm-1.0.0.json"
LIVE_CUTOFF = (2026, 6)
LIVE_NAMES = {"00-0034844": "Saquon Barkley", "00-0038120": "Breece Hall"}


def _out(args) -> Path:
    p = args.ifp_dir / "research" / "ros_availability"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _participation(args) -> ParticipationArtifact:
    return ParticipationArtifact.from_dict(json.loads((args.ifp_dir / PARTICIPATION).read_text()))


def _current(src, part, season):
    """The current ROS path: participation.predict_week at feature week W for target week T (undesignated)."""
    def f(w, t):
        return {g: v["p_play"] for g, v in predict_week(src, part, season, t, feature_week=w).items()}
    return f


def _store(args, src) -> pl.DataFrame:
    """ifp-2 training rows (features + label for every appearance), 2020-2025, cached by input fingerprint."""
    weeks = completed_weeks(src, list(range(ch.TRAIN_START, max(ra.EXPLORATORY_SEASONS) + 1)))
    fp = hr.input_fingerprint(args, src, src_scoring_fp(args), weeks)
    path, meta = _out(args) / "store.parquet", _out(args) / "store.meta.json"
    if path.exists() and meta.exists() and json.loads(meta.read_text())["fingerprint"] == fp:
        return pl.read_parquet(path)
    t = time.time()
    store = build_store(src, weeks)
    store.write_parquet(path)
    meta.write_text(json.dumps({"fingerprint": fp, "rows": store.height, "build_seconds": round(time.time() - t, 1)}))
    return store


def src_scoring_fp(args) -> str:
    from app.analytics.independent_projection_store import league_scoring
    con = duckdb.connect(str(args.live_db), read_only=True)
    try:
        return league_scoring(con, args.season)[1].fingerprint
    finally:
        con.close()


def _conditional(src, store: pl.DataFrame, cfg, df: pl.DataFrame) -> pl.DataFrame:
    """ifp-2 conditional points for each cohort row: the frozen ifp-2 ridge fitted walk-forward on rows before the
    cutoff, applied to week-W features against week T's opponent (as a ROS run does). Abstains (null) without a
    lookback game."""
    out = []
    games = src.games.select("season", "week", "team", "opponent", "is_home")
    for (season, w), g in df.group_by("season", "cutoff_week", maintain_order=True):
        k = key(season, w)
        models = {}
        for pos in POSITIONS:
            tr = ch.train_rows(store, k, pos)
            if tr.height:
                models[pos] = ic.fit_candidate(tr, cfg, pos, False, k)
        tg = (g.select("gsis_id", "position", "team", "target_week").unique()
              .join(games.filter(pl.col("season") == season), left_on=["team", "target_week"], right_on=["team", "week"]))
        for (t,), tt in tg.group_by("target_week"):
            feats = features_for_week(src, season, w, tt.select("gsis_id", "position", "team", "opponent", "is_home"))
            for pos, m in models.items():
                rows = feats.filter(pl.col("position") == pos)
                if rows.height == 0:
                    continue
                pred = m.predict(rows)
                for i, r in enumerate(rows.iter_rows(named=True)):
                    out.append({"season": season, "cutoff_week": w, "target_week": t, "gsis_id": r["gsis_id"],
                                "conditional": float(pred[i]) if (r.get("games_recent") or 0) > 0 else None})
    c = pl.DataFrame(out, schema={"season": pl.Int64, "cutoff_week": pl.Int64, "target_week": pl.Int64,
                                  "gsis_id": pl.String, "conditional": pl.Float64})
    return df.join(c, on=["season", "cutoff_week", "target_week", "gsis_id"], how="left")


def _table_summary(rates: dict) -> dict:
    return {f"h{h}": {"rate": round(rates[("ALL", "ALL", h)]["rate"], 3), "n": rates[("ALL", "ALL", h)]["n"]}
            for h in range(1, 5) if ("ALL", "ALL", h) in rates}


def _strip(rep: dict) -> dict:
    """Short form of a horizon report for the summary printed to stdout."""
    return {"n": rep["current"]["n"], "observed": rep["current"]["observed"],
            "mean_p": [rep["current"]["mean_p"], rep["candidate"]["mean_p"]],
            "brier": [rep["current"]["brier"], rep["candidate"]["brier"]], "brier_diff": rep["brier_diff"],
            "ece": [rep["current"]["ece"], rep["candidate"]["ece"]],
            "max_bin_gap": [rep["current"]["max_bin_gap"], rep["candidate"]["max_bin_gap"]]}


def cmd_evaluate(args) -> dict:
    t0 = time.time()
    src, _ = hr._sources(args)
    part = _participation(args)
    cfg = ch.ChallengerConfig.load()
    fixed = iw.fit_return_rates(src, through_key=key(2023, 1), seasons=(2019, 2020, 2021, 2022))
    frames, tables = [], {}
    for season in (*ra.TEST_SEASONS, *ra.EXPLORATORY_SEASONS):
        rates = ra.table_for_season(src, season)
        tables[season] = _table_summary(rates)
        df = ra.cohort(src, season, _current(src, part, season), rates)
        df = df.with_columns(pl.struct("position", "missed_team_games", "h").map_elements(
            lambda r: ra.replacement_p(fixed, r["position"], r["missed_team_games"], r["h"]),
            return_dtype=pl.Float64).alias("p_fixed_2019_22"))
        frames.append(df)
        print(f"  cohort {season}: {df.height} rows, {time.time() - t0:.0f}s", file=sys.stderr)
    allrows = pl.concat(frames)
    store = _store(args, src)
    allrows = _conditional(src, store, cfg, allrows)
    allrows.write_parquet(_out(args) / "cohort.parquet")
    out = {"version": ra.VERSION, "rules_sha": ra.rules_sha(), "participation": part.version,
           "tables_all_all": {str(k): v for k, v in tables.items()}, "fixed_2019_22": _table_summary(fixed)}
    for name, seasons in (("confirmation", ra.TEST_SEASONS), ("exploratory", ra.EXPLORATORY_SEASONS)):
        d = allrows.filter(pl.col("season").is_in(list(seasons)))
        hz = {h: ra.horizon_report(d, h) for h in (*ra.GATED_HORIZONS, *ra.DESCRIPTIVE_HORIZONS)}
        fixed_h = {h: ra.calibration(d.filter(pl.col("h") == h)["appeared"].to_numpy(),
                                     d.filter(pl.col("h") == h)["p_fixed_2019_22"].to_numpy())
                   for h in ra.GATED_HORIZONS}
        tot = ra.totals_report(ra.ros_totals(d))
        res = {"horizons": hz, "fixed_table": fixed_h, "ros_totals": tot,
               "ros_totals_by_position": {p: ra.totals_report(g) for (p,), g in
                                          ra.ros_totals(d).group_by("position", maintain_order=True)}}
        if name == "confirmation":
            res["verdict"] = ra.gates(hz, tot)
        out[name] = res
    out["runtime_seconds"] = round(time.time() - t0, 1)
    (_out(args) / "evaluation.json").write_text(json.dumps(out, indent=1, default=str))
    return {"verdict": out["confirmation"]["verdict"], "tables": out["tables_all_all"],
            **{n: {f"h{h}": _strip(out[n]["horizons"][h]) for h in (*ra.GATED_HORIZONS, *ra.DESCRIPTIVE_HORIZONS)}
               for n in ("confirmation", "exploratory")},
            "ros_totals": {n: out[n]["ros_totals"] for n in ("confirmation", "exploratory")},
            "runtime_seconds": out["runtime_seconds"]}


def cmd_live(args) -> dict:
    """2026, cutoff week 6: every RB/WR/TE Out/Doubtful on the week-6 report, p_play for his team's next four games
    under the current ROS path and under the candidate (table refit on 2019-2025). Descriptive."""
    src, _ = hr._sources(args)
    part = _participation(args)
    season, w = LIVE_CUTOFF
    rates = ra.table_for_season(src, season)
    cur = _current(src, part, season)
    tl = iw.injury_timeline(src, season, w)
    hurt = tl.filter(pl.col("designation").is_in(["out", "doubtful"]) & pl.col("position").is_in(list(iw.SKILL))
                     & ~pl.col("gsis_id").is_in(list(ra.non_injury_ids(src, season, w))))
    teams = ra.cutoff_teams(src, season, w)
    sched: dict[str, list[int]] = {}
    for t, wk in src.games.filter(pl.col("season") == season).select("team", "week").sort("week").iter_rows():
        sched.setdefault(t, []).append(int(wk))
    cache, rows = {}, []
    live = duckdb.connect(str(args.live_db), read_only=True)
    names = dict(live.execute("SELECT gsis_id, display_name FROM nfl_players").fetchall())
    for r in hurt.iter_rows(named=True):
        team = teams.get(r["gsis_id"], r["team"])
        later = [x for x in sched.get(team, []) if x > w][:4]
        for h, t in enumerate(later, 1):
            if t not in cache:
                cache[t] = cur(w, t)
            pc = cache[t].get(r["gsis_id"])
            p, srcname = ra.ros_p_play(pc, designation=r["designation"], position=r["position"],
                                       missed_team_games=r["missed_team_games"], h=h, rates=rates)
            rows.append({"player": names.get(r["gsis_id"], r["gsis_id"]), "gsis_id": r["gsis_id"],
                         "position": r["position"], "team": team, "designation": r["designation"],
                         "missed": r["missed_team_games"], "h": h, "week": t,
                         "p_current": pc, "p_candidate": round(p, 3) if pc is not None else None, "source": srcname})
    df = pl.DataFrame(rows)
    df.write_parquet(_out(args) / "live_2026w6.parquet")
    named = df.filter(pl.col("gsis_id").is_in(list(LIVE_NAMES))).to_dicts()
    covered = df.filter(pl.col("p_current").is_not_null())
    return {"cutoff": f"{season} week {w}", "table": _table_summary(rates), "players_out_or_doubtful": hurt.height,
            "rows_with_a_current_p": covered.height,
            "mean_p_by_h": {int(h): {"current": round(float(g["p_current"].mean()), 3),
                                     "candidate": round(float(g["p_candidate"].mean()), 3), "n": g.height}
                            for (h,), g in covered.group_by("h", maintain_order=True)},
            "named": named}


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["evaluate", "live"])
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args(argv)
    cmds = {"evaluate": cmd_evaluate, "live": cmd_live}
    print(json.dumps(cmds[args.command](args), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
