"""Unattended weekly learning cycle for the independent projection engine (research only).

Wraps the existing `weekly_learning.py` commands in one restartable, idempotent run meant for a
scheduler (scripts/launchd/com.fantasy-intel.weekly-learning.plist). It must run on the machine that
holds the writable DuckDB files and the git-ignored model directory (the laptop in this project's
setup); MotherDuck and Vercel only carry a read-only published copy. Nothing here touches the
production model files, ESPN or MotherDuck. The one live-database write is the last stage, `project`:
the production model's run for the target week (backtest_independent_projections.py project), which
the next refresh_all.py publishes (skip it with --no-project).

    cd backend
    uv run python ../scripts/learning_cycle.py run                 # one cycle (what the scheduler runs)
    uv run python ../scripts/learning_cycle.py run --skip-sync     # data already synced by refresh_all.py
    uv run python ../scripts/learning_cycle.py reproject           # Fri / Sun morning: final injury reports
    uv run python ../scripts/learning_cycle.py status              # latest cycle report + lock state

A cycle: lock -> preflight -> sync_nfl.py -> sync-exits -> pick the target week (the next week whose
first kickoff, US Eastern from nflverse, is still ahead) -> readiness (every earlier game final,
box scores / snaps / xFP synced) -> evaluate frozen forecasts -> train (reused when the training
data, configuration and scoring fingerprint are unchanged; the ledger's ifp-1.0.0 / ifp-2.0.0 and the production
ifp-2.1.0) -> predict (pregame only) -> project (the production ifp-2.1.0 run in the live DB; an identical run
already stored is a no-op) -> report.
The project stage also projects EVERY LATER regular-season week W+1..last from the same week-W artifact (rest-of-season
projections, docs/independent_projection_engine.md; no training per week). A failure on one later week is listed under
`projection.rest_of_season` and in `warnings`, and does not stop the other weeks or change the cycle status; skip all
of it with --no-rest-of-season.
After the production run, two separate models run as non-fatal stages (a failure is a warning, never a failed cycle):
`kicker` (kfp-1.0.0) and `ctx_shadow`, the research shadow of the frozen ifp-ctx-p1 candidate (scripts/ctx_shadow.py
cycle: scores its frozen pregame ledgers against actuals alongside ifp-2 and tries the gated write-once freeze; it
writes only <ifp-dir>/research/context/). Skip them with --no-kicker / --no-ctx-shadow. Two more research shadows run
the same way right after it (RESEARCH_SHADOWS; skip with --no-qb-scenario-shadow / --no-ipm-cal-shadow /
--no-signal-shadows): `qb_scenario_shadow` (ifp-ctx-p2q, scripts/qb_scenario_shadow.py cycle), `ipm_cal_shadow` (the QB
isotonic participation map, scripts/participation_cal_shadow.py cycle) and `signal_shadows` (ifp-fr-p1 / ifp-dct-p1 /
ifp-awd-p1, scripts/signal_shadows.py cycle, writes only <ifp-dir>/research/signal_shadows/). A further research shadow, `dst_shadow`
(scripts/dst_shadow.py cycle: the D/ST model dfp-r1 vs ESPN; writes only data/models/dst/shadow/), follows them;
skip it with --no-dst-shadow.
Then `dist_ledger`, the frozen pregame point-distribution ledger (scripts/distribution_ledger.py cycle: scores
frozen weeks' interval coverage and freezes the target week's distributions from the forecast `predict` just
froze; research only, writes only <ifp-dir>/research/point_distributions/ledger/; skip with --no-dist-ledger).
Last, `promotion_check` (scripts/shadow_promotion_check.py check, docs/shadow_promotion.md): reads every research
shadow's saved gate result and reports, per candidate and position, whether it is ready to promote into Ours; a ready
candidate gets a "PROMOTION READY" line in this cycle's log and stderr and an entry in data/logs/promotion-queue.json.
It edits no production code or registry; skip with --no-promotion-check.
Neither the kicker nor the research shadows (dist_ledger included) need the target week's new data, so they also
run when the cycle stops early: after `waiting_for_data` (the kicker only when it may write, i.e. without
--no-project), and the read-only shadows also after a failed production stage (all of them also after
`no_upcoming_week`).
Their status and exit code stay the cycle's.
A stage failure stops every later production stage. Nothing is backdated, overwritten or replayed: a week whose
first kickoff passed without a frozen pregame forecast is recorded under <out>/missed/.

`reproject` (run_reproject) is the later, separate step: after sync_nfl.py it stores a NEW run of the week in progress
with the artifact of that week's newest verified production run, so Friday's final game designations reach
expected_points (docs/independent_projection_engine.md, "Final-designation re-projection"). Teams already kicked off keep
their base-run rows; no run, ledger entry or artifact is modified. Not scheduled yet (a launchd change needs the owner).

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
from app.analytics import injury_model as im  # noqa: E402
from app.analytics import weekly_learning as wl  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.connection import LOCK_WAIT_ENV  # noqa: E402

SCHEMA = "weekly-learning-cycle/v1"
REPROJECT_SCHEMA = "final-reprojection-step/v1"
EASTERN = ZoneInfo("America/New_York")
EXIT_OK, EXIT_FAILED, EXIT_NOT_READY, EXIT_LOCKED, EXIT_CONFIG = 0, 1, 3, 4, 5
STALE_LOCK = timedelta(hours=6)
LOCK_NAME = "cycle.lock"
LOCK_WAIT_S = 300                    # FANTASY_DB_LOCK_WAIT_S for the stage processes (seconds)


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


def child_env() -> dict:
    """Environment of every stage process: unbuffered output, and FANTASY_DB_LOCK_WAIT_S (LOCK_WAIT_S unless set) so
    a stage waits out another process's DuckDB lock (refresh_all.py, the social-trends job's read-only reads)."""
    return {**os.environ, "PYTHONUNBUFFERED": "1", LOCK_WAIT_ENV: os.getenv(LOCK_WAIT_ENV) or str(LOCK_WAIT_S)}


def stage_sync_nfl(args, log_path: Path) -> dict:
    """The existing nflverse sync (writes the live DB, as refresh_all.py does). Output -> log file."""
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"==> sync_nfl.py {datetime.now(timezone.utc).isoformat()}\n")
        log.flush()
        # sync_nfl.py reads its database and season from the environment: pass this cycle's choices
        env = {**child_env(), "FANTASY_DB_PATH": str(Path(args.live_db).resolve()),
               "NFL_SEASON": str(args.season)}
        code = subprocess.run([sys.executable, str(SCRIPTS / "sync_nfl.py")], cwd=_bootstrap.BACKEND,
                              stdout=log, stderr=subprocess.STDOUT, env=env).returncode
    if code != 0:
        raise RuntimeError(f"sync_nfl.py exited {code} (see {log_path}); a DuckDB lock error means another "
                           "process (refresh_all.py, the API) is writing the live database: retry later")
    return {"seconds": round(time.monotonic() - started, 1), "log": str(log_path)}


def stage_sync_exits(args) -> dict:
    return cli.cmd_sync_exits(args)


def stage_project(args, conns: list, log_path: Path) -> dict:
    """Store the production model's run for the target week in the live DB, the run the projection switch
    reads (backtest_independent_projections.py project, which picks the newest artifact this cycle trained or
    reused), then every later regular-season week through args.last_week from that same artifact (`--through-week`,
    one process). The cycle's read-only connections are closed first: DuckDB allows one writing process."""
    for con in conns:
        if con is not None:
            con.close()
    conns.clear()
    cmd = [sys.executable, str(SCRIPTS / "backtest_independent_projections.py"), "project",
           "--season", str(args.season), "--week", str(args.week), "--live-db", str(args.live_db),
           "--history-db", str(args.history_db), "--out", str(args.ifp_dir), "--weekly-dir", str(args.out),
           "--register-artifact", "learning_cycle"]
    last = getattr(args, "last_week", None)
    if last is not None and last > args.week and not getattr(args, "no_rest_of_season", False):
        cmd += ["--through-week", str(last)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, cwd=_bootstrap.BACKEND, capture_output=True, text=True, env=child_env())
    out, err = getattr(proc, "stdout", "") or "", getattr(proc, "stderr", "") or ""
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"==> backtest_independent_projections.py project {datetime.now(timezone.utc).isoformat()}\n{out}{err}")
    if proc.returncode != 0:
        raise RuntimeError(f"project exited {proc.returncode}: {err.strip()[-500:]} (see {log_path}); a DuckDB lock "
                           "error means another process is writing the live database: retry later")
    try:
        return json.loads(out)
    except ValueError:
        return {}


def stage_kicker(args, log_path: Path) -> dict:
    """Kicker model (kfp-1.0.0, scripts/kicker_projections.py): fit and approve the season's artifact once (it trains on
    finished seasons only, so one artifact serves every week of the season), then store the target week's kicker run
    and every later regular-season week's (experimental) run. Called after `project`, or after the cycle stopped at
    waiting_for_data (the kicker model does not need the ifp readiness gate; an identical rerun is a no-op and a later
    cycle's run on newer data supersedes it), with the cycle's read connections closed. Never fails the cycle:
    run_cycle turns an error into a warning."""
    base = [sys.executable, str(SCRIPTS / "kicker_projections.py")]
    common = ["--season", str(args.season), "--live-db", str(args.live_db), "--history-db", str(args.history_db),
              "--out", str(args.kicker_dir)]
    out: dict = {}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = child_env()

    def run(cmd: list[str]) -> dict:
        proc = subprocess.run(base + cmd + common, cwd=_bootstrap.BACKEND, capture_output=True, text=True, env=env)
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"==> kicker_projections.py {cmd[0]} {datetime.now(timezone.utc).isoformat()}\n"
                      f"{proc.stdout}{proc.stderr}")
        if proc.returncode != 0:
            raise RuntimeError(f"kicker_projections.py {cmd[0]} exited {proc.returncode}: {proc.stderr.strip()[-400:]}")
        try:
            return json.loads(proc.stdout)
        except ValueError:
            return {}

    if not (Path(args.kicker_dir) / f"kfp-1.0.0_{args.season}.json").exists():
        fit = run(["fit", "--register"])
        out["fit"] = {k: fit.get(k) for k in ("artifact", "sha256", "registered")}
    else:
        # An artifact fitted without --register, a failed registration or a database restored apart from the model
        # directory would otherwise never be approved; registering an approved artifact again is a no-op.
        reg = run(["register"])
        out["register"] = {k: reg.get(k) for k in ("artifact", "sha256", "registered")}
    cmd = ["project", "--week", str(args.week)]
    last = getattr(args, "last_week", None)
    if last is not None and last > args.week and not getattr(args, "no_rest_of_season", False):
        cmd += ["--through-week", str(last)]
    pj = run(cmd)
    out["runs"] = [{k: r.get(k) for k in ("week", "run_id", "created", "row_count", "coverage")} for r in pj.get("runs", [])]
    return out


CTX_SHADOW_TIMEOUT_S = 45 * 60       # the whole research stage (store build + scoring + a possible freeze)
# The other research shadows: (stage / report key, script, --no-* attribute, warning label). Same contract as ctx_shadow.
RESEARCH_SHADOWS = (("qb_scenario_shadow", "qb_scenario_shadow.py", "no_qb_scenario_shadow", "ifp-ctx-p2q shadow"),
                    ("ipm_cal_shadow", "participation_cal_shadow.py", "no_ipm_cal_shadow", "ipm-cal-0.1.0 shadow"),
                    ("signal_shadows", "signal_shadows.py", "no_signal_shadows", "signal shadows"))


def stage_ctx_shadow(args, conns: list, log_path: Path) -> dict:
    """Research shadow of the frozen ifp-ctx-p1 candidate (scripts/ctx_shadow.py `cycle`, own process): score every
    fully final frozen week against actuals alongside ifp-2, record a week missed without a ledger, and try the target
    week's write-once pregame freeze (usually deferred on Tue-Thu: the final injury reports land Friday, and
    refresh_all.py's evidence stage freezes it before the main slate). Reads the databases read-only and writes only
    <ifp-dir>/research/context/. Never fails the cycle: run_cycle turns an error into a warning. Exit 3 means it ran and
    reported warnings (missing or stale injury evidence), which are returned, not raised."""
    for con in conns:            # DuckDB: the research process opens its own read-only connections
        if con is not None:
            con.close()
    conns.clear()
    cmd = [sys.executable, str(SCRIPTS / "ctx_shadow.py"), "cycle", "--season", str(args.season),
           "--live-db", str(args.live_db), "--history-db", str(args.history_db),
           "--weekly-dir", str(args.out), "--exits-dir", str(args.out)]
    if args.week is not None:          # None after the season's last week: score only (and check that week)
        cmd += ["--week", str(args.week)]
    if getattr(args, "ctx_dir", None) is not None:
        cmd += ["--ifp-dir", str(args.ctx_dir)]
    if getattr(args, "now", None):     # a simulated cycle clock: ctx_shadow.py then decides only (dry run, no writes)
        cmd += ["--now", str(args.now)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, cwd=_bootstrap.BACKEND, capture_output=True, text=True,
                          env=child_env(), timeout=CTX_SHADOW_TIMEOUT_S)
    out, err = proc.stdout or "", proc.stderr or ""
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"==> ctx_shadow.py cycle {datetime.now(timezone.utc).isoformat()}\n{out}{err}")
    if proc.returncode not in (0, 3):
        raise RuntimeError(f"ctx_shadow.py exited {proc.returncode}: {err.strip()[-400:]} (see {log_path})")
    try:
        return json.loads(out)
    except ValueError:
        return {}


DIST_LEDGER_TIMEOUT_S = 15 * 60


def stage_dist_ledger(args, conns: list, log_path: Path) -> dict:
    """Research: the frozen pregame point-distribution ledger (scripts/distribution_ledger.py `cycle`, own process).
    Scores the interval coverage of every fully final frozen week and freezes the target week's distributions around
    the pregame forecast this cycle's `predict` stage froze. Never fails the cycle (run_cycle turns an error into a
    warning); exit 3 means it ran and reported warnings, which are returned."""
    for con in conns:            # DuckDB: the research process opens its own read-only connection
        if con is not None:
            con.close()
    conns.clear()
    cmd = [sys.executable, str(SCRIPTS / "distribution_ledger.py"), "cycle", "--season", str(args.season),
           "--live-db", str(args.live_db), "--weekly-dir", str(args.out)]
    if args.week is not None:
        cmd += ["--week", str(args.week)]
    if getattr(args, "dist_dir", None) is not None:
        cmd += ["--ifp-dir", str(args.dist_dir)]
    if getattr(args, "now", None):     # a simulated cycle clock: decide only (dry run, no writes)
        cmd += ["--now", str(args.now)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, cwd=_bootstrap.BACKEND, capture_output=True, text=True,
                          env=child_env(), timeout=DIST_LEDGER_TIMEOUT_S)
    out, err = proc.stdout or "", proc.stderr or ""
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"==> distribution_ledger.py cycle {datetime.now(timezone.utc).isoformat()}\n{out}{err}")
    if proc.returncode not in (0, 3):
        raise RuntimeError(f"distribution_ledger.py exited {proc.returncode}: {err.strip()[-400:]} (see {log_path})")
    try:
        return json.loads(out)
    except ValueError:
        return {}


def stage_research_shadow(args, conns: list, log_path: Path, script: str) -> dict:
    """One of RESEARCH_SHADOWS (`scripts/<script> cycle`, own process), with ctx_shadow's contract: the same arguments,
    read-only databases, writes only under the model directory's research/, exit 3 = ran with warnings (returned)."""
    for con in conns:
        if con is not None:
            con.close()
    conns.clear()
    cmd = [sys.executable, str(SCRIPTS / script), "cycle", "--season", str(args.season),
           "--live-db", str(args.live_db), "--history-db", str(args.history_db),
           "--weekly-dir", str(args.out), "--exits-dir", str(args.out)]
    if args.week is not None:
        cmd += ["--week", str(args.week)]
    if getattr(args, "ctx_dir", None) is not None:
        cmd += ["--ifp-dir", str(args.ctx_dir)]
    if getattr(args, "now", None):     # a simulated cycle clock: the script then decides only (dry run, no writes)
        cmd += ["--now", str(args.now)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, cwd=_bootstrap.BACKEND, capture_output=True, text=True,
                          env=child_env(), timeout=CTX_SHADOW_TIMEOUT_S)
    out, err = proc.stdout or "", proc.stderr or ""
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"==> {script} cycle {datetime.now(timezone.utc).isoformat()}\n{out}{err}")
    if proc.returncode not in (0, 3):
        raise RuntimeError(f"{script} exited {proc.returncode}: {err.strip()[-400:]} (see {log_path})")
    try:
        return json.loads(out)
    except ValueError:
        return {}


DST_SHADOW_TIMEOUT_S = 20 * 60       # a possible ESPN fetch, the fit and scoring (about a minute in practice)


def stage_dst_shadow(args, conns: list, log_path: Path) -> dict:
    """Research shadow of the D/ST model dfp-r1 (scripts/dst_shadow.py `cycle`, own process): score the frozen weeks
    against ESPN's pregame snapshots, record a missed week, and freeze the target week's write-once ledger once Monday
    night's statistics and the lines are in. Reads the databases read-only; writes only data/models/dst/shadow/ (and
    the ESPN D/ST cache). Never fails the cycle: run_cycle turns an error into a warning; exit 3 = ran with warnings."""
    for con in conns:            # DuckDB: the research process opens its own read-only connections
        if con is not None:
            con.close()
    conns.clear()
    cmd = [sys.executable, str(SCRIPTS / "dst_shadow.py"), "cycle", "--season", str(args.season),
           "--live-db", str(args.live_db), "--history-db", str(args.history_db)]
    if args.week is not None:
        cmd += ["--week", str(args.week)]
    if getattr(args, "dst_dir", None) is not None:
        cmd += ["--out", str(args.dst_dir)]
    if getattr(args, "now", None):     # a simulated cycle clock: dst_shadow.py then decides only (dry run, no writes)
        cmd += ["--now", str(args.now)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, cwd=_bootstrap.BACKEND, capture_output=True, text=True,
                          env=child_env(), timeout=DST_SHADOW_TIMEOUT_S)
    out, err = proc.stdout or "", proc.stderr or ""
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"==> dst_shadow.py cycle {datetime.now(timezone.utc).isoformat()}\n{out}{err}")
    if proc.returncode not in (0, 3):
        raise RuntimeError(f"dst_shadow.py exited {proc.returncode}: {err.strip()[-400:]} (see {log_path})")
    try:
        return json.loads(out)
    except ValueError:
        return {}


PROMOTION_CHECK_TIMEOUT_S = 5 * 60   # reads a handful of JSON files


def stage_promotion_check(args, log_path: Path) -> dict:
    """Promote-when-proven watcher (scripts/shadow_promotion_check.py `check`, own process): reads every research
    shadow's saved gate result (nothing is re-scored) and flags the candidates whose pre-registered gate passes. Writes
    only the summary, its log and promotion-queue.json under the logs directory (data/logs). Never fails the cycle:
    run_cycle turns an error into a warning; exit 3 = ran, a result could not be read (returned as warnings)."""
    cmd = [sys.executable, str(SCRIPTS / "shadow_promotion_check.py"), "check"]
    if getattr(args, "ctx_dir", None) is not None:
        cmd += ["--ifp-dir", str(args.ctx_dir)]
    if getattr(args, "dst_dir", None) is not None:
        cmd += ["--dst-dir", str(args.dst_dir)]
    if getattr(args, "dist_dir", None) is not None:
        cmd += ["--dist-dir", str(args.dist_dir)]
    if getattr(args, "promotion_logs_dir", None) is not None:
        cmd += ["--logs-dir", str(args.promotion_logs_dir)]
    if getattr(args, "now", None):     # a simulated cycle clock: report only, write nothing
        cmd += ["--now", str(args.now)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, cwd=_bootstrap.BACKEND, capture_output=True, text=True,
                          env=child_env(), timeout=PROMOTION_CHECK_TIMEOUT_S)
    out, err = proc.stdout or "", proc.stderr or ""
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"==> shadow_promotion_check.py check {datetime.now(timezone.utc).isoformat()}\n{out}{err}")
    if proc.returncode not in (0, 3):
        raise RuntimeError(f"shadow_promotion_check.py exited {proc.returncode}: {err.strip()[-400:]} (see {log_path})")
    try:
        return json.loads(out)
    except ValueError:
        return {}


def training_fingerprint(args, store, scoring, src=None, codes=None) -> str:
    """Every training row, label and feature (scalar columns) plus everything else a fit depends on."""
    cols = [c for c, t in store.schema.items() if c not in ("key", "gsis_id", "label") and not t.is_nested()]
    return _sha({"data": wl.store_hash(store, cols) if store.height else None,
                 "challenger_config": ch.ChallengerConfig.load().hash(),
                 "ifp_selection": hashlib.sha256((Path(args.ifp_dir) / "selection.json").read_bytes()).hexdigest(),
                 "ifp_report": hashlib.sha256((Path(args.ifp_dir) / "heldout_report.json").read_bytes()).hexdigest(),
                 "production_config": im.config_hash(ch.ChallengerConfig.load()),
                 # ifp-2.1.0 also reads injury reports, rosters, roster codes and the schedule (handoff, return table)
                 "injury_inputs": None if src is None else im.injury_inputs_hash(src, args.season, args.week, codes),
                 "scoring": scoring.fingerprint, "versions": list(TRAINED_VERSIONS),
                 "target": [args.season, args.week]})


TRAINED_VERSIONS = (wl.MODEL_VERSION, ch.CHALLENGER_VERSION, im.MODEL_VERSION)   # ledger champion, challenger, production


def stage_train(args, src, scoring, live=None, hist=None) -> dict:
    """Train both models unless an earlier cycle trained on exactly these inputs (same training
    rows and features, configuration, production selection and scoring). Then reuse its artifacts.
    `--force-retrain` always refits; content addressing keeps every earlier artifact."""
    store = wl.training_store(src, args.season, args.week)
    fp = training_fingerprint(args, store, scoring, src, cli.roster_codes(live, hist))
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
    res = cli.cmd_train(args, src=src, scoring=scoring, store=store, live=live, hist=hist)
    arts = {v: {k: res[v][k] for k in ("id", "path", "new", "latest_included_week", "data_hash", "config_hash")}
            for v in TRAINED_VERSIONS}
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

    def kicker(after=None):
        """Kicker model (kfp), after `project` or a waiting_for_data stop: a failure is a warning, never a failed cycle.
        A live-database write, so never with --no-project; needs the target week. `after`: the early stop it follows."""
        if args.no_kicker or args.no_project or args.week is None:
            return                      # (no stage entry when disabled: a report's stage list is unchanged)
        for con in conns:               # DuckDB allows one writing process: close the cycle's read connections
            if con is not None:
                con.close()
        conns.clear()
        t_k = time.monotonic()
        tag = {"after": after} if after else {}
        try:     # the kicker model is separate: its failure is a warning, never a failed cycle
            report["kicker_projection"] = stage_kicker(args, Path(args.out) / "logs" / f"cycle-{stamp}.log")
            report["stages"].append({"name": "kicker", "status": "ok", "seconds": round(time.monotonic() - t_k, 2),
                                     **tag})
        except Exception as err:      # noqa: BLE001
            report["stages"].append({"name": "kicker", "status": "failed_nonfatal",
                                     "seconds": round(time.monotonic() - t_k, 2),
                                     "error": f"{err.__class__.__name__}: {err}", **tag})
            report["warnings"].append(f"kicker projection failed (skill-position run unaffected): {err}")

    def ctx_shadow(after=None):
        """Research shadow (ifp-ctx-p1), after the production stages: a failure is a warning, never a failed cycle.
        `after`: the early stop (waiting_for_data, failed) it follows."""
        if args.no_ctx_shadow:
            return
        t_c = time.monotonic()
        tag = {"after": after} if after else {}
        try:
            cs = stage_ctx_shadow(args, conns, Path(args.out) / "logs" / f"cycle-{stamp}.log")
            report["ctx_shadow"] = cs
            report["stages"].append({"name": "ctx_shadow", "status": "ok", "seconds": round(time.monotonic() - t_c, 2),
                                     **tag})
            report["warnings"] += [f"ifp-ctx-p1 shadow: {w}" for w in cs.get("warnings") or []]
        except Exception as err:      # noqa: BLE001
            report["stages"].append({"name": "ctx_shadow", "status": "failed_nonfatal",
                                     "seconds": round(time.monotonic() - t_c, 2),
                                     "error": f"{err.__class__.__name__}: {err}", **tag})
            report["warnings"].append(f"ifp-ctx-p1 shadow stage failed (production runs unaffected): {err}")

    def research_shadows():
        """The other research shadows (RESEARCH_SHADOWS), right after ctx_shadow and handled exactly like it."""
        for name, script, skip, label in RESEARCH_SHADOWS:
            if getattr(args, skip, False):
                continue
            t_c = time.monotonic()
            try:
                rs = stage_research_shadow(args, conns, Path(args.out) / "logs" / f"cycle-{stamp}.log", script)
                report[name] = rs
                report["stages"].append({"name": name, "status": "ok", "seconds": round(time.monotonic() - t_c, 2)})
                report["warnings"] += [f"{label}: {w}" for w in rs.get("warnings") or []]
            except Exception as err:      # noqa: BLE001
                report["stages"].append({"name": name, "status": "failed_nonfatal",
                                         "seconds": round(time.monotonic() - t_c, 2),
                                         "error": f"{err.__class__.__name__}: {err}"})
                report["warnings"].append(f"{label} stage failed (production runs unaffected): {err}")

    def dst_shadow():
        """Research shadow (dfp-r1 D/ST), after ctx_shadow: a failure is a warning, never a failed cycle."""
        if args.no_dst_shadow:
            return
        t_d = time.monotonic()
        try:
            ds = stage_dst_shadow(args, conns, Path(args.out) / "logs" / f"cycle-{stamp}.log")
            report["dst_shadow"] = ds
            report["stages"].append({"name": "dst_shadow", "status": "ok", "seconds": round(time.monotonic() - t_d, 2)})
            report["warnings"] += [f"dfp-r1 shadow: {w}" for w in ds.get("warnings") or []]
        except Exception as err:      # noqa: BLE001
            report["stages"].append({"name": "dst_shadow", "status": "failed_nonfatal",
                                     "seconds": round(time.monotonic() - t_d, 2),
                                     "error": f"{err.__class__.__name__}: {err}"})
            report["warnings"].append(f"dfp-r1 shadow stage failed (production runs unaffected): {err}")

    def dist_ledger():
        """Research (point-distribution ledger), after the production stages: a failure is a warning, never a failure."""
        if args.no_dist_ledger:
            return
        t_d = time.monotonic()
        try:
            dl = stage_dist_ledger(args, conns, Path(args.out) / "logs" / f"cycle-{stamp}.log")
            report["dist_ledger"] = dl
            report["stages"].append({"name": "dist_ledger", "status": "ok", "seconds": round(time.monotonic() - t_d, 2)})
            report["warnings"] += [f"distribution ledger: {w}" for w in dl.get("warnings") or []]
        except Exception as err:      # noqa: BLE001
            report["stages"].append({"name": "dist_ledger", "status": "failed_nonfatal",
                                     "seconds": round(time.monotonic() - t_d, 2),
                                     "error": f"{err.__class__.__name__}: {err}"})
            report["warnings"].append(f"distribution ledger stage failed (production runs unaffected): {err}")

    def promotion_check():
        """Promote-when-proven watcher, after every research shadow: a failure is a warning, never a failed cycle."""
        if getattr(args, "no_promotion_check", False):
            return
        t_p = time.monotonic()
        try:
            pc = stage_promotion_check(args, Path(args.out) / "logs" / f"cycle-{stamp}.log")
            report["promotion_check"] = pc
            report["promotion_ready"] = list(pc.get("lines") or [])
            report["stages"].append({"name": "promotion_check", "status": "ok",
                                     "seconds": round(time.monotonic() - t_p, 2)})
            report["warnings"] += [f"promotion check: {w}" for w in pc.get("warnings") or []]
            for line in report["promotion_ready"]:
                print(line, file=sys.stderr)
        except Exception as err:      # noqa: BLE001
            report["stages"].append({"name": "promotion_check", "status": "failed_nonfatal",
                                     "seconds": round(time.monotonic() - t_p, 2),
                                     "error": f"{err.__class__.__name__}: {err}"})
            report["warnings"].append(f"promotion check stage failed (production runs unaffected): {err}")

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
            ctx_shadow()                # the season's last frozen week still needs scoring once it is final
            research_shadows()
            dst_shadow()
            dist_ledger()
            promotion_check()
            raise CycleStop("no_upcoming_week", EXIT_OK, f"no {args.season} regular-season week kicks off after "
                            f"{now.isoformat()}")
        args.week, args.last_week = target, max(kick)
        report["target_week"], report["target_first_kickoff"] = target, kick[target].isoformat()
        report["last_regular_season_week"] = args.last_week
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
        tr = stage("train", stage_train, args, src, scoring, live, hist)
        report["training"] = {"reused": tr["reused"], "fingerprint": tr["fingerprint"],
                              "latest_included_week": tr["artifacts"][wl.MODEL_VERSION]["latest_included_week"],
                              "artifacts": {v: {"id": a["id"], "created": bool(a["new"]) and not tr["reused"],
                                                "path": a["path"]} for v, a in tr["artifacts"].items()}}
        pr = stage("predict", cli.cmd_predict, args, live=live, hist=hist, src=src, scoring=scoring,
                   artifacts={v: a["path"] for v, a in tr["artifacts"].items()})
        report["forecast"] = {k: pr[k] for k in ("run_id", "path", "new", "rows", "pregame_rows", "with_challenger",
                                                 "source_manifest")}
        report["status"] = "forecast_frozen" if pr["new"] else "up_to_date"
        if args.no_project:
            report["stages"].append({"name": "project", "status": "skipped"})
        else:
            pj = stage("project", stage_project, args, conns, Path(args.out) / "logs" / f"cycle-{stamp}.log")
            report["projection"] = {k: pj.get(k) for k in ("run_id", "created", "rows", "model_version", "artifact_id")}
            if "rest_of_season" in pj:
                ros = pj["rest_of_season"]
                report["projection"]["rest_of_season"] = ros
                report["projection"]["rest_of_season_summary"] = {
                    "weeks": [r["week"] for r in ros if "error" not in r],
                    "created": sum(1 for r in ros if r.get("created")),
                    "unchanged": sum(1 for r in ros if "error" not in r and not r.get("created")),
                    "failed": [{"week": r["week"], "error": r["error"]} for r in ros if "error" in r]}
                for r in ros:
                    if "error" in r:
                        report["warnings"].append(f"rest-of-season projection for week {r['week']} failed: {r['error']}")
            kicker()
        ctx_shadow()
        research_shadows()
        dst_shadow()
        dist_ledger()
        promotion_check()
    except CycleStop as stop:
        report["status"], code = stop.status, stop.code
        if stop.status not in ("no_upcoming_week",):
            report["errors"].append(str(stop))
        report["required_actions"] += stop.actions
        # The separate models do not need the target week's new data: they still run after an early stop, in the
        # normal path's order (never after a refused lock or a configuration error; no_upcoming_week ran them above).
        if lock is not None and stop.status in ("waiting_for_data", "failed"):
            if stop.status == "waiting_for_data":
                kicker(after=stop.status)
            ctx_shadow(after=stop.status)
            research_shadows()
            dst_shadow()
            dist_ledger()
            promotion_check()
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


# ----------------------------------------------------------------------------- final-designation re-projection


def stage_reproject(args, log_path: Path) -> dict:
    """backtest_independent_projections.py reproject (own process: it opens the live DB read-only, then for one
    write_run). Exit 2 is a refusal (no base run, nothing still pregame, artifact file missing), returned as
    {"refused": message}, never raised."""
    cmd = [sys.executable, str(SCRIPTS / "backtest_independent_projections.py"), "reproject",
           "--season", str(args.season), "--live-db", str(args.live_db), "--history-db", str(args.history_db),
           "--out", str(args.ifp_dir), "--weekly-dir", str(args.out)]
    if args.week is not None:
        cmd += ["--week", str(args.week)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, cwd=_bootstrap.BACKEND, capture_output=True, text=True,
                          env=child_env())
    out, err = proc.stdout or "", proc.stderr or ""
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"==> backtest_independent_projections.py reproject {datetime.now(timezone.utc).isoformat()}\n{out}{err}")
    if proc.returncode == 2:
        return {"refused": err.strip()[-500:]}
    if proc.returncode != 0:
        raise RuntimeError(f"reproject exited {proc.returncode}: {err.strip()[-500:]} (see {log_path}); a DuckDB lock "
                           "error means another process is writing the live database: retry later")
    try:
        return json.loads(out)
    except ValueError:
        return {}


def run_reproject(args, now: datetime | None = None) -> tuple[int, dict]:
    """Friday / Sunday-morning step (docs/independent_projection_engine.md, "Final-designation re-projection"): lock
    -> preflight -> sync_nfl.py -> a NEW production run of the week in progress with its base run's artifact, so the
    final game designations reach expected_points. Teams already kicked off keep their base-run rows. Never trains,
    never predicts, never touches the ledger, the ESPN data or MotherDuck (the next refresh_all.py publishes).
    Exit codes: 0 written or unchanged; 3 refused (no base run / nothing pregame); 4 locked; 5 config; 1 failure.
    Reports: <out>/reports/reproject-<UTC stamp>-<pid>.json (write-once), <out>/status/latest-reproject.json."""
    if now is None:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    stamp = f"{now:%Y%m%dT%H%M%SZ}"
    report = {"schema": REPROJECT_SCHEMA, "started_at": now.isoformat(), "season": args.season, "week": args.week,
              "status": None, "stages": [], "errors": [], "required_actions": [], "reprojection": None}
    lock, code = None, EXIT_OK
    log_path = Path(args.out) / "logs" / f"reproject-{stamp}.log"
    try:
        if args.now:
            raise CycleStop("config_error", EXIT_CONFIG, "--now is not supported by reproject: kickoffs are judged "
                            "against the real clock, so a simulated time could re-project a game already played",
                            ["run without --now"])
        preflight(args)
        lock = acquire_lock(args.out, now)
        if args.skip_sync:
            report["stages"].append({"name": "sync_nfl", "status": "skipped"})
        else:
            t = time.monotonic()
            try:
                stage_sync_nfl(args, log_path)
            except Exception as err:
                report["stages"].append({"name": "sync_nfl", "status": "failed", "error": str(err)})
                raise CycleStop("failed", EXIT_FAILED, f"stage sync_nfl failed: {err}",
                                ["retry later; the stored runs are unchanged"]) from err
            report["stages"].append({"name": "sync_nfl", "status": "ok", "seconds": round(time.monotonic() - t, 2)})
        t = time.monotonic()
        try:
            rp = stage_reproject(args, log_path)
        except Exception as err:
            report["stages"].append({"name": "reproject", "status": "failed", "error": str(err)})
            raise CycleStop("failed", EXIT_FAILED, f"stage reproject failed: {err}",
                            ["fix the error and rerun; the stored runs are unchanged"]) from err
        report["stages"].append({"name": "reproject", "status": "refused" if "refused" in rp else "ok",
                                 "seconds": round(time.monotonic() - t, 2)})
        report["reprojection"] = rp
        if "refused" in rp:
            raise CycleStop("refused", EXIT_NOT_READY, rp["refused"],
                            ["nothing was written; see the message (a week without a stored run is never backfilled)"])
        report["week"] = rp.get("week")
        report["status"] = rp.get("status")
    except CycleStop as stop:
        report["status"], code = stop.status, stop.code
        report["errors"].append(str(stop))
        report["required_actions"] += stop.actions
    except Exception as err:          # noqa: BLE001
        report["status"], code = "failed", EXIT_FAILED
        report["errors"].append(f"{err.__class__.__name__}: {err}")
    finally:
        release_lock(lock)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["exit_code"] = code
    try:
        wl.write_once(Path(args.out) / "reports" / f"reproject-{stamp}-{os.getpid()}.json",
                      json.dumps(report, indent=1, default=str))
        if report["status"] != "locked":
            _replace_json(Path(args.out) / "status" / "latest-reproject.json", report)
    except OSError as err:
        print(f"could not write the re-projection report under {args.out}: {err}", file=sys.stderr)
    return code, report


def cmd_status(args) -> dict:
    out = Path(args.out)
    latest = out / "status" / "latest-cycle.json"
    lock = out / "locks" / LOCK_NAME
    return {"latest_cycle": json.loads(latest.read_text()) if latest.exists() else None,
            "lock": json.loads(lock.read_text()) if lock.exists() else None,
            "missed_windows": sorted(p.name for p in (out / "missed").glob("*.json")),
            "artifacts": {v: len(list((out / "artifacts" / v).glob("*.json")))
                          for v in TRAINED_VERSIONS},
            "ledger_runs": len(list((out / "ledger").glob("s*/*.json")))}


def parse(argv=None):
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "reproject", "status"])
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--skip-sync", action="store_true", help="do not run sync_nfl.py / sync-exits first")
    ap.add_argument("--force-retrain", action="store_true",
                    help="refit even if the training inputs are unchanged (e.g. to confirm a stat correction)")
    ap.add_argument("--no-project", action="store_true",
                    help="do not store the production model's run in the live database after predict")
    ap.add_argument("--no-rest-of-season", action="store_true",
                    help="project only the target week, not every later regular-season week from the same artifact")
    ap.add_argument("--no-kicker", action="store_true",
                    help="do not fit/project the kicker model (kfp) after the skill-position projection")
    ap.add_argument("--kicker-dir", type=Path, default=cli._path("KICKER_MODEL_DIR", "data/models/kicker"))
    ap.add_argument("--no-ctx-shadow", action="store_true",
                    help="do not run the ifp-ctx-p1 research shadow stage (scripts/ctx_shadow.py cycle)")
    ap.add_argument("--no-qb-scenario-shadow", action="store_true",
                    help="do not run the ifp-ctx-p2q research shadow stage (scripts/qb_scenario_shadow.py cycle)")
    ap.add_argument("--no-ipm-cal-shadow", action="store_true",
                    help="do not run the QB participation-calibration re-test (scripts/participation_cal_shadow.py cycle)")
    ap.add_argument("--no-signal-shadows", action="store_true",
                    help="do not run the ifp-fr-p1 / ifp-dct-p1 / ifp-awd-p1 research shadows (signal_shadows.py cycle)")
    ap.add_argument("--ctx-dir", type=Path, default=None,
                    help="model directory holding research/ for the research shadows (default: each script's, "
                         "data/models)")
    ap.add_argument("--no-dist-ledger", action="store_true",
                    help="do not run the point-distribution ledger stage (scripts/distribution_ledger.py cycle)")
    ap.add_argument("--dist-dir", type=Path, default=None,
                    help="model directory holding research/point_distributions/ (default: distribution_ledger.py's)")
    ap.add_argument("--no-dst-shadow", action="store_true",
                    help="do not run the dfp-r1 D/ST research shadow stage (scripts/dst_shadow.py cycle)")
    ap.add_argument("--dst-dir", type=Path, default=None,
                    help="D/ST research directory holding shadow/ (default: dst_shadow.py's, DST_RESEARCH_DIR)")
    ap.add_argument("--no-promotion-check", action="store_true",
                    help="do not run the promote-when-proven check (scripts/shadow_promotion_check.py check)")
    ap.add_argument("--promotion-logs-dir", type=Path, default=None,
                    help="where the promotion check writes its summary and queue (default: its own, data/logs)")
    ap.add_argument("--week", type=int, default=None, dest="reproject_week",
                    help="reproject: the week to re-project (default: the week of the next kickoff)")
    ap.add_argument("--now", help="ISO timestamp to use as the current time (tests and audits only)")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cli._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--ifp-dir", type=Path, default=cli._path("INDEPENDENT_MODEL_DIR", "data/models"))
    ap.add_argument("--out", type=Path, default=cli._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    args = ap.parse_args(argv)
    # fields the weekly_learning.py commands read
    args.week, args.retrospective, args.history_seasons, args.after_kickoff = None, False, None, False
    if args.command == "reproject":
        args.week = args.reproject_week
    elif args.reproject_week is not None:
        ap.error("--week applies to reproject only (run picks the target week itself)")
    return args


def main(argv=None) -> int:
    args = parse(argv)
    if args.command == "status":
        print(json.dumps(cmd_status(args), indent=1, default=str))
        return 0
    if args.command == "reproject":
        code, report = run_reproject(args)
        keys = ("status", "season", "week", "errors", "required_actions", "exit_code")
        rp = report.get("reprojection") or {}
        print(json.dumps({**{k: report.get(k) for k in keys},
                          **{k: rp.get(k) for k in ("run_id", "base_run_id", "kicked_off_teams", "reprojected",
                                                    "carried", "changes")}}, indent=1, default=str))
        return code
    code, report = run_cycle(args)
    keys = ("status", "season", "target_week", "target_first_kickoff_eastern", "errors", "required_actions",
            "training", "forecast", "projection", "kicker_projection", "ctx_shadow", *(r[0] for r in RESEARCH_SHADOWS),
            "dst_shadow", "dist_ledger", "promotion_check", "promotion_ready", "evaluation", "missed_window", "warnings",
            "exit_code")
    print(json.dumps({k: report.get(k) for k in keys}, indent=1, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
