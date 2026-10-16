"""Unattended weekly learning cycle for the independent projection engine (research only).

Wraps the existing `weekly_learning.py` commands in one restartable, idempotent run meant for a
scheduler (scripts/launchd/com.fantasy-intel.weekly-learning.plist). It must run on the machine that
holds the writable DuckDB files and the git-ignored model directory (the laptop in this project's
setup); MotherDuck and Vercel only carry a read-only published copy. Nothing here touches the
production model files, the live projection tables, ESPN or MotherDuck.

    cd backend
    uv run python ../scripts/learning_cycle.py run                 # one cycle (what the scheduler runs)
    uv run python ../scripts/learning_cycle.py run --skip-sync     # data already synced by refresh_all.py
    uv run python ../scripts/learning_cycle.py status              # latest cycle report + lock state

A cycle: lock -> preflight -> sync_nfl.py -> sync-exits -> pick the target week (the next week whose
first kickoff, US Eastern from nflverse, is still ahead) -> readiness (every earlier game final,
box scores / snaps / xFP synced) -> evaluate frozen forecasts -> train (reused when the training
data, configuration and scoring fingerprint are unchanged) -> predict (pregame only) -> report.
A stage failure stops every later stage. Nothing is backdated, overwritten or replayed: a week whose
first kickoff passed without a frozen pregame forecast is recorded under <out>/missed/.

Injury-retrieval freshness (docs/injury_freshness.md section 5) is reported as `warnings` /
`injury_freshness` in the cycle report; it never changes a status, an exit code or a forecast.

Exit codes: 0 forecast frozen or already up to date (or no upcoming week); 3 not ready yet (retry
before kickoff); 4 another cycle holds the lock; 5 configuration/dependency error; 1 stage failure.
Reports: <out>/reports/cycle-<UTC stamp>.json (write-once), <out>/status/latest-cycle.json.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import weekly_learning as cli  # noqa: E402  (the existing weekly_learning.py commands)

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import weekly_learning as wl  # noqa: E402
from app.config import get_settings  # noqa: E402

SCHEMA = "weekly-learning-cycle/v1"
EASTERN = ZoneInfo("America/New_York")
EXIT_OK, EXIT_FAILED, EXIT_NOT_READY, EXIT_LOCKED, EXIT_CONFIG = 0, 1, 3, 4, 5
STALE_LOCK = timedelta(hours=6)
LOCK_NAME = "cycle.lock"


class CycleStop(Exception):
    """Stops the cycle with a status, an exit code and the actions a person must take."""

    def __init__(self, status: str, code: int, message: str, actions: list[str] | None = None):
        super().__init__(message)
        self.status, self.code, self.actions = status, code, actions or []


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _replace_json(path: Path, obj) -> None:
    """Atomically replace a mutable index file (status/state, never evidence)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- lock


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(out: Path, now: datetime) -> Path:
    """One cycle at a time per output directory. A lock whose process is gone (same host) or that
    is older than STALE_LOCK is taken over; the takeover is reported, never silent."""
    path = Path(out) / "locks" / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    me = {"pid": os.getpid(), "host": socket.gethostname(), "started_at": now.isoformat()}
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                held = json.loads(path.read_text())
                started = datetime.fromisoformat(held["started_at"])
            except (OSError, ValueError, KeyError):
                held, started = {}, None
            same_host = held.get("host") == me["host"]
            dead = same_host and isinstance(held.get("pid"), int) and not _pid_alive(held["pid"])
            old = started is None or now - started > STALE_LOCK
            if not (dead or old):
                raise CycleStop("locked", EXIT_LOCKED, f"another learning cycle holds {path} (pid {held.get('pid')} "
                                f"on {held.get('host')}, since {held.get('started_at')})",
                                ["wait for it to finish; if it is not running, delete the lock file"])
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w") as f:
            json.dump(me, f)
        return path
    raise CycleStop("locked", EXIT_LOCKED, f"could not acquire {path}")


def release_lock(path: Path | None) -> None:
    if path is None:
        return
    try:
        held = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if held.get("pid") == os.getpid():
        path.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- preflight / schedule


def preflight(args) -> None:
    """Dependencies that only exist on the machine holding the writable data. Actionable errors."""
    errors, actions = [], []
    if (os.getenv("MOTHERDUCK_DATABASE") or "").strip():
        errors.append("MOTHERDUCK_DATABASE is set: this process is configured as the hosted read-only API")
        actions.append("run the learning cycle where the writable local DuckDB lives, without MOTHERDUCK_DATABASE")
    if not Path(args.live_db).exists():
        errors.append(f"live database {args.live_db} not found")
        actions.append("run scripts/sync_espn.py and scripts/sync_nfl.py on this machine, or set FANTASY_DB_PATH")
    if not Path(args.history_db).exists():
        errors.append(f"history database {args.history_db} not found (training needs 2020 onward)")
        actions.append("run scripts/sync_nfl_history.py once, or set NFL_HISTORY_DB_PATH")
    for name in ("selection.json", "heldout_report.json"):
        if not (Path(args.ifp_dir) / name).exists():
            errors.append(f"production ifp-1.0.0 file {Path(args.ifp_dir) / name} not found")
            actions.append("run backtest_independent_projections.py select/evaluate (see the runbook), or set "
                           "INDEPENDENT_MODEL_DIR to the directory that holds them")
    try:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        probe = Path(args.out) / f".write-probe-{os.getpid()}"
        probe.write_text("")
        probe.unlink()
    except OSError as err:
        errors.append(f"output directory {args.out} is not writable ({err})")
        actions.append("set WEEKLY_MODEL_DIR to a writable, persistent, git-ignored directory")
    if errors:
        raise CycleStop("config_error", EXIT_CONFIG, "; ".join(errors), actions)


def week_kickoffs(con, season: int) -> dict[int, datetime]:
    """First kickoff (UTC) of every REG week, from nflverse gameday/gametime (US Eastern)."""
    from app.analytics.projection_backtest import _kickoff

    out: dict[int, datetime] = {}
    for week, gameday, gametime in con.execute(
            "SELECT week, gameday, gametime FROM nfl_schedules WHERE season = ? AND game_type = 'REG'",
            [season]).fetchall():
        k = _kickoff({"gameday": gameday, "gametime": gametime})
        if k is not None and (week not in out or k < out[week]):
            out[int(week)] = k
    return out


def pick_target(kickoffs: dict[int, datetime], now: datetime) -> tuple[int | None, int | None]:
    """(target week = first week whose first kickoff is after now, last week that has started)."""
    ahead = [w for w, k in kickoffs.items() if k > now]
    started = [w for w, k in kickoffs.items() if k <= now]
    return (min(ahead) if ahead else None), (max(started) if started else None)


def nfl_synced_at(con) -> str | None:
    try:
        row = con.execute("SELECT max(finished_at) FROM sync_runs WHERE source = 'nfl' AND status = 'ok'").fetchone()
    except Exception:  # table absent on an old database: freshness unknown, not an error
        return None
    return None if row is None or row[0] is None else str(row[0])


# ----------------------------------------------------------------------------- stages


def stage_sync_nfl(args, log_path: Path) -> dict:
    """The existing nflverse sync (writes the live DB, as refresh_all.py does). Output -> log file."""
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"==> sync_nfl.py {datetime.now(timezone.utc).isoformat()}\n")
        log.flush()
        # sync_nfl.py reads its database and season from the environment: pass this cycle's choices
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "FANTASY_DB_PATH": str(Path(args.live_db).resolve()),
               "NFL_SEASON": str(args.season)}
        code = subprocess.run([sys.executable, str(SCRIPTS / "sync_nfl.py")], cwd=_bootstrap.BACKEND,
                              stdout=log, stderr=subprocess.STDOUT, env=env).returncode
    if code != 0:
        raise RuntimeError(f"sync_nfl.py exited {code} (see {log_path}); a DuckDB lock error means another "
                           "process (refresh_all.py, the API) is writing the live database: retry later")
    return {"seconds": round(time.monotonic() - started, 1), "log": str(log_path)}


def stage_sync_exits(args) -> dict:
    return cli.cmd_sync_exits(args)


def training_fingerprint(args, store, scoring) -> str:
    """Every training row, label and feature (scalar columns) plus everything else a fit depends on."""
    cols = [c for c, t in store.schema.items() if c not in ("key", "gsis_id", "label") and not t.is_nested()]
    return _sha({"data": wl.store_hash(store, cols) if store.height else None,
                 "challenger_config": ch.ChallengerConfig.load().hash(),
                 "ifp_selection": hashlib.sha256((Path(args.ifp_dir) / "selection.json").read_bytes()).hexdigest(),
                 "ifp_report": hashlib.sha256((Path(args.ifp_dir) / "heldout_report.json").read_bytes()).hexdigest(),
                 "scoring": scoring.fingerprint, "versions": [wl.MODEL_VERSION, ch.CHALLENGER_VERSION],
                 "target": [args.season, args.week]})


def stage_train(args, src, scoring, live=None) -> dict:
    """Train both models unless an earlier cycle trained on exactly these inputs (same training
    rows and features, configuration, production selection and scoring). Then reuse its artifacts.
    `--force-retrain` always refits; content addressing keeps every earlier artifact."""
    store = wl.training_store(src, args.season, args.week)
    fp = training_fingerprint(args, store, scoring)
    state_path = Path(args.out) / "state" / f"train-s{args.season}w{args.week:02d}.json"
    state = None
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except ValueError:
            state = None
    if (state and state.get("fingerprint") == fp and not args.force_retrain
            and all(Path(a["path"]).exists() for a in state["artifacts"].values())):
        return {"reused": True, "fingerprint": fp, **state}
    res = cli.cmd_train(args, src=src, scoring=scoring, store=store, live=live)
    arts = {v: {k: res[v][k] for k in ("id", "path", "new", "latest_included_week", "data_hash", "config_hash")}
            for v in (wl.MODEL_VERSION, ch.CHALLENGER_VERSION)}
    state = {"fingerprint": fp, "artifacts": arts, "trained_at": datetime.now(timezone.utc).isoformat(),
             "runtime_seconds": res.get("runtime_seconds")}
    _replace_json(state_path, state)
    return {"reused": False, **state}


def missed_window(args, week: int | None, kickoffs: dict[int, datetime], now: datetime) -> dict | None:
    """Record (write-once) a started week that has no frozen pregame forecast. Never backfilled."""
    if week is None:
        return None
    d = Path(args.out) / "ledger" / f"s{args.season}w{week:02d}"
    runs = [wl._read_json(f) for f in sorted(d.glob("*.json"))] if d.exists() else []   # NotReady if unreadable
    pregame_rows = sum(r["pregame"] for run in runs if run.get("kind") == "pregame" for r in run["rows"])
    if pregame_rows:
        return None
    rec = {"season": args.season, "week": week, "first_kickoff": kickoffs[week].isoformat(),
           "recorded_at": now.isoformat(),
           "reason": "no pregame forecast was frozen before the week's first kickoff; nothing is backdated and no "
                     "retrospective replay is substituted (a labeled replay never counts as pregame evidence)"}
    wl.write_once(Path(args.out) / "missed" / f"s{args.season}w{week:02d}.json", json.dumps(rec, indent=1))
    return rec


def espn_benchmark(live, season: int, week: int) -> dict:
    """Read-only: is there an ESPN projection snapshot for the target week captured before its first
    kickoff? (Captured by refresh_all.py/sync_espn.py; this cycle never writes ESPN data.)"""
    from app.analytics.promotion_tracker import pregame_snapshot
    try:
        snap = pregame_snapshot(live, season, week)
    except Exception as err:  # optional benchmark: report, never fail the cycle
        return {"week": week, "available": False, "error": err.__class__.__name__}
    return {**snap, "available": True,
            "note": None if snap["captured"] else "no pregame ESPN snapshot yet: refresh_all.py (launchd Thu/Sun/Mon) "
                                                  "must run before kickoff"}


# ----------------------------------------------------------------------------- cycle


def run_cycle(args, now: datetime | None = None) -> tuple[int, dict]:
    if now is None:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    stamp = f"{now:%Y%m%dT%H%M%SZ}"
    report = {"schema": SCHEMA, "started_at": now.isoformat(), "now_eastern": now.astimezone(EASTERN).isoformat(),
              "season": args.season, "target_week": None, "status": None, "stages": [], "errors": [],
              "required_actions": [], "missed_window": None, "prediction_models_changed": False,
              "warnings": [], "injury_freshness": None}
    lock, code, conns = None, EXIT_OK, []

    def stage(name, fn, *a, **kw):
        t = time.monotonic()
        try:
            out = fn(*a, **kw)
        except CycleStop:
            raise
        except wl.NotReady as err:
            report["stages"].append({"name": name, "status": "not_ready", "seconds": round(time.monotonic() - t, 2)})
            raise CycleStop("waiting_for_data", EXIT_NOT_READY, str(err),
                            ["rerun before the target week's first kickoff once the data is published"]) from err
        except Exception as err:
            report["stages"].append({"name": name, "status": "failed", "seconds": round(time.monotonic() - t, 2),
                                     "error": f"{err.__class__.__name__}: {err}"})
            raise CycleStop("failed", EXIT_FAILED, f"stage {name} failed: {err}",
                            [f"fix the {name} error and rerun; later stages were not run"]) from err
        report["stages"].append({"name": name, "status": "ok", "seconds": round(time.monotonic() - t, 2)})
        return out

    try:
        preflight(args)
        lock = acquire_lock(args.out, now)
        if args.skip_sync:
            report["stages"].append({"name": "sync_nfl", "status": "skipped"})
            report["stages"].append({"name": "sync_exits", "status": "skipped"})
        else:
            stage("sync_nfl", stage_sync_nfl, args, Path(args.out) / "logs" / f"cycle-{stamp}.log")
            report["sync_exits"] = stage("sync_exits", stage_sync_exits, args)
        live, hist, scoring, src = stage("load_sources", cli._context, args)
        conns += [live, hist]
        kick = week_kickoffs(live, args.season)
        target, last_started = pick_target(kick, now)
        report["data"] = {"nfl_synced_at": nfl_synced_at(live),
                          "latest_final_week": max((int(w) for w in src.games.filter(
                              (src.games["season"] == args.season) & src.games["points_for"].is_not_null())["week"]),
                              default=None)}
        report["missed_window"] = missed_window(args, last_started, kick, now)
        if target is None:
            raise CycleStop("no_upcoming_week", EXIT_OK, f"no {args.season} regular-season week kicks off after "
                            f"{now.isoformat()}")
        args.week = target
        report["target_week"], report["target_first_kickoff"] = target, kick[target].isoformat()
        report["target_first_kickoff_eastern"] = kick[target].astimezone(EASTERN).isoformat()
        rd = stage("readiness", wl.readiness, src, args.season, target, now, cli._fetched_at(live, args.season),
                   con=live)
        report["data"].update(readiness_ok=rd["ok"], warnings=rd["warnings"], pbp_exits_through_week=rd[
            "pbp_exits_through_week"], completed_weeks=len(rd["completed_weeks"]))
        # Informational only (injury retrieval freshness, ESPN fallback adding no designations): never a gate.
        report["warnings"] = list(rd["warnings"])
        report["injury_freshness"] = rd.get("injury_freshness")
        if target > 1:
            report["data"]["previous_week_finality"] = wl.week_finality(src, args.season, target - 1,
                                                                       cli._fetched_at(live, args.season))
        report["espn_pregame_benchmark"] = espn_benchmark(live, args.season, target)
        if not rd["ok"]:
            report["stages"][-1]["status"] = "not_ready"
            raise CycleStop("waiting_for_data", EXIT_NOT_READY, "; ".join(rd["errors"]),
                            ["rerun after nflverse publishes the missing data, before "
                             f"{kick[target].astimezone(EASTERN):%a %Y-%m-%d %H:%M %Z}"])
        ev = stage("evaluate", cli.cmd_evaluate, args, src=src, live=live)
        report["evaluation"] = {"report": ev["report"], "prospective_weeks": len(ev["pregame_weeks"] or []),
                                "preliminary_weeks": ev.get("preliminary_weeks_not_evidence"),
                                "status_by_position": {p: s["status"] for p, s in (ev["shadow_status"] or {}).items()}}
        tr = stage("train", stage_train, args, src, scoring, live)
        report["training"] = {"reused": tr["reused"], "fingerprint": tr["fingerprint"],
                              "latest_included_week": tr["artifacts"][wl.MODEL_VERSION]["latest_included_week"],
                              "artifacts": {v: {"id": a["id"], "created": bool(a["new"]) and not tr["reused"],
                                                "path": a["path"]} for v, a in tr["artifacts"].items()}}
        pr = stage("predict", cli.cmd_predict, args, live=live, hist=hist, src=src, scoring=scoring,
                   artifacts={v: a["path"] for v, a in tr["artifacts"].items()})
        report["forecast"] = {k: pr[k] for k in ("run_id", "path", "new", "rows", "pregame_rows", "with_challenger",
                                                 "source_manifest")}
        report["status"] = "forecast_frozen" if pr["new"] else "up_to_date"
    except CycleStop as stop:
        report["status"], code = stop.status, stop.code
        if stop.status not in ("no_upcoming_week",):
            report["errors"].append(str(stop))
        report["required_actions"] += stop.actions
    except Exception as err:          # outside a stage: still report, release the lock, exit 1
        report["status"], code = "failed", EXIT_FAILED
        report["errors"].append(f"{err.__class__.__name__}: {err}")
        report["required_actions"].append("fix the error above and rerun; later stages were not run")
    finally:
        for con in conns:
            if con is not None:
                con.close()
        release_lock(lock)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["exit_code"] = code
    try:
        wl.write_once(Path(args.out) / "reports" / f"cycle-{stamp}-{os.getpid()}.json",
                      json.dumps(report, indent=1, default=str))
        if report["status"] != "locked":       # the running cycle owns the status file
            _replace_json(Path(args.out) / "status" / "latest-cycle.json", report)
    except OSError as err:
        print(f"could not write the cycle report under {args.out}: {err}", file=sys.stderr)
    return code, report


def cmd_status(args) -> dict:
    out = Path(args.out)
    latest = out / "status" / "latest-cycle.json"
    lock = out / "locks" / LOCK_NAME
    return {"latest_cycle": json.loads(latest.read_text()) if latest.exists() else None,
            "lock": json.loads(lock.read_text()) if lock.exists() else None,
            "missed_windows": sorted(p.name for p in (out / "missed").glob("*.json")),
            "artifacts": {v: len(list((out / "artifacts" / v).glob("*.json")))
                          for v in (wl.MODEL_VERSION, ch.CHALLENGER_VERSION)},
            "ledger_runs": len(list((out / "ledger").glob("s*/*.json")))}


def parse(argv=None):
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "status"])
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--skip-sync", action="store_true", help="do not run sync_nfl.py / sync-exits first")
    ap.add_argument("--force-retrain", action="store_true",
                    help="refit even if the training inputs are unchanged (e.g. to confirm a stat correction)")
    ap.add_argument("--now", help="ISO timestamp to use as the current time (tests and audits only)")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cli._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--ifp-dir", type=Path, default=cli._path("INDEPENDENT_MODEL_DIR", "data/models"))
    ap.add_argument("--out", type=Path, default=cli._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    args = ap.parse_args(argv)
    # fields the weekly_learning.py commands read
    args.week, args.retrospective, args.history_seasons = None, False, None
    return args


def main(argv=None) -> int:
    args = parse(argv)
    if args.command == "status":
        print(json.dumps(cmd_status(args), indent=1, default=str))
        return 0
    code, report = run_cycle(args)
    keys = ("status", "season", "target_week", "target_first_kickoff_eastern", "errors", "required_actions",
            "training", "forecast", "evaluation", "missed_window", "warnings", "exit_code")
    print(json.dumps({k: report.get(k) for k in keys}, indent=1, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
