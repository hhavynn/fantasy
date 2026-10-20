"""Evidence health: did this Mac really collect the prospective evidence, week by week?

"The feature exists in GitHub" is not "the system collected valid real-world evidence". A refresh can exit 0
while its evidence stage (refresh_all.py) only logged WARNINGs, an ESPN sync is not an ESPN *pregame* snapshot,
and a production run created after a game kicked off is not a forecast of that game. This module answers, from
what is actually on disk, read-only:

  scheduler   are the launchd agents installed, identical to the templates, loaded, and did they run at their
              scheduled times (refresh logs / learning-cycle reports, matched to the calendar)?
  refreshes   per refresh log: sync steps, MotherDuck publish, pregame check, and each evidence substep
              (injury freshness, start/sit decision set, ESPN benchmark), separately from the syncs
  weeks       for each week from the first prospective week: kickoff slots, the scheduled capture windows and
              which ran, ESPN snapshots before each slot, immutable production runs and their valid pregame rows
              by position, which model the projection switch serves (ifp-2 or the ifp-1 fallback), frozen
              research ledgers, Start/Sit decision sets, finality, matched benchmark observations by position,
              and every missing piece of evidence with its cause
  next game   what is captured and what is missing before the next kickoff, and the next scheduled run
  storage     schema version, hosted publication (from publish records; MotherDuck itself only on request),
              disk space, a write probe, damaged evidence files, unverifiable snapshot timestamps

It lives in scripts/ (beside its CLI, scripts/evidence_health.py, and learning_cycle.py), not in app/: it is
local operations tooling that the hosted API never imports, and it reads the research-only benchmark files that
app/ must not reference (test_espn_benchmark.test_app_modules_never_reference_the_benchmark).

Nothing here writes to a database, syncs, trains or backfills: a missed pregame capture is reported as missed,
never regenerated. The only files written are the guard's attempt record and the alert de-duplication state,
both under data/logs/evidence_health/ (git-ignored). Severity: `fail` needs action now, `warn` is a gap or
risk, `info` is context. Evidence missed in a week older than the latest played one is `info` (it cannot be
recovered, and repeating it as a warning every week would bury new problems).
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import _bootstrap  # noqa: F401  (backend/ on sys.path when imported from scripts/)
from app.db.connection import SCHEMA_VERSION, fetch_dicts, schema_version, table_exists

UTC = timezone.utc
SCHEMA = "evidence-health/v1"
OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
_RANK = {OK: 0, INFO: 1, WARN: 2, FAIL: 3}
POSITIONS = ("QB", "RB", "WR", "TE")
# Prospective (pregame, write-once) evidence collection started in week 6 of 2026; earlier weeks never had it,
# so reporting them as "missed" would be noise. Other seasons start at week 1. --from-week overrides.
FIRST_PROSPECTIVE_WEEK = {2026: 6}
WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")      # launchd Weekday 0..6 (7 is Sunday too)

LABEL_PREFIX = "com.fantasy-intel."
JOBS = ("refresh", "weekly-learning", "pregame-guard")
REQUIRED_JOBS = ("refresh", "weekly-learning")                     # the guard is optional (opt-in)
START_SIT_NOTHING_ELIGIBLE = 2                                     # refresh_all.START_SIT_NOTHING_ELIGIBLE
EVIDENCE_SCRIPTS = ("injury_freshness.py", "start_sit_evaluation.py", "espn_benchmark.py")


@dataclass(frozen=True)
class Thresholds:
    capture_max_age_h: float = 12.0      # a pregame capture older than this at kickoff is stale for that slot
    scheduled_grace_h: float = 6.0       # a refresh that starts this long after a calendar slot ran "for" it
                                         # (a sleeping Mac runs the missed job on wake)
    lookback_days: float = 9.0           # scheduled-run audit window
    unfinished_after_min: float = 90.0   # a refresh log without an end line older than this was killed
    upcoming_warn_h: float = 48.0        # missing evidence for a kickoff this close is a warning
    upcoming_fail_h: float = 6.0         # ... and this close a failure
    disk_warn_gb: float = 5.0
    disk_fail_gb: float = 1.0
    provenance_slack_s: float = 120.0    # a snapshot stamp must fall inside an ok ESPN sync run (+- this)


@dataclass
class Issue:
    code: str
    severity: str
    subject: str
    message: str
    fix: str | None = None
    week: int | None = None

    @property
    def fingerprint(self) -> str:
        return f"{self.code}:{self.subject}"

    def to_dict(self) -> dict:
        return {**asdict(self), "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class Paths:
    root: Path                 # the fantasy-intel folder (holds .env, data/, scripts/)
    db: Path
    logs: Path                 # data/logs (refresh-*.log, launchd stderr)
    weekly: Path               # WEEKLY_MODEL_DIR: research ledger, cycle reports and status
    start_sit: Path            # START_SIT_EVAL_DIR: decision_sets/
    benchmark: Path            # ESPN_BENCHMARK_DIR: scorecards
    launch_agents: Path        # ~/Library/LaunchAgents
    templates: Path            # scripts/launchd

    @property
    def state(self) -> Path:
        return self.logs / "evidence_health"


def default_paths(root: Path, db: Path, env: Mapping[str, str] | None = None, home: Path | None = None) -> Paths:
    """The directories the evidence scripts write by default (same env overrides)."""
    env = os.environ if env is None else env

    def p(key: str, default: str) -> Path:
        raw = Path(env.get(key) or default)
        return raw if raw.is_absolute() else root / raw

    return Paths(root=root, db=Path(db), logs=root / "data" / "logs", weekly=p("WEEKLY_MODEL_DIR", "data/models/weekly"),
                 start_sit=p("START_SIT_EVAL_DIR", "data/models/start_sit"),
                 benchmark=p("ESPN_BENCHMARK_DIR", "data/models/espn_benchmark"),
                 launch_agents=(home or Path.home()) / "Library" / "LaunchAgents", templates=root / "scripts" / "launchd")


# ----------------------------------------------------------------------------- time helpers


def utc(v: Any) -> datetime | None:
    """Aware UTC datetime from a datetime (naive = UTC, as DuckDB stores it) or an ISO string."""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return None
    return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)


def _iso(v: datetime | None) -> str | None:
    return None if v is None else v.astimezone(UTC).isoformat()


def _local(dt: datetime, tz: tzinfo | None) -> datetime:
    return dt.astimezone(tz) if tz is not None else dt.astimezone()


def show(dt: datetime | None, tz: tzinfo | None = None) -> str:
    """'Thu 10/15 17:15 PDT' in the Mac's zone (or `tz`)."""
    if dt is None:
        return "never"
    loc = _local(dt, tz)
    return f"{loc:%a %m/%d %H:%M} {loc.tzname() or ''}".rstrip()


def _hours(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600


# ----------------------------------------------------------------------------- launchd


Runner = Callable[[Sequence[str]], tuple[int, str]]


def run_command(args: Sequence[str], timeout: float = 15.0) -> tuple[int, str]:
    """(exit code, stdout+stderr); never raises (a missing binary is exit 127)."""
    try:
        p = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as err:
        return 127, str(err)


def label(job: str) -> str:
    return LABEL_PREFIX + job


def render_template(text: str, root: Path, uv: str) -> str:
    """The README's install `sed`: fill in the fantasy-intel folder and the uv binary."""
    return text.replace("__FANTASY_INTEL_DIR__", str(root)).replace("__UV__", uv)


def expected_plist(paths: Paths, job: str, uv: str) -> tuple[dict | None, str | None]:
    """(parsed plist the template renders to, rendered text); (None, None) if the template is missing."""
    tpl = paths.templates / f"{label(job)}.plist"
    if not tpl.exists():
        return None, None
    text = render_template(tpl.read_text(), paths.root, uv)
    return plistlib.loads(text.encode()), text


def calendar_entries(plist: Mapping | None) -> list[tuple[int | None, int, int]]:
    """StartCalendarInterval as sorted (weekday 0-6 or None for daily, hour, minute)."""
    raw = (plist or {}).get("StartCalendarInterval")
    if raw is None:
        return []
    out = set()
    for e in raw if isinstance(raw, list) else [raw]:
        wd = e.get("Weekday")
        out.add((None if wd is None else int(wd) % 7, int(e.get("Hour", 0)), int(e.get("Minute", 0))))
    return sorted(out, key=lambda t: (-1 if t[0] is None else t[0], t[1], t[2]))


def describe_entry(e: tuple[int | None, int, int]) -> str:
    return f"{'daily' if e[0] is None else WEEKDAYS[e[0]]} {e[1]:02d}:{e[2]:02d}"


def plist_drift(expected: Mapping, installed: Mapping) -> list[str]:
    """Human-readable differences between the rendered template and the installed plist (key order and
    formatting are ignored: plutil may have rewritten the file)."""
    out = []
    for key in sorted(set(expected) | set(installed)):
        if key == "StartCalendarInterval":
            want, have = set(calendar_entries(expected)), set(calendar_entries(installed))
            if have - want:
                out.append("extra schedule entries not in the template: "
                           + ", ".join(describe_entry(e) for e in sorted(have - want, key=str)))
            if want - have:
                out.append("template schedule entries missing from the installed job: "
                           + ", ".join(describe_entry(e) for e in sorted(want - have, key=str)))
        elif key not in installed:
            out.append(f"{key} missing from the installed job")
        elif key not in expected:
            out.append(f"{key} set in the installed job but not in the template")
        elif expected[key] != installed[key]:
            out.append(f"{key} differs from the template")
    return out


def launchctl_status(job_label: str, runner: Runner, uid: int) -> dict:
    """Parse `launchctl print gui/<uid>/<label>`: loaded, state, runs (since it was loaded), last exit code."""
    code, out = runner(["launchctl", "print", f"gui/{uid}/{job_label}"])
    if code != 0:
        return {"loaded": False, "state": None, "runs": None, "last_exit_code": None}

    def first(pattern: str) -> str | None:
        m = re.search(pattern, out, re.MULTILINE)
        return m.group(1).strip() if m else None

    runs, last = first(r"^\s*runs = (\d+)"), first(r"^\s*last exit code = (.+)$")
    try:
        last_code: int | None = int(last) if last is not None else None
    except ValueError:                   # "(never exited)"
        last_code = None
    return {"loaded": True, "state": first(r"^\s*state = (.+)$"), "runs": int(runs) if runs else None,
            "last_exit_code": last_code, "last_exit_text": last}


def job_status(paths: Paths, job: str, runner: Runner, uv: str, uid: int) -> dict:
    lbl = label(job)
    installed_path = paths.launch_agents / f"{lbl}.plist"
    out: dict[str, Any] = {"job": job, "label": lbl, "plist": str(installed_path), "installed": installed_path.exists(),
                           "valid": None, "matches_template": None, "drift": [], "template": None, "program_ok": None,
                           "installed_mtime": None, "calendar": [], "start_interval": None}
    try:
        expected, _ = expected_plist(paths, job, uv)
    except Exception as err:            # a broken template is reported, never raised
        expected, out["template_error"] = None, f"{err.__class__.__name__}: {err}"
    out["template"] = expected is not None
    installed = None
    if out["installed"]:
        out["installed_mtime"] = _iso(datetime.fromtimestamp(installed_path.stat().st_mtime, UTC))
        try:
            installed = plistlib.loads(installed_path.read_bytes())
            out["valid"] = True
        except Exception as err:
            out["valid"], out["parse_error"] = False, f"{err.__class__.__name__}: {err}"
    if installed is not None and expected is not None:
        out["drift"] = plist_drift(expected, installed)
        out["matches_template"] = not out["drift"]
    if installed is not None:
        prog = (installed.get("ProgramArguments") or [None])[0]
        out["program_ok"] = bool(prog) and os.access(prog, os.X_OK)
        wd = installed.get("WorkingDirectory")
        out["working_directory_ok"] = wd is None or Path(wd).is_dir()
    ref = installed if installed is not None else expected
    out["calendar"] = [describe_entry(e) for e in calendar_entries(ref)]
    out["_entries"] = calendar_entries(ref)
    out["start_interval"] = (ref or {}).get("StartInterval")
    out["launchctl"] = launchctl_status(lbl, runner, uid) if out["installed"] else {"loaded": False}
    return out


def occurrences(entries: Iterable[tuple[int | None, int, int]], start: datetime, end: datetime,
                tz: tzinfo | None = None) -> list[datetime]:
    """Calendar firings in (start, end] as aware UTC. launchd's calendar is the Mac's local wall clock, so a
    DST change keeps 08:00 at 08:00 local."""
    entries = list(entries)
    if not entries:
        return []
    out = []
    d = _local(start, tz).date() - timedelta(days=1)
    last = _local(end, tz).date() + timedelta(days=1)
    while d <= last:
        wd = (d.weekday() + 1) % 7                       # Python Mon=0 -> launchd Sun=0
        for e_wd, h, m in entries:
            if e_wd is None or e_wd == wd:
                naive = datetime.combine(d, time(h, m))
                loc = naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()
                t = loc.astimezone(UTC)
                if start < t <= end:
                    out.append(t)
        d += timedelta(days=1)
    return sorted(set(out))


def scheduled_runs(entries, starts: Sequence[datetime], since: datetime, now: datetime, grace_h: float,
                   tz: tzinfo | None = None) -> list[dict]:
    """Each calendar firing in (since, now] and whether a run started within [firing - 2 min, firing + grace]."""
    out = []
    for occ in occurrences(entries, since, now, tz):
        hit = [s for s in starts if occ - timedelta(minutes=2) <= s <= occ + timedelta(hours=grace_h)]
        out.append({"scheduled_for": _iso(occ), "ran_at": _iso(min(hit)) if hit else None,
                    "status": "ran" if hit else ("pending" if now < occ + timedelta(hours=grace_h) else "missed")})
    return out


# ----------------------------------------------------------------------------- refresh logs

_LOG_NAME = re.compile(r"^refresh-(\d{8}T\d{6}Z)\.log$")


def _stamp(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


PUBLISH_STEPS = ("publish_snapshot.py", "publish_motherduck.py")   # refresh_all's hosted publish step, R2 or legacy


def parse_refresh_log(text: str) -> dict:
    """Structure of one refresh_all.py log. Only refresh_all's own lines are interpreted: a line is a step's
    output between its `==> name` and `<== exit N` markers, so a child's output can never fake a result.

    Returns {steps, status (complete | failed | unfinished), failed, publish, pregame, evidence, warnings}.
    evidence.ran is False for a log written by code without the evidence stage (before PR #72) or by
    --skip-evidence / --check-only runs."""
    steps: list[dict] = []
    ev: dict | None = None
    cur: dict | None = None
    res: dict[str, Any] = {"steps": steps, "status": "unfinished", "failed": None, "warnings": [],
                           "publish": {"ran": False, "exit": None, "published_at": None, "skipped": None},
                           "pregame": {"ok": None, "warning": None}}

    def evidence() -> dict:
        nonlocal ev
        if ev is None:
            ev = {"ran": True, "steps": [], "problems": [], "notes": [], "selection": None, "summary": None}
        return ev

    for line in text.splitlines():
        if cur is not None:                              # inside a step: only its end marker matters
            m = re.match(r"^<== exit (-?\d+) after", line)
            if m:
                cur["exit"], cur = int(m.group(1)), None
            elif line.startswith("<== killed"):
                cur["killed"], cur = True, None
            elif cur["name"] in PUBLISH_STEPS:
                m = re.match(r"^Published .* at (\S+)$", line)
                if m:
                    res["publish"]["published_at"] = m.group(1)
            continue
        if line.startswith("==> Evidence:"):
            evidence()
            continue
        m = re.match(r"^==> (\S+)\s+\(", line)
        if m:
            cur = {"name": m.group(1), "exit": None, "killed": False}
            (ev["steps"] if ev is not None else steps).append(cur)
            if cur["name"] in PUBLISH_STEPS:
                res["publish"]["ran"], res["publish"]["step"] = True, cur["name"]
            continue
        if line.startswith("WARNING: Evidence: "):
            evidence()["problems"].append(line[len("WARNING: Evidence: "):])
        elif line.startswith("INFO: Evidence: "):
            evidence()["notes"].append(line[len("INFO: Evidence: "):])
        elif line.startswith("Evidence: "):
            rest = line[len("Evidence: "):]
            e = evidence()
            if rest.startswith("season "):
                e["selection"] = rest
            elif rest == "all steps ok" or "problem(s)" in rest:
                e["summary"] = rest
            else:
                e["notes"].append(rest)
        elif line.startswith("WARNING: "):
            msg = line[len("WARNING: "):]
            res["warnings"].append(msg)
            if "pregame" in msg.lower() and "snapshot" in msg.lower():
                res["pregame"]["warning"] = msg
        elif line.startswith("Pregame check:"):
            res["pregame"]["ok"] = line[len("Pregame check: "):]
            m = re.match(r"^Pregame check: week (\d+) ESPN snapshot captured at (\S+) \(", line)
            if m:
                res["pregame"]["week"], res["pregame"]["captured_at"] = int(m.group(1)), m.group(2)
        elif line.startswith(("Hosted publish skipped", "MotherDuck publish skipped")):  # older logs say MotherDuck
            res["publish"]["skipped"] = line
        elif line.startswith("FAILED: "):
            res["failed"], res["status"] = line[len("FAILED: "):], "failed"
        elif line.startswith("Refresh complete."):
            if res["status"] != "failed":
                res["status"] = "complete"
    for s in steps:
        if s["name"] in PUBLISH_STEPS:
            res["publish"]["exit"] = s["exit"]
    if ev is None:
        res["evidence"] = {"ran": False, "status": "not_run", "steps": [], "problems": [], "notes": []}
    else:
        for s in ev["steps"]:
            s["result"] = ("timeout" if s["killed"] else None if s["exit"] is None else "ok" if s["exit"] == 0
                           else "nothing_eligible" if s["exit"] == START_SIT_NOTHING_ELIGIBLE
                           and s["name"] == "start_sit_evaluation.py" else "failed")
        ev["status"] = "problems" if ev["problems"] else ("ok" if ev["summary"] == "all steps ok" else "unfinished")
        res["evidence"] = ev
    return res


def refresh_runs(logs_dir: Path, now: datetime, unfinished_after_min: float = 90.0) -> list[dict]:
    """Every refresh log, oldest first, parsed. A log still being written (no end line, touched recently) is
    `running`; an old one without an end line was killed or crashed (`unfinished`)."""
    out = []
    for f in sorted(Path(logs_dir).glob("refresh-*.log")):
        m = _LOG_NAME.match(f.name)
        if not m:
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError as err:
            out.append({"log": f.name, "started_at": _iso(_stamp(m.group(1))), "status": "unreadable",
                        "error": str(err), "evidence": {"ran": False, "status": "unknown"}, "steps": []})
            continue
        run = {"log": f.name, "started_at": _iso(_stamp(m.group(1))), **parse_refresh_log(text)}
        if run["status"] == "unfinished":
            touched = datetime.fromtimestamp(f.stat().st_mtime, UTC)
            if now - touched < timedelta(minutes=unfinished_after_min):
                run["status"] = "running"
        out.append(run)
    return out


def learning_cycle_runs(weekly_dir: Path) -> list[dict]:
    """The weekly learning cycle's reports (reports/cycle-<UTC stamp>-<pid>.json), oldest first."""
    out = []
    for f in sorted((Path(weekly_dir) / "reports").glob("cycle-*.json")):
        m = re.match(r"^cycle-(\d{8}T\d{6}Z)-", f.name)
        if not m:
            continue
        try:
            rep = json.loads(f.read_text())
        except (OSError, ValueError):
            rep = {"status": "unreadable"}
        out.append({"report": f.name, "started_at": _iso(_stamp(m.group(1))), "status": rep.get("status"),
                    "exit_code": rep.get("exit_code"), "target_week": rep.get("target_week")})
    return out


def publish_records(logs_dir: Path, runs: Sequence[dict]) -> list[dict]:
    """Known hosted publishes: 'Published ... at' lines of refresh logs, and publish-history.jsonl
    (publish_snapshot.py / publish_motherduck.py append one line per successful publish, manual ones too)."""
    out = [{"published_at": r["publish"]["published_at"], "source": r["log"]}
           for r in runs if r.get("publish", {}).get("published_at")]
    hist = Path(logs_dir) / "publish-history.jsonl"
    if hist.exists():
        for line in hist.read_text(errors="replace").splitlines():
            try:
                rec = json.loads(line)
                out.append({"published_at": rec["published_at"], "source": hist.name,
                            "local_schema_version": rec.get("local_schema_version")})
            except (ValueError, KeyError, TypeError):
                continue
    return sorted(out, key=lambda r: utc(r["published_at"]) or datetime.min.replace(tzinfo=UTC))


# ----------------------------------------------------------------------------- database: schedule + captures


def kickoff_slots(con, season: int) -> dict[int, list[dict]]:
    """{week: [{kickoff (aware UTC | None), teams, games}]} of the REG schedule, by kickoff time. A game whose
    clock is missing or ambiguous gets kickoff None (never guessed) and its own slot."""
    from app.analytics.projection_backtest import _kickoff

    if not table_exists(con, "nfl_schedules"):
        return {}
    by: dict[int, dict] = defaultdict(dict)
    for g in fetch_dicts(con, "SELECT week, gameday, gametime, home_team, away_team FROM nfl_schedules "
                              "WHERE season = ? AND game_type = 'REG' ORDER BY week, gameday, gametime, home_team",
                         [season]):
        ko = _kickoff(g)
        key = ko or f"unknown:{g['away_team']}@{g['home_team']}"
        slot = by[int(g["week"])].setdefault(key, {"kickoff": ko, "teams": [], "games": []})
        slot["teams"] += [g["away_team"], g["home_team"]]
        slot["games"].append(f"{g['away_team']}@{g['home_team']}")
    return {w: sorted(slots.values(), key=lambda s: (s["kickoff"] is None, s["kickoff"] or datetime.max.replace(tzinfo=UTC)))
            for w, slots in sorted(by.items())}


def verified_snapshot_stamps(con, season: int, slack_s: float = 120.0) -> set[datetime]:
    """ESPN snapshot stamps of the season that a successful ESPN sync run covers (started_at - slack <=
    synced_at <= finished_at + slack): the only stamps whose capture time is proven."""
    if not (table_exists(con, "espn_projection_history") and table_exists(con, "sync_runs")):
        return set()
    rows = fetch_dicts(con, """
        SELECT DISTINCT h.synced_at FROM espn_projection_history h
        WHERE h.season = ? AND EXISTS (
            SELECT 1 FROM sync_runs r WHERE r.source = 'espn' AND r.status = 'ok'
              AND h.synced_at BETWEEN r.started_at - to_seconds(?) AND coalesce(r.finished_at, r.started_at) + to_seconds(?))
        """, [season, slack_s, slack_s])
    return {t for r in rows if (t := utc(r["synced_at"])) is not None}


def espn_snapshots(con, season: int, week: int, slack_s: float = 120.0) -> dict:
    """Stored ESPN projection snapshots of `week`: [{synced_at, players, verified}] and the stamps per NFL team
    (fantasy_players.pro_team, the team the ESPN sync's own pregame lock uses). Only provenance-verified stamps
    (verified_snapshot_stamps) are in `by_team`, the captures every check counts; a stamp no successful ESPN sync
    run covers is in `unverified_by_team`, reported but never taken as a pregame capture."""
    if not table_exists(con, "espn_projection_history"):
        return {"snapshots": [], "by_team": {}, "unverified_by_team": {}}
    rows = fetch_dicts(con, """SELECT h.synced_at, fp.pro_team, count(DISTINCT h.espn_player_id) AS n
                               FROM espn_projection_history h LEFT JOIN fantasy_players fp USING (espn_player_id)
                               WHERE h.season = ? AND h.scoring_period = ? AND h.projected_points IS NOT NULL
                               GROUP BY 1, 2""", [season, week]) if table_exists(con, "fantasy_players") else []
    verified = verified_snapshot_stamps(con, season, slack_s)
    total: Counter = Counter()
    by_team: dict[str, list[datetime]] = defaultdict(list)
    unverified: dict[str, list[datetime]] = defaultdict(list)
    for r in rows:
        t = utc(r["synced_at"])
        if t is None:
            continue
        total[t] += r["n"]
        if r["pro_team"]:
            (by_team if t in verified else unverified)[r["pro_team"]].append(t)
    return {"snapshots": [{"synced_at": _iso(t), "players": n, "verified": t in verified}
                          for t, n in sorted(total.items())],
            "by_team": {k: sorted(v) for k, v in by_team.items()},
            "unverified_by_team": {k: sorted(v) for k, v in unverified.items()}}


def unverified_snapshot_stamps(con, season: int, slack_s: float = 120.0) -> list[str]:
    """ESPN snapshot stamps that no successful ESPN sync run covers: their capture time cannot be traced to a
    sync, so their pregame status cannot be proven."""
    if not (table_exists(con, "espn_projection_history") and table_exists(con, "sync_runs")):
        return []
    rows = fetch_dicts(con, """
        SELECT DISTINCT h.synced_at FROM espn_projection_history h
        WHERE h.season = ? AND NOT EXISTS (
            SELECT 1 FROM sync_runs r WHERE r.source = 'espn' AND r.status = 'ok'
              AND h.synced_at BETWEEN r.started_at - to_seconds(?) AND coalesce(r.finished_at, r.started_at) + to_seconds(?))
        ORDER BY 1""", [season, slack_s, slack_s])
    return [_iso(utc(r["synced_at"])) for r in rows]


def production_runs(con, season: int) -> dict[int, list[dict]]:
    """Every stored production-forecast run per week, with its stored row count (integrity at a glance). Horizon 0
    only: a rest-of-season run (forecast_horizon > 0) is not pregame evidence, so it neither extends the audited
    weeks nor stands in for the week's missing pregame run."""
    from app.analytics import production_forecasts as pf

    if not table_exists(con, pf.RUNS):
        return {}
    rows_by_run = {}
    if table_exists(con, pf.ROWS):
        rows_by_run = {r["run_id"]: r["n"] for r in fetch_dicts(
            con, f"SELECT run_id, count(*) AS n FROM {pf.ROWS} WHERE season = ? GROUP BY 1", [season])}
    out: dict[int, list[dict]] = defaultdict(list)
    for r in fetch_dicts(con, f"""SELECT run_id, week, model_version, created_at, artifact_created_at, row_count,
                                         content_sha256 FROM {pf.RUNS} WHERE season = ?{pf.evidence_horizon_sql(con)}
                                  ORDER BY week, created_at""",
                         [season]):
        out[int(r["week"])].append({"run_id": r["run_id"], "model_version": r["model_version"],
                                    "created_at": _iso(utc(r["created_at"])),
                                    "artifact_created_at": _iso(utc(r["artifact_created_at"])),
                                    "row_count": r["row_count"], "stored_rows": rows_by_run.get(r["run_id"], 0),
                                    "sealed": bool(r["content_sha256"])})
    return dict(out)


def pregame_rows(con, season: int, weeks: Sequence[int], model_version: str) -> tuple[list[dict], list[dict], str | None]:
    """(valid pregame rows, run exclusions, error) of `model_version` under the league's current scoring:
    production_forecasts.pregame_forecasts, the reader every evaluation uses."""
    from app.analytics import production_forecasts as pf
    from app.analytics.independent_projection_store import league_scoring

    try:
        _, scoring = league_scoring(con, season)
        if not scoring.supported:
            return [], [], "league scoring is not supported by the independent model"
        rows, excl = pf.pregame_forecasts(con, season, weeks, model_version=model_version,
                                          scoring_fingerprint=scoring.fingerprint)
        return rows, excl, None
    except Exception as err:
        return [], [], f"{err.__class__.__name__}: {err}"


def served_model(con, season: int, week: int) -> dict:
    """What the projection switch actually serves for `week` with source=independent (projection_source.weekly):
    the production model, the fallback, or nothing (ESPN)."""
    from app.analytics import projection_source as ps

    try:
        d = ps.describe(ps.weekly(con, season, week, "independent"))
    except Exception as err:
        return {"available": False, "model_version": None, "fallback": None, "reason": f"{err.__class__.__name__}: {err}",
                "run_id": None, "run_created_at": None}
    run = d.get("run") or {}
    mv = run.get("model_version")
    return {"available": bool(d["available"]), "model_version": mv, "production_model": ps.MODEL_VERSION,
            "fallback": None if mv is None else mv != ps.MODEL_VERSION, "reason": d.get("reason"),
            "run_id": run.get("run_id"), "run_created_at": run.get("created_at"), "counts": d.get("counts")}


def roster_kickoffs(con, season: int, week: int) -> list[datetime | None]:
    """Kickoffs of the league-rostered players' games: the slots a Start/Sit decision set must cover."""
    from app.analytics import start_sit_evaluation as sse

    try:
        return [c["kickoff"] for c in sse.roster_candidates(con, season, [week])]
    except Exception:
        return []


def week_finality(con, season: int, weeks: Sequence[int]) -> dict[int, dict]:
    """{week: {fully_final, detail}} with weekly_learning.week_finality (the rule the benchmark counts by).
    Sources are loaded once; an unsupported scoring or missing table is an error entry, not an exception."""
    from app.analytics import weekly_learning as wl
    from app.analytics.forecast_features import load_sources
    from app.analytics.independent_projection_store import league_scoring
    from app.analytics.schedule_intelligence import nfl_fetch_times

    if not weeks:
        return {}
    try:
        _, scoring = league_scoring(con, season)
        scoring.require_supported()
        sources, fetched = load_sources(con, scoring, [season]), nfl_fetch_times(con, season)["schedules"]
    except Exception as err:
        return {w: {"fully_final": None, "detail": f"finality unknown: {err.__class__.__name__}: {err}"} for w in weeks}
    out = {}
    for w in weeks:
        try:
            f = wl.week_finality(sources, season, w, fetched)
        except Exception as err:
            out[w] = {"fully_final": None, "detail": f"finality unknown: {err!r}"}
            continue
        out[w] = {"fully_final": bool(f["fully_final"]),
                  "detail": "fully final" if f["fully_final"] else
                  f"{f['final_teams']}/{f['scheduled_teams']} teams final, {len(f['missing_ingestion_teams'])} "
                  f"final without synced box scores or snaps"}
    return out


# ----------------------------------------------------------------------------- evidence files


def research_ledger(weekly_dir: Path, season: int, week: int) -> dict:
    """Frozen research forecasts of the week (weekly ledger, write-once JSON) and a missed-window record."""
    d = Path(weekly_dir) / "ledger" / f"s{season}w{week:02d}"
    runs, corrupt = [], []
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            run = json.loads(f.read_text())
            rows = run["rows"]
            created = utc(run["created_at"])
        except (OSError, ValueError, KeyError, TypeError) as err:
            corrupt.append({"file": str(f), "error": f"{err.__class__.__name__}: {err}"})
            continue
        pre = [r for r in rows if r.get("pregame")]
        runs.append({"file": f.name, "run_id": run.get("run_id"), "kind": run.get("kind"), "created_at": _iso(created),
                     "rows": len(rows), "pregame_rows": len(pre),
                     "pregame_by_position": dict(sorted(Counter(r.get("position") for r in pre).items(), key=str)),
                     "pregame_kickoffs": sorted({_iso(utc(r.get("kickoff"))) for r in pre if utc(r.get("kickoff"))})})
    missed = None
    mf = Path(weekly_dir) / "missed" / f"s{season}w{week:02d}.json"
    if mf.exists():
        try:
            missed = json.loads(mf.read_text())
        except (OSError, ValueError) as err:
            corrupt.append({"file": str(mf), "error": f"{err.__class__.__name__}: {err}"})
    return {"runs": runs, "corrupt": corrupt, "missed": missed}


def decision_sets(start_sit_dir: Path, season: int, week: int) -> dict:
    """Start/Sit decision sets of the week; a file that does not parse or whose hashes do not verify is corrupt
    (start_sit_evaluation.load_decision_sets silently skips those, so they are only visible here)."""
    from app.analytics import start_sit_evaluation as sse

    sets, corrupt = [], []
    for f in sorted((Path(start_sit_dir) / "decision_sets").glob(f"s{season}w{week:02d}-*.json")):
        try:
            ds = json.loads(f.read_text())
            ok = ds.get("schema") == sse.DECISION_SET_SCHEMA and sse.verify_decision_set(ds)
        except (OSError, ValueError, TypeError, AttributeError) as err:
            corrupt.append({"file": str(f), "error": f"{err.__class__.__name__}: {err}"})
            continue
        if not ok:
            corrupt.append({"file": str(f), "error": "schema or content hash does not verify (edited or damaged)"})
            continue
        cands = ds.get("candidates") or []
        sets.append({"file": f.name, "recorded_at": _iso(utc(ds.get("recorded_at"))), "audit_only": bool(ds.get("audit_only")),
                     "candidates": len(cands),
                     "by_position": dict(sorted(Counter(c.get("position") for c in cands).items(), key=str)),
                     "refused_kicked_off": sum(1 for r in ds.get("refused") or [] if r.get("reason") == "kicked_off"),
                     "kickoffs": sorted({_iso(utc(c.get("kickoff"))) for c in cands if utc(c.get("kickoff"))}),
                     "unavailable_models": ds.get("unavailable_models") or [], "_ds": ds})
    effective: dict = {"candidates": 0, "by_position": {}, "team_weeks": 0}
    usable = [s["_ds"] for s in sets if not s["audit_only"]]
    if usable:
        try:
            cands, _ = sse.candidates_from_sets(usable)
            cands = [c for c in cands if c.get("week") == week]
            effective = {"candidates": len(cands),
                         "by_position": dict(sorted(Counter(c.get("position") for c in cands).items(), key=str)),
                         "team_weeks": len({(c.get("league_id"), c.get("fantasy_team_id")) for c in cands})}
        except Exception as err:
            effective["error"] = f"{err.__class__.__name__}: {err}"
    for s in sets:
        s.pop("_ds")
    return {"sets": sets, "corrupt": corrupt, "effective": effective}


def benchmark_cards(benchmark_dir: Path, season: int) -> dict:
    """Recorded ESPN benchmark scorecards (write-once, named by the sha256 of their bytes; a name that does
    not match the content is a damaged file). Matched (scored) rows per week and position come from the newest
    card of the latest week (each card scores weeks 1..W)."""
    import hashlib

    cards, corrupt = [], []
    for f in sorted(Path(benchmark_dir).glob(f"s{season}w*-*.json")):
        m = re.match(rf"^s{season}w(\d+)-([0-9a-f]{{16}})\.json$", f.name)
        if not m:
            continue
        try:
            text = f.read_text()
            card = json.loads(text)
        except (OSError, ValueError) as err:
            corrupt.append({"file": str(f), "error": f"{err.__class__.__name__}: {err}"})
            continue
        if hashlib.sha256(text.encode()).hexdigest()[:16] != m.group(2):
            corrupt.append({"file": str(f), "error": "content does not match the sha in the file name"})
            continue
        cards.append({"file": f.name, "week": int(m.group(1)), "mtime": f.stat().st_mtime, "card": card})
    matched: dict[int, dict[str, int]] = {}
    if cards:
        latest = max(cards, key=lambda c: (c["week"], c["mtime"]))
        for cell in ((latest["card"].get("coverage") or {}).get("by_week_position") or []):
            matched.setdefault(int(cell["week"]), {})[cell["position"]] = int(cell.get("scored") or 0)
    return {"cards": [{"file": c["file"], "week": c["week"]} for c in cards], "corrupt": corrupt, "matched": matched}


def learning_cycle_status(weekly_dir: Path) -> dict | None:
    f = Path(weekly_dir) / "status" / "latest-cycle.json"
    if not f.exists():
        return None
    try:
        rep = json.loads(f.read_text())
    except (OSError, ValueError) as err:
        return {"status": "unreadable", "error": str(err)}
    return {k: rep.get(k) for k in ("status", "exit_code", "season", "target_week", "target_first_kickoff",
                                    "errors", "required_actions", "finished_at")}


# ----------------------------------------------------------------------------- storage


def storage(paths: Paths, thresholds: Thresholds) -> dict:
    out: dict[str, Any] = {"free_gb": None, "write_ok": None, "write_error": None}
    target = paths.db.parent if paths.db.parent.exists() else paths.root
    try:
        out["free_gb"] = round(shutil.disk_usage(target).free / 1e9, 1)
    except OSError as err:
        out["disk_error"] = str(err)
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=paths.logs, prefix=".write-probe-", delete=True) as f:
            f.write(b"ok")
            f.flush()
        out["write_ok"] = True
    except OSError as err:
        out["write_ok"], out["write_error"] = False, f"{err.__class__.__name__}: {err}"
    return out


def local_last_write(con) -> datetime | None:
    """The newest local write a publish should carry: an ok sync/analytics run or a production run."""
    from app.analytics import production_forecasts as pf

    stamps = []
    if table_exists(con, "sync_runs"):
        stamps.append(con.execute("SELECT max(finished_at) FROM sync_runs WHERE status = 'ok'").fetchone()[0])
    if table_exists(con, pf.RUNS):
        stamps.append(con.execute(f"SELECT max(created_at) FROM {pf.RUNS}").fetchone()[0])
    stamps = [utc(s) for s in stamps if s is not None]
    return max(stamps) if stamps else None


def hosted_status(database: str, connect: Callable[[str], Any]) -> dict:
    """Schema version and published_at of the hosted copy (read-only query; needs MOTHERDUCK_TOKEN)."""
    try:
        con = connect(database)
        try:
            meta = dict(con.execute("SELECT key, value FROM schema_meta WHERE key IN ('schema_version', 'published_at')").fetchall())
        finally:
            con.close()
    except Exception as err:
        return {"checked": True, "error": f"{err.__class__.__name__}: {str(err)[:200]}"}
    sv = meta.get("schema_version")
    return {"checked": True, "schema_version": int(sv) if sv and str(sv).isdigit() else None,
            "published_at": _iso(utc(meta.get("published_at")))}


# ----------------------------------------------------------------------------- the audit


@dataclass
class Context:
    now: datetime
    season: int
    paths: Paths
    thresholds: Thresholds = field(default_factory=Thresholds)
    tz: tzinfo | None = None                 # display + calendar zone (None: the Mac's local zone)
    uv: str = "uv"
    uid: int = 0
    runner: Runner = run_command
    from_week: int | None = None
    hosted: dict | None = None               # hosted_status(), when --hosted was asked for
    db_note: str | None = None               # e.g. "read from a copy: a writer held the lock"


def _slot_label(s: dict, tz) -> str:
    games = ", ".join(s["games"][:3]) + (f" +{len(s['games']) - 3}" if len(s["games"]) > 3 else "")
    return f"{show(s['kickoff'], tz)} ({games})"


def _latest_before(stamps: Iterable[datetime], t: datetime | None) -> datetime | None:
    pre = [s for s in stamps if t is not None and s < t]
    return max(pre) if pre else None


def _runs_between(runs: Sequence[dict], a: datetime | None, b: datetime) -> list[dict]:
    return [r for r in runs if (s := utc(r["started_at"])) is not None and (a is None or s >= a) and s < b]


def _cause_no_run(runs, window_start, kickoff, entries, tz, what: str) -> str:
    """Why nothing captured `what` before `kickoff`: no refresh ran, or what the refreshes that ran did."""
    ran = _runs_between(runs, window_start, kickoff)
    if not ran:
        planned = occurrences(entries, window_start or kickoff - timedelta(days=7), kickoff, tz)
        when = (f"; the scheduled run(s) at {', '.join(show(t, tz) for t in planned[-3:])} left no refresh log "
                "(Mac asleep or powered off, or the job was not loaded)") if planned else \
            "; no scheduled refresh falls in this window (fixed Thu/Sun/Mon/Tue times do not cover it)"
        return f"no refresh ran between {show(window_start, tz)} and kickoff{when}"
    parts = []
    for r in ran[-3:]:
        st = f"{r['log']} ({show(utc(r['started_at']), tz)}): {r['status']}"
        if r["status"] == "failed":
            st += f" [{r['failed']}]"
        ev = r.get("evidence", {})
        if not ev.get("ran"):
            st += ", evidence stage did not run (code before PR #72, or --skip-evidence)"
        elif ev.get("problems"):
            st += f", evidence problems: {'; '.join(ev['problems'][:2])}"
        parts.append(st)
    return f"{what}: refresh(es) before kickoff: " + " | ".join(parts)


def audit_weeks(con, ctx: Context, refresh: Sequence[dict], refresh_entries, tracked_since: datetime | None = None) -> dict:
    """Per-week evidence audit from the first prospective week to the week of the next kickoff (and any later
    week that already has evidence). Every played slot without evidence gets a cause."""
    from app.analytics import projection_source as ps

    tz, now, season = ctx.tz, ctx.now, ctx.season
    slots_by_week = kickoff_slots(con, season)
    first = ctx.from_week or FIRST_PROSPECTIVE_WEEK.get(season, 1)
    runs_by_week = production_runs(con, season)
    future = [(s["kickoff"], w) for w, ss in slots_by_week.items() for s in ss if s["kickoff"] and s["kickoff"] > now]
    next_week = min(future)[1] if future else None
    evidence_weeks = set(runs_by_week)
    ledger_root = ctx.paths.weekly / "ledger"
    if ledger_root.exists():
        evidence_weeks |= {int(m.group(1)) for d in ledger_root.glob(f"s{season}w*")
                           if (m := re.match(rf"^s{season}w(\d+)$", d.name))}
    # every week that has kicked off is audited even with no evidence at all (collection failing completely, or the
    # season over with no future kickoff): those misses are what this audit must show
    started = [w for w, ss in slots_by_week.items() if any(s["kickoff"] and s["kickoff"] <= now for s in ss)]
    last = max([w for w in (next_week, *started, *evidence_weeks) if w is not None] or [first - 1])
    weeks = [w for w in range(first, last + 1) if w in slots_by_week]
    prod_rows, prod_excl, prod_err = pregame_rows(con, season, weeks, ps.MODEL_VERSION)
    fb_rows, _, _ = pregame_rows(con, season, weeks, ps.FALLBACK_VERSION)
    played_weeks = [w for w in weeks if all(s["kickoff"] and s["kickoff"] <= now for s in slots_by_week[w])]
    finality = week_finality(con, season, played_weeks)
    bench = benchmark_cards(ctx.paths.benchmark, season)
    out = []
    for w in weeks:
        slots = slots_by_week[w]
        kicks = [s["kickoff"] for s in slots if s["kickoff"]]
        first_k, last_k = (min(kicks), max(kicks)) if kicks else (None, None)
        prev_kicks = [s["kickoff"] for s in slots_by_week.get(w - 1) or [] if s["kickoff"]]
        prev_last = max(prev_kicks) if prev_kicks else None
        window_start = prev_last or (first_k - timedelta(days=7) if first_k else None)
        state = ("unknown_kickoffs" if not kicks else "future" if first_k > now else
                 "played" if last_k <= now and len(kicks) == len(slots) else "in_progress")
        espn = espn_snapshots(con, season, w, ctx.thresholds.provenance_slack_s)
        rows_w = [r for r in prod_rows if r["week"] == w]
        fb_w = [r for r in fb_rows if r["week"] == w]
        ledger = research_ledger(ctx.paths.weekly, season, w)
        sets = decision_sets(ctx.paths.start_sit, season, w)
        roster_k = {k for k in roster_kickoffs(con, season, w) if k is not None}
        served = served_model(con, season, w)
        windows = []
        if window_start and last_k:
            for occ in occurrences(refresh_entries, window_start, last_k, tz):
                hit = [r for r in refresh if (s := utc(r["started_at"])) and occ - timedelta(minutes=2) <= s
                       <= occ + timedelta(hours=ctx.thresholds.scheduled_grace_h)]
                windows.append({"scheduled_for": _iso(occ), "local": show(occ, tz),
                                "ran": [{"log": r["log"], "status": r["status"],
                                         "evidence": r.get("evidence", {}).get("status")} for r in hit],
                                # before the job's first trace (install) a missing run proves nothing
                                "status": "ran" if hit else "pending" if occ > now else
                                "before_install" if tracked_since is None or occ < tracked_since else "missed"})
        slot_rows = []
        missing, stale, lost = [], [], []
        for s in slots:
            k = s["kickoff"]
            teams = set(s["teams"])
            esnap = _latest_before([t for team in teams for t in espn["by_team"].get(team, [])], k)
            unver = _latest_before([t for team in teams for t in espn["unverified_by_team"].get(team, [])], k)
            unver = unver if unver is not None and (esnap is None or unver > esnap) else None
            prod_n = sum(1 for r in rows_w if r["nfl_team"] in teams and r["coverage_status"] == "projected")
            fb_n = sum(1 for r in fb_w if r["nfl_team"] in teams and r["coverage_status"] == "projected")
            res_n = sum(1 for run in ledger["runs"] if k and _iso(k) in run["pregame_kickoffs"])
            need_ss = k in roster_k
            ss_at = _latest_before([utc(x["recorded_at"]) for x in sets["sets"] if not x["audit_only"]
                                    and k and _iso(k) in x["kickoffs"]], k)
            row = {"kickoff": _iso(k), "local": show(k, tz), "games": s["games"], "kicked_off": bool(k and k <= now),
                   "espn_pregame_snapshot": _iso(esnap), "espn_unverified_snapshot": _iso(unver),
                   "espn_age_h": round(_hours(esnap, k), 1) if esnap and k else None,
                   "production_pregame_projected": prod_n, "fallback_pregame_projected": fb_n,
                   "research_frozen": res_n > 0, "start_sit_needed": need_ss, "start_sit_recorded_at": _iso(ss_at)}
            slot_rows.append(row)
            if k is None or k > now:
                continue                                  # future slots are judged under "next kickoff"
            label_ = _slot_label(s, tz)
            if esnap is None:
                missing.append({"evidence": "espn_snapshot", "slot": label_,
                                "cause": (f"only an UNVERIFIED snapshot ({show(unver, tz)}) precedes kickoff: no successful "
                                          "ESPN sync run covers its stamp, so it is not counted as a pregame capture; "
                                          if unver else "")
                                + _cause_no_run(refresh, window_start, k, refresh_entries, tz,
                                                "no provenance-verified ESPN snapshot of this week stored before kickoff")})
            elif _hours(esnap, k) > ctx.thresholds.capture_max_age_h:
                stale.append({"slot": label_, "age_h": round(_hours(esnap, k), 1), "snapshot": _iso(esnap)})
            reported = [(t, r["log"]) for r in refresh if r.get("pregame", {}).get("week") == w
                        and (t := utc(r["pregame"].get("captured_at"))) is not None and t < k]
            if reported and (esnap is None or max(reported)[0] > esnap):
                t, log_ = max(reported)
                lost.append({"slot": label_, "reported": _iso(t), "log": log_, "stored_latest": _iso(esnap)})
            if prod_n == 0:
                made = [r for r in runs_by_week.get(w, []) if r["model_version"] == ps.MODEL_VERSION]
                excl = [e["reason"] for e in prod_excl if e["week"] == w and e["run_id"] in {r["run_id"] for r in made}]
                cause = prod_err or (
                    f"no {ps.MODEL_VERSION} production run stored for week {w}" if not made else
                    f"{ps.MODEL_VERSION} run(s) created {', '.join(show(utc(r['created_at']), tz) for r in made)}, "
                    "after this kickoff" + (f" or excluded ({'; '.join(excl)[:200]})" if excl else ""))
                cause += (f"; the {ps.FALLBACK_VERSION} fallback has {fb_n} pregame projected row(s) for it" if fb_n
                          else f"; no {ps.FALLBACK_VERSION} pregame rows either")
                missing.append({"evidence": "production_forecast", "slot": label_, "cause": cause,
                                "fallback_rows": fb_n})
            if res_n == 0:
                cyc = ledger["missed"]
                cause = (f"missed window recorded: {cyc.get('reason')}" if cyc else
                         "no frozen research forecast with a pregame row for this slot (learning cycle did not freeze "
                         "one before kickoff; see `learning_cycle.py status`)")
                missing.append({"evidence": "research_forecast", "slot": label_, "cause": cause})
            if need_ss and ss_at is None:
                missing.append({"evidence": "start_sit_decision_set", "slot": label_,
                                "cause": _cause_no_run(refresh, window_start, k, refresh_entries, tz,
                                                       "no Start/Sit decision set recorded before kickoff")})
        prod_pos = Counter(r["position"] for r in rows_w if r["coverage_status"] == "projected")
        fb_pos = Counter(r["position"] for r in fb_w if r["coverage_status"] == "projected")
        fin = finality.get(w) or {"fully_final": False if state != "played" else None,
                                  "detail": {"future": "not started", "in_progress": "games still to play"}.get(state, "")}
        bench_w = [c for c in bench["cards"] if c["week"] >= w]       # a card for week W scores weeks 1..W
        out.append({
            "week": w, "state": state, "first_kickoff": _iso(first_k), "last_kickoff": _iso(last_k),
            "capture_window_start": _iso(window_start), "scheduled_captures": windows, "slots": slot_rows,
            "espn_snapshots": espn["snapshots"],
            "production": {"model_version": ps.MODEL_VERSION, "runs": runs_by_week.get(w, []),
                           "valid_pregame_rows": len(rows_w),
                           "usable_by_position": {p: prod_pos.get(p, 0) for p in POSITIONS},
                           "fallback_model": ps.FALLBACK_VERSION,
                           "fallback_usable_by_position": {p: fb_pos.get(p, 0) for p in POSITIONS},
                           "excluded_runs": [e for e in prod_excl if e["week"] == w], "error": prod_err},
            "served": served,
            "research": {"runs": ledger["runs"], "missed": ledger["missed"], "corrupt": ledger["corrupt"]},
            "start_sit": sets,
            "finality": fin,
            "benchmark": {"recorded": [c["file"] for c in bench_w],
                          "matched_by_position": {p: (bench["matched"].get(w) or {}).get(p, 0) for p in POSITIONS}},
            "missing": missing, "stale_espn": stale, "lost_espn": lost,
        })
    return {"weeks": out, "next_week": next_week, "benchmark": {k: bench[k] for k in ("cards", "corrupt")},
            "slots_by_week": slots_by_week}


def next_kickoff(con, ctx: Context, slots_by_week: Mapping[int, list[dict]], refresh: Sequence[dict],
                 refresh_entries, weeks: Sequence[dict]) -> dict | None:
    """What exists, and what is missing, for the next kickoff slot; the next scheduled refresh before it."""
    tz, now = ctx.tz, ctx.now
    upcoming = sorted(((s["kickoff"], w, s) for w, ss in slots_by_week.items() for s in ss
                       if s["kickoff"] and s["kickoff"] > now), key=lambda x: x[0])
    if not upcoming:
        return None
    k, w, s = upcoming[0]
    wk = next((x for x in weeks if x["week"] == w), None)
    slot = next((x for x in (wk or {}).get("slots", []) if x["kickoff"] == _iso(k)), None) or {}
    max_age = timedelta(hours=ctx.thresholds.capture_max_age_h)
    espn_at = utc(slot.get("espn_pregame_snapshot"))
    planned = occurrences(refresh_entries, now, k, tz)
    fresh_planned = [t for t in planned if t >= k - max_age]
    first_of_week = wk is not None and wk["first_kickoff"] == _iso(k)
    served = (wk or {}).get("served") or {}
    have, need = [], []
    if slot.get("espn_unverified_snapshot"):
        need.append(f"ignored: the newer ESPN snapshot {show(utc(slot['espn_unverified_snapshot']), tz)} has no matching "
                    "successful ESPN sync run (unverified provenance)")
    if espn_at and k - espn_at <= max_age:
        have.append(f"ESPN snapshot {show(espn_at, tz)} ({_hours(espn_at, k):.1f}h before kickoff)")
    elif fresh_planned:
        have.append(f"ESPN snapshot due from the scheduled refresh at {show(fresh_planned[0], tz)}")
    else:
        need.append("a fresh ESPN snapshot: " + ("none stored yet" if espn_at is None else
                    f"the latest ({show(espn_at, tz)}) is {_hours(espn_at, k):.0f}h before kickoff")
                    + ("; no scheduled refresh lands within "
                       f"{ctx.thresholds.capture_max_age_h:g}h before it" if not fresh_planned else ""))
    if slot.get("production_pregame_projected"):
        have.append(f"{served.get('production_model')} production run ({slot['production_pregame_projected']} projected rows)")
    else:
        need.append(f"a {served.get('production_model') or 'production'} run for week {w} before kickoff"
                    + (f" (the switch serves {served.get('model_version')} now)" if served.get("model_version") else ""))
    if slot.get("research_frozen"):
        have.append("frozen research forecast")
    elif first_of_week:
        need.append(f"a frozen research forecast for week {w} (learning_cycle.py, before the first kickoff)")
    if slot.get("start_sit_needed"):
        if slot.get("start_sit_recorded_at"):
            have.append(f"Start/Sit set recorded {show(utc(slot['start_sit_recorded_at']), tz)}")
        elif fresh_planned:
            have.append(f"Start/Sit set due from the scheduled refresh at {show(fresh_planned[0], tz)}")
        else:
            need.append("a Start/Sit decision set covering this slot's rostered players")
    return {"week": w, "kickoff": _iso(k), "local": show(k, tz), "games": s["games"], "hours_away": round(_hours(now, k), 1),
            "next_scheduled_refresh": _iso(planned[0]) if planned else None,
            "next_scheduled_refresh_local": show(planned[0], tz) if planned else None,
            "have": have, "missing": need}


def schedule_gaps(slots_by_week: Mapping[int, list[dict]], entries, now: datetime, max_age_h: float,
                  tz: tzinfo | None = None, horizon_days: float = 21.0) -> list[dict]:
    """Future kickoff slots (within `horizon_days`) whose latest scheduled refresh is more than `max_age_h` before
    kickoff (or absent): London/Germany morning games before the Sunday run, Thanksgiving and other
    Thursday-afternoon games, Friday/Saturday/Christmas games. Only the pregame guard (or a manual run) gives
    them a fresh capture."""
    out = []
    end = now + timedelta(days=horizon_days)
    for w, ss in slots_by_week.items():
        for s in ss:
            k = s["kickoff"]
            if not k or not (now < k <= end):
                continue
            prior = occurrences(entries, k - timedelta(days=8), k, tz)
            prior = [t for t in prior if t < k]
            last = max(prior) if prior else None
            if last is None or _hours(last, k) > max_age_h:
                out.append({"week": w, "kickoff": _iso(k), "local": show(k, tz), "games": s["games"],
                            "last_scheduled_refresh": show(last, tz) if last else None,
                            "age_h": round(_hours(last, k), 1) if last else None})
    return sorted(out, key=lambda g: g["kickoff"])


# ----------------------------------------------------------------------------- issues


def _fix(ctx: Context, cmd: str) -> str:
    return f"cd {ctx.paths.root}/backend && {cmd}"


def _publish_fix(ctx: Context, step: str | None = None) -> str:
    """The publish command to suggest: the step that ran, else the R2 snapshot when it is configured."""
    if step is None:
        from app.config import get_settings
        step = "publish_snapshot.py" if get_settings().r2_configured else "publish_motherduck.py"
    return _fix(ctx, f"uv run python ../scripts/{step}")


def build_issues(ctx: Context, report: dict) -> list[Issue]:
    """Turn the audit into issues with severities and remediation commands."""
    th, tz, now = ctx.thresholds, ctx.tz, ctx.now
    issues: list[Issue] = []
    add = issues.append
    root = ctx.paths.root
    install = _fix(ctx, "uv run python ../scripts/evidence_health.py launchd install")

    # scheduler
    for j in report["scheduler"]["jobs"]:
        name, required = j["job"], j["job"] in REQUIRED_JOBS
        if j.get("template_error"):
            add(Issue("scheduler_template_invalid", WARN, name, f"template for {j['label']} does not parse: "
                      f"{j['template_error']}", f"plutil -lint {ctx.paths.templates}/{j['label']}.plist"))
        if not j["installed"]:
            if required:
                add(Issue("scheduler_not_installed", FAIL, name, f"launchd job {j['label']} is not installed: "
                          "nothing runs on schedule", f"{install}   # prints the plan; add --apply to install"))
            continue
        if j["valid"] is False:
            add(Issue("scheduler_plist_invalid", FAIL, name, f"{j['plist']} is not a valid plist ({j.get('parse_error')})",
                      f"plutil -lint {j['plist']}; then {install} --apply"))
            continue
        if j["drift"]:
            add(Issue("scheduler_drift", WARN, name, f"installed {j['label']} differs from the template: "
                      + "; ".join(j["drift"]), f"{install}   # shows the change; --apply replaces it (backup kept)"))
        lc = j.get("launchctl") or {}
        if not lc.get("loaded"):
            add(Issue("scheduler_not_loaded", FAIL if required else WARN, name,
                      f"{j['label']} is installed but not loaded in launchd: it will never run",
                      f"launchctl bootstrap gui/{ctx.uid} {j['plist']}"))
        elif lc.get("last_exit_code") not in (None, 0) and not (name == "weekly-learning" and lc["last_exit_code"] == 3):
            add(Issue("scheduler_last_exit", WARN, name, f"{j['label']} last exited {lc['last_exit_code']}",
                      f"tail -n 40 {ctx.paths.logs}/launchd.err.log"))
        if j.get("program_ok") is False:
            add(Issue("scheduler_program_missing", FAIL, name, f"{j['label']}'s program is missing or not executable",
                      f"{install} --apply   # re-renders with `command -v uv`"))
        missed = [o for o in j.get("scheduled", []) if o["status"] == "missed"]
        if missed:
            add(Issue("scheduler_missed_runs", WARN, name,
                      f"{len(missed)} scheduled {name} run(s) left no trace: "
                      + ", ".join(show(utc(o["scheduled_for"]), tz) for o in missed[-4:])
                      + " (the Mac was off or asleep past the grace window, or the job was not loaded)",
                      "pmset -g sched; pmset -g log | grep -E 'Sleep|Wake' | tail -20"))

    # last refresh
    rr = report["refresh"]
    last = rr.get("last")
    if last is None:
        add(Issue("refresh_never_ran", FAIL, "refresh", "no refresh log exists: no ESPN snapshot or evidence was "
                  "ever captured by refresh_all.py", _fix(ctx, "uv run python ../scripts/refresh_all.py")))
    else:
        when = show(utc(last["started_at"]), tz)
        if last["status"] in ("failed", "unfinished", "unreadable"):
            add(Issue(f"refresh_{last['status']}", FAIL, "refresh", f"latest refresh ({when}, {last['log']}) "
                      f"{last['status']}" + (f": {last['failed']}" if last.get("failed") else ""),
                      f"tail -n 40 {ctx.paths.logs}/{last['log']}; then "
                      + _fix(ctx, "uv run python ../scripts/refresh_all.py")))
        done = rr.get("last_finished")
        if done is not None:
            ev = done.get("evidence", {})
            if not ev.get("ran"):
                has_stage = "def run_evidence" in _read(root / "scripts" / "refresh_all.py")
                add(Issue("evidence_stage_not_run", FAIL, "refresh",
                          f"latest finished refresh ({show(utc(done['started_at']), tz)}) synced ESPN/NFL but ran no "
                          "evidence stage, so it captured no Start/Sit set and no ESPN benchmark ("
                          + ("this checkout's refresh_all.py has the stage now, so that run used older code or "
                             "--skip-evidence/--check-only; the next refresh runs it" if has_stage else
                             "this checkout's refresh_all.py predates the evidence stage (PR #72)") + ")",
                          _fix(ctx, "uv run python ../scripts/refresh_all.py   # captures now") if has_stage else
                          f"cd {root} && git pull --ff-only"))
            elif ev.get("problems"):
                pregame_lost = not pregame_capture_ok(done) and any(
                    s["name"] == "start_sit_evaluation.py" for s in ev.get("steps", []))
                add(Issue("evidence_step_failed", FAIL if pregame_lost or not ev.get("steps") else WARN, "refresh",
                          f"refresh {show(utc(done['started_at']), tz)} exited 0 but evidence failed: "
                          + "; ".join(ev["problems"][:3]),
                          f"grep -n 'Evidence' {ctx.paths.logs}/{done['log']}"))
            if done.get("publish", {}).get("ran") and done["publish"].get("exit") not in (0, None):
                step = done["publish"].get("step") or "publish_motherduck.py"
                add(Issue("publish_failed", WARN, "motherduck", f"hosted publish ({step}) exited "
                          f"{done['publish']['exit']} in {done['log']}", _publish_fix(ctx, step)))

    # schema / hosted
    db = report["database"]
    if db.get("error"):
        add(Issue("database_unreadable", FAIL, "database", db["error"], f"ls -la {ctx.paths.db}"))
    elif db["schema_version"] != SCHEMA_VERSION:
        add(Issue("local_schema_old", FAIL, "database", f"local schema v{db['schema_version']}, code expects "
                  f"v{SCHEMA_VERSION}", _fix(ctx, "uv run python ../scripts/compute_analytics.py   # init_db migrates")))
    hosted = report["hosted"]
    last_write = utc(db.get("last_write"))
    if hosted.get("checked"):
        if hosted.get("error"):
            add(Issue("hosted_unreachable", WARN, "motherduck", f"hosted copy not readable: {hosted['error']}"))
        else:
            if hosted.get("schema_version") != db.get("schema_version"):
                add(Issue("hosted_schema_mismatch", FAIL, "motherduck", f"hosted schema v{hosted.get('schema_version')}"
                          f" != local v{db.get('schema_version')}", _publish_fix(ctx)))
            pub = utc(hosted.get("published_at"))
            if last_write and (pub is None or pub < last_write):
                add(Issue("hosted_outdated", WARN, "motherduck", f"hosted copy published {show(pub, tz)}, local data "
                          f"changed {show(last_write, tz)}", _publish_fix(ctx)))
    else:
        pub = utc(hosted.get("last_known_publish"))
        if last_write and (pub is None or pub < last_write):
            add(Issue("hosted_maybe_outdated", INFO, "motherduck", f"last recorded publish {show(pub, tz)} is older "
                      f"than the latest local write {show(last_write, tz)} (a later manual publish is only known "
                      "with --hosted)", _publish_fix(ctx)))

    # weeks
    weeks = report["weeks"]
    played = [w["week"] for w in weeks if w["state"] in ("played", "in_progress")]
    recent = max(played) if played else None
    for w in weeks:
        sev = WARN if recent is not None and w["week"] >= recent - 1 else INFO   # older misses: context only
        by_kind: dict[str, list[dict]] = defaultdict(list)
        for m in w["missing"]:
            by_kind[m["evidence"]].append(m)
        for kind, ms in by_kind.items():
            add(Issue(f"{kind}_missed", sev, f"w{w['week']}",
                      f"week {w['week']}: {len(ms)} kicked-off slot(s) without a pregame {kind.replace('_', ' ')}: "
                      f"{ms[0]['slot']}: {ms[0]['cause']}" + (f" (+{len(ms) - 1} more)" if len(ms) > 1 else ""),
                      None, w["week"]))
        if w["stale_espn"]:
            st = w["stale_espn"]
            add(Issue("espn_snapshot_stale", INFO if sev == INFO else WARN, f"w{w['week']}",
                      f"week {w['week']}: {len(st)} slot(s) whose latest pregame ESPN snapshot is older than "
                      f"{th.capture_max_age_h:g}h at kickoff: " + ", ".join(f"{x['slot']} {x['age_h']:g}h" for x in st[:3]),
                      None, w["week"]))
        for x in w["lost_espn"]:
            add(Issue("espn_pregame_snapshot_lost", sev, f"w{w['week']}:{x['slot']}",
                      f"week {w['week']} {x['slot']}: {x['log']} reported a pregame ESPN snapshot at "
                      f"{show(utc(x['reported']), tz)}, but the newest pregame one stored is "
                      f"{show(utc(x['stored_latest']), tz) if x['stored_latest'] else 'none'}: a later same-day ESPN "
                      "sync replaced it after kickoff (espn_projection_history keeps one row per player per local "
                      "day; the pregame lock in espn/sync.py prevents this since commit 0ca8e30)", None, w["week"]))
        if w["served"].get("fallback") and w["state"] != "played":
            first_k = utc(w["first_kickoff"])
            soon = w["state"] == "in_progress" or (first_k is not None and _hours(now, first_k) <= th.upcoming_warn_h)
            add(Issue("ifp_fallback_served", WARN if soon else INFO, f"w{w['week']}",
                      f"week {w['week']}: the projection switch serves "
                      f"{w['served']['model_version']} ({w['served'].get('reason')})",
                      _fix(ctx, "uv run python ../scripts/learning_cycle.py status   # its `project` stage stores the run"),
                      w["week"]))
        if w["state"] == "played":
            fin = w["finality"]
            if fin.get("fully_final"):
                if not w["benchmark"]["recorded"]:
                    add(Issue("benchmark_not_recorded", WARN, f"w{w['week']}", f"week {w['week']} is fully final but no "
                              "ESPN benchmark scorecard is recorded",
                              _fix(ctx, f"uv run python ../scripts/espn_benchmark.py record --season {ctx.season} "
                                        f"--week {w['week']}"), w["week"]))
            else:
                add(Issue("week_pending_finality", INFO, f"w{w['week']}", f"week {w['week']} not fully final: "
                          f"{fin.get('detail')}; its benchmark waits", _fix(ctx, "uv run python ../scripts/sync_nfl.py"),
                          w["week"]))
        for c in w["start_sit"]["corrupt"] + w["research"]["corrupt"]:
            add(Issue("evidence_file_corrupt", FAIL, c["file"], f"damaged evidence file {c['file']}: {c['error']}",
                      f"mkdir -p {ctx.paths.root}/data/quarantine && mv '{c['file']}' {ctx.paths.root}/data/quarantine/"
                      "   # never edit evidence; the next capture writes a fresh file", w["week"]))
    for c in report["benchmark"]["corrupt"]:
        add(Issue("evidence_file_corrupt", FAIL, c["file"], f"damaged benchmark file {c['file']}: {c['error']}",
                  f"mkdir -p {ctx.paths.root}/data/quarantine && mv '{c['file']}' {ctx.paths.root}/data/quarantine/"))
    for stamp in report["provenance"]["unverified_espn_snapshots"]:
        add(Issue("snapshot_provenance_unverified", WARN, stamp, f"ESPN snapshot stamped {show(utc(stamp), tz)} matches no "
                  "successful ESPN sync run (sync_runs), so its pregame time cannot be verified; do not count it as "
                  "pregame evidence"))

    # next kickoff
    nk = report.get("next_kickoff")
    if nk and nk["missing"]:
        h = nk["hours_away"]
        sev = FAIL if h <= th.upcoming_fail_h else WARN if h <= th.upcoming_warn_h else INFO
        add(Issue("next_kickoff_unready", sev, f"w{nk['week']}:{nk['kickoff']}",
                  f"next kickoff {nk['local']} ({', '.join(nk['games'][:3])}), {h:g}h away, lacks: "
                  + "; ".join(nk["missing"]),
                  _fix(ctx, "uv run python ../scripts/refresh_all.py") + f"   # before {nk['local']}", nk["week"]))
    for g in report["schedule_gaps"]:
        add(Issue("schedule_gap", WARN if ctx.thresholds.upcoming_warn_h >= _hours(now, utc(g["kickoff"])) else INFO,
                  f"w{g['week']}:{g['kickoff']}",
                  f"week {g['week']} kickoff {g['local']} ({', '.join(g['games'])}): last scheduled refresh "
                  + (f"{g['last_scheduled_refresh']} ({g['age_h']:g}h earlier)" if g["last_scheduled_refresh"] else "none")
                  + "; only the pregame guard or a manual run gives it a fresh capture",
                  f"{install} --jobs pregame-guard   # or run refresh_all.py by hand before kickoff", g["week"]))

    # learning cycle
    lc = report.get("learning_cycle")
    if lc:
        st = lc.get("status")
        if st in ("failed", "config_error", "unreadable"):
            add(Issue("learning_cycle_failed", FAIL, "weekly-learning", f"learning cycle {st}: "
                      + "; ".join((lc.get("errors") or [])[:2]), _fix(ctx, "uv run python ../scripts/learning_cycle.py status")))
        elif st == "waiting_for_data":
            add(Issue("learning_cycle_waiting", INFO, "weekly-learning", f"learning cycle waiting for data (target week "
                      f"{lc.get('target_week')}): " + "; ".join(lc.get("required_actions") or [])))

    # storage
    s = report["storage"]
    if s.get("write_ok") is False:
        add(Issue("filesystem_write_failed", FAIL, "storage", f"cannot write under {ctx.paths.logs}: {s['write_error']}",
                  f"ls -ld {ctx.paths.logs}; df -h {ctx.paths.root}"))
    free = s.get("free_gb")
    if free is not None and free < th.disk_warn_gb:
        add(Issue("disk_low", FAIL if free < th.disk_fail_gb else WARN, "storage", f"{free:g} GB free on the data volume",
                  f"du -sh {ctx.paths.root}/data/* | sort -h | tail"))
    return issues


def _read(path: Path) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def overall(issues: Sequence[Issue]) -> str:
    return max((i.severity for i in issues), key=_RANK.get, default=OK) if issues else OK


def assess(con, ctx: Context) -> dict:
    """The whole health report (JSON-ready). `con` is a read-only connection to the live database (or None
    when it could not be opened: the file-based checks still run)."""
    now, tz, th, paths = ctx.now, ctx.tz, ctx.thresholds, ctx.paths
    refresh = refresh_runs(paths.logs, now, th.unfinished_after_min)
    cycles = learning_cycle_runs(paths.weekly)
    jobs = []
    for job in JOBS:
        j = job_status(paths, job, ctx.runner, ctx.uv, ctx.uid)
        starts = [utc(r["started_at"]) for r in (refresh if job == "refresh" else cycles if job == "weekly-learning" else [])]
        j["_since"] = None
        if j["installed"] and j["_entries"]:
            since_cands = [t for t in [utc(j["installed_mtime"]), *starts[:1]] if t is not None]
            j["_since"] = min(since_cands) if since_cands else now       # first trace of the job on this Mac
            since = max(j["_since"], now - timedelta(days=th.lookback_days))
            j["scheduled"] = scheduled_runs(j["_entries"], starts, since, now, th.scheduled_grace_h, tz)
        else:
            j["scheduled"] = []
        last_start = max(starts) if starts else None
        j["last_run_at"], j["last_run_local"] = _iso(last_start), show(last_start, tz) if last_start else None
        jobs.append(j)
    refresh_entries = next(j["_entries"] for j in jobs if j["job"] == "refresh")
    refresh_since = next(j["_since"] for j in jobs if j["job"] == "refresh")
    for j in jobs:
        j.pop("_entries")
        j.pop("_since")
    finished = [r for r in refresh if r["status"] in ("complete", "failed") and r.get("steps")]   # not --check-only
    report: dict[str, Any] = {
        "schema": SCHEMA, "generated_at": _iso(now), "season": ctx.season, "db_note": ctx.db_note,
        "scheduler": {"jobs": jobs, "learning_cycle_runs": cycles[-5:]},
        "refresh": {"last": refresh[-1] if refresh else None, "last_finished": finished[-1] if finished else None,
                    "recent": [{k: r.get(k) for k in ("log", "started_at", "status")} |
                               {"evidence": r.get("evidence", {}).get("status"),
                                "publish": (r.get("publish") or {}).get("published_at")} for r in refresh[-8:]]},
        "storage": storage(paths, th), "learning_cycle": learning_cycle_status(paths.weekly),
        "database": {}, "hosted": {}, "weeks": [], "benchmark": {"cards": [], "corrupt": []},
        "provenance": {"unverified_espn_snapshots": []}, "next_kickoff": None, "schedule_gaps": [],
    }
    pubs = publish_records(paths.logs, refresh)
    report["hosted"] = dict(ctx.hosted or {"checked": False})
    report["hosted"]["last_known_publish"] = pubs[-1]["published_at"] if pubs else None
    if con is None:
        report["database"] = {"error": f"could not open {paths.db} read-only", "schema_version": None}
    else:
        report["database"] = {"path": str(paths.db), "schema_version": schema_version(con), "expected": SCHEMA_VERSION,
                              "last_write": _iso(local_last_write(con))}
        wk = audit_weeks(con, ctx, refresh, refresh_entries, refresh_since)
        report["weeks"], report["benchmark"] = wk["weeks"], wk["benchmark"]
        report["provenance"]["unverified_espn_snapshots"] = unverified_snapshot_stamps(con, ctx.season, th.provenance_slack_s)
        report["next_kickoff"] = next_kickoff(con, ctx, wk["slots_by_week"], refresh, refresh_entries, wk["weeks"])
        report["schedule_gaps"] = schedule_gaps(wk["slots_by_week"], refresh_entries, now, th.capture_max_age_h, tz)
    issues = build_issues(ctx, report)
    issues.sort(key=lambda i: (-_RANK[i.severity], i.week or 0, i.code))
    report["issues"] = [i.to_dict() for i in issues]
    report["status"] = overall(issues)
    report["answers"] = answers(report)
    return report


def answers(report: dict) -> dict:
    """The acceptance questions, one line each."""
    weeks = report["weeks"]
    cur = next((w for w in reversed(weeks) if w["state"] in ("played", "in_progress")), None)
    nxt = report.get("next_kickoff")
    jobs = {j["job"]: j for j in report["scheduler"]["jobs"]}

    def job_line(name: str, what: str) -> str:
        j = jobs.get(name, {})
        lc = j.get("launchctl") or {}
        return (f"{name} {'installed' if j.get('installed') else 'NOT installed'}, "
                f"{'loaded' if lc.get('loaded') else 'NOT loaded'}, last {what} {j.get('last_run_local') or 'never'}")

    lc = report.get("learning_cycle") or {}
    out = {"scheduler": job_line("refresh", "refresh") + "; " + job_line("weekly-learning", "cycle")
                        + (f" ({lc.get('status')})" if lc.get("status") else "")
                        + ("; pregame-guard installed" if jobs.get("pregame-guard", {}).get("installed") else "")}
    if cur:
        miss = Counter(m["evidence"] for m in cur["missing"])
        out["pregame_captures"] = (f"week {cur['week']} ({cur['state']}): " + (
            "every kicked-off slot has every pregame capture" if not miss else
            "missing " + ", ".join(f"{k.replace('_', ' ')} for {n} slot(s)" for k, n in miss.items())))
        prod = cur["production"]["usable_by_position"]
        out["usable_forecasts"] = (f"week {cur['week']} {cur['production']['model_version']} valid pregame projected rows: "
                                   + ", ".join(f"{p} {n}" for p, n in prod.items()))
        out["benchmark"] = ("recorded: " + ", ".join(c["file"] for c in report["benchmark"]["cards"][-2:])
                            if report["benchmark"]["cards"] else "no ESPN benchmark scorecard recorded yet"
                            + (f" (week {cur['week']}: {cur['finality'].get('detail')})" if cur else ""))
        sets = [s for w in weeks for s in w["start_sit"]["sets"]]
        out["start_sit_growth"] = (f"{len(sets)} decision set(s), "
                                   f"{sum(w['start_sit']['effective']['candidates'] for w in weeks)} effective roster rows "
                                   f"across weeks {', '.join(str(w['week']) for w in weeks if w['start_sit']['sets']) or 'none'}")
    served = [(w["week"], w["served"]) for w in weeks if w["state"] != "played"]
    out["ifp2_served"] = "; ".join(
        f"week {wk}: {s.get('model_version') or 'nothing (ESPN shown)'}" + (" (FALLBACK)" if s.get("fallback") else "")
        for wk, s in served) or "no current week"
    fails = [i for i in report["issues"] if i["severity"] == FAIL]
    if nxt:
        out["before_next_kickoff"] = (f"{nxt['local']}: " + ("; ".join(nxt["missing"]) if nxt["missing"] else "ready")
                                      + (f"; also fix: {'; '.join(i['message'] for i in fails[:3])}" if fails else ""))
    return out


# ----------------------------------------------------------------------------- rendering


_MARK = {OK: "OK  ", INFO: "info", WARN: "WARN", FAIL: "FAIL"}


def render(report: dict) -> str:
    """Compact human summary (the JSON has everything)."""
    L = [f"Evidence health {report['season']}: {report['status'].upper()}   ({report['generated_at'][:16]}Z)"]
    if report.get("db_note"):
        L.append(f"  note: {report['db_note']}")
    for k, v in report["answers"].items():
        L.append(f"  {k.replace('_', ' ')}: {v}")
    L.append("Scheduler")
    for j in report["scheduler"]["jobs"]:
        lc = j.get("launchctl") or {}
        if not j["installed"]:
            L.append(f"  {j['job']:<16} not installed" + ("" if j["job"] in REQUIRED_JOBS else " (optional)"))
            continue
        sched = j.get("scheduled") or []
        ran = sum(o["status"] == "ran" for o in sched)
        missed = sum(o["status"] == "missed" for o in sched)
        L.append(f"  {j['job']:<16} {'loaded' if lc.get('loaded') else 'NOT LOADED'}, last exit "
                 f"{lc.get('last_exit_text') or '-'}, last run {j.get('last_run_local') or 'never'}, "
                 + ("template unreadable" if j["matches_template"] is None else
                    "matches template" if j["matches_template"] else "DRIFT")
                 + f"; scheduled {ran} ran / {missed} missed")
    r = report["refresh"]
    if r["last"]:
        ev = r["last_finished"]["evidence"] if r["last_finished"] else {}
        L.append(f"Last refresh {r['last']['log']}: {r['last']['status']}; evidence stage: {ev.get('status', '-')}"
                 + "".join(f"\n  {s['name']}: {s.get('result')}" for s in ev.get("steps", [])))
    db, h = report["database"], report["hosted"]
    L.append(f"Database schema v{db.get('schema_version')} (expects v{db.get('expected', SCHEMA_VERSION)}); hosted: "
             + (f"v{h.get('schema_version')} published {h.get('published_at')}" if h.get("checked") and not h.get("error")
                else f"last recorded publish {h.get('last_known_publish') or 'none'} (not queried)"))
    for w in report["weeks"]:
        sl = w["slots"]
        ko = [s for s in sl if s["kicked_off"]]
        def frac(key):
            return f"{sum(1 for s in ko if s[key])}/{len(ko)}"
        ss_need = [s for s in ko if s["start_sit_needed"]]
        L.append(f"Week {w['week']} [{w['state']}] {len(ko)}/{len(sl)} slots kicked off; "
                 f"ESPN {frac('espn_pregame_snapshot')}, prod {frac('production_pregame_projected')}, "
                 f"research {frac('research_frozen')}, start/sit "
                 f"{sum(1 for s in ss_need if s['start_sit_recorded_at'])}/{len(ss_need)}; "
                 f"served {w['served'].get('model_version') or '-'}{' (fallback)' if w['served'].get('fallback') else ''}; "
                 f"final: {w['finality'].get('detail') or '-'}")
        pos = w["production"]["usable_by_position"]
        L.append(f"  usable {w['production']['model_version']} pregame rows " + " ".join(f"{p}:{n}" for p, n in pos.items())
                 + "; matched " + " ".join(f"{p}:{n}" for p, n in w["benchmark"]["matched_by_position"].items())
                 + f"; start/sit sets {len(w['start_sit']['sets'])}")
    nk = report.get("next_kickoff")
    if nk:
        L.append(f"Next kickoff {nk['local']} week {nk['week']} ({nk['hours_away']:g}h): next scheduled refresh "
                 f"{nk['next_scheduled_refresh_local'] or 'NONE before kickoff'}")
        for x in nk["have"]:
            L.append(f"  have: {x}")
        for x in nk["missing"]:
            L.append(f"  MISSING: {x}")
    if report["issues"]:
        L.append("Issues")
        for i in report["issues"]:
            L.append(f"  [{_MARK[i['severity']]}] {i['code']}: {i['message']}")
            if i.get("fix") and i["severity"] in (WARN, FAIL):
                L.append(f"         fix: {i['fix']}")
    return "\n".join(L)


# ----------------------------------------------------------------------------- alerts (no repeats)


def new_alerts(issues: Sequence[Mapping], state: Mapping[str, str], now: datetime,
               severities: Iterable[str] = (WARN, FAIL)) -> tuple[list[Mapping], dict[str, str]]:
    """(issues to notify now, next state). An issue is notified once while it stays unresolved; the state keeps
    only currently open fingerprints, so an issue that resolves and later returns is notified again."""
    sev = set(severities)
    open_ = {i["fingerprint"]: i for i in issues if i["severity"] in sev}
    fresh = [i for fp, i in open_.items() if fp not in state]
    nxt = {fp: state.get(fp) or _iso(now) for fp in open_}
    return fresh, nxt


def notify_macos(title: str, message: str, runner: Runner = run_command) -> bool:
    """macOS Notification Center via the built-in `osascript` (no dependency, no network)."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    code, _ = runner(["osascript", "-e", f'display notification "{esc(message[:240])}" with title "{esc(title[:80])}"'])
    return code == 0


def read_plist(path: Path) -> dict | None:
    """A plist file parsed, or None when missing or invalid."""
    try:
        return plistlib.loads(Path(path).read_bytes())
    except Exception:
        return None


def read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json_atomic(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str))
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- pregame guard


@dataclass(frozen=True)
class GuardConfig:
    lead_h: float = 6.0              # start trying this long before a kickoff
    margin_min: float = 10.0         # stop trying this close to kickoff (a capture must land before it)
    max_age_h: float = 12.0          # a capture this recent counts as fresh for the slot
    max_attempts: int = 3            # per kickoff slot
    retry_min: float = 25.0          # between attempts
    scheduled_grace_min: float = 45.0  # a scheduled refresh this recent may still be running or about to log


def guard_decision(now: datetime, kickoffs: Sequence[datetime], refresh: Sequence[dict], espn_by_slot: Mapping[str, datetime | None],
                   attempts: Mapping[str, list], cfg: GuardConfig = GuardConfig(),
                   scheduled: Sequence[datetime] = ()) -> dict:
    """Decide (pure) whether the pregame guard should run a refresh now.

    The target is the earliest kickoff more than `margin_min` away. Inside [kickoff - lead_h, kickoff - margin_min]
    it is captured when a refresh that started within max_age_h of kickoff completed with its Start/Sit step
    recorded (or nothing eligible) AND an ESPN snapshot of the slot's teams landed in that span. Other evidence
    steps (the ESPN benchmark of a finished week, the injury report) are not pregame captures: their failures are
    health issues, not a reason to refresh again before kickoff. The guard is a fallback: when a scheduled
    refresh (`scheduled`, the refresh job's calendar firings) lands within max_age_h before kickoff and is still
    ahead (or under scheduled_grace_min ago), it waits for it. Otherwise run, unless a refresh is in progress, the
    slot already used max_attempts, or the last attempt was under retry_min ago. A kickoff that already passed is
    never chased: missed is reported, not repaired."""
    future = sorted(k for k in kickoffs if k - timedelta(minutes=cfg.margin_min) > now)
    if not future:
        return {"action": "idle", "reason": "no kickoff ahead", "slot": None}
    k = future[0]
    slot = _iso(k)
    base = {"slot": slot, "kickoff": slot, "hours_away": round(_hours(now, k), 2)}
    if k - timedelta(hours=cfg.lead_h) > now:
        return {**base, "action": "idle", "reason": f"next kickoff more than {cfg.lead_h:g}h away"}
    since = k - timedelta(hours=cfg.max_age_h)
    good = [r for r in refresh if (s := utc(r["started_at"])) and since <= s < k and r["status"] == "complete"
            and pregame_capture_ok(r)]
    snap = espn_by_slot.get(slot)
    if good and snap is not None and since <= snap < k:
        return {**base, "action": "captured", "reason": f"refresh {good[-1]['log']} recorded Start/Sit and ESPN snapshot "
                f"{_iso(snap)} are within {cfg.max_age_h:g}h of kickoff"}
    if any(r["status"] == "running" for r in refresh):
        return {**base, "action": "wait", "reason": "a refresh is running"}
    due = [p for p in scheduled if since <= p < k - timedelta(minutes=cfg.margin_min)
           and p > now - timedelta(minutes=cfg.scheduled_grace_min)]
    if due:
        return {**base, "action": "wait", "reason": f"the scheduled refresh at {_iso(min(due))} covers this kickoff"}
    tried = attempts.get(slot) or []
    if len(tried) >= cfg.max_attempts:
        return {**base, "action": "gave_up", "reason": f"{len(tried)} attempts for this kickoff already failed to capture"}
    if tried and now - utc(tried[-1]["at"]) < timedelta(minutes=cfg.retry_min):
        return {**base, "action": "wait", "reason": f"last attempt under {cfg.retry_min:g} min ago"}
    why = ("no completed refresh with its Start/Sit step recorded" if not good else "no ESPN snapshot of these teams") + \
          f" within {cfg.max_age_h:g}h of kickoff"
    return {**base, "action": "run", "reason": why, "attempt": len(tried) + 1}


@dataclass(frozen=True)
class CatchupConfig:
    grace_min: float = 15.0          # a firing this recent may still be about to write its log
    lookback_d: float = 7.0          # only the latest firing within this window is caught up
    max_attempts: int = 3            # per missed firing
    retry_min: float = 25.0          # between attempts


def catchup_decision(now: datetime, firings: Sequence[datetime], refresh: Sequence[dict], attempts: Mapping[str, list],
                     cfg: CatchupConfig = CatchupConfig(), job_running: bool = False) -> dict:
    """Decide (pure) whether a scheduled refresh the Mac missed should run now.

    The slot is the latest firing of the refresh job's calendar (`firings`) at least grace_min ago and within
    lookback_d. It is covered once a refresh that started at or after it completed (a manual run counts). A firing
    missed while the Mac was shut down, or a run that failed or was cut short, is run again: at most max_attempts
    per firing, retry_min apart, never while a refresh is running: a running log, or `job_running` (launchd reports
    the refresh job running, e.g. the calendar firing it replays on wake before its log exists). Only the latest
    firing is chased: a week the Mac was off costs one refresh, not one per missed firing."""
    past = [p for p in firings if now - timedelta(days=cfg.lookback_d) < p <= now - timedelta(minutes=cfg.grace_min)]
    if not past:
        return {"action": "idle", "reason": "no scheduled refresh firing to catch up", "slot": None}
    p = max(past)
    base = {"slot": _iso(p)}
    after = [r for r in refresh if (s := utc(r["started_at"])) and s >= p]
    if any(r["status"] == "complete" for r in after):
        done = next(r for r in after if r["status"] == "complete")
        return {**base, "action": "covered", "reason": f"refresh {done['log']} completed after the {_iso(p)} firing"}
    if job_running or any(r["status"] == "running" for r in refresh):
        return {**base, "action": "wait", "reason": "a refresh is running"}
    tried = attempts.get(base["slot"]) or []
    if len(tried) >= cfg.max_attempts:
        return {**base, "action": "gave_up", "reason": f"{len(tried)} catch-up attempts for this firing already failed"}
    if tried and now - utc(tried[-1]["at"]) < timedelta(minutes=cfg.retry_min):
        return {**base, "action": "wait", "reason": f"last attempt under {cfg.retry_min:g} min ago"}
    why = ("no refresh ran after the scheduled firing" if not after
           else f"the refresh after the scheduled firing is {after[-1]['status']}")
    return {**base, "action": "run", "reason": why, "attempt": len(tried) + 1}


def pregame_capture_ok(run: Mapping) -> bool:
    """Did this refresh's evidence stage record the pregame Start/Sit set (or find nothing eligible)?"""
    steps = (run.get("evidence") or {}).get("steps") or []
    return any(s["name"] == "start_sit_evaluation.py" and s.get("result") in ("ok", "nothing_eligible") for s in steps)


def espn_latest_by_slot(con, season: int, now: datetime) -> tuple[list[datetime], dict[str, datetime | None]]:
    """(future kickoffs, {slot iso: latest provenance-verified ESPN snapshot of the slot's teams before kickoff}).
    An unverified stamp never counts, so it cannot make the guard skip a needed refresh."""
    out, kicks = {}, []
    for w, slots in kickoff_slots(con, season).items():
        snaps = None
        for s in slots:
            if not s["kickoff"] or s["kickoff"] <= now:
                continue
            snaps = snaps or espn_snapshots(con, season, w)
            kicks.append(s["kickoff"])
            out[_iso(s["kickoff"])] = _latest_before([t for team in s["teams"] for t in snaps["by_team"].get(team, [])],
                                                     s["kickoff"])
    return kicks, out
