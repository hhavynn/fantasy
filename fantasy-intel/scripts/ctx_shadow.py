"""Prospective shadow automation for the frozen `ifp-ctx-p1` research candidate (research only; see
docs/research/ifp_ctx_shadow_automation.md and docs/research/qb_teammate_context.md section 8). It decides
WHEN to call the existing `context_research.py predict` / `prospective` commands and records why; the
candidate's model, features and ledger format are context_research.py's and are not changed here.

    cd backend
    uv run python ../scripts/ctx_shadow.py pregame [--week W]     # gated write-once pregame ledger for week W
    uv run python ../scripts/ctx_shadow.py score                  # score frozen weeks: ctx vs ifp-2 vs actuals
    uv run python ../scripts/ctx_shadow.py cycle   [--week W]     # score, missed-week check, then pregame
    uv run python ../scripts/ctx_shadow.py status
    uv run python ../scripts/ctx_shadow.py pregame --week W --now 2026-10-25T12:00Z   # gate only (dry run)

Who runs it (no new scheduler):
  * refresh_all.py's read-only evidence stage runs `pregame` for the upcoming week on every refresh (launchd
    Thu 15:00 / Sun 08:00 / Mon 15:00 / Tue 08:00 PT, and every pregame-guard run within 6 h of a kickoff).
    That is where the freeze normally happens: just before the main slate, after the final injury reports.
  * learning_cycle.py runs `cycle` as a non-fatal stage after the production run (Tue-Thu): it scores every
    fully final frozen week and records a week that started without a ledger.

Freeze rule (fixed here, before any prospective week exists). The ledger is write-once, so a forecast frozen
on a mid-week practice report (no game designations yet) would stay degraded forever; ifp-ctx-p1's QB and
teammate features read the nflverse week-W designations (Out / Doubtful / Questionable). For the teams that
have not kicked off ("open"), a team is READY when injury_freshness reports nflverse designations retrieved
`fresh`. The ledger is frozen when
  * at least READY_SHARE of the open teams are ready, or
  * the main slate is imminent: at least SLATE_SHARE of the open teams kick off within DEADLINE_HOURS
    (then it is frozen anyway, and every team that is not ready is recorded as a warning);
otherwise it is deferred to a later run. It is never frozen without the week's report (predict refuses), and
teams that already kicked off are excluded by predict itself. Thursday (and early international) games
therefore usually fall outside the ledger: a single write-once file per week cannot hold them without
freezing the Sunday teams on a practice report.

Writes only under <ifp-dir>/research/context/ (git-ignored): the ledger, artifact and frozen spec via
context_research.py, plus shadow/runs/*.json (one write-once record per attempt), shadow/freeze/s<S>w<WW>.json
(write-once injury snapshot at freeze time), shadow/missed/*.json (write-once) and shadow/scores.json
(derived, replaced). It opens the databases read-only and never touches the live database tables, ifp-1/ifp-2
artifacts, the weekly ledger, the registry, the API or the frontend.

Exit codes: 0 ok (frozen, already frozen, deferred, nothing to do); 3 ok but with warnings (missing or stale
injury evidence, degraded freeze, missed week); 1 a step failed (recorded, nothing half-written).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import challenger_research as cr  # noqa: E402  (path defaults shared with context_research.py)

from app.config import get_settings  # noqa: E402
from app.db.connection import connect as db_connect  # noqa: E402

SCHEMA = "ifp-ctx-shadow-run/v1"
CANDIDATE = "ifp-ctx-p1"
FIRST_PROSPECTIVE = (2026, 7)       # pre-registered: only frozen forecasts from 2026 week 7 on count
READY_SHARE = 0.9
SLATE_SHARE = 0.5
DEADLINE_HOURS = 6.0                # the pregame guard's lead window
STEP_TIMEOUT_S = 20 * 60
EXIT_OK, EXIT_FAILED, EXIT_WARNINGS = 0, 1, 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ctx_dir(args) -> Path:
    return Path(args.ifp_dir) / "research" / "context"


def ledger_path(args, season: int, week: int) -> Path:
    """context_research.py cmd_predict's ledger path (one write-once file per week)."""
    return ctx_dir(args) / "ledger" / f"s{season}w{week:02d}-pregame.json"


def _write_once(path: Path, obj) -> bool:
    from app.analytics.weekly_learning import write_once
    return write_once(path, json.dumps(obj, indent=1, default=str))


def _replace_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


def _sha_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ----------------------------------------------------------------------------- injury evidence + freeze rule


def injury_snapshot(args, season: int, week: int, now: datetime) -> dict:
    """injury_freshness.diagnostics for the week, from a read-only connection (changes nothing)."""
    from app.analytics import injury_freshness as inj

    con = db_connect(args.live_db, read_only=True)
    try:
        return inj.diagnostics(con, season, week, now)
    finally:
        con.close()


def freeze_decision(teams: list[dict], *, ready_share: float = READY_SHARE, slate_share: float = SLATE_SHARE,
                    deadline_hours: float = DEADLINE_HOURS) -> dict:
    """The freeze rule above, on injury_freshness.team_freshness rows. Pure.
    Returns {action: freeze | defer | nothing_open, degraded, reason, warnings, notes, counts...}."""
    open_ = [t for t in teams if t["kickoff"] is not None and not t["game_started"]]
    ready = [t for t in open_ if t["nflverse_designation_synced"] and t["status"] == "fresh"]
    ready_names = {t["team"] for t in ready}
    imminent = [t for t in open_ if t["hours_to_kickoff"] is not None and t["hours_to_kickoff"] <= deadline_hours]
    report_rows = sum(t["nflverse_report"]["rows"] for t in teams)

    def why(t: dict) -> str:
        rep = t["nflverse_report"]
        if not rep["rows"]:
            return "no nflverse injury report rows"
        if not t["nflverse_designation_synced"]:
            return (f"practice report only ({rep['practice_only']} rows, no game designation)"
                    + ("; ESPN status exists but ifp-ctx-p1 reads only the nflverse report"
                       if t["source_used"] == "espn_status" else ""))
        return f"{t['status']}: {t['reason']}"

    not_ready = {t["team"]: why(t) for t in open_ if t["team"] not in ready_names}
    out = {"open_teams": len(open_), "ready_teams": len(ready), "imminent_teams": len(imminent),
           "started_teams": sum(1 for t in teams if t["game_started"]), "report_rows": report_rows,
           "not_ready": not_ready, "unknown_kickoff": sorted(t["team"] for t in teams if t["kickoff"] is None),
           "degraded": False, "warnings": [], "notes": []}
    if out["unknown_kickoff"]:
        out["warnings"].append("unknown kickoff (excluded, never guessed): " + ", ".join(out["unknown_kickoff"]))
    slate_now = bool(open_) and len(imminent) >= slate_share * len(open_)
    if not open_:
        return {**out, "action": "nothing_open", "reason": "every game of the week has kicked off"}
    if report_rows == 0:
        msg = "the week's nflverse injury report is not ingested yet (run sync_nfl.py)"
        if slate_now:
            out["warnings"].append(f"{msg} and {len(imminent)} of {len(open_)} open teams kick off within "
                                   f"{deadline_hours:g}h: this week's ledger will be missed unless it lands first")
        else:
            out["notes"].append(f"{msg}; deferred")
        return {**out, "action": "defer", "reason": msg}
    if len(ready) >= ready_share * len(open_):
        if not_ready:
            out["warnings"].append(f"frozen with {len(not_ready)} of {len(open_)} open teams lacking fresh nflverse "
                                   "designations: " + "; ".join(f"{k} ({v})" for k, v in sorted(not_ready.items())))
        return {**out, "action": "freeze", "degraded": bool(not_ready),
                "reason": f"{len(ready)} of {len(open_)} open teams have fresh nflverse designations"}
    if slate_now:
        out["warnings"].append(f"main slate within {deadline_hours:g}h, so frozen with degraded injury evidence for "
                               f"{len(not_ready)} of {len(open_)} open teams: "
                               + "; ".join(f"{k} ({v})" for k, v in sorted(not_ready.items())))
        return {**out, "action": "freeze", "degraded": True,
                "reason": f"main slate imminent ({len(imminent)} of {len(open_)} open teams within {deadline_hours:g}h)"}
    out["notes"].append(f"only {len(ready)} of {len(open_)} open teams have fresh nflverse designations "
                        f"(need {ready_share:.0%}); deferred to a later pregame run")
    return {**out, "action": "defer",
            "reason": f"{len(ready)} of {len(open_)} open teams ready, slate not within {deadline_hours:g}h"}


# ----------------------------------------------------------------------------- context_research.py calls


def _research_cmd(args, command: str, *extra: str) -> list[str]:
    return [sys.executable, str(SCRIPTS / "context_research.py"), command, "--season", str(args.season),
            "--ifp-dir", str(args.ifp_dir), "--live-db", str(args.live_db), "--history-db", str(args.history_db),
            "--exits-dir", str(args.exits_dir), *extra]


def run_research(args, command: str, *extra: str) -> dict:
    """Run one context_research.py command (its public CLI) and return its JSON. Raises RuntimeError with
    the command's own message (a refusal is a SystemExit message on stderr)."""
    proc = subprocess.run(_research_cmd(args, command, *extra), cwd=_bootstrap.BACKEND, capture_output=True,
                          text=True, env={**os.environ, "PYTHONUNBUFFERED": "1"}, timeout=args.timeout)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(f"context_research.py {command} exited {proc.returncode}: "
                           f"{msg[-1] if msg else 'no output'}")
    text = proc.stdout.strip()
    try:
        return json.loads(text[text.index("{"):]) if "{" in text else {}
    except ValueError as err:
        raise RuntimeError(f"context_research.py {command} printed no JSON result") from err


def default_predict(args, season: int, week: int) -> dict:
    return run_research(args, "predict", "--week", str(week))


def default_prospective(args) -> dict:
    return run_research(args, "prospective")


# ----------------------------------------------------------------------------- pregame


def _record(args, rec: dict, now: datetime) -> Path:
    path = (ctx_dir(args) / "shadow" / "runs"
            / f"s{rec['season']}w{rec['week']:02d}-{now:%Y%m%dT%H%M%SZ}-{rec['kind']}-{os.getpid()}.json")
    if args.write:
        _write_once(path, rec)
    return path


def run_pregame(args, season: int, week: int, *, now: datetime | None = None, snapshot=None, predict=None) -> dict:
    """Freeze week W's ifp-ctx-p1 pregame ledger once, when the freeze rule allows. Never raises: every
    outcome (including a failure) is returned and, unless dry, recorded write-once under shadow/runs/."""
    now = now or _utcnow()
    snapshot = snapshot or injury_snapshot
    predict = predict or default_predict
    rec = {"schema": SCHEMA, "kind": "pregame", "candidate": CANDIDATE, "season": season, "week": week,
           "started_at": now.isoformat(), "dry_run": not args.write, "warnings": [], "notes": []}
    lpath = ledger_path(args, season, week)
    try:
        if (season, week) < FIRST_PROSPECTIVE:
            rec.update(status="before_prospective_start",
                       reason=f"prospective evidence starts {FIRST_PROSPECTIVE[0]} week {FIRST_PROSPECTIVE[1]}")
            return rec                                  # not recorded: nothing was attempted
        if lpath.exists():
            rec.update(status="already_frozen", ledger=str(lpath), ledger_sha256=_sha_file(lpath))
            if not (ctx_dir(args) / "shadow" / "freeze" / f"s{season}w{week:02d}.json").exists():
                rec["notes"].append("ledger was frozen outside this automation: no freeze-time injury snapshot")
            return rec                                  # idempotent: no record, no predict, nothing rewritten
        diag = snapshot(args, season, week, now)
        dec = freeze_decision(diag["teams"])
        rec["injury"] = {"summary": diag["summary"], "nflverse_writer": diag.get("nflverse_writer"),
                         "log_present": diag.get("log_present"), "upcoming_issues": diag.get("upcoming_issues")}
        rec["decision"] = {k: v for k, v in dec.items() if k not in ("warnings", "notes")}
        rec["warnings"] += dec["warnings"]
        rec["notes"] += dec["notes"]
        if not diag.get("log_present"):
            rec["warnings"].append("no injury_retrievals log: no injury retrieval has ever been verified")
        if dec["action"] != "freeze":
            rec["status"] = "deferred" if dec["action"] == "defer" else "nothing_open"
        elif not args.write:
            rec["status"] = "would_freeze"
        else:
            res = predict(args, season, week)
            rec["predict"] = res
            written = bool(res.get("written"))
            path = Path(res.get("ledger") or lpath)
            rec.update(status="frozen" if written else "already_frozen", ledger=str(path),
                       ledger_sha256=_sha_file(path) if path.exists() else None)
            if written:
                # write-once: the injury evidence as it stood when this ledger was frozen
                _write_once(ctx_dir(args) / "shadow" / "freeze" / f"s{season}w{week:02d}.json", {
                    "schema": "ifp-ctx-shadow-freeze/v1", "candidate": CANDIDATE, "season": season, "week": week,
                    "ledger": path.name, "ledger_sha256": rec["ledger_sha256"],
                    "ledger_created_at": res.get("created_at"), "decided_at": now.isoformat(),
                    "decision": rec["decision"], "degraded": dec["degraded"], "warnings": rec["warnings"],
                    "injury": rec["injury"]})
    except Exception as err:          # noqa: BLE001  recorded, never raised: the callers must not fail
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        rec["warnings"].append(f"pregame shadow forecast for week {week} failed: {err}")
    rec["finished_at"] = _utcnow().isoformat()
    rec["record"] = str(_record(args, rec, now))
    return rec


def check_missed(args, season: int, week: int, *, now: datetime | None = None, kickoffs=None) -> dict | None:
    """A prospective week whose games have all kicked off without a frozen ledger is recorded write-once
    under shadow/missed/ (never backfilled). Returns the record, or None."""
    if week < 1 or (season, week) < FIRST_PROSPECTIVE or ledger_path(args, season, week).exists():
        return None
    now = now or _utcnow()
    if kickoffs is None:
        from app.analytics.injury_freshness import scheduled_kickoffs
        con = db_connect(args.live_db, read_only=True)
        try:
            kickoffs = scheduled_kickoffs(con, season, week)
        finally:
            con.close()
    known = [k for k in kickoffs.values() if k is not None]
    if not known or max(known) > now:
        return None
    rec = {"candidate": CANDIDATE, "season": season, "week": week, "last_kickoff": max(known).isoformat(),
           "recorded_at": now.isoformat(),
           "reason": "no ifp-ctx-p1 pregame ledger was frozen before the week's games; nothing is backdated"}
    if args.write:
        _write_once(ctx_dir(args) / "shadow" / "missed" / f"s{season}w{week:02d}.json", rec)
    return rec


# ----------------------------------------------------------------------------- score


def _production_rows(weekly_dir: Path, season: int, week: int) -> dict[str, float]:
    """ifp-2.0.0 (the weekly ledger's `challenger` block) of the earliest pregame run frozen for the week:
    the production forecast that was actually frozen before kickoff."""
    d = Path(weekly_dir) / "ledger" / f"s{season}w{week:02d}"
    runs = []
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            run = json.loads(f.read_text())
        except ValueError:
            continue
        if run.get("kind") == "pregame":
            runs.append(run)
    if not runs:
        return {}
    run = min(runs, key=lambda r: r["created_at"])
    return {r["gsis_id"]: r["challenger"]["points"] for r in run["rows"]
            if r.get("pregame") and (r.get("challenger") or {}).get("points") is not None}


def score_alongside(args) -> dict:
    """Per fully final frozen week (in the context store with labels), MAE / bias of ifp-ctx-p1, the ifp-2
    refit frozen in the same ledger, and the production ifp-2 frozen in the weekly ledger, on identical rows
    (appearances where all three exist). Reads files only; writes shadow/scores.json."""
    import polars as pl

    store_path = ctx_dir(args) / "store.parquet"
    ledgers = sorted((ctx_dir(args) / "ledger").glob("s*-pregame.json"))
    if not store_path.exists() or not ledgers:
        return {"weeks": [], "pooled": None}
    store = pl.read_parquet(store_path, columns=["key", "gsis_id", "position", "label"]).filter(
        pl.col("label").is_not_null())
    weeks, rows = [], []
    for path in ledgers:
        led = json.loads(path.read_text())
        key = led["season"] * 100 + led["week"]
        actual = {g: (p, y) for g, p, y in store.filter(pl.col("key") == key).select(
            "gsis_id", "position", "label").iter_rows()}
        if not actual:                                  # not fully final yet
            continue
        prod = _production_rows(args.weekly_dir, led["season"], led["week"])
        common = [(r["gsis_id"], actual[r["gsis_id"]][0], actual[r["gsis_id"]][1], r["ctx_points"],
                   r["ifp2_points"], prod[r["gsis_id"]]) for r in led["rows"]
                  if r["gsis_id"] in actual and r["gsis_id"] in prod
                  and r["ctx_points"] is not None and r["ifp2_points"] is not None]
        rows += [(led["week"], *c) for c in common]
        weeks.append({"season": led["season"], "week": led["week"], "ledger": path.name,
                      "ledger_rows": len(led["rows"]), "production_rows": len(prod), **_metrics(common)})
    pooled = _metrics([r[1:] for r in rows])
    by_pos = {pos: _metrics([r[1:] for r in rows if r[2] == pos]) for pos in sorted({r[2] for r in rows})}
    return {"weeks": weeks, "pooled": pooled, "by_position": by_pos}


def _metrics(rows: list[tuple]) -> dict:
    """rows: (gsis_id, position, label, ctx, ifp2_same_run, ifp2_production)."""
    n = len(rows)
    if not n:
        return {"n": 0}
    out = {"n": n}
    for i, name in ((3, "ctx"), (4, "ifp2_same_run"), (5, "ifp2_production")):
        err = [r[i] - r[2] for r in rows]
        out[name] = {"mae": round(sum(abs(e) for e in err) / n, 4), "bias": round(sum(err) / n, 4)}
    out["ctx_minus_ifp2_production_mae"] = round(out["ctx"]["mae"] - out["ifp2_production"]["mae"], 4)
    out["ctx_minus_ifp2_same_run_mae"] = round(out["ctx"]["mae"] - out["ifp2_same_run"]["mae"], 4)
    return out


def run_score(args, *, now: datetime | None = None, prospective=None) -> dict:
    """context_research.py prospective (the pre-registered gate, ctx vs its same-run ifp-2), then the
    comparison with the frozen production ifp-2. Never raises; recorded write-once under shadow/runs/."""
    now = now or _utcnow()
    prospective = prospective or default_prospective
    rec = {"schema": SCHEMA, "kind": "score", "candidate": CANDIDATE, "season": args.season, "week": 0,
           "started_at": now.isoformat(), "dry_run": not args.write, "warnings": [], "notes": []}
    ledgers = sorted((ctx_dir(args) / "ledger").glob("s*-pregame.json"))
    try:
        if not ledgers:
            rec.update(status="no_ledgers", notes=["no ifp-ctx-p1 ledger frozen yet: nothing to score"])
            return rec                                  # not recorded: nothing was attempted
        if not args.write:              # prospective writes prospective.json: a dry run only reports what exists
            rec.update(status="would_score", ledgers=[p.name for p in ledgers])
            return rec
        rec["prospective"] = prospective(args)
        rec["alongside_production"] = score_alongside(args)
        rec["status"] = "scored"
        if args.write:
            _replace_json(ctx_dir(args) / "shadow" / "scores.json",
                          {"scored_at": now.isoformat(), "candidate": CANDIDATE, "prospective": rec["prospective"],
                           "alongside_production": rec["alongside_production"]})
    except Exception as err:          # noqa: BLE001
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        rec["warnings"].append(f"scoring the ifp-ctx-p1 ledgers failed: {err}")
    rec["finished_at"] = _utcnow().isoformat()
    rec["record"] = str(_record(args, rec, now))
    return rec


# ----------------------------------------------------------------------------- commands


def _summary(rec: dict) -> dict:
    keep = ("kind", "season", "week", "status", "reason", "ledger", "ledger_sha256", "error", "record", "warnings",
            "notes")
    out = {k: rec[k] for k in keep if k in rec}
    if "decision" in rec:
        out["decision"] = {k: rec["decision"][k] for k in ("action", "degraded", "reason", "open_teams",
                                                           "ready_teams", "imminent_teams")}
    if rec.get("kind") == "score" and rec.get("status") == "scored":
        out["gate"] = {p: g["status"] for p, g in (rec["prospective"].get("gate") or {}).items()}
        out["scored_weeks"] = rec["prospective"].get("scored_weeks")
        out["alongside_production"] = rec["alongside_production"].get("pooled")
    return out


def _target_week(args, now: datetime) -> int | None:
    """The upcoming week: the earliest REG week with a game still to kick off (refresh_all.py's rule)."""
    from app.analytics.injury_freshness import scheduled_kickoffs

    con = db_connect(args.live_db, read_only=True)
    try:
        weeks = [r[0] for r in con.execute("SELECT DISTINCT week FROM nfl_schedules WHERE season = ? AND "
                                           "game_type = 'REG' ORDER BY week", [args.season]).fetchall()]
        for w in weeks:
            ks = [k for k in scheduled_kickoffs(con, args.season, int(w)).values() if k is not None]
            if ks and max(ks) > now:
                return int(w)
    finally:
        con.close()
    return None


def _last_week(args) -> int | None:
    con = db_connect(args.live_db, read_only=True)
    try:
        row = con.execute("SELECT max(week) FROM nfl_schedules WHERE season = ? AND game_type = 'REG'",
                          [args.season]).fetchone()
    finally:
        con.close()
    return None if row is None or row[0] is None else int(row[0])


def cmd_pregame(args, now: datetime) -> dict:
    week = args.week if args.week is not None else _target_week(args, now)
    if week is None:
        return {"kind": "pregame", "status": "no_upcoming_week", "warnings": [], "notes": []}
    return _summary(run_pregame(args, args.season, week, now=now))


def cmd_score(args, now: datetime) -> dict:
    return _summary(run_score(args, now=now))


def cmd_cycle(args, now: datetime) -> dict:
    """learning_cycle.py's stage: score the frozen weeks, record a missed previous week, then try the
    target week's pregame freeze (usually deferred on Tue-Thu: the final reports land Friday)."""
    week = args.week if args.week is not None else _target_week(args, now)
    out = {"score": _summary(run_score(args, now=now))}
    # the week before the target, or after the season's last week has started, that last week
    prev = week - 1 if week else _last_week(args)
    missed = check_missed(args, args.season, prev, now=now) if prev else None
    out["missed"] = missed
    out["pregame"] = (_summary(run_pregame(args, args.season, week, now=now)) if week
                      else {"status": "no_upcoming_week", "warnings": [], "notes": []})
    out["warnings"] = [*out["score"]["warnings"], *out["pregame"]["warnings"]]
    if missed:
        out["warnings"].append(f"week {missed['week']} started without a frozen ifp-ctx-p1 ledger (recorded as missed)")
    out["status"] = "failed" if "failed" in (out["score"]["status"], out["pregame"]["status"]) else "ok"
    return out


def cmd_status(args, now: datetime) -> dict:
    d = ctx_dir(args)
    runs = sorted((d / "shadow" / "runs").glob("*.json"), key=lambda p: p.stat().st_mtime) if (
        d / "shadow" / "runs").exists() else []
    scores = d / "shadow" / "scores.json"
    return {"ledgers": sorted(p.name for p in (d / "ledger").glob("s*-pregame.json")),
            "freeze_snapshots": sorted(p.name for p in (d / "shadow" / "freeze").glob("*.json")),
            "missed": sorted(p.name for p in (d / "shadow" / "missed").glob("*.json")),
            "latest_run": json.loads(runs[-1].read_text()) if runs else None,
            "scores": json.loads(scores.read_text()) if scores.exists() else None}


def parse(argv=None):
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["pregame", "score", "cycle", "status"])
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--week", type=int, help="default: the upcoming week (earliest with a game still to kick off)")
    ap.add_argument("--ifp-dir", type=Path, default=cr._path("IFP_MODEL_DIR", "data/models"))
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--weekly-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                    help="the weekly learning output holding the frozen production ifp-2 ledger")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--timeout", type=float, default=STEP_TIMEOUT_S, help="seconds per context_research.py call")
    ap.add_argument("--now", help="evaluate the freeze rule at this ISO time; implies --dry-run")
    ap.add_argument("--dry-run", action="store_true", help="decide and print only: no forecast, no record")
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
    print(json.dumps(out, indent=1, default=str))
    if args.command == "status":
        return EXIT_OK
    for w in out.get("warnings", []):
        print(f"WARNING: {CANDIDATE} shadow: {w}", file=sys.stderr)
    if out.get("status") == "failed":
        return EXIT_FAILED
    return EXIT_WARNINGS if out.get("warnings") else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
