"""Production D/ST projections (dfp-r1): fit the season's artifact, approve it, and store a week's run in the live DB.

    cd backend
    uv run python ../scripts/dst_projections.py fit [--register]        # the season's artifact (+ approve it)
    uv run python ../scripts/dst_projections.py register                # approve an existing artifact file
    uv run python ../scripts/dst_projections.py ensure                  # register, or fit + register (the cycle)
    uv run python ../scripts/dst_projections.py project [--week W]      # store the week's verified run

dfp-r1 is the D/ST model of docs/research/dst_projection_model.md, promoted to production by the owner's decision of
2026-10-18 (section 8; app/analytics/dst_store.PROMOTION). It projects ONE week: the coming week, with the betting
line as stored when the run is made. Later weeks keep ESPN's D/ST numbers (the research found the no-line, multi-week
form worse than ESPN). learning_cycle.py runs `ensure` (fit + register once per season and D/ST scoring, else
register) and `project` for the target week as its non-fatal `dst_project` stage.

The artifact (<out>/dfp-r1_<season>_<scoring fingerprint>.json, default data/models/dst, git-ignored) freezes the configuration selected in
the research run, the model code's sha256, the training window (2020 .. season - 1; the stack is refitted from those
finished seasons on every run, deterministically) and the 80% interval bands from out-of-sample walk-forward residuals
of the three previous seasons. Its sha256 is the run's artifact_id, approved in approved_artifacts.

`project` reads the ESPN D/ST statistics cache (refreshed from ESPN's public read API when it misses a final game,
unless --no-fetch), the history and live schedules, and the line from the live nfl_schedules. It writes one write-once
run to dst_projection_runs / dst_projection_rows (an identical rerun is a no-op). The prospective shadow
(scripts/dst_shadow.py) is separate and unchanged: it keeps its own write-once ledger as the evidence track.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import dst_shadow as ds  # noqa: E402  (the shadow's loaders and forecast: one implementation of dfp-r1)

from app.config import get_settings  # noqa: E402

research, dr = ds.research, ds.dr
BAND_SEASONS = 3            # interval bands: out-of-sample residuals of the three seasons before the target season
# The research run's headline evaluation (docs/research/dst_projection_model.md; test 2023-25, common support).
EVALUATION = {"report": "docs/research/dst_projection_model.md",
              "test_2023_2025": {"n": 1632, "model_mae": 4.762, "espn_mae": 4.878, "model_spearman": 0.374,
                                 "espn_spearman": 0.342, "model_bias": 0.03, "espn_bias": 0.76},
              "prospective_2026_w1_5": {"n": 160, "mae_minus_espn": -0.147, "spearman_minus_espn": 0.081}}


def artifact_path(args, scoring) -> Path:
    """The season's artifact for the league's CURRENT D/ST scoring: keyed by its fingerprint, so a scoring change needs
    (and `ensure` makes) a new artifact instead of reusing bands calibrated in the old rules' points."""
    if args.artifact:
        return Path(args.artifact)
    return Path(args.out) / f"{dr.MODEL_VERSION}_{args.season}_{scoring.fingerprint.split(':')[-1]}.json"


def _scoring(args):
    scoring = ds.league_scoring(args)
    if not scoring.supported:
        raise SystemExit(f"league D/ST scoring not supported (the model abstains): {scoring.describe()}")
    return scoring


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_rows(args, scoring):
    live, hist, rows = research.load_rows(args, scoring)
    for con in (live, hist):
        if con is not None:
            con.close()
    return rows


def _ensure_cache(args, season: int, week: int) -> dict:
    """Refresh the ESPN D/ST statistics cache when it misses a final game (unless --no-fetch). Returns the gaps left."""
    needed, unfinished = ds.games_before(args, season, week)
    gaps = ds.cache_gaps(args, season, needed)
    fetched = None
    if (gaps["missing_files"] or gaps["missing_actuals"]) and args.fetch:
        try:
            fetched = ds.default_fetch(args)
        except Exception as err:          # noqa: BLE001  ESPN unreachable: project from the cache as it is
            fetched = {"error": f"{err.__class__.__name__}: {err}"}
        gaps = ds.cache_gaps(args, season, needed)
    if gaps["missing_files"]:
        raise SystemExit(f"ESPN D/ST statistics cache misses seasons {gaps['missing_files']} "
                         "(run `dst_research.py fetch`)")
    return {**gaps, "previous_week_unfinished": unfinished, "fetch": fetched}


# ----------------------------------------------------------------------------- fit / register


def build_artifact(rows: list[dict], scoring, season: int, created_at: str) -> dict:
    """The season's frozen specification: configuration, code hash, training window and interval bands. Pure."""
    import numpy as np

    eval_rows = [r for r in rows if r["final"] and r["season"] >= research.FIRST_ROW_SEASON and r["season"] < season]
    teams = sorted({r["team"] for r in rows} | {r["opp"] for r in rows})
    hist = dr.history_arrays([r for r in rows if r["final"]], teams)
    feats = dr.rating_features(eval_rows, hist, teams, ds.FROZEN, 1)
    band_seasons = [s for s in range(season - BAND_SEASONS, season) if s > research.FIRST_ROW_SEASON]
    oos = [r for r in research.walk_forward(eval_rows, feats, scoring, ds.FROZEN, band_seasons) if r["final"]]
    bands = dr.residual_bands(np.array([r["pred"] for r in oos]), np.array([r["points"] for r in oos]))
    return {"version": dr.MODEL_VERSION, "season": season, "created_at": created_at,
            "config": ds.FROZEN.to_dict(), "code_sha256": ds._sha_bytes(Path(dr.__file__).read_bytes()),
            "training_seasons": [research.FIRST_ROW_SEASON, season - 1], "data_cutoff_key": season * 100,
            "scoring_fingerprint": scoring.fingerprint,
            "interval_bands": {"source": f"out-of-sample walk-forward residuals, seasons {band_seasons}", "n": len(oos),
                               **bands},
            "evaluation": EVALUATION}


def cmd_fit(args) -> dict:
    scoring = _scoring(args)
    path = artifact_path(args, scoring)
    if path.exists() and not args.overwrite:
        raise SystemExit(f"{path} exists; artifacts are write-once (pass --overwrite only for an artifact that was "
                         "never registered or used)")
    first = (args.season, 1)
    _ensure_cache(args, *first)
    art = build_artifact(_load_rows(args, scoring), scoring, args.season, datetime.now(timezone.utc).isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(research._clean(art), indent=1, sort_keys=True))
    out = {"artifact": str(path), "sha256": _sha(path), "bands": art["interval_bands"]["bands"]}
    if args.register:
        out["registered"] = register(args, path)
    return out


def register(args, path: Path) -> bool:
    from app.analytics.dst_store import register_artifact
    from app.db.connection import open_db

    art = json.loads(path.read_text())
    if art.get("version") != dr.MODEL_VERSION:
        raise SystemExit(f"{path} is a {art.get('version')!r} artifact, not {dr.MODEL_VERSION}")
    with open_db(args.live_db) as con:
        return register_artifact(con, model_version=dr.MODEL_VERSION, artifact_sha256=_sha(path),
                                 target_season=art["season"], training_cutoff_key=art.get("data_cutoff_key"),
                                 artifact_created_at=art.get("created_at"))


def cmd_register(args) -> dict:
    path = artifact_path(args, _scoring(args))
    if not path.exists():
        raise SystemExit(f"{path} does not exist; run `fit --register` first")
    return {"artifact": str(path), "sha256": _sha(path), "registered": register(args, path)}


def cmd_ensure(args) -> dict:
    """What the learning cycle runs: approve the artifact for the league's current D/ST scoring, fitting it first when
    there is none (the first run of a season, or after the league's D/ST scoring changed)."""
    path = artifact_path(args, _scoring(args))
    if path.exists():
        return {"artifact": str(path), "sha256": _sha(path), "registered": register(args, path), "fitted": False}
    args.register = True
    return {**cmd_fit(args), "fitted": True}


# ----------------------------------------------------------------------------- project


def _band(bands: dict, pred: float) -> tuple[float | None, float | None]:
    b = next((b for b in bands.get("bands") or [] if (b["low"] is None or b["low"] == "-inf" or pred >= float(b["low"]))
              and (b["high"] is None or b["high"] == "inf" or pred < float(b["high"]))), None)
    if b is None:
        return None, None
    return round(pred + b["q_low"], 2), round(pred + b["q_high"], 2)


def _explain(f: dict, g: dict) -> list[dict]:
    """Plain reasons from the model's own inputs (no claim about ESPN)."""
    out = []
    takeaways = (f["e_ints"] or 0) + (f["e_fr"] or 0)
    if f["implied_opp"] is not None:
        fav = f["margin"]
        line = (f"favored by {fav:g}" if fav > 0 else f"{-fav:g}-point underdog" if fav < 0 else "a pick'em")
        out.append({"feature": "headline", "contribution": None,
                    "text": f"{g['opponent']} are expected to score {f['implied_opp']:.1f} points ({line}, total "
                            f"{g['total_line']:g})."})
    else:
        out.append({"feature": "headline", "contribution": None,
                    "text": f"No betting line yet: projected from {f['team']}'s and {g['opponent']}'s rates."})
    out += [{"feature": "sacks", "contribution": None, "text": f"{f['e_sacks']:.1f} expected sacks"},
            {"feature": "takeaways", "contribution": None, "text": f"{takeaways:.1f} expected takeaways"},
            {"feature": "points_allowed", "contribution": None, "text": f"{f['e_pa']:.1f} expected points allowed"},
            {"feature": "yards_allowed", "contribution": None, "text": f"{f['e_ya']:.0f} expected yards allowed"},
            {"feature": "touchdown", "contribution": None,
             "text": f"{100 * (f['p_any_td'] or 0):.0f}% chance of a D/ST touchdown"}]
    return out


def build_rows(fc: dict, games: list[dict], dsts: list[dict], bands: dict, missing_prev: list,
               byes: set[str] = frozenset()) -> list[dict]:
    """One row per ESPN D/ST: projected (it has a game), bye (0; only when `byes` confirms it), no_estimate (no game
    found and no confirmed bye: consumers keep ESPN's number) or no_nfl_team. Pure."""
    by_team = {g["team"]: g for g in games}
    preds = {r["team"]: r for r in fc["rows"]}
    late = sorted({t for t, _ in missing_prev})
    out = []
    for d in dsts:
        team = dr.norm_team(d["pro_team"])
        g, f = by_team.get(team), preds.get(team)
        base = {"espn_player_id": d["espn_player_id"], "nfl_team": team, "opponent": None, "is_home": None,
                "projected_points": None, "interval_low": None, "interval_high": None, "expected_points": None,
                "stat_line": None, "context": None, "explanation": [], "flags": [], "missing_inputs": []}
        if team is None:
            out.append({**base, "coverage_status": "no_nfl_team", "missing_inputs": ["no NFL team"]})
            continue
        if g is None and team in byes:
            out.append({**base, "coverage_status": "bye", "expected_points": 0.0})
            continue
        if g is None:      # a partial schedule sync must never turn an active defense into a confirmed 0
            out.append({**base, "coverage_status": "no_estimate",
                        "missing_inputs": ["no game this week in the NFL schedule, and no confirmed bye"]})
            continue
        if f is None:
            out.append({**base, "coverage_status": "no_estimate", "opponent": g["opponent"], "is_home": g["home"],
                        "missing_inputs": ["no game-table row for this game"]})
            continue
        lo, hi = _band(bands, f["pred"])
        flags = []
        if not f["has_line"]:
            flags.append("No betting line yet: projected from team ratings only.")
        if late:
            flags.append("Last week's statistics were not all in yet for " + ", ".join(late) + ".")
        out.append({**base, "coverage_status": "projected", "opponent": g["opponent"], "is_home": g["home"],
                    "projected_points": f["pred"], "expected_points": f["pred"], "interval_low": lo,
                    "interval_high": hi,
                    "stat_line": {"sacks": f["e_sacks"], "interceptions": f["e_ints"], "fumble_recoveries": f["e_fr"],
                                  "blocked_kicks": f["e_blk"], "safeties": f["e_saf"], "return_tds": f["e_ret_td"],
                                  "points_allowed": f["e_pa"], "yards_allowed": f["e_ya"],
                                  "p_any_td": f["p_any_td"], "td_points": f["pred_td_points"]},
                    "context": {"spread_line": g["spread_line"], "total_line": g["total_line"],
                                "opponent_implied_total": f["implied_opp"], "expected_margin": f["margin"],
                                "has_line": f["has_line"],
                                "kickoff_utc": g["kickoff"].isoformat() if g["kickoff"] else None},
                    "explanation": _explain(f, g), "flags": flags})
    return out


def _dsts(args) -> list[dict]:
    from app.db.connection import fetch_dicts

    con = ds._live(args)
    try:
        return fetch_dicts(con, "SELECT espn_player_id, pro_team FROM fantasy_players WHERE position = 'D/ST' "
                                "ORDER BY espn_player_id")
    finally:
        con.close()


def confirmed_byes(args, season: int, week: int) -> set[str]:
    """Teams whose bye in `week` the schedule confirms: no game this week, and a game in every other regular-season
    week of the season (an NFL team plays every week but one). A team missing from a partially synced week, or from a
    partial season schedule, is not a confirmed bye."""
    con = ds._live(args)
    try:
        rows = con.execute("""SELECT week, home_team, away_team FROM nfl_schedules WHERE season = ?
                              AND game_type = 'REG'""", [season]).fetchall()
    finally:
        con.close()
    weeks = {int(w) for w, _, _ in rows}
    played: dict[str, set[int]] = {}
    for w, h, a in rows:
        for t in (h, a):
            played.setdefault(dr.norm_team(t), set()).add(int(w))
    return {t for t, ws in played.items() if week not in ws and ws == weeks - {week}}


def build_run(args, week: int, art: dict, artifact_sha: str, now: datetime) -> tuple[dict, list[dict]]:
    scoring = _scoring(args)
    if art.get("scoring_fingerprint") != scoring.fingerprint:
        raise SystemExit(f"the artifact was fitted for D/ST scoring {art.get('scoring_fingerprint')}, the league now "
                         f"scores {scoring.fingerprint}: run `ensure` (or `fit --register`) for the current scoring")
    if art.get("version") != dr.MODEL_VERSION or art.get("season") != args.season:
        raise SystemExit(f"the artifact is not a {dr.MODEL_VERSION} artifact for {args.season}")
    if art.get("config") != ds.FROZEN.to_dict():
        raise SystemExit("the artifact's configuration differs from the frozen dfp-r1 configuration")
    cache = _ensure_cache(args, args.season, week)
    games = ds.week_games(args, args.season, week)
    if not games:
        raise SystemExit(f"no {args.season} week {week} games in nfl_schedules")
    rows = _load_rows(args, scoring)
    ds.apply_lines(rows, args.season, week, games)
    fc = ds.forecast(rows, scoring, args.season, week, {g["team"] for g in games}, last=week)
    out_rows = build_rows(fc, games, _dsts(args), art["interval_bands"], cache["missing_actuals"],
                          confirmed_byes(args, args.season, week))
    lines = [{k: g[k] for k in ("team", "opponent", "home", "spread_line", "total_line")} for g in games]
    stamp = now.strftime("%Y%m%dT%H%M%S")
    meta = {"run_id": None, "season": args.season,
            "week": week, "model_version": dr.MODEL_VERSION, "scoring_fingerprint": scoring.fingerprint,
            "scoring": scoring.describe(), "artifact_id": artifact_sha, "artifact_created_at": art.get("created_at"),
            "data_through_season": args.season, "data_through_week": week - 1,
            "model_status": {"D/ST": "owner_override"}, "evaluation": art.get("evaluation"),
            "inputs": {"lines": {"source": "nfl_schedules spread_line / total_line in the live DB when the run was made",
                                 "rows": lines, "sha256": ds._sha_obj(lines)},
                       "espn_dst_cache": ds.cache_provenance(args, args.season),
                       "missing_previous_week_statistics": [list(m) for m in cache["missing_actuals"]],
                       "previous_week_unfinished": cache["previous_week_unfinished"]},
            "forecast_horizon": 0, "nfl_synced_at": ds.nfl_synced_at(args), "created_at": now.replace(tzinfo=None)}
    meta, out_rows = research._clean(meta) | {"created_at": meta["created_at"]}, research._clean(out_rows)
    from app.analytics.dst_store import content_sha256
    # the content hash in the id: two runs made in the same second with different content never share an id
    meta["run_id"] = f"dfp-{args.season}w{week}-{stamp}-{content_sha256(meta, out_rows)[:10]}"
    return meta, out_rows


def cmd_project(args) -> dict:
    from app.analytics.dst_store import write_run
    from app.db.connection import open_db

    path = artifact_path(args, _scoring(args))
    if not path.exists():
        raise SystemExit(f"{path} does not exist; run `ensure` (or `fit --register`) for the league's D/ST scoring")
    art = json.loads(path.read_text())
    now = datetime.now(timezone.utc)
    week = args.week if args.week is not None else ds.target_week(args, now)
    if week is None:
        return {"status": "no upcoming week"}
    meta, rows = build_run(args, week, art, _sha(path), now)
    with open_db(args.live_db) as con:
        res = write_run(con, meta, rows)
    res["week"] = week
    res["coverage"] = {s: sum(r["coverage_status"] == s for r in rows) for s in sorted({r["coverage_status"] for r in rows})}
    return {"artifact": str(path), "artifact_sha256": _sha(path), "runs": [res]}


def parse(argv=None):
    s = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("fit", "register", "ensure", "project"))
    ap.add_argument("--season", type=int, default=s.nfl_season)
    ap.add_argument("--week", type=int, help="project: default the upcoming week")
    ap.add_argument("--live-db", type=Path, default=s.db_path)
    ap.add_argument("--history-db", type=Path, default=research._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--cache", type=Path, default=research._path("DST_ESPN_CACHE", "data/raw/espn_dst"))
    ap.add_argument("--out", type=Path, default=research._path("DST_RESEARCH_DIR", "data/models/dst"))
    ap.add_argument("--artifact", type=Path, default=None,
                    help="default <out>/dfp-r1_<season>_<D/ST scoring fingerprint>.json")
    ap.add_argument("--register", action="store_true", help="fit: approve the new artifact in the live DB")
    ap.add_argument("--overwrite", action="store_true", help="fit: replace an artifact that was never registered")
    ap.add_argument("--no-fetch", dest="fetch", action="store_false",
                    help="never download ESPN D/ST statistics (use the cache as it is)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse(argv)
    fn = {"fit": cmd_fit, "register": cmd_register, "ensure": cmd_ensure, "project": cmd_project}[args.command]
    print(json.dumps(research._clean(fn(args)), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
