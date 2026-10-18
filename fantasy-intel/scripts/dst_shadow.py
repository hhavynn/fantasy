"""Prospective shadow of the D/ST research model `dfp-r1` against ESPN's one-week D/ST projection (research only;
see docs/research/dst_projection_model.md, "Prospective shadow"). It is the evidence track: since the owner's override
of 2026-10-18 the same model serves D/ST in production (scripts/dst_projections.py, dst_store; section 8), and this
shadow keeps running unchanged next to it. Nothing in the app, the API or the frontend reads anything written here.

    cd backend
    uv run python ../scripts/dst_shadow.py pregame [--week W]     # gated write-once pregame ledger for week W
    uv run python ../scripts/dst_shadow.py score                  # score frozen weeks vs ESPN's pregame snapshots
    uv run python ../scripts/dst_shadow.py cycle   [--week W]     # score, missed-week check, then pregame
    uv run python ../scripts/dst_shadow.py status
    uv run python ../scripts/dst_shadow.py pregame --week W --now 2026-10-20T15:00Z   # gate only (dry run)

Who runs it (no new scheduler):
  * learning_cycle.py, stage `dst_shadow` (Tue-Thu 07:00 PT): `cycle` scores every fully final frozen week, records a
    week that started without a ledger, and freezes the target week once its inputs are complete.
  * refresh_all.py's read-only evidence stage runs `pregame` for the upcoming week on every refresh (and every
    pregame-guard run): it freezes the week if the cycle could not.

Freeze rule (fixed here, before any prospective week exists). One write-once ledger per week, frozen as EARLY as the
inputs are complete, because the decision it serves (a waiver / streaming pick) is made mid-week:
  * every game of the previous week is final in nfl_schedules, and every final game of the season's earlier weeks
    has its ESPN D/ST statistics in the cache (the ratings use the full previous week), and
  * every team that has not kicked off ("open") has a spread and a total in the live database's nflverse schedule.
If either is missing the freeze is deferred, unless an open team kicks off within DEADLINE_HOURS: then it is frozen
anyway (a team without a line gets the ratings-only projection) and every gap is recorded as a warning. Teams that
already kicked off are excluded (never backfilled). Weeks before FIRST_PROSPECTIVE are never frozen.

The line is the one stored when the ledger is frozen (nflverse's current spread_line / total_line in nfl_schedules,
the source of team_game_lines), recorded per row with the nflverse sync time and a hash. Each ledger row holds:
  * `pred`: dfp-r1 (market stack, frozen configuration of the research doc);
  * `pred_line_only`: the "line + league means" baseline (no team ratings), the cheaper candidate of section 4.1;
  * `pred_ratings_only`: the no-line model, with horizons 2-4 for the next weeks under `multi_week`;
  * `espn_at_freeze`: ESPN's league-scored projection from the latest snapshot stored at freeze time.

Scoring (`score`, recomputed every cycle so stat corrections land): a week counts once every game of it is final and
every ledger team has ESPN's actual league points (espn_player_weekly_points). The baseline is ESPN's pregame
projection: the latest espn_projection_history snapshot synced strictly before the team's own kickoff. The gate
(GATE) needs MIN_WEEKS scored weeks before it can pass or fail; until then it is `accumulating`.

ESPN's D/ST statistics come from its public read API (scripts/dst_research.py fetch: one request per season, cached
under data/raw/espn_dst, git-ignored); a fetch happens only when the cache misses a final game, never with --no-fetch
or in a dry run. Writes only under <out>/shadow/ (default data/models/dst, git-ignored): ledger/ (write-once),
runs/ (one write-once record per attempt), missed/ (write-once) and scores.json (replaced). Both databases are opened
read-only.

Exit codes: 0 ok (frozen, already frozen, deferred, nothing to do); 3 ok but with warnings (degraded freeze, missed
week, missing inputs close to kickoff); 1 a step failed (recorded, nothing half-written).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import dst_research as research  # noqa: E402  (fetch / cache / game-table loading shared with the research run)

from app.analytics import dst_research as dr  # noqa: E402
from app.config import get_settings  # noqa: E402

SCHEMA = "dfp-r1-shadow/v1"
CANDIDATE = dr.MODEL_VERSION                # dfp-r1
FIRST_PROSPECTIVE = (2026, 7)               # pre-registered: weeks 1-6 of 2026 were read by the research run
DEADLINE_HOURS = 6.0                        # the pregame guard's lead window
MAX_HORIZON = 4
# The research doc's frozen selection (validation 2021-22): half-life 16 weeks, offseason gap 6, k 3, market on,
# pressure off. The line-only baseline is its "no team ratings (line + league means)" ablation.
FROZEN = dr.DConfig(half_life=16.0, offseason_gap=6.0, k=3.0, market=True, pressure=False)
LINE_ONLY = dr.replace_cfg(FROZEN, iterations=0, k=1e9)
RATINGS_ONLY = dr.replace_cfg(FROZEN, market=False)
# Pre-registered prospective gate (against ESPN's pregame snapshot, on common support). MIN_WEEKS is the repo's
# shadow-promotion minimum; the other criteria are the research run's (scripts/dst_research.py CRITERIA).
GATE = {"min_weeks": 8, "mae_ci90_upper_below": 0.0, "spearman_ci90_lower_above": -0.02, "max_abs_bias": 0.5}
EXIT_OK, EXIT_FAILED, EXIT_WARNINGS = 0, 1, 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def shadow_dir(args) -> Path:
    return Path(args.out) / "shadow"


def ledger_path(args, season: int, week: int) -> Path:
    return shadow_dir(args) / "ledger" / f"s{season}w{week:02d}-pregame.json"


def _write_once(path: Path, obj) -> bool:
    from app.analytics.weekly_learning import write_once
    return write_once(path, json.dumps(obj, indent=1, default=str))


def _replace_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha_obj(obj) -> str:
    return _sha_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode())


def _r(v, nd=4):
    if v is None:
        return None
    v = float(v)
    return None if math.isnan(v) or math.isinf(v) else round(v, nd)


def _aware(v) -> datetime | None:
    from app.analytics.injury_freshness import naive_utc
    t = naive_utc(v)
    return None if t is None else t.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------------------- live-database reads (read-only)


def _live(args):
    from app.db.connection import connect
    return connect(args.live_db, read_only=True)


def week_games(args, season: int, week: int) -> list[dict]:
    """[{team, opponent, kickoff (aware UTC | None), spread_line, total_line, home, final}] of a REG week, one row
    per team, read now from the live nfl_schedules (spread_line > 0: the home team is favoured)."""
    from app.analytics.projection_backtest import _kickoff
    from app.db.connection import fetch_dicts

    con = _live(args)
    try:
        games = fetch_dicts(con, """SELECT gameday, gametime, home_team, away_team, home_score, spread_line,
                                    total_line FROM nfl_schedules WHERE season = ? AND week = ? AND game_type = 'REG'""",
                            [season, week])
    finally:
        con.close()
    out = []
    for g in games:
        ko = _kickoff(g)
        for team, opp, home in ((g["home_team"], g["away_team"], True), (g["away_team"], g["home_team"], False)):
            out.append({"team": dr.norm_team(team), "opponent": dr.norm_team(opp), "kickoff": ko, "home": home,
                        "spread_line": _r(g["spread_line"]), "total_line": _r(g["total_line"]),
                        "final": g["home_score"] is not None})
    return sorted(out, key=lambda g: g["team"])


def games_before(args, season: int, week: int) -> tuple[set[tuple[str, int]], list[str]]:
    """((team, week) of every final REG game of `season` before `week`, teams of week - 1 without a final score)."""
    con = _live(args)
    try:
        rows = con.execute("""SELECT week, home_team, away_team, home_score FROM nfl_schedules WHERE season = ?
                              AND week < ? AND game_type = 'REG'""", [season, week]).fetchall()
    finally:
        con.close()
    final = {(dr.norm_team(t), int(w)) for w, h, a, sc in rows if sc is not None for t in (h, a)}
    unfinished = sorted(dr.norm_team(t) for w, h, a, sc in rows if sc is None and w == week - 1 for t in (h, a))
    return final, unfinished


def last_week(args, season: int) -> int | None:
    con = _live(args)
    try:
        row = con.execute("SELECT max(week) FROM nfl_schedules WHERE season = ? AND game_type = 'REG'",
                          [season]).fetchone()
    finally:
        con.close()
    return None if row is None or row[0] is None else int(row[0])


def nfl_synced_at(args) -> str | None:
    con = _live(args)
    try:
        row = con.execute("SELECT max(finished_at) FROM sync_runs WHERE source = 'nfl' AND status = 'ok'").fetchone()
    except Exception:          # noqa: BLE001  table absent on an old database: unknown, not an error
        return None
    finally:
        con.close()
    return None if row is None or row[0] is None else str(row[0])


def _dst_ids(con) -> dict[int, str]:
    from app.db.connection import fetch_dicts
    return {r["espn_player_id"]: dr.norm_team(r["pro_team"]) for r in fetch_dicts(
        con, "SELECT espn_player_id, pro_team FROM fantasy_players WHERE position = 'D/ST' AND pro_team IS NOT NULL")}


def espn_snapshots(args, season: int, weeks: list[int]) -> dict[tuple[str, int], list[tuple[datetime, float]]]:
    """{(team, week): [(synced_at, league-scored ESPN projection), ...] oldest first} from espn_projection_history."""
    from app.db.connection import fetch_dicts, table_exists

    con = _live(args)
    try:
        if not table_exists(con, "espn_projection_history") or not weeks:
            return {}
        ids = _dst_ids(con)
        rows = fetch_dicts(con, """SELECT espn_player_id, scoring_period AS week, projected_points, synced_at
                                   FROM espn_projection_history WHERE season = ? AND scoring_period IN (SELECT unnest(?))
                                   AND projected_points IS NOT NULL ORDER BY synced_at""", [season, weeks])
    finally:
        con.close()
    out: dict = {}
    for r in rows:
        team, t = ids.get(r["espn_player_id"]), _aware(r["synced_at"])
        if team is not None and t is not None:
            out.setdefault((team, int(r["week"])), []).append((t, float(r["projected_points"])))
    return out


def latest_before(snaps: list[tuple[datetime, float]] | None, t: datetime | None, *, strict: bool = True):
    """(synced_at, value) of the latest snapshot before t (strictly, or at/before with strict=False)."""
    if t is None:
        return None
    ok = [s for s in snaps or [] if (s[0] < t if strict else s[0] <= t)]
    return ok[-1] if ok else None


def espn_actuals(args, season: int, weeks: list[int]) -> dict[tuple[str, int], float]:
    """ESPN's actual league points of every D/ST week (the league's own number; reconciled exact to the rescored raw
    statistics by dst_research.py reconcile)."""
    from app.db.connection import fetch_dicts

    con = _live(args)
    try:
        ids = _dst_ids(con)
        rows = fetch_dicts(con, """SELECT espn_player_id, scoring_period AS week, actual_points
                                   FROM espn_player_weekly_points WHERE season = ? AND scoring_period IN (SELECT unnest(?))
                                   AND actual_points IS NOT NULL""", [season, weeks])
    finally:
        con.close()
    return {(ids[r["espn_player_id"]], int(r["week"])): float(r["actual_points"]) for r in rows
            if r["espn_player_id"] in ids}


def league_scoring(args) -> dr.DstScoring:
    from app.analytics.kicker_scoring import league_items

    con = _live(args)
    try:
        return dr.resolve_dst_scoring(league_items(con, args.season))
    finally:
        con.close()


# ----------------------------------------------------------------------------- ESPN statistics cache


def cache_file(args, season: int) -> Path:
    return Path(args.cache) / f"espn_dst_{season}.json"


def cache_gaps(args, season: int, needed: set[tuple[str, int]]) -> dict:
    """Which cache files are missing, and which final games (team, week) of `season` lack ESPN actual statistics."""
    missing_files = [s for s in range(research.FIRST_SEASON, season + 1) if not cache_file(args, s).exists()]
    have: set = set()
    if season not in missing_files:
        have = set(dr.parse_espn_dst(json.loads(cache_file(args, season).read_text()), season)["actual"])
    return {"missing_files": missing_files, "missing_actuals": sorted(needed - have)}


def default_fetch(args) -> dict:
    """scripts/dst_research.py fetch (cached finished seasons are reused; the current season is re-downloaded)."""
    ns = argparse.Namespace(cache=Path(args.cache), season=args.season, refresh=False)
    return research.cmd_fetch(ns)


def cache_provenance(args, season: int) -> dict:
    out = {}
    for s in range(research.FIRST_SEASON, season + 1):
        p = cache_file(args, s)
        if p.exists():
            raw = p.read_bytes()
            out[str(s)] = {"file": p.name, "sha256": _sha_bytes(raw), "fetched_at": json.loads(raw).get("_fetched_at")}
    return out


# ----------------------------------------------------------------------------- the forecast (pure on the game table)


def forecast(rows: list[dict], scoring: dr.DstScoring, season: int, week: int, open_teams: set[str], *,
             last: int | None = None) -> dict:
    """dfp-r1, the line-only baseline and the ratings-only model for `week`'s open teams, plus the ratings-only model
    at horizons 2..MAX_HORIZON for the next weeks (every team scheduled). Ratings use final games through week - 1;
    the stacks are fitted on FIRST_ROW_SEASON .. season - 1 only. `rows`: dr.build_games output (target-week rows
    carry the line as stored at freeze time). Pure."""
    rows = [r for r in rows if r["final"] or r["season"] == season]
    teams = sorted({r["team"] for r in rows} | {r["opp"] for r in rows})
    hist = dr.history_arrays(rows, teams)
    train = [r for r in rows if r["final"] and research.FIRST_ROW_SEASON <= r["season"] < season]
    if not train:
        raise ValueError(f"no final training rows in {research.FIRST_ROW_SEASON}..{season - 1}")
    target = [r for r in rows if r["season"] == season and r["week"] == week and r["team"] in open_teams]

    def project(cfg, horizon, cache, these):
        tf = dr.rating_features(train, hist, teams, cfg, horizon, cache)
        xf = dr.rating_features(these, hist, teams, cfg, horizon, cache)
        return dr.project_season(train, tf, these, xf, scoring, cfg), xf

    rating_cache: dict = {}          # FROZEN and RATINGS_ONLY share ratings (the market flag only changes the stack)
    main, feats = project(FROZEN, 1, rating_cache, target)
    line_only, _ = project(LINE_ONLY, 1, {}, target)
    ratings_only, _ = project(RATINGS_ONLY, 1, rating_cache, target)
    out_rows = []
    for r, m, lo, ro, f in zip(target, main, line_only, ratings_only, feats):
        out_rows.append({
            "team": r["team"], "opp": r["opp"], "home": r["home"], "margin": _r(r["margin"]),
            "implied_opp": _r(r["implied_opp"]), "has_line": r["implied_opp"] is not None,
            "pred": _r(m["pred"]), "pred_line_only": _r(lo["pred"]), "pred_ratings_only": _r(ro["pred"]),
            **{k: _r(m[k]) for k in ("e_sacks", "e_ints", "e_fr", "e_blk", "e_saf", "e_ret_td", "e_pa", "e_ya",
                                     "lambda_td", "p_any_td", "pred_td_points")},
            "history_games": f["history_games"]})
    multi = []
    for h in range(1, MAX_HORIZON + 1):
        wk = week + h - 1
        if last is not None and wk > last:
            break
        these = [r for r in rows if r["season"] == season and r["week"] == wk
                 and (h > 1 or r["team"] in open_teams)]
        if not these:
            continue
        preds, _ = project(RATINGS_ONLY, h, {}, these) if h > 1 else (ratings_only, None)
        multi += [{"horizon": h, "week": wk, "team": r["team"], "opp": r["opp"], "pred_ratings_only": _r(p["pred"])}
                  for r, p in zip(these, preds)]
    return {"rows": out_rows, "multi_week": multi,
            "training": {"seasons": [research.FIRST_ROW_SEASON, season - 1], "rows": len(train),
                         "information_through": {"season": season, "week": week - 1},
                         "history_games": int(len(hist["key"]))}}


# ----------------------------------------------------------------------------- freeze rule


def freeze_decision(games: list[dict], now: datetime, missing_actuals: list, unfinished: list | None = None, *,
                    deadline_hours: float = DEADLINE_HOURS) -> dict:
    """The freeze rule above, on week_games rows. Pure. Returns {action: freeze | defer | nothing_open, degraded,
    reason, open, started, unknown_kickoff, no_line, warnings, notes}."""
    open_ = [g for g in games if g["kickoff"] is not None and g["kickoff"] > now]
    started = sorted(g["team"] for g in games if g["kickoff"] is not None and g["kickoff"] <= now)
    unknown = sorted(g["team"] for g in games if g["kickoff"] is None)
    no_line = sorted(g["team"] for g in open_ if g["spread_line"] is None or g["total_line"] is None)
    first = min((g["kickoff"] for g in open_), default=None)
    hours = None if first is None else round((first - now).total_seconds() / 3600, 2)
    out = {"open": sorted(g["team"] for g in open_), "started": started, "unknown_kickoff": unknown,
           "no_line": no_line, "missing_actuals": [list(m) for m in missing_actuals],
           "previous_week_unfinished": list(unfinished or []),
           "hours_to_first_open_kickoff": hours, "degraded": False, "warnings": [], "notes": []}
    if unknown:
        out["warnings"].append("unknown kickoff (excluded, never guessed): " + ", ".join(unknown))
    if not open_:
        return {**out, "action": "nothing_open", "reason": "every game of the week has kicked off"}
    if started:
        out["notes"].append(f"{len(started)} teams already kicked off and are excluded: " + ", ".join(started))
    gaps = []
    if unfinished:
        gaps.append(f"previous week not final yet for {', '.join(unfinished)} (no final score in nfl_schedules)")
    if no_line:
        gaps.append(f"{len(no_line)} open teams have no spread/total yet ({', '.join(no_line)})")
    if missing_actuals:
        gaps.append(f"{len(missing_actuals)} final games lack ESPN D/ST statistics "
                    f"({', '.join(f'{t} w{w}' for t, w in missing_actuals[:8])}{' ...' if len(missing_actuals) > 8 else ''})")
    if not gaps:
        return {**out, "action": "freeze", "reason": f"lines and previous-week statistics complete for {len(open_)} "
                                                     "open teams"}
    if hours is not None and hours <= deadline_hours:
        out["warnings"].append(f"first open kickoff within {deadline_hours:g}h, so frozen with incomplete inputs: "
                               + "; ".join(gaps) + (" (ratings-only projection without a line)" if no_line else ""))
        return {**out, "action": "freeze", "degraded": True,
                "reason": f"first open kickoff in {hours:g}h with incomplete inputs"}
    out["notes"].append("deferred: " + "; ".join(gaps))
    return {**out, "action": "defer", "reason": "; ".join(gaps)}


# ----------------------------------------------------------------------------- pregame


def _record(args, rec: dict, now: datetime) -> Path:
    path = (shadow_dir(args) / "runs"
            / f"s{rec['season']}w{rec['week']:02d}-{now:%Y%m%dT%H%M%SZ}-{rec['kind']}-{os.getpid()}.json")
    if args.write:
        _write_once(path, rec)
    return path


def _code_hashes() -> dict:
    files = {"backend/app/analytics/dst_research.py": Path(dr.__file__), "scripts/dst_research.py":
             SCRIPTS / "dst_research.py", "scripts/dst_shadow.py": Path(__file__).resolve()}
    return {k: _sha_bytes(p.read_bytes()) for k, p in files.items()}


def apply_lines(rows: list[dict], season: int, week: int, games: list[dict]) -> None:
    """Put the line stored NOW (week_games, the live DB, whatever the history DB holds) on the target week's rows,
    oriented to each defense: margin = the defense's expected margin, implied_opp = the opponent's implied total."""
    by_team = {g["team"]: g for g in games}
    for r in rows:
        g = by_team.get(r["team"]) if (r["season"], r["week"]) == (season, week) else None
        if g is None:
            continue
        r["margin"] = r["implied_opp"] = None
        if g["spread_line"] is not None and g["total_line"] is not None:
            r["margin"] = g["spread_line"] if g["home"] else -g["spread_line"]
            r["implied_opp"] = (g["total_line"] - r["margin"]) / 2


def build_ledger(args, season: int, week: int, now: datetime, games: list[dict], dec: dict) -> dict:
    """Load the game table (ESPN cache + schedules), put the line stored NOW on the target rows, forecast, and attach
    ESPN's snapshot at freeze time and the provenance."""
    scoring = league_scoring(args)
    if not scoring.supported:
        raise RuntimeError(f"league D/ST scoring not supported (the model abstains): {scoring.describe()}")
    live, hist, rows = research.load_rows(args, scoring)
    for con in (live, hist):
        if con is not None:
            con.close()
    by_team = {g["team"]: g for g in games}
    open_teams = set(dec["open"])
    apply_lines(rows, season, week, games)
    fc = forecast(rows, scoring, season, week, open_teams, last=last_week(args, season))
    weeks = sorted({m["week"] for m in fc["multi_week"]} | {week})
    snaps = espn_snapshots(args, season, weeks)
    for r in fc["rows"]:
        g = by_team[r["team"]]
        at = latest_before(snaps.get((r["team"], week)), now, strict=False)
        r.update(kickoff=g["kickoff"].isoformat(), spread_line=g["spread_line"], total_line=g["total_line"],
                 espn_at_freeze=None if at is None else _r(at[1]),
                 espn_at_freeze_synced_at=None if at is None else at[0].isoformat())
    for m in fc["multi_week"]:
        at = latest_before(snaps.get((m["team"], m["week"])), now, strict=False)
        m.update(espn_at_freeze=None if at is None else _r(at[1]),
                 espn_at_freeze_synced_at=None if at is None else at[0].isoformat())
    lines = [{k: by_team[t][k] for k in ("team", "opponent", "home", "spread_line", "total_line")}
             for t in sorted(open_teams)]
    ledger = {"schema": SCHEMA, "candidate": CANDIDATE, "season": season, "week": week, "created_at": now.isoformat(),
              "variants": {"pred": "dfp-r1 (ratings + line, frozen config)",
                           "pred_line_only": "line + league means (no team ratings)",
                           "pred_ratings_only": "ratings only (no line); horizons 2-4 under multi_week"},
              "config": {"dfp-r1": FROZEN.to_dict(), "line_only": LINE_ONLY.to_dict(),
                         "ratings_only": RATINGS_ONLY.to_dict()},
              "config_sha256": _sha_obj([FROZEN.to_dict(), LINE_ONLY.to_dict(), RATINGS_ONLY.to_dict()]),
              "scoring": scoring.describe(), "decision": {k: v for k, v in dec.items() if k not in ("notes",)},
              "training": fc["training"],
              "inputs": {"lines": {"source": "nfl_schedules spread_line / total_line in the live DB at freeze time "
                                             "(nflverse; team_game_lines is derived from it)",
                                   "nfl_synced_at": nfl_synced_at(args), "rows": lines, "sha256": _sha_obj(lines)},
                         "espn_dst_cache": cache_provenance(args, season),
                         "espn_projection_history": "latest snapshot synced at or before created_at",
                         "live_db": str(args.live_db), "history_db": str(args.history_db)},
              "code_sha256": _code_hashes(), "rows": fc["rows"], "multi_week": fc["multi_week"]}
    ledger["rows_sha256"] = _sha_obj([ledger["rows"], ledger["multi_week"]])
    return ledger


def run_pregame(args, season: int, week: int, *, now: datetime | None = None, games_fn=None, fetch=None,
                ledger_fn=None) -> dict:
    """Freeze week W's dfp-r1 pregame ledger once, when the freeze rule allows. Never raises: every outcome (a
    failure included) is returned and, unless dry, recorded write-once under shadow/runs/."""
    now = now or _utcnow()
    games_fn = games_fn or week_games
    fetch = fetch or default_fetch
    ledger_fn = ledger_fn or build_ledger
    rec = {"schema": SCHEMA, "kind": "pregame", "candidate": CANDIDATE, "season": season, "week": week,
           "started_at": now.isoformat(), "dry_run": not args.write, "warnings": [], "notes": []}
    lpath = ledger_path(args, season, week)
    try:
        if (season, week) < FIRST_PROSPECTIVE:
            rec.update(status="before_prospective_start",
                       reason=f"prospective evidence starts {FIRST_PROSPECTIVE[0]} week {FIRST_PROSPECTIVE[1]}")
            return rec                                  # not recorded: nothing was attempted
        if lpath.exists():
            rec.update(status="already_frozen", ledger=str(lpath), ledger_sha256=_sha_bytes(lpath.read_bytes()))
            return rec                                  # idempotent: no record, nothing rewritten
        games = games_fn(args, season, week)
        if not games:
            rec.update(status="no_games", reason=f"no {season} week {week} REG games in nfl_schedules")
            return rec
        needed, unfinished = games_before(args, season, week)
        gaps = cache_gaps(args, season, needed)
        if (gaps["missing_files"] or gaps["missing_actuals"]) and args.write and args.fetch:
            try:
                rec["fetch"] = fetch(args)
            except Exception as err:      # noqa: BLE001  ESPN unreachable: decide on the cache as it is
                rec["fetch"] = {"error": f"{err.__class__.__name__}: {err}"}
                rec["notes"].append(f"ESPN D/ST statistics fetch failed ({err}); deciding on the cached statistics")
            gaps = cache_gaps(args, season, needed)
        rec["cache"] = gaps
        dec = freeze_decision(games, now, gaps["missing_actuals"], unfinished)
        if gaps["missing_files"]:                       # no history at all: a forecast is impossible
            dec = {**dec, "action": "defer" if dec["action"] == "freeze" else dec["action"],
                   "reason": f"ESPN D/ST cache missing seasons {gaps['missing_files']} (run dst_research.py fetch)"}
            (dec["warnings"] if dec["hours_to_first_open_kickoff"] is not None
             and dec["hours_to_first_open_kickoff"] <= DEADLINE_HOURS else dec["notes"]).append(dec["reason"])
        rec["decision"] = {k: v for k, v in dec.items() if k not in ("warnings", "notes")}
        rec["warnings"] += dec["warnings"]
        rec["notes"] += dec["notes"]
        if dec["action"] != "freeze":
            rec["status"] = "deferred" if dec["action"] == "defer" else "nothing_open"
        elif not args.write:
            rec["status"] = "would_freeze"
        else:
            ledger = ledger_fn(args, season, week, now, games, {**dec, "warnings": rec["warnings"]})
            written = _write_once(lpath, ledger)
            rec.update(status="frozen" if written else "already_frozen", ledger=str(lpath),
                       ledger_sha256=_sha_bytes(lpath.read_bytes()), rows=len(ledger["rows"]),
                       multi_week_rows=len(ledger["multi_week"]))
    except Exception as err:          # noqa: BLE001  recorded, never raised: the callers must not fail
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        rec["warnings"].append(f"dfp-r1 pregame shadow forecast for week {week} failed: {err}")
    rec["finished_at"] = _utcnow().isoformat()
    rec["record"] = str(_record(args, rec, now))
    return rec


def check_missed(args, season: int, week: int, *, now: datetime | None = None, games_fn=None) -> dict | None:
    """A prospective week whose games have all kicked off without a frozen ledger is recorded write-once under
    shadow/missed/ (never backfilled). Returns the record, or None."""
    if week < 1 or (season, week) < FIRST_PROSPECTIVE or ledger_path(args, season, week).exists():
        return None
    now = now or _utcnow()
    games = (games_fn or week_games)(args, season, week)
    known = [g["kickoff"] for g in games if g["kickoff"] is not None]
    if not known or max(known) > now:
        return None
    rec = {"candidate": CANDIDATE, "season": season, "week": week, "last_kickoff": max(known).isoformat(),
           "recorded_at": now.isoformat(),
           "reason": "no dfp-r1 pregame ledger was frozen before the week's games; nothing is backdated"}
    if args.write:
        _write_once(shadow_dir(args) / "missed" / f"s{season}w{week:02d}.json", rec)
    return rec


# ----------------------------------------------------------------------------- score


def scored_rows(args, ledgers: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(one-week rows, multi-week rows, per-week status) for every ledger week that is fully final: every game of the
    week final and ESPN's actual league points stored for every ledger team. ESPN pregame = the latest snapshot synced
    strictly before the team's own kickoff."""
    one, multi, weeks = [], [], []
    for led in ledgers:
        season, week = led["season"], led["week"]
        all_weeks = sorted({week} | {m["week"] for m in led["multi_week"]})
        games = {w: {g["team"]: g for g in week_games(args, season, w)} for w in all_weeks}
        actual = espn_actuals(args, season, all_weeks)
        snaps = espn_snapshots(args, season, all_weeks)

        def final(w, teams):
            return bool(games[w]) and all(g["final"] for g in games[w].values()) and all(
                (t, w) in actual for t in teams)

        ok = final(week, [r["team"] for r in led["rows"]])
        weeks.append({"season": season, "week": week, "final": ok, "ledger_rows": len(led["rows"])})
        if ok:
            for r in led["rows"]:
                ko = games[week].get(r["team"], {}).get("kickoff")
                pre = latest_before(snaps.get((r["team"], week)), ko)
                one.append({"key": season * 100 + week, "season": season, "week": week, "team": r["team"],
                            "points": actual[(r["team"], week)], "pred": r["pred"],
                            "pred_line_only": r["pred_line_only"], "pred_ratings_only": r["pred_ratings_only"],
                            "has_line": r["has_line"], "espn_at_freeze": r.get("espn_at_freeze"),
                            "espn_pregame": None if pre is None else pre[1]})
        for m in led["multi_week"]:
            w = m["week"]
            if not final(w, [x["team"] for x in led["multi_week"] if x["week"] == w]):
                continue
            ko = games[w].get(m["team"], {}).get("kickoff")
            pre = latest_before(snaps.get((m["team"], w)), ko)
            multi.append({"key": season * 100 + w, "horizon": m["horizon"], "team": m["team"],
                          "points": actual[(m["team"], w)], "pred": m["pred_ratings_only"],
                          "espn_at_freeze": m.get("espn_at_freeze"), "espn_pregame": None if pre is None else pre[1],
                          "frozen_week": week})
    return one, multi, weeks


def _brief(ev: dict) -> dict:
    keep = ("mae", "bias_actual_minus_pred", "within_week_spearman", "pairwise_order_accuracy", "mean_pred")
    return {"n": ev["n"], "weeks": ev["weeks"], **({"methods": {m: {k: v[k] for k in keep} for m, v in
                                                                ev["methods"].items()}} if ev["n"] else {}),
            **{k: v for k, v in ev.items() if k.startswith("vs_")}}


def gate(ev: dict, candidates: list[str], baseline: str) -> dict:
    """The pre-registered GATE on a dr.evaluate result with a `vs_<baseline>` block."""
    out = {"criteria": GATE, "baseline": baseline, "weeks": ev["weeks"], "n": ev["n"], "candidates": {}}
    for name in candidates:
        if ev["weeks"] < GATE["min_weeks"] or name not in ev.get(f"vs_{baseline}", {}):
            out["candidates"][name] = {"status": "accumulating",
                                       "reason": f"{ev['weeks']} of {GATE['min_weeks']} scored weeks"}
            continue
        cmp, m = ev[f"vs_{baseline}"][name], ev["methods"][name]
        checks = {"mae_better_than_espn": cmp["mae_diff_ci90"][1] < GATE["mae_ci90_upper_below"],
                  "ranking_not_worse": cmp["spearman_diff_ci90"][0] > GATE["spearman_ci90_lower_above"],
                  "bias_ok": abs(m["bias_actual_minus_pred"]) <= GATE["max_abs_bias"]}
        out["candidates"][name] = {"status": "passes" if all(checks.values()) else "fails", "checks": checks}
    return out


def score(args) -> dict:
    """Per-week and pooled scores of every frozen ledger whose week is fully final, against ESPN's pregame snapshot
    (the gate) and ESPN's snapshot at freeze time (same information time), plus the multi-week comparison."""
    paths = sorted((shadow_dir(args) / "ledger").glob("s*-pregame.json"))
    ledgers = [led for led in (json.loads(p.read_text()) for p in paths) if led["season"] == args.season]
    one, multi, weeks = scored_rows(args, ledgers)
    cands = ["dfp-r1", "line + league means"]
    pre = {"dfp-r1": "pred", "line + league means": "pred_line_only", "ESPN pregame": "espn_pregame"}
    at = {"dfp-r1": "pred", "line + league means": "pred_line_only", "ESPN at freeze": "espn_at_freeze"}
    pooled = dr.evaluate(one, pre, baseline="ESPN pregame", top_k=(3, 5))
    per_week = [{**w, **(_brief(dr.evaluate([r for r in one if r["key"] == w["season"] * 100 + w["week"]], pre,
                                            resamples=0, top_k=(3,))) if w["final"] else {})} for w in weeks]
    horizons = {}
    for h in range(1, MAX_HORIZON + 1):
        rows = [r for r in multi if r["horizon"] == h]
        if rows:
            horizons[str(h)] = {
                "vs_espn_at_freeze": _brief(dr.evaluate(rows, {"ratings only": "pred", "ESPN at freeze": "espn_at_freeze"},
                                                        baseline="ESPN at freeze", resamples=500, top_k=(3,))),
                "vs_espn_pregame": _brief(dr.evaluate(rows, {"ratings only": "pred", "ESPN pregame": "espn_pregame"},
                                                      baseline="ESPN pregame", resamples=500, top_k=(3,)))}
    return {"candidate": CANDIDATE, "season": args.season, "ledgers": [p.name for p in paths],
            "scored_weeks": sum(w["final"] for w in weeks), "weeks": per_week,
            "pooled_vs_espn_pregame": _brief(pooled), "gate": gate(pooled, cands, "ESPN pregame"),
            "pooled_vs_espn_at_freeze": _brief(dr.evaluate(one, at, baseline="ESPN at freeze", top_k=(3, 5))),
            "rows_without_line": sum(not r["has_line"] for r in one),
            "multi_week": {"note": "ratings-only model h weeks ahead vs ESPN's projection from the snapshot at freeze "
                                   "time (same information time) and ESPN's pregame snapshot", "horizons": horizons}}


def run_score(args, *, now: datetime | None = None) -> dict:
    """Score the frozen ledgers. Never raises; recorded write-once under shadow/runs/, scores.json replaced."""
    now = now or _utcnow()
    rec = {"schema": SCHEMA, "kind": "score", "candidate": CANDIDATE, "season": args.season, "week": 0,
           "started_at": now.isoformat(), "dry_run": not args.write, "warnings": [], "notes": []}
    try:
        if not list((shadow_dir(args) / "ledger").glob("s*-pregame.json")):
            rec.update(status="no_ledgers", notes=["no dfp-r1 ledger frozen yet: nothing to score"])
            return rec                                  # not recorded: nothing was attempted
        rec["scores"] = score(args)
        rec["status"] = "scored"
        if args.write:
            _replace_json(shadow_dir(args) / "scores.json", {"scored_at": now.isoformat(), **rec["scores"]})
    except Exception as err:          # noqa: BLE001
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        rec["warnings"].append(f"scoring the dfp-r1 ledgers failed: {err}")
    rec["finished_at"] = _utcnow().isoformat()
    rec["record"] = str(_record(args, rec, now))
    return rec


# ----------------------------------------------------------------------------- commands


def _summary(rec: dict) -> dict:
    keep = ("kind", "season", "week", "status", "reason", "ledger", "ledger_sha256", "rows", "error", "record",
            "warnings", "notes")
    out = {k: rec[k] for k in keep if k in rec}
    if "decision" in rec:
        out["decision"] = {k: rec["decision"].get(k) for k in ("action", "degraded", "reason",
                                                               "hours_to_first_open_kickoff")}
    if rec.get("status") == "scored":
        s = rec["scores"]
        out["scored_weeks"] = s["scored_weeks"]
        out["gate"] = {n: c["status"] for n, c in s["gate"]["candidates"].items()}
        out["pooled_vs_espn_pregame"] = s["pooled_vs_espn_pregame"].get("methods")
    return out


def target_week(args, now: datetime) -> int | None:
    """The upcoming week: the earliest REG week with a game still to kick off (refresh_all.py's rule)."""
    con = _live(args)
    try:
        weeks = [int(r[0]) for r in con.execute("SELECT DISTINCT week FROM nfl_schedules WHERE season = ? AND "
                                                "game_type = 'REG' ORDER BY week", [args.season]).fetchall()]
    finally:
        con.close()
    for w in weeks:
        ks = [g["kickoff"] for g in week_games(args, args.season, w) if g["kickoff"] is not None]
        if ks and max(ks) > now:
            return w
    return None


def cmd_pregame(args, now: datetime) -> dict:
    week = args.week if args.week is not None else target_week(args, now)
    if week is None:
        return {"kind": "pregame", "status": "no_upcoming_week", "warnings": [], "notes": []}
    return _summary(run_pregame(args, args.season, week, now=now))


def cmd_score(args, now: datetime) -> dict:
    return _summary(run_score(args, now=now))


def cmd_cycle(args, now: datetime) -> dict:
    """learning_cycle.py's stage: score the frozen weeks, record a missed previous week, then try the target week's
    freeze (normally it happens here, Tue-Thu, once Monday night's statistics are posted)."""
    week = args.week if args.week is not None else target_week(args, now)
    out = {"score": _summary(run_score(args, now=now))}
    prev = week - 1 if week else last_week(args, args.season)
    missed = check_missed(args, args.season, prev, now=now) if prev else None
    out["missed"] = missed
    out["pregame"] = (_summary(run_pregame(args, args.season, week, now=now)) if week
                      else {"status": "no_upcoming_week", "warnings": [], "notes": []})
    out["warnings"] = [*out["score"]["warnings"], *out["pregame"]["warnings"]]
    if missed:
        out["warnings"].append(f"week {missed['week']} started without a frozen dfp-r1 ledger (recorded as missed)")
    out["status"] = "failed" if "failed" in (out["score"]["status"], out["pregame"]["status"]) else "ok"
    return out


def cmd_status(args, now: datetime) -> dict:
    d = shadow_dir(args)
    runs = sorted((d / "runs").glob("*.json"), key=lambda p: p.stat().st_mtime) if (d / "runs").exists() else []
    scores = d / "scores.json"
    return {"ledgers": sorted(p.name for p in (d / "ledger").glob("s*-pregame.json")),
            "missed": sorted(p.name for p in (d / "missed").glob("*.json")),
            "latest_run": json.loads(runs[-1].read_text()) if runs else None,
            "scores": json.loads(scores.read_text()) if scores.exists() else None}


def parse(argv=None):
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["pregame", "score", "cycle", "status"])
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--week", type=int, help="default: the upcoming week (earliest with a game still to kick off)")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=research._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--cache", type=Path, default=research._path("DST_ESPN_CACHE", "data/raw/espn_dst"))
    ap.add_argument("--out", type=Path, default=research._path("DST_RESEARCH_DIR", "data/models/dst"))
    ap.add_argument("--no-fetch", dest="fetch", action="store_false",
                    help="never download ESPN D/ST statistics (use the cache as it is)")
    ap.add_argument("--now", help="evaluate the freeze rule at this ISO time; implies --dry-run")
    ap.add_argument("--dry-run", action="store_true", help="decide and print only: no forecast, no fetch, no record")
    args = ap.parse_args(argv)
    args.write = not (args.dry_run or args.now)
    return args


def main(argv=None) -> int:
    args = parse(argv)
    now = _utcnow()
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    cmds = {"pregame": cmd_pregame, "score": cmd_score, "cycle": cmd_cycle, "status": cmd_status}
    out = cmds[args.command](args, now)
    print(json.dumps(research._clean(out), indent=1, default=str))
    if args.command == "status":
        return EXIT_OK
    for w in out.get("warnings", []):
        print(f"WARNING: {CANDIDATE} shadow: {w}", file=sys.stderr)
    if out.get("status") == "failed":
        return EXIT_FAILED
    return EXIT_WARNINGS if out.get("warnings") else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
