"""Promote-when-proven watcher for the prospective research shadows (read-only; see docs/shadow_promotion.md).

    cd backend
    uv run python ../scripts/shadow_promotion_check.py check              # summary JSON, log line, promotion queue
    uv run python ../scripts/shadow_promotion_check.py check --dry-run    # print the summary only, write nothing
    uv run python ../scripts/shadow_promotion_check.py queue              # the promotion queue
    uv run python ../scripts/shadow_promotion_check.py plan ifp-fr-p1     # the brief for folding a candidate into Ours

Every prospective shadow freezes write-once pregame ledgers from 2026 week 7 and, once a week is fully final, scores
them against its pre-registered gate and saves the result (its own `cycle` / `score` / `prospective` command). This
script only READS those saved results; it never re-scores a ledger or re-implements a gate:

  ifp-ctx-p1      ctx_shadow.py                   <ifp-dir>/research/context/shadow/scores.json (+ prospective.json)
  ifp-ctx-p2q     qb_scenario_shadow.py           <ifp-dir>/research/qb_scenario_shadow/prospective.json
  ipm-cal-0.1.0   participation_cal_shadow.py     <ifp-dir>/research/participation_cal_shadow/scores.json
  dfp-r1          dst_shadow.py                   <dst-dir>/shadow/scores.json
  ifpd-0.1.0      distribution_ledger.py          <dist-dir>/research/point_distributions/ledger/scores.json
  ifp-iwh-0.1.1   injury_workload_research.py     <ifp-dir>/research/injury_workload/prospective_ifp-iwh-0.1.1.json
  ifp-fr-p1, ifp-dct-p1, ifp-awd-p1
                  signal_shadows.py               <ifp-dir>/research/signal_shadows/scores.json
  ifp-irs-p1, ifp-irb-p1, ifp-irsb-p1 (both together; ready instead of the singles when the joint rule picks it)
                  injury_return_shadow.py         <ifp-dir>/research/injury_return_shadow/scores.json

Per candidate and position it reports the weeks scored, the matched appearances, the shadow's own gate status and
checks, and `ready_to_promote` when the shadow's gate passes, every one of its checks passes and at least MIN_WEEKS
fully final weeks are scored (a fail-closed floor for the one gate without a week count, the distribution ledger).
A missing or unreadable result is `no_evidence`, never a pass.

When a candidate is ready, `check` writes "PROMOTION READY: <candidate> <positions>" to stderr (the learning cycle's
log), appends it to <logs-dir>/shadow-promotion.log and records it in <logs-dir>/promotion-queue.json. It also
replaces <logs-dir>/shadow-promotion-latest.json with the summary. That is all it writes: it never edits production
code, the model registry, a ledger or a database. Folding a candidate into Ours is a reviewed pull request
(`plan` prints the brief; docs/shadow_promotion.md). A candidate already folded into production is listed in PROMOTED;
its passing gate is then reported as a confirmation, not as a new promotion.

learning_cycle.py runs `check` as its last non-fatal research stage (skip with --no-promotion-check).

Exit codes: 0 ok (whether or not a candidate is ready); 3 ok but a shadow's result could not be read; 1 failed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]           # fantasy-intel/
SCHEMA = "shadow-promotion-check/v1"
QUEUE_SCHEMA = "shadow-promotion-queue/v1"
MIN_WEEKS = 8           # challenger.SHADOW_GATE["min_prospective_weeks"], the repo's shadow-promotion minimum
EXIT_OK, EXIT_FAILED, EXIT_WARNINGS = 0, 1, 3
READY, CONFIRMED, FAILS, ACCUMULATING, NO_EVIDENCE = (
    "ready_to_promote", "confirmed_in_production", "fails_gate", "accumulating", "no_evidence")

# Candidates already folded into a production version: {candidate: {"version": "ifp-2.x.0", "positions": [...]}}.
# The promotion pull request adds its candidate here, so later passing gates read as confirmations.
PROMOTED: dict[str, dict] = {
    # ifp-2.2.0 by owner override (app/analytics/context_model.SHIPPED; docs/research/ifp22_promotion.md). ipm-cal-0.1.0
    # was left out by its backtest guard and ifp-fr-p1 waits on the FTN licensing decision: neither is listed.
    "ifp-ctx-p2q": {"version": "ifp-2.2.0", "positions": ["QB"]},
    "ifp-ctx-p1": {"version": "ifp-2.2.0", "positions": ["RB", "WR"]},
    "ifp-dct-p1": {"version": "ifp-2.2.0", "positions": ["QB", "RB", "WR"]},
    "ifp-awd-p1": {"version": "ifp-2.2.0", "positions": ["RB", "WR", "TE"]},
}


def _path(env: str, default: str) -> Path:
    raw = Path(os.getenv(env) or default)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------------- readers (one per shadow's saved result)
# Each reader takes the loaded JSON files of its shadow and returns (positions, weeks_scored): positions maps a
# position to {"weeks", "appearances", "gate_status", "checks"} exactly as the shadow recorded them.


def _bool_checks(checks) -> dict[str, bool]:
    return {str(k): bool(v) for k, v in (checks or {}).items()}


def read_ctx(files: dict) -> tuple[dict, int | None]:
    pro = (files["scores"] or {}).get("prospective") or {}
    report = (files.get("report") or {}).get("report") or {}
    weeks = pro.get("scored_weeks")
    out = {}
    for pos, g in (pro.get("gate") or {}).items():
        out[pos] = {"weeks": weeks, "appearances": (report.get(pos) or {}).get("n"),
                    "gate_status": g.get("status"), "checks": _bool_checks(g.get("checks"))}
    return out, weeks


def read_qb_scenario(files: dict) -> tuple[dict, int | None]:
    d = files["scores"] or {}
    weeks, mw, report = d.get("scored_weeks"), d.get("matched_weeks") or {}, d.get("report") or {}
    out = {}
    for pos, g in (d.get("gate") or {}).items():
        out[pos] = {"weeks": mw.get(pos, weeks), "appearances": (report.get(pos) or {}).get("n"),
                    "gate_status": g.get("status"), "checks": _bool_checks(g.get("checks"))}
    return out, weeks


def read_ipm_cal(files: dict) -> tuple[dict, int | None]:
    d = files["scores"] or {}
    g = d.get("gate") or {}
    return {"QB": {"weeks": d.get("weeks"), "appearances": d.get("n"), "gate_status": g.get("status"),
                   "checks": _bool_checks(g.get("checks"))}}, d.get("weeks")


def read_dst(files: dict) -> tuple[dict, int | None]:
    d = files["scores"] or {}
    g = d.get("gate") or {}
    c = (g.get("candidates") or {}).get("dfp-r1")
    weeks = d.get("scored_weeks", g.get("weeks"))
    if c is None:
        return {}, weeks
    return {"DST": {"weeks": g.get("weeks", weeks), "appearances": g.get("n"), "gate_status": c.get("status"),
                    "checks": _bool_checks(c.get("checks"))}}, weeks


DIST_PRIMARY_MEAN = "ifp-1.0.0"      # distribution_ledger.PRIMARY_MEAN: the mean the ranges were validated on


def read_dist(files: dict) -> tuple[dict, int | None]:
    d = files["scores"] or {}
    weeks = len(d.get("weeks_scored") or [])
    out = {}
    for pos, e in (((d.get("by_mean") or {}).get(DIST_PRIMARY_MEAN) or {}).get("positions") or {}).items():
        policy, cond, unc = e.get("policy") or {}, e.get("conditional") or {}, e.get("unconditional") or {}
        bounds = [(cond.get("bounds") or {}).get("status")]
        checks = {"position_not_restricted": policy.get("conditional") != "restricted",
                  "conditional_within_bounds": bounds[0] == "within_bounds"}
        if policy.get("unconditional") == "candidate":
            bounds.append((unc.get("bounds") or {}).get("status"))
            checks["unconditional_within_bounds"] = bounds[-1] == "within_bounds"
        status = ("within_bounds" if all(checks.values()) else "insufficient_data"
                  if "insufficient_data" in bounds or None in bounds else "outside_bounds")
        out[pos] = {"weeks": cond.get("weeks", weeks), "appearances": cond.get("n"), "gate_status": status,
                    "checks": checks}
    return out, weeks


def read_iwh(files: dict) -> tuple[dict, int | None]:
    g = (files["scores"] or {}).get("gate") or {}
    pooled = _bool_checks(g.get("pooled_checks"))
    out = {}
    for pos, v in (g.get("per_position") or {}).items():      # the outcome is joint over the adopted positions
        out[pos] = {"weeks": g.get("weeks"), "appearances": v.get("n"), "gate_status": g.get("outcome"),
                    "checks": {**_bool_checks(v.get("checks")), **{f"pooled_{k}": b for k, b in pooled.items()}}}
    return out, g.get("weeks")


def read_signal(name: str):
    def read(files: dict) -> tuple[dict, int | None]:
        d = files["scores"] or {}
        weeks = len(d.get("scored_weeks") or [])
        c = (d.get("candidates") or {}).get(name) or {}
        rep = (c.get("report") or {}).get("positions") or {}
        out = {}
        for pos, g in (c.get("gate") or {}).items():
            out[pos] = {"weeks": (rep.get(pos) or {}).get("weeks"), "appearances": (rep.get(pos) or {}).get("n"),
                        "gate_status": g.get("status"),
                        "checks": {cr["name"]: cr.get("result") == "pass" for cr in g.get("criteria") or []}}
        return out, weeks
    return read


IR_SINGLES = ("ifp-irs-p1", "ifp-irb-p1")


def read_injury_return(name: str):
    def read(files: dict) -> tuple[dict, int | None]:
        d = files["scores"] or {}
        weeks = len(d.get("scored_weeks") or [])
        c = (d.get("candidates") or {}).get(name) or {}
        g = c.get("gate") or {}
        # When both candidates pass, the shadow's joint rule (scores.json both.choice, frozen write-once at the second
        # decision) picks ONE implementation: this candidate alone, or the combination (ifp-irsb-p1, its own entry).
        # A passing single the choice did not select is never ready.
        choice = (d.get("both") or {}).get("choice")
        checks = _bool_checks(g.get("checks"))
        if choice is not None:
            checks["selected_by_joint_rule"] = choice.get("ship") == name
        out = {}
        for pos, v in (c.get("positions") or {}).items():      # one look, joint over RB/WR/TE (pooled cohort)
            out[pos] = {"weeks": g.get("weeks"), "appearances": v.get("n"), "gate_status": g.get("status"),
                        "checks": dict(checks)}
        return out, weeks
    return read


def read_injury_return_both(files: dict) -> tuple[dict, int | None]:
    """ifp-irsb-p1: both injury-return columns shipped together, ready only when both singles pass AND the frozen
    joint choice is "both" (then neither single is ready: one combined promotion, never two plans)."""
    d = files["scores"] or {}
    weeks = len(d.get("scored_weeks") or [])
    b = d.get("both") or {}
    gates = [((d.get("candidates") or {}).get(n) or {}).get("gate") or {} for n in IR_SINGLES]
    statuses = [g.get("status") for g in gates]
    choice = b.get("choice")
    if choice is not None:
        status = "passes" if choice.get("ship") == "both" else "fails"
    elif "fails" in statuses:
        status = "fails"
    else:
        status = "accumulating"
    checks = {"both_candidates_pass": all(x == "passes" for x in statuses),
              "joint_rule_selects_both": choice is not None and choice.get("ship") == "both"}
    gw = [g.get("weeks") for g in gates if g.get("weeks") is not None]
    out = {pos: {"weeks": min(gw) if gw else None, "appearances": v.get("n"), "gate_status": status,
                 "checks": dict(checks)} for pos, v in (b.get("positions") or {}).items()}
    return out, weeks


# ----------------------------------------------------------------------------- the shadows


def _ifp(a, *p) -> Path:
    return Path(a.ifp_dir).joinpath("research", *p)


# target: what a pass changes. "ours" = the next ifp-2.x production version; "dst" = the D/ST production model;
# "distributions" = percentile ranges shown in the product (a separate product approval).
SHADOWS = (
    {"candidate": "ifp-ctx-p1", "shadow": "ctx_shadow.py", "reader": read_ctx, "pass": "meets_gate",
     "files": {"scores": lambda a: _ifp(a, "context", "shadow", "scores.json"),
               "report": lambda a: _ifp(a, "context", "prospective.json")},
     "target": "ours", "doc": "docs/research/ifp_ctx_shadow_automation.md",
     "implementation": "app/analytics/context_challenger.py (frozen PROSPECTIVE_SPEC)"},
    {"candidate": "ifp-ctx-p2q", "shadow": "qb_scenario_shadow.py", "reader": read_qb_scenario, "pass": "meets_gate",
     "files": {"scores": lambda a: _ifp(a, "qb_scenario_shadow", "prospective.json")},
     "target": "ours", "doc": "docs/research/qb_scenario_mixture.md",
     "implementation": "scripts/qb_scenarios.py + qb_scenario_shadow.py (frozen_selection_ifp-ctx-p2.json)"},
    {"candidate": "ipm-cal-0.1.0", "shadow": "participation_cal_shadow.py", "reader": read_ipm_cal,
     "pass": "meets_gate",
     "files": {"scores": lambda a: _ifp(a, "participation_cal_shadow", "scores.json")},
     "target": "ours", "doc": "docs/research/participation_calibration.md",
     "implementation": "app/analytics/participation_calibration.py (QB isotonic map on top of ipm-1.0.0)"},
    {"candidate": "dfp-r1", "shadow": "dst_shadow.py", "reader": read_dst, "pass": "passes",
     "files": {"scores": lambda a: Path(a.dst_dir) / "shadow" / "scores.json"},
     "target": "dst", "doc": "docs/research/dst_projection_model.md",
     "implementation": "the FROZEN D/ST config in scripts/dst_shadow.py"},
    {"candidate": "ifpd-0.1.0", "shadow": "distribution_ledger.py", "reader": read_dist, "pass": "within_bounds",
     "files": {"scores": lambda a: Path(a.dist_dir) / "research" / "point_distributions" / "ledger" / "scores.json"},
     "target": "distributions", "doc": "docs/research/fantasy_point_distributions.md",
     "implementation": "app/analytics/point_distributions.py (the pinned research artifact)"},
    {"candidate": "ifp-iwh-0.1.1", "shadow": "injury_workload_research.py", "reader": read_iwh, "pass": "meets_gate",
     "files": {"scores": lambda a: _ifp(a, "injury_workload", "prospective_ifp-iwh-0.1.1.json")},
     "target": "ours", "doc": "docs/research/injury_workload_handoff.md",
     "implementation": "app/analytics/injury_workload.py + injury_challenger.py"},
    *({"candidate": name, "shadow": "signal_shadows.py", "reader": read_signal(name), "pass": "eligible_for_review",
       "files": {"scores": lambda a: _ifp(a, "signal_shadows", "scores.json")},
       "target": "ours", "doc": "docs/research/signal_shadows.md", "implementation": impl}
      for name, impl in (("ifp-fr-p1", "app/analytics/usage_signals.py (FTN first-read shares)"),
                         ("ifp-dct-p1", "app/analytics/usage_signals.py (pregame depth-chart tier)"),
                         ("ifp-awd-p1", "app/analytics/adaptive_workload.py (decline-only adaptive role)"))),
    *({"candidate": name, "shadow": "injury_return_shadow.py", "reader": read_injury_return(name), "pass": "passes",
       "files": {"scores": lambda a: _ifp(a, "injury_return_shadow", "scores.json")},
       "target": "ours", "doc": "docs/research/injury_return_split.md", "implementation": impl}
      for name, impl in (("ifp-irs-p1", "scripts/injury_return_shadow.py (snap_trend_ret: returners' own snap_trend "
                                        "slope, RB/WR/TE)"),
                         ("ifp-irb-p1", "scripts/injury_return_shadow.py (ret_ind: position x returner indicator, "
                                        "RB/WR/TE)"))),
    {"candidate": "ifp-irsb-p1", "shadow": "injury_return_shadow.py", "reader": read_injury_return_both,
     "pass": "passes", "files": {"scores": lambda a: _ifp(a, "injury_return_shadow", "scores.json")},
     "target": "ours", "doc": "docs/research/injury_return_split.md",
     "implementation": "scripts/injury_return_shadow.py (snap_trend_ret AND ret_ind together, RB/WR/TE: one change)"},
)


def production_version() -> str | None:
    """projection_source.MODEL_VERSION, read from the source (no app import)."""
    src = REPO_ROOT / "backend" / "app" / "analytics" / "projection_source.py"
    m = re.search(r'^MODEL_VERSION\s*=\s*"([^"]+)"', src.read_text(), re.M) if src.exists() else None
    return m.group(1) if m else None


def next_version(current: str | None) -> str | None:
    """ifp-2.1.0 -> ifp-2.2.0: the next ifp-2.x production version."""
    m = re.fullmatch(r"ifp-(\d+)\.(\d+)\.(\d+)", current or "")
    return f"ifp-{m.group(1)}.{int(m.group(2)) + 1}.0" if m else None


# ----------------------------------------------------------------------------- the check


def judge(pos: dict, pass_status: str) -> dict:
    """One position: the shadow's own gate status and checks, plus the MIN_WEEKS floor."""
    checks = dict(pos.get("checks") or {})
    weeks = pos.get("weeks") or 0
    failing = sorted(k for k, ok in checks.items() if not ok)
    if weeks < MIN_WEEKS:
        failing.append(f"min_weeks ({weeks} of {MIN_WEEKS})")
    ok = pos.get("gate_status") == pass_status and bool(checks) and not failing
    if ok:
        status = READY
    elif weeks < MIN_WEEKS or pos.get("gate_status") in ("insufficient_data", "insufficient_evidence", "accumulating"):
        status = ACCUMULATING
    else:
        status = FAILS
    return {"weeks_scored": pos.get("weeks"), "appearances": pos.get("appearances"),
            "gate_status": pos.get("gate_status"), "checks": checks, "failing_checks": failing,
            "ready_to_promote": ok, "status": status}


def check_candidate(spec: dict, args) -> dict:
    paths = {k: f(args) for k, f in spec["files"].items()}
    out = {"candidate": spec["candidate"], "shadow": spec["shadow"], "target": spec["target"],
           "evidence": {k: str(p) for k, p in paths.items()}, "scored_at": None, "weeks_scored": None,
           "positions": {}, "ready_positions": [], "status": NO_EVIDENCE, "warnings": []}
    if not paths["scores"].exists():
        out["notes"] = [f"no scored result yet ({paths['scores'].name}): no week of this shadow is final and scored"]
        return out
    files = {}
    for k, p in paths.items():
        try:
            files[k] = json.loads(p.read_text()) if p.exists() else None
        except (OSError, ValueError) as err:
            files[k] = None
            out["warnings"].append(f"{spec['candidate']}: could not read {p}: {err}")
    if files["scores"] is None:
        return out
    try:
        positions, weeks = spec["reader"](files)
    except Exception as err:          # noqa: BLE001  an unexpected shape is reported, never a pass
        out["warnings"].append(f"{spec['candidate']}: unexpected result shape in {paths['scores'].name}: {err}")
        return out
    out["scored_at"] = files["scores"].get("scored_at")
    out["weeks_scored"] = weeks
    out["positions"] = {p: judge(v, spec["pass"]) for p, v in sorted(positions.items())}
    out["ready_positions"] = [p for p, v in out["positions"].items() if v["ready_to_promote"]]
    promoted = PROMOTED.get(spec["candidate"])
    if promoted:
        out["promoted"] = promoted
    if out["ready_positions"]:
        out["status"] = CONFIRMED if promoted else READY
    elif any(v["status"] == FAILS for v in out["positions"].values()):
        out["status"] = FAILS
    else:
        out["status"] = ACCUMULATING
    return out


def ready_line(c: dict, nxt: str | None) -> str:
    pos = " ".join(c["ready_positions"])
    if c["status"] == FAILS:          # a candidate already in production whose gate does not pass
        failing = " ".join(p for p, v in c["positions"].items() if v["status"] == FAILS)
        return (f"PROMOTION REVIEW: {c['candidate']} {failing} fails its gate but is in "
                f"{c['promoted'].get('version')} ({c['weeks_scored']} weeks scored)")
    if c["status"] == CONFIRMED:
        return (f"PROMOTION CONFIRMED: {c['candidate']} {pos} (already in {c['promoted'].get('version')}; "
                f"{c['weeks_scored']} weeks scored)")
    into = {"ours": f"next Ours version {nxt}", "dst": "the D/ST production model",
            "distributions": "the product's point ranges"}[c["target"]]
    return (f"PROMOTION READY: {c['candidate']} {pos} ({c['weeks_scored']} weeks scored; into {into}; "
            f"brief: uv run python ../scripts/shadow_promotion_check.py plan {c['candidate']})")


def run_check(args, now: datetime | None = None) -> dict:
    now = now or _utcnow()
    current = production_version()
    nxt = next_version(current)
    cands = [check_candidate(s, args) for s in SHADOWS]
    summary = {"schema": SCHEMA, "checked_at": now.isoformat(), "dry_run": not args.write, "min_weeks": MIN_WEEKS,
               "production_version": current, "next_version": nxt, "candidates": cands,
               "ready": [{"candidate": c["candidate"], "positions": c["ready_positions"], "target": c["target"]}
                         for c in cands if c["status"] == READY],
               "confirmed": [{"candidate": c["candidate"], "positions": c["ready_positions"]}
                             for c in cands if c["status"] == CONFIRMED],
               "counts": {s: sum(c["status"] == s for c in cands)
                          for s in (READY, CONFIRMED, FAILS, ACCUMULATING, NO_EVIDENCE)},
               "lines": [ready_line(c, nxt) for c in cands
                         if c["status"] in (READY, CONFIRMED) or (c["status"] == FAILS and "promoted" in c)],
               "warnings": [w for c in cands for w in c["warnings"]]}
    if args.write:
        write_outputs(args, summary, now)
    return summary


def _replace_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


def load_queue(logs_dir: Path) -> dict:
    p = Path(logs_dir) / "promotion-queue.json"
    try:
        q = json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        q = {}
    return {"schema": QUEUE_SCHEMA, "updated_at": q.get("updated_at"), "entries": q.get("entries") or {}}


def update_queue(queue: dict, summary: dict, now: datetime) -> dict:
    """A ready candidate is added (or refreshed); an entry whose candidate no longer passes is kept and marked
    no_longer_ready; a candidate listed in PROMOTED is marked promoted. Nothing is ever dropped."""
    entries = queue["entries"]
    by = {c["candidate"]: c for c in summary["candidates"]}
    for c in summary["candidates"]:
        if c["status"] != READY:
            continue
        e = entries.get(c["candidate"]) or {"first_ready_at": now.isoformat()}
        e.update(candidate=c["candidate"], status="ready", positions=c["ready_positions"], target=c["target"],
                 weeks_scored=c["weeks_scored"], last_ready_at=now.isoformat(), evidence=c["evidence"],
                 next_version=summary["next_version"] if c["target"] == "ours" else None,
                 plan=f"uv run python ../scripts/shadow_promotion_check.py plan {c['candidate']}")
        entries[c["candidate"]] = e
    for name, e in entries.items():
        if name in PROMOTED:
            e.update(status="promoted", promoted=PROMOTED[name])
        elif e.get("status") == "ready" and (by.get(name) or {}).get("status") != READY:
            e.update(status="no_longer_ready", checked_at=now.isoformat())
    queue["updated_at"] = now.isoformat()
    return queue


def write_outputs(args, summary: dict, now: datetime) -> None:
    logs = Path(args.logs_dir)
    _replace_json(logs / "shadow-promotion-latest.json", summary)
    _replace_json(logs / "promotion-queue.json", update_queue(load_queue(logs), summary, now))
    c = summary["counts"]
    lines = summary["lines"] or [f"no candidate ready ({c[ACCUMULATING]} accumulating, {c[FAILS]} failing, "
                                 f"{c[NO_EVIDENCE]} without a scored week)"]
    with open(logs / "shadow-promotion.log", "a", encoding="utf-8") as log:
        log.writelines(f"{now.isoformat()} {line}\n" for line in lines)


# ----------------------------------------------------------------------------- plan


STEPS = {
    "ours": [
        "git fetch origin && git switch -c feat/{next}-{candidate} origin/main",
        "Confirm the evidence: `uv run python ../scripts/shadow_promotion_check.py check --dry-run` lists {candidate} "
        "as ready_to_promote for {positions}; read the evidence file(s) above.",
        "Build {next} = production {current} + {candidate}'s frozen change on {positions} only (every other position "
        "keeps {current}'s spec), fitted on {current}'s exact training rows with its frozen penalties, as "
        "app/analytics/injury_model.py did for ifp-2.1.0. Source of the frozen change: {implementation}.",
        "Register it like ifp-2.1.0 (docs/model_registry.md): model_registry.json entry approved_production citing the "
        "shadow evidence and a promotion event; projection_source MODEL_VERSION = {next}, FALLBACK_VERSION = "
        "{current}; production_pin.REGISTERED_MODEL_VERSIONS; weekly_learning.train_production and the "
        "learning_cycle `project` default.",
        "Add {candidate} to PROMOTED in scripts/shadow_promotion_check.py "
        "({{\"version\": \"{next}\", \"positions\": [...]}}).",
        "Tests: a new tests/test_<module>.py for the model; update version strings in the existing suites; "
        "`uv run pytest -q` green; regenerate frontend fixtures that carry the version; lint, typecheck, unit and E2E.",
        "Open the PR with gh pr create, wait for the Codex review and fix its findings, then hand it to the merge "
        "coordinator. Train and project {next} on the real data only after the PR merges (backups first).",
    ],
    "dst": [
        "git fetch origin && git switch -c feat/{candidate}-production origin/main",
        "Confirm the evidence with `check --dry-run` and read {doc}.",
        "Serve {candidate} as the D/ST production model ({implementation}); keep kickers on kfp and Ours on {current}.",
        "Add {candidate} to PROMOTED in scripts/shadow_promotion_check.py.",
        "Full test suite, PR, Codex review, merge coordinator.",
    ],
    "distributions": [
        "git fetch origin && git switch -c feat/{candidate}-ranges origin/main",
        "Confirm the evidence with `check --dry-run` and read {doc} (the ranges for {positions} meet the "
        "pre-registered validation bounds on prospective weeks).",
        "Show the {positions} point ranges in the product from the pinned artifact ({implementation}); QB stays "
        "restricted.",
        "Add {candidate} to PROMOTED in scripts/shadow_promotion_check.py.",
        "Full test suite, PR, Codex review, merge coordinator.",
    ],
}


def cmd_plan(args) -> dict:
    spec = next((s for s in SHADOWS if s["candidate"] == args.candidate), None)
    if spec is None:
        raise SystemExit(f"unknown candidate {args.candidate!r}; one of: " + ", ".join(s["candidate"] for s in SHADOWS))
    c = check_candidate(spec, args)
    current = production_version()
    nxt = next_version(current)
    positions = " ".join(c["ready_positions"]) or "the ready positions"
    fmt = {"candidate": spec["candidate"], "positions": positions, "current": current, "next": nxt,
           "doc": spec["doc"], "implementation": spec["implementation"]}
    return {"candidate": spec["candidate"], "status": c["status"], "ready_positions": c["ready_positions"],
            "target": spec["target"], "production_version": current,
            "next_version": nxt if spec["target"] == "ours" else None, "evidence": c["evidence"],
            "positions": c["positions"], "research_doc": spec["doc"], "runbook": "docs/shadow_promotion.md",
            "steps": [s.format(**fmt) for s in STEPS[spec["target"]]],
            **({"note": "not ready_to_promote: these steps apply once `check` reports it ready"}
               if c["status"] != READY else {})}


# ----------------------------------------------------------------------------- CLI


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "queue", "plan"])
    ap.add_argument("candidate", nargs="?", help="plan: the candidate (e.g. ifp-fr-p1)")
    ap.add_argument("--ifp-dir", type=Path, default=_path("IFP_MODEL_DIR", "data/models"),
                    help="model directory holding research/ (the ifp shadows)")
    ap.add_argument("--dst-dir", type=Path, default=_path("DST_RESEARCH_DIR", "data/models/dst"),
                    help="D/ST research directory holding shadow/")
    ap.add_argument("--dist-dir", type=Path, default=None,
                    help="model directory holding research/point_distributions/ (default: --ifp-dir)")
    ap.add_argument("--logs-dir", type=Path, default=_path("SHADOW_PROMOTION_LOG_DIR", "data/logs"),
                    help="where the summary, the log and promotion-queue.json are written")
    ap.add_argument("--now", help="ISO time to stamp the check with; implies --dry-run (tests and audits)")
    ap.add_argument("--dry-run", action="store_true", help="print the summary only: write nothing")
    args = ap.parse_args(argv)
    if args.command == "plan" and not args.candidate:
        ap.error("plan needs a candidate")
    args.dist_dir = args.dist_dir or args.ifp_dir
    args.write = not (args.dry_run or args.now)
    return args


def main(argv=None) -> int:
    args = parse(argv)
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else _utcnow()
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    try:
        if args.command == "queue":
            out = load_queue(args.logs_dir)
        elif args.command == "plan":
            out = cmd_plan(args)
        else:
            out = run_check(args, now)
    except SystemExit:
        raise
    except Exception as err:          # noqa: BLE001
        print(json.dumps({"status": "failed", "error": f"{err.__class__.__name__}: {err}"}, indent=1))
        return EXIT_FAILED
    print(json.dumps(out, indent=1, default=str))
    for line in out.get("lines") or []:
        print(line, file=sys.stderr)
    for w in out.get("warnings") or []:
        print(f"WARNING: shadow promotion check: {w}", file=sys.stderr)
    return EXIT_WARNINGS if out.get("warnings") else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
