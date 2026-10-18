"""Prospective re-test of the QB isotonic participation map `ipm-cal-0.1.0` (research only; see
docs/research/participation_calibration.md, "Prospective re-test").

The calibration study kept ipm-1.0.0 in production and the QB isotonic map as a shadow to re-test on new weeks.
This script freezes that map once (a write-once copy of the shadow artifact, bound to its sha256, the base
participation version and the QB knots) and, every week, applies it to the P(appears) that PRODUCTION froze
before kickoff: the `p_play` of each player's latest weekly-ledger pregame row created before his kickoff
(weekly_learning.frozen_rows), made by ipm-1.0.0. The map is a fixed 1-D function of that frozen probability, so the calibrated probability is
as pregame as the base one; nothing here re-forecasts. Once a week is fully final, both are scored against who
appeared (weekly_learning.label_outcomes: unresolved players never become absences).

    cd backend
    uv run python ../scripts/participation_cal_shadow.py freeze    # write-once copy of the map (builds it if missing)
    uv run python ../scripts/participation_cal_shadow.py score     # base vs calibrated on every fully final week
    uv run python ../scripts/participation_cal_shadow.py cycle     # freeze (once), then score
    uv run python ../scripts/participation_cal_shadow.py status

Who runs it (no new scheduler): learning_cycle.py's non-fatal `ipm_cal_shadow` stage and refresh_all.py's read-only
evidence stage both run `cycle`. When no shadow artifact exists yet, `freeze` first runs
`participation_calibration.py run` (its public CLI: the study's own selection and fit, written to
<models>/research/), then copies the artifact.

Pre-registered rule (fixed here before any prospective week is scored): a week counts when it is from 2026 week 7
on, after the frozen map's training cutoff, fully final, and its rows were frozen by a pregame run of the same base
participation version; rows whose kickoff is not after the freeze are excluded. The re-test passes when there are
>= 8 such weeks and >= 400 QB rows, the week-clustered 95% CI of both the log-loss and the Brier difference
(calibrated - base) is below 0, and the unconditional MAE (P x the frozen ifp-1.0.0 conditional points) is not worse
by more than 0.5% (the study's own SELECTION_RULES). A pass is a reason to review the map, never a deployment:
production participation (participation.py, ipm-1.0.0) is not changed by any outcome here.

Writes only under <models>/research/participation_cal_shadow/ (git-ignored): frozen_ipm-cal-0.1.0.json (write-once),
weeks/s<S>w<WW>.json (write-once: the week's base and calibrated probabilities as first scored), runs/ (write-once)
and scores.json (derived, replaced). Opens the databases read-only; never touches the production participation
artifact, the weekly ledger, the registry, the API or the frontend.

Exit codes: 0 ok (frozen, already frozen, scored, nothing to score yet); 3 ok but with warnings; 1 a step failed.
"""

from __future__ import annotations

import argparse
from collections import Counter
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
import challenger_research as cr  # noqa: E402

from app.config import get_settings  # noqa: E402

FREEZE_SCHEMA = "ipm-cal-shadow-freeze/v1"
RUN_SCHEMA = "ipm-cal-shadow-run/v1"
POSITION = "QB"
METHOD = "isotonic"
FIRST_PROSPECTIVE = (2026, 7)
SEED = 20261018
STEP_TIMEOUT_S = 20 * 60
EXIT_OK, EXIT_FAILED, EXIT_WARNINGS = 0, 1, 3

GATE = {"min_prospective_weeks": 8, "min_rows": 400, "log_loss_ci_upper_below_zero": True,
        "brier_ci_upper_below_zero": True, "max_relative_unconditional_mae_worsening": 0.005}
RULES = {
    "candidate": "ipm-cal-0.1.0's QB isotonic map (expanding window, every block >= 50 rows), frozen as published "
                 "by participation_calibration.py; RB/WR/TE keep the base probability and are not re-tested",
    "base": "the P(appears) production froze before kickoff: weekly-ledger pregame rows (frozen_rows) made by the "
            "frozen map's base participation version (ipm-1.0.0); runs made by another version are excluded",
    "evidence": "weeks from 2026 week 7 on, after the map's training cutoff, fully final (week_finality); only rows "
                "whose kickoff is after the freeze; outcomes from label_outcomes (appeared / confirmed absent only)",
    "gate": GATE,
    "unconditional_points": "P x the frozen ifp-1.0.0 (champion) conditional points, as in the study; P x the frozen "
                            "production ifp-2.0.0 points is reported, never gating",
    "honesty": "a pass is a reason to review, never a deployment; production participation is unchanged",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def shadow_dir(args) -> Path:
    return Path(args.models) / "research" / "participation_cal_shadow"


def source_path(args) -> Path:
    from app.analytics.participation_calibration import CAL_VERSION
    return Path(args.models) / "research" / f"participation_calibration_{CAL_VERSION}.json"


def frozen_path(args) -> Path:
    from app.analytics.participation_calibration import CAL_VERSION
    return shadow_dir(args) / f"frozen_{CAL_VERSION}.json"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _write_once(path: Path, obj) -> bool:
    from app.analytics.weekly_learning import write_once
    return write_once(path, json.dumps(obj, indent=1, default=str))


def _replace_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- the frozen map


def _map_sha(cal: dict) -> str:
    return _sha(json.dumps({k: cal["params"].get(k) for k in ("x", "y")}, sort_keys=True).encode())[:16]


def binding_of(art: dict, source_sha: str) -> dict:
    """What the frozen copy is bound to. Raises ValueError when the artifact is not the pre-registered candidate."""
    from app.analytics.participation_calibration import CAL_VERSION, CalibrationArtifact

    CalibrationArtifact.from_dict(art)                 # schema + shadow-only status
    if art.get("version") != CAL_VERSION:
        raise ValueError(f"artifact version {art.get('version')!r} is not {CAL_VERSION}")
    cal = (art.get("calibrators") or {}).get(POSITION) or {}
    if cal.get("method") != METHOD or cal.get("fallback") or not (cal.get("params") or {}).get("x"):
        raise ValueError(f"the artifact's {POSITION} map is {cal.get('method')!r} "
                         f"(fallback {cal.get('fallback')!r}), not the pre-registered fitted {METHOD} map")
    return {"cal_version": art["version"], "participation_version": art["base_model"].get("participation_version"),
            "position": POSITION, "method": METHOD, "window": cal["params"].get("window"),
            "qb_map_sha": _map_sha(cal), "training_cutoff_key": art.get("training_cutoff_key"),
            "scoring_fingerprint": (art.get("fingerprints") or {}).get("scoring"),
            "data_hash": (art.get("fingerprints") or {}).get("data_hash"), "source_sha256": source_sha}


def freeze_record(art: dict, source: Path, source_sha: str, now: datetime) -> dict:
    binding = binding_of(art, source_sha)
    rec = {"schema": FREEZE_SCHEMA, "candidate": art["version"], "binding": binding, "rules": RULES,
           "first_prospective": list(FIRST_PROSPECTIVE), "source": str(source), "artifact": art}
    rec["config_hash"] = _sha(json.dumps(rec, sort_keys=True, default=str).encode())[:16]
    rec["frozen_at"] = now.isoformat()
    return rec


def load_frozen(path: Path) -> dict:
    """The frozen copy, verified: its content hash, the artifact's own checks and the bound QB knots. Raises
    ValueError on any mismatch (a hand-edited or corrupted copy is never scored)."""
    rec = json.loads(path.read_text())
    body = {k: v for k, v in rec.items() if k not in ("config_hash", "frozen_at")}
    if rec.get("schema") != FREEZE_SCHEMA:
        raise ValueError(f"{path.name}: schema {rec.get('schema')!r} is not {FREEZE_SCHEMA}")
    if _sha(json.dumps(body, sort_keys=True, default=str).encode())[:16] != rec.get("config_hash"):
        raise ValueError(f"{path.name} does not match its config_hash: it was changed after the freeze")
    if binding_of(rec["artifact"], rec["binding"]["source_sha256"]) != rec["binding"]:
        raise ValueError(f"{path.name}: the frozen artifact no longer matches its binding")
    return rec


def build_artifact(args) -> None:
    """participation_calibration.py run (the study's public CLI): writes the shadow artifact under <models>/research/."""
    cmd = [sys.executable, str(SCRIPTS / "participation_calibration.py"), "run", "--live-db", str(args.live_db),
           "--history-db", str(args.history_db), "--models", str(args.models), "--season", str(args.season)]
    proc = subprocess.run(cmd, cwd=_bootstrap.BACKEND, capture_output=True, text=True,
                          env={**os.environ, "PYTHONUNBUFFERED": "1"}, timeout=args.timeout)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(f"participation_calibration.py run exited {proc.returncode}: "
                           f"{msg[-1] if msg else 'no output'}")


def run_freeze(args, *, now: datetime | None = None, build=None) -> dict:
    """Freeze the map once. Afterwards: verify the frozen copy, and note (never adopt) a changed source artifact."""
    now = now or _utcnow()
    build = build or build_artifact
    out = {"kind": "freeze", "warnings": [], "notes": []}
    fpath, spath = frozen_path(args), source_path(args)
    if fpath.exists():
        rec = load_frozen(fpath)
        out.update(status="already_frozen", frozen=str(fpath), config_hash=rec["config_hash"],
                   frozen_at=rec["frozen_at"])
        if spath.exists() and _sha(spath.read_bytes()) != rec["binding"]["source_sha256"]:
            out["notes"].append(f"{spath.name} changed since the freeze (participation_calibration.py was re-run); "
                                "the frozen copy is scored, never the new one")
        return out
    if not spath.exists():
        if not args.write:
            out.update(status="would_build", reason=f"no {spath.name} yet")
            return out
        build(args)
        if not spath.exists():
            raise RuntimeError(f"participation_calibration.py run did not write {spath}")
        out["notes"].append(f"built {spath.name} with participation_calibration.py run")
    raw = spath.read_bytes()
    rec = freeze_record(json.loads(raw), spath, _sha(raw), now)
    if not args.write:
        out.update(status="would_freeze", binding=rec["binding"])
        return out
    written = _write_once(fpath, rec)
    rec = load_frozen(fpath)
    out.update(status="frozen" if written else "already_frozen", frozen=str(fpath), config_hash=rec["config_hash"],
               frozen_at=rec["frozen_at"], binding=rec["binding"])
    return out


# ----------------------------------------------------------------------------- the prospective re-test


def evidence_rows(runs: list[dict], frozen: dict):
    """Frozen pregame QB rows that count (RULES["evidence"] minus finality, which needs the sources), with the
    calibrated probability. Returns (rows, excluded counts)."""
    import polars as pl

    from app.analytics import weekly_learning as wl
    from app.analytics.participation_calibration import CalibrationArtifact

    b = frozen["binding"]
    first = max(FIRST_PROSPECTIVE[0] * 100 + FIRST_PROSPECTIVE[1], (b["training_cutoff_key"] or 0) + 1)
    excluded = Counter()
    used = []
    for r in runs:
        if r.get("kind") != "pregame":
            continue
        if r["season"] * 100 + r["week"] < first:
            excluded["before_prospective_start"] += 1
        elif r.get("participation") != b["participation_version"]:
            excluded[f"participation_{r.get('participation')}"] += 1
        else:
            used.append(r)
    rows = wl.frozen_rows(used).filter((pl.col("position") == POSITION) & pl.col("p_play").is_not_null())
    frozen_at = datetime.fromisoformat(frozen["frozen_at"])
    late = rows.filter(pl.col("kickoff").is_null() | (pl.col("kickoff").str.to_datetime(time_zone="UTC") <= frozen_at))
    if late.height:
        excluded["rows_not_kicking_off_after_the_freeze"] += late.height
        rows = rows.join(late.select("key", "gsis_id"), on=["key", "gsis_id"], how="anti")
    art = CalibrationArtifact.from_dict(frozen["artifact"])
    rows = rows.with_columns(pl.Series("p_cal", art.apply(POSITION, rows["p_play"].to_numpy()), dtype=pl.Float64))
    return rows, dict(excluded)


def retest(lab, seed: int = SEED) -> dict:
    """Base vs calibrated on identical resolved rows (lab: label_outcomes output with p_play, p_cal)."""
    import polars as pl

    from app.analytics import participation_calibration as pc
    from app.analytics import weekly_learning as wl

    d = lab.filter(pl.col("outcome_status").is_in(list(wl.RESOLVED)) & pl.col("pred_champion").is_not_null()).with_columns(
        (pl.col("outcome_status") == wl.APPEARED).cast(pl.Int8).alias("played"),
        pl.col("p_play").alias("p_none"), pl.col("pred_champion").alias("cond"))
    weeks = d.select("key").n_unique() if d.height else 0
    out = {"weeks": weeks, "n": d.height, "base": pc.candidate_report(d, "p_none", seed),
           "calibrated": pc.candidate_report(d, "p_cal", seed)}
    prod = d.filter(pl.col("pred_challenger").is_not_null()).with_columns(pl.col("pred_challenger").alias("cond"))
    out["unconditional_production_ifp2"] = {c: (pc.candidate_report(prod, c, seed).get("unconditional_points"))
                                            for c in ("p_none", "p_cal")} if prod.height else None
    out["by_week"] = [{"key": int(k), "n": g.height,
                       **{c: {m: v for m, v in pc.prob_metrics(g["played"].cast(pl.Float64).to_numpy(),
                                                               g[c].to_numpy()).items() if m in ("brier", "log_loss")}
                          for c in ("p_none", "p_cal")}}
                      for (k,), g in d.group_by(["key"], maintain_order=True)] if d.height else []
    out["gate"] = gate(out)
    return out


def gate(rep: dict) -> dict:
    cal = rep.get("calibrated") or {}
    checks = {"weeks": rep["weeks"] >= GATE["min_prospective_weeks"], "rows": rep["n"] >= GATE["min_rows"]}
    if cal.get("n"):
        ll, br = cal["minus_base_log_loss"]["ci95"], cal["minus_base_brier"]["ci95"]
        um, ub = cal["unconditional_points"]["mae"], rep["base"]["unconditional_points"]["mae"]
        checks.update(log_loss_ci_upper_below_0=bool(ll) and ll[1] < 0, brier_ci_upper_below_0=bool(br) and br[1] < 0,
                      unconditional_mae=bool(ub) and (um - ub) / ub <= GATE["max_relative_unconditional_mae_worsening"])
    else:
        checks.update(log_loss_ci_upper_below_0=False, brier_ci_upper_below_0=False, unconditional_mae=False)
    status = ("meets_gate" if all(checks.values()) else
              "insufficient_data" if not (checks["weeks"] and checks["rows"]) else "fails_gate")
    return {"checks": checks, "status": status}


def _context(args):
    """(sources, {season: schedule fetch time}) from read-only connections, as the calibration study builds them."""
    import duckdb

    import hgb_research as hr
    import participation_calibration as pcal
    from types import SimpleNamespace

    src, _ = hr._sources(SimpleNamespace(live_db=args.live_db, history_db=args.history_db, season=args.season,
                                         exits_dir=args.exits_dir))
    live = duckdb.connect(str(args.live_db), read_only=True)
    hist = duckdb.connect(str(args.history_db), read_only=True) if Path(args.history_db).exists() else None
    try:
        fetched = pcal.fetch_times(live, hist, args.season)
    finally:
        for c in (live, hist):
            if c is not None:
                c.close()
    return src, fetched


def run_score(args, *, now: datetime | None = None, runs=None, context=None) -> dict:
    """Score every counted, fully final week. Never raises; recorded write-once under runs/."""
    import polars as pl

    from app.analytics import weekly_learning as wl

    now = now or _utcnow()
    rec = {"schema": RUN_SCHEMA, "kind": "score", "season": args.season, "started_at": now.isoformat(),
           "dry_run": not args.write, "warnings": [], "notes": []}
    try:
        if not frozen_path(args).exists():
            rec.update(status="not_frozen", notes=["the map is not frozen yet: nothing to score"])
            return rec
        frozen = load_frozen(frozen_path(args))
        runs = wl.load_ledger(args.weekly_dir) if runs is None else runs
        rows, excluded = evidence_rows(runs, frozen)
        rec["excluded"] = excluded
        if rows.height == 0:
            rec.update(status="no_evidence", notes=["no counted pregame QB row yet"])
            return rec
        src, fetched = context(args) if context else _context(args)
        weeks = sorted({(int(s), int(w)) for s, w in rows.select("season", "week").unique().iter_rows()})
        final = [sw for sw in weeks if wl.week_finality(src, sw[0], sw[1], fetched.get(sw[0]))["fully_final"]]
        rec["pending_weeks"] = [sw for sw in weeks if sw not in final]
        rows = rows.filter(pl.col("key").is_in([s * 100 + w for s, w in final]))
        lab = wl.label_outcomes(rows, src, fetched) if rows.height else rows
        rep = retest(lab) if rows.height else {"weeks": 0, "n": 0, "gate": {"status": "insufficient_data"}}
        rec.update(status="scored", retest=rep)
        if args.write:
            for s, w in final:
                g = rows.filter((pl.col("season") == s) & (pl.col("week") == w))
                _write_once(shadow_dir(args) / "weeks" / f"s{s}w{w:02d}.json", {
                    "candidate": frozen["candidate"], "config_hash": frozen["config_hash"], "season": s, "week": w,
                    "first_scored_at": now.isoformat(),
                    "rows": g.select("gsis_id", "team", "kickoff", "run_id", "p_play", "p_cal").to_dicts()})
            _replace_json(shadow_dir(args) / "scores.json", {"scored_at": now.isoformat(),
                                                             "config_hash": frozen["config_hash"],
                                                             "excluded": excluded, **rep})
    except (Exception, SystemExit) as err:     # noqa: BLE001  recorded, never raised
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        rec["warnings"].append(f"scoring the ipm-cal shadow failed: {err}")
    rec["finished_at"] = _utcnow().isoformat()
    path = shadow_dir(args) / "runs" / f"{now:%Y%m%dT%H%M%SZ}-score-{os.getpid()}.json"
    if args.write:
        _write_once(path, rec)
    rec["record"] = str(path)
    return rec


# ----------------------------------------------------------------------------- commands


def _safe_freeze(args, now: datetime) -> dict:
    try:
        return run_freeze(args, now=now)
    except (Exception, SystemExit) as err:     # noqa: BLE001
        return {"kind": "freeze", "status": "failed", "error": f"{err.__class__.__name__}: {err}",
                "warnings": [f"freezing the ipm-cal map failed: {err}"], "notes": []}


def _score_summary(rec: dict) -> dict:
    out = {k: rec[k] for k in ("kind", "status", "error", "record", "warnings", "notes", "excluded", "pending_weeks")
           if k in rec}
    rep = rec.get("retest")
    if rep:
        out.update(weeks=rep["weeks"], n=rep["n"], gate=rep["gate"]["status"])
        if rep.get("n"):
            out["log_loss"] = {c: (rep[c].get("probability") or {}).get("log_loss") for c in ("base", "calibrated")}
            out["brier"] = {c: (rep[c].get("probability") or {}).get("brier") for c in ("base", "calibrated")}
    return out


def cmd_cycle(args, now: datetime) -> dict:
    fz = _safe_freeze(args, now)
    sc = _score_summary(run_score(args, now=now))
    out = {"freeze": fz, "score": sc, "warnings": [*fz.get("warnings", []), *sc.get("warnings", [])]}
    out["status"] = "failed" if "failed" in (fz["status"], sc["status"]) else "ok"
    return out


def cmd_status(args, now: datetime) -> dict:
    d = shadow_dir(args)
    runs = sorted((d / "runs").glob("*.json"), key=lambda p: p.stat().st_mtime) if (d / "runs").exists() else []
    sc = d / "scores.json"
    fz = frozen_path(args)
    return {"frozen": {k: v for k, v in json.loads(fz.read_text()).items() if k != "artifact"} if fz.exists() else None,
            "weeks": sorted(p.name for p in (d / "weeks").glob("*.json")),
            "latest_run": json.loads(runs[-1].read_text()) if runs else None,
            "scores": json.loads(sc.read_text()) if sc.exists() else None}


def parse(argv=None):
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["freeze", "score", "cycle", "status"])
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--week", type=int, help="accepted for the shadow-stage contract; every counted week is scored")
    ap.add_argument("--ifp-dir", "--models", dest="models", type=Path,
                    default=cr._path("INDEPENDENT_MODEL_DIR", "data/models"),
                    help="model directory holding research/participation_calibration_<version>.json")
    ap.add_argument("--exits-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"))
    ap.add_argument("--weekly-dir", type=Path, default=cr._path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                    help="the weekly learning output holding the frozen production pregame ledger")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--history-db", type=Path, default=cr._path("NFL_HISTORY_DB_PATH", "data/nfl_history.duckdb"))
    ap.add_argument("--timeout", type=float, default=STEP_TIMEOUT_S, help="seconds for participation_calibration.py run")
    ap.add_argument("--now", help="use this ISO time; implies --dry-run")
    ap.add_argument("--dry-run", action="store_true", help="decide and print only: nothing built, frozen or recorded")
    args = ap.parse_args(argv)
    args.write = not (args.dry_run or args.now)
    return args


def main(argv=None) -> int:
    args = parse(argv)
    now = _utcnow()
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    if args.command == "status":
        print(json.dumps(cmd_status(args, now), indent=1, default=str))
        return EXIT_OK
    if args.command == "freeze":
        out = _safe_freeze(args, now)
    elif args.command == "score":
        out = _score_summary(run_score(args, now=now))
    else:
        out = cmd_cycle(args, now)
    print(json.dumps(out, indent=1, default=str))
    for w in out.get("warnings", []):
        print(f"WARNING: ipm-cal shadow: {w}", file=sys.stderr)
    if out.get("status") == "failed":
        return EXIT_FAILED
    return EXIT_WARNINGS if out.get("warnings") else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
