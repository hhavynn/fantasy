"""Frozen pregame point-distribution ledger and its prospective coverage check (research only; see
docs/research/fantasy_point_distributions.md section 17). The research recommendation was: before any product use,
freeze each week's distributions before kickoff and re-check interval coverage on prospective weeks. This script
does exactly that and nothing else: nothing in the app, the API or the frontend reads its output.

    cd backend
    uv run python ../scripts/distribution_ledger.py freeze [--week W]      # write-once pregame ledger for week W
    uv run python ../scripts/distribution_ledger.py score                  # prospective coverage of every final week
    uv run python ../scripts/distribution_ledger.py cycle  [--week W]      # score, missed-week check, then freeze
    uv run python ../scripts/distribution_ledger.py status
    uv run python ../scripts/distribution_ledger.py pin --artifact FILE --reason TEXT   # explicit re-pin (rare)
    uv run python ../scripts/distribution_ledger.py freeze --week W --now 2026-10-25T12:00Z   # decide only (dry run)

What is frozen. For week W, every player row of the production weekly ledger (data/models/weekly/ledger, written by
learning_cycle.py's `predict` stage) that is pregame and whose own kickoff is still ahead. Each row carries the two
frozen means the weekly ledger holds, dressed with the PINNED research artifact (point_distribution_research.py
`run`, frozen write-once under its content hash):
  * ifp-1.0.0 (the weekly champion, frozen model C refit on earlier weeks): the mean the distributions were
    developed and validated on (primary);
  * ifp-2.0.0 (the production challenger): the mean the product shows (secondary, not part of the validation).
Per row and mean: the 99-knot conditional quantile function (points if he plays), P10/P50/P90, and the
unconditional P10/P50/P90 (with ipm-1.0.0 p_play) where the artifact approves it (WR/TE). Provenance: the artifact
file, its content hash and sha256, the pin record, the weekly ledger run ids and file sha256s, and a sha256 of the
rows. A frozen ledger is never rewritten; a week whose games all started without one is recorded as missed and never
backfilled.

The pin. One artifact is used for every week until someone re-pins on purpose (`pin`, recorded write-once with a
reason). The first freeze pins the newest artifact trained only on weeks before the one being frozen. A missing
artifact is a warning (run point_distribution_research.py `run` once); nothing is fitted here.

Scoring. Once a frozen week is fully final (weekly_learning.week_finality; outcomes from weekly_learning.
label_outcomes, so an unplayed game never becomes a zero), the ledger's own frozen quantiles are scored with the
research module's calibration (randomized PIT, exact with point masses at 0) per position: conditional ranges for
every position, unconditional ranges for WR/TE. The report compares each with the pre-registered validation bounds
but decides nothing: QB stays restricted, and no percentile reaches a product without a separate approval.

Writes only under <ifp-dir>/research/point_distributions/ledger/ (git-ignored): pins/*.json, weeks/*.json and
missed/*.json (write-once) and scores.json (derived, replaced). Opens the live database read-only and never touches
a table, a model artifact, the weekly ledger, the registry, the API or the frontend.

Exit codes: 0 ok (frozen, already frozen, deferred, nothing to do); 3 ok but with warnings (no artifact, missed
week); 1 a step failed (recorded in the output, nothing half-written).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

import _bootstrap  # noqa: F401

import numpy as np  # noqa: E402

from app.analytics import point_distributions as pd_  # noqa: E402
from app.analytics import weekly_learning as wl  # noqa: E402
from app.analytics.forecast_features import POSITIONS, key  # noqa: E402
from app.config import REPO_ROOT, get_settings  # noqa: E402

SCHEMA = "point-distribution-ledger/v1"
PIN_SCHEMA = "point-distribution-pin/v1"
FIRST_PROSPECTIVE = (2026, 6)        # the research report: re-check coverage on prospective weeks, 2026 week 6 on
PRIMARY_MEAN = "ifp-1.0.0"
MEANS = {"ifp-1.0.0": "champion", "ifp-2.0.0": "challenger"}      # model version -> weekly ledger block
# docs/research/fantasy_point_distributions.md section 14: what a future integration may use. Scoring reports every
# position, but only these may ever be considered, and only after a separate approval.
POSITION_POLICY = {
    "QB": {"conditional": "restricted", "unconditional": "restricted",
           "note": "QB stays restricted (lower tail too optimistic in the 2025-26 replay, participation experimental); "
                   "scored here for monitoring only"},
    "RB": {"conditional": "candidate", "unconditional": "withheld",
           "note": "conditional ranges only: ipm-1.0.0 RB participation is experimental"},
    "WR": {"conditional": "candidate", "unconditional": "candidate", "note": "conditional and unconditional ranges"},
    "TE": {"conditional": "candidate", "unconditional": "candidate", "note": "conditional and unconditional ranges"},
}
CHECKED = ("coverage80", "p10_below_rate", "p90_above_rate", "p50_mid_rate")
EXIT_OK, EXIT_FAILED, EXIT_WARNINGS = 0, 1, 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _path(env: str, default: str) -> Path:
    raw = Path(os.getenv(env) or default)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _write_once(path: Path, obj, compact: bool = False) -> bool:
    text = _canon(obj) if compact else json.dumps(obj, indent=1, sort_keys=True, default=str)
    return wl.write_once(path, text)


def _replace_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str))
    os.replace(tmp, path)


def research_dir(args) -> Path:
    return Path(args.ifp_dir) / "research" / "point_distributions"


def ledger_dir(args) -> Path:
    return research_dir(args) / "ledger"


def week_path(args, season: int, week: int) -> Path:
    return ledger_dir(args) / "weeks" / f"s{season}w{week:02d}-pregame.json"


def _r(x, nd=4):
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


# ----------------------------------------------------------------------------- artifact + pin


def artifact_body_hash(art: dict) -> str:
    """point_distributions.fit_artifact's content hash, recomputed (the file must match the name it carries)."""
    body = {k: v for k, v in art.items() if k != "content_hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def list_artifacts(args) -> list[Path]:
    d = research_dir(args)
    return sorted(d.glob(f"point_distributions_{pd_.DISTRIBUTION_VERSION}_*.json")) if d.exists() else []


def current_pin(args) -> dict | None:
    pins = sorted((ledger_dir(args) / "pins").glob("pin-*.json"))
    return json.loads(pins[-1].read_text()) if pins else None


def load_pinned(args, pin: dict) -> dict:
    """The pinned artifact, verified byte for byte against the pin. Raises ValueError on any mismatch."""
    path = research_dir(args) / pin["artifact_file"]
    if not path.exists():
        raise ValueError(f"pinned artifact {pin['artifact_file']} is missing (pin {pin['pin_id']})")
    raw = path.read_bytes()
    if _sha_bytes(raw) != pin["artifact_sha256"]:
        raise ValueError(f"pinned artifact {pin['artifact_file']} changed since it was pinned (sha256 mismatch)")
    art = json.loads(raw)
    if art.get("content_hash") != artifact_body_hash(art) or art["content_hash"] != pin["content_hash"]:
        raise ValueError(f"pinned artifact {pin['artifact_file']} fails its content-hash check")
    return art


def make_pin(args, path: Path, *, how: str, reason: str, now: datetime) -> dict:
    """A pin record for one artifact file (not yet written)."""
    raw = Path(path).read_bytes()
    art = json.loads(raw)
    if art.get("schema") != pd_.ARTIFACT_SCHEMA or art.get("version") != pd_.DISTRIBUTION_VERSION:
        raise ValueError(f"{Path(path).name} is not a {pd_.DISTRIBUTION_VERSION} distribution artifact")
    if art.get("content_hash") != artifact_body_hash(art):
        raise ValueError(f"{Path(path).name} fails its content-hash check")
    pins = sorted((ledger_dir(args) / "pins").glob("pin-*.json"))
    return {"schema": PIN_SCHEMA, "pin_id": len(pins) + 1, "pinned_at": now.isoformat(), "how": how, "reason": reason,
            "artifact_file": Path(path).name, "artifact_sha256": _sha_bytes(raw), "content_hash": art["content_hash"],
            "version": art["version"], "trained_through_key": art["trained_through_key"],
            "scoring_fingerprint": art["scoring_fingerprint"], "input_fingerprint": art["input_fingerprint"],
            "positions": {p: {k: e.get(k) for k in ("method", "status", "unconditional")}
                          for p, e in art["positions"].items()}}


def write_pin(args, pin: dict) -> Path:
    path = ledger_dir(args) / "pins" / f"pin-{pin['pin_id']:04d}.json"
    if not _write_once(path, pin):
        raise ValueError(f"{path.name} already exists (a concurrent pin); rerun")
    return path


def ensure_pin(args, season: int, week: int, now: datetime) -> tuple[dict | None, list[str]]:
    """(pin, warnings). The current pin, or (first freeze only) an automatic pin of the newest artifact trained
    strictly before (season, week). Never re-pins on its own."""
    pin = current_pin(args)
    if pin is not None:
        return pin, []
    cands = []
    for p in list_artifacts(args):
        try:
            art = json.loads(p.read_text())
        except ValueError:
            continue
        if art.get("trained_through_key", 10**9) < key(season, week) and art.get("content_hash") == artifact_body_hash(art):
            cands.append((art["trained_through_key"], art["content_hash"], p))
    if not cands:
        return None, [f"no {pd_.DISTRIBUTION_VERSION} distribution artifact trained before {season} week {week} under "
                      f"{research_dir(args)}: run `point_distribution_research.py run` once; nothing is frozen until then"]
    _, _, path = max(cands)
    pin = make_pin(args, path, how="auto", now=now,
                   reason="first freeze: the newest artifact trained only on weeks before the week being frozen")
    if args.write:
        write_pin(args, pin)
    return pin, []


def check_scoring(args, art: dict, season: int) -> str | None:
    """The live database's league scoring must be the one the artifact was built under (None when it is)."""
    if not Path(args.live_db).exists():
        return None
    import duckdb
    from app.analytics.independent_projection_store import league_scoring

    con = duckdb.connect(str(args.live_db), read_only=True)
    try:
        _, scoring = league_scoring(con, season)
    finally:
        con.close()
    if scoring.fingerprint != art["scoring_fingerprint"]:
        return (f"league scoring {scoring.fingerprint} differs from the artifact's {art['scoring_fingerprint']}: "
                "its distributions do not describe these points")
    return None


# ----------------------------------------------------------------------------- the frozen rows


def weekly_runs(args, season: int, week: int, now: datetime) -> list[tuple[dict, str]]:
    """(run, file sha256) of the weekly ledger's pregame runs for the week that existed at `now`."""
    d = Path(args.weekly_dir) / "ledger" / f"s{season}w{week:02d}"
    out = []
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        raw = f.read_bytes()
        run = json.loads(raw)
        if run.get("kind") == "pregame" and datetime.fromisoformat(run["created_at"]) <= now:
            out.append((run, _sha_bytes(raw)))
    return out


def open_rows(runs: list[dict], now: datetime) -> tuple[list[dict], dict]:
    """Per player: the latest weekly-ledger row created strictly before his kickoff (weekly_learning.frozen_rows's
    rule), kept only while his kickoff is still ahead of `now`. Returns (rows, exclusion counts)."""
    best: dict[str, tuple] = {}
    for run in runs:
        created = datetime.fromisoformat(run["created_at"])
        for r in run["rows"]:
            if not r.get("pregame") or not r.get("kickoff") or created >= datetime.fromisoformat(r["kickoff"]):
                continue
            if r["gsis_id"] not in best or created > best[r["gsis_id"]][0]:
                best[r["gsis_id"]] = (created, run["run_id"], r)
    rows, excluded = [], {"kicked_off": 0, "unsupported_position": 0}
    for g in sorted(best):
        _, rid, r = best[g]
        if datetime.fromisoformat(r["kickoff"]) <= now:
            excluded["kicked_off"] += 1
        elif r.get("position") not in POSITIONS:
            excluded["unsupported_position"] += 1
        else:
            rows.append({**r, "_run_id": rid})
    return rows, excluded


def dress(art: dict, position: str, mean: float | None, p_play: float | None) -> dict:
    """One mean with the artifact's conditional quantile function, and the summary percentiles."""
    entry = art["positions"].get(position) or {}
    tab = entry.get("table")
    if mean is None:
        return {"mean": None, "withheld": "no frozen mean"}
    if not tab:
        return {"mean": mean, "withheld": f"no fitted table for {position}"}
    if tab["kind"] != "mean_grid":
        return {"mean": mean, "withheld": f"{position} method {entry.get('method')} needs pregame volatility features "
                                         "the weekly ledger does not carry"}
    qf = pd_.artifact_quantiles(art, position, float(mean))
    out = {"mean": mean, "q": [round(v, 3) for v in qf.values],
           "conditional": {f"p{int(u * 100)}": round(qf.quantile(u), 3) for u in pd_.SUMMARY_TAUS}}
    if entry.get("unconditional") == "validated" and p_play is not None:
        mix = pd_.Mixture(float(p_play), qf)
        out["unconditional"] = {f"p{int(u * 100)}": round(mix.quantile(u), 3) for u in pd_.SUMMARY_TAUS}
    return out


def ledger_rows(art: dict, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        p = r.get("p_play")
        out.append({"gsis_id": r["gsis_id"], "position": r["position"], "team": r.get("team"),
                    "opponent": r.get("opponent"), "kickoff": r["kickoff"], "source_run_id": r["_run_id"],
                    "p_play": p, "distributions": {m: dress(art, r["position"], (r.get(block) or {}).get("points"), p)
                                                   for m, block in MEANS.items()}})
    return out


# ----------------------------------------------------------------------------- freeze


def run_freeze(args, season: int, week: int, *, now: datetime | None = None) -> dict:
    """Freeze week W's pregame distribution ledger once. Never raises: every outcome is returned."""
    now = now or _utcnow()
    rec = {"kind": "freeze", "season": season, "week": week, "decided_at": now.isoformat(), "dry_run": not args.write,
           "warnings": [], "notes": []}
    path = week_path(args, season, week)
    try:
        if (season, week) < FIRST_PROSPECTIVE:
            return {**rec, "status": "before_prospective_start",
                    "reason": f"prospective evidence starts {FIRST_PROSPECTIVE[0]} week {FIRST_PROSPECTIVE[1]}"}
        if path.exists():
            return {**rec, "status": "already_frozen", "ledger": str(path), "ledger_sha256": _sha_bytes(path.read_bytes())}
        runs = weekly_runs(args, season, week, now)
        if not runs:
            rec["notes"].append(f"no pregame forecast for week {week} in the weekly ledger yet (learning_cycle.py "
                                "`predict` writes it); deferred")
            return {**rec, "status": "deferred", "reason": "no frozen pregame forecast yet"}
        rows, excluded = open_rows([r for r, _ in runs], now)
        if not rows:
            return {**rec, "status": "nothing_open", "excluded": excluded,
                    "reason": "every forecast player's game has kicked off"}
        pin, warns = ensure_pin(args, season, week, now)
        rec["warnings"] += warns
        if pin is None:
            return {**rec, "status": "no_artifact"}
        art = load_pinned(args, pin)
        if art["trained_through_key"] >= key(season, week):
            raise ValueError(f"pinned artifact is trained through {art['trained_through_key']}, not before week {week}")
        problem = check_scoring(args, art, season)
        if problem:
            raise ValueError(problem)
        body_rows = ledger_rows(art, rows)
        used = sorted({r["source_run_id"] for r in body_rows})
        body = {"schema": SCHEMA, "version": pd_.DISTRIBUTION_VERSION, "lifecycle": "research_only",
                "season": season, "week": week, "created_at": now.isoformat(),
                "first_open_kickoff": min(r["kickoff"] for r in body_rows), "primary_mean": PRIMARY_MEAN,
                "means": {m: f"weekly ledger `{b}` points" for m, b in MEANS.items()},
                "artifact": {"pin_id": pin["pin_id"], "file": pin["artifact_file"], "sha256": pin["artifact_sha256"],
                             "content_hash": art["content_hash"], "trained_through_key": art["trained_through_key"],
                             "input_fingerprint": art["input_fingerprint"],
                             "scoring_fingerprint": art["scoring_fingerprint"],
                             "positions": {p: {k: e.get(k) for k in ("method", "status", "unconditional")}
                                           for p, e in art["positions"].items()}},
                "taus": art["taus"], "policy": POSITION_POLICY,
                "source": {"weekly_ledger_runs": [{"run_id": r["run_id"], "created_at": r["created_at"], "sha256": s}
                                                  for r, s in runs if r["run_id"] in used]},
                "excluded": excluded, "rows": body_rows}
        body["rows_sha256"] = _sha_bytes(_canon(body_rows).encode())
        rec.update(rows=len(body_rows), excluded=excluded, pin_id=pin["pin_id"], artifact=pin["artifact_file"])
        if not args.write:
            return {**rec, "status": "would_freeze"}
        if not _write_once(path, body, compact=True):           # ~0.5 MB a week: 99 knots x 2 means per row
            return {**rec, "status": "already_frozen", "ledger": str(path)}
        rec.update(status="frozen", ledger=str(path), ledger_sha256=_sha_bytes(path.read_bytes()))
    except Exception as err:          # noqa: BLE001  reported, never raised: the callers must not fail
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        rec["warnings"].append(f"distribution ledger freeze for week {week} failed: {err}")
    return rec


def missed_path(args, season: int, week: int) -> Path:
    return ledger_dir(args) / "missed" / f"s{season}w{week:02d}.json"


def check_missed(args, season: int, week: int | None, *, now: datetime, kickoffs=None) -> dict | None:
    """A prospective week whose games have all kicked off without a ledger is recorded write-once (never backfilled).
    None when it has a ledger, is already recorded, or is not over yet."""
    if (not week or week < 1 or (season, week) < FIRST_PROSPECTIVE or week_path(args, season, week).exists()
            or missed_path(args, season, week).exists()):
        return None
    if kickoffs is None:
        import duckdb
        from app.analytics.injury_freshness import scheduled_kickoffs   # read-only schedule helper
        con = duckdb.connect(str(args.live_db), read_only=True)
        try:
            kickoffs = scheduled_kickoffs(con, season, week)
        finally:
            con.close()
    known = [k for k in kickoffs.values() if k is not None]
    if not known or max(known) > now:
        return None
    rec = {"season": season, "week": week, "last_kickoff": max(known).isoformat(), "recorded_at": now.isoformat(),
           "reason": "no pregame distribution ledger was frozen before the week's games; nothing is backdated"}
    if args.write:
        _write_once(missed_path(args, season, week), rec)
    return rec


# ----------------------------------------------------------------------------- score


def _labelled(args, ledgers: list[dict]):
    """Every ledger row with outcome_status / label (weekly_learning.label_outcomes) and the fully final weeks."""
    import duckdb
    import polars as pl

    from app.analytics.forecast_features import load_sources
    from app.analytics.independent_projection_store import league_scoring
    from app.analytics.schedule_intelligence import nfl_fetch_times

    schema = {"season": pl.Int64, "week": pl.Int64, "key": pl.Int64, "gsis_id": pl.String, "team": pl.String,
              "kickoff": pl.String, "_i": pl.Int64, "_j": pl.Int64}
    srcs, labs, fetched = {}, [], {}
    con = duckdb.connect(str(args.live_db), read_only=True)
    try:
        # each season's points under ITS league scoring; a ledger frozen under other rules is refused, never relabelled
        for season in sorted({l["season"] for l in ledgers}):
            _, scoring = league_scoring(con, season)
            for l in ledgers:
                frozen_fp = (l.get("artifact") or {}).get("scoring_fingerprint")
                if l["season"] == season and frozen_fp != scoring.fingerprint:
                    raise ValueError(f"s{season}w{l['week']:02d} was frozen under scoring {frozen_fp}, but the "
                                     f"league's {season} scoring is {scoring.fingerprint}: its outcomes would be scored "
                                     "under other rules")
            srcs[season] = load_sources(con, scoring.require_supported(), [season])
            fetched[season] = nfl_fetch_times(con, season)["schedules"]
    finally:
        con.close()
    for season, src in srcs.items():
        recs = [{"season": l["season"], "week": l["week"], "key": key(l["season"], l["week"]), "gsis_id": r["gsis_id"],
                 "team": r["team"], "kickoff": r["kickoff"], "_i": i, "_j": j}
                for i, l in enumerate(ledgers) if l["season"] == season for j, r in enumerate(l["rows"])]
        labs.append(wl.label_outcomes(pl.DataFrame(recs, schema=schema), src, fetched))
    lab = pl.concat(labs, how="diagonal_relaxed")
    final = {}
    for l in ledgers:
        f = wl.week_finality(srcs[l["season"]], l["season"], l["week"], fetched.get(l["season"]))
        missing = lab.filter((pl.col("season") == l["season"]) & (pl.col("week") == l["week"])
                             & (pl.col("outcome_status") == wl.MISSING_INGESTION)).height
        final[(l["season"], l["week"])] = f["fully_final"] and not missing     # a team the ledger saw is still open
    return lab, final


def _coverage_ci(y: np.ndarray, q: np.ndarray, keys: np.ndarray) -> list | None:
    """Week-clustered 95% CI of coverage80 (per-row P(0.1 < PIT <= 0.9), randomized PIT)."""
    lo, hi = pd_.cdf_left(q, pd_.TAUS, y), pd_.cdf_right(q, pd_.TAUS, y)
    inside = pd_.pit_at_most(lo, hi, 0.9) - pd_.pit_at_most(lo, hi, 0.1)
    ci = pd_.week_bootstrap(keys, inside)["ci95"]
    return None if ci is None else [round(c, 4) for c in ci]


def check_bounds(block: dict, criteria: dict, min_rows: int) -> dict:
    """The pre-registered validation bounds, applied descriptively (monitoring; nothing is decided here)."""
    if block.get("n", 0) < min_rows:
        return {"status": "insufficient_data", "reasons": [f"n {block.get('n', 0)} < {min_rows}"]}
    reasons = [f"{c}: observed {round(block[c], 4)}, bounds {criteria[c]}" for c in CHECKED
               if c in criteria and block.get(c) is not None and not criteria[c][0] <= block[c] <= criteria[c][1]]
    return {"status": "outside_bounds" if reasons else "within_bounds", "reasons": reasons}


def conditional_block(y: np.ndarray, q: np.ndarray, keys: np.ndarray) -> dict:
    if len(y) == 0:
        return {"n": 0}
    cal = pd_.calibration(y, q)
    out = {k: _r(v) for k, v in cal.items() if k != "n"} | {"n": cal["n"], "weeks": int(len(np.unique(keys)))}
    out["crps"] = _r(pd_.row_losses(y, q)["crps"].mean())
    out["coverage80_ci95"] = _coverage_ci(y, q, keys)
    out["bounds"] = check_bounds(out, pd_.VALIDATION_CRITERIA, pd_.VALIDATION_CRITERIA["min_rows"])
    return out


def unconditional_block(y, p, played, mean, q, keys) -> dict:
    import polars as pl

    if len(y) == 0:
        return {"n": 0}
    df = pl.DataFrame({"label": y, "p": p, "played": played, "mean": mean, "key": keys})
    b = pd_.unconditional_block(df, q)
    out = {k: _r(b[k]) for k in ("coverage80", "p10_below_rate", "p90_above_rate", "p50_mid_rate",
                                 "inclusive_coverage80", "width80", "expected_points_mae")}
    out.update(n=b["n"], weeks=b["weeks"], absence={k: _r(v) for k, v in b["absence"].items()})
    crit = pd_.UNCONDITIONAL_CRITERIA
    out["bounds"] = check_bounds(out, {"coverage80": crit["coverage80"]}, pd_.VALIDATION_CRITERIA["min_rows"])
    gap = abs(b["absence"]["predicted"] - b["absence"]["observed"])
    if out["bounds"]["status"] != "insufficient_data" and gap > crit["absence_calibration_tolerance"]:
        out["bounds"] = {"status": "outside_bounds", "reasons": out["bounds"]["reasons"] + [
            f"absence rate: predicted {round(b['absence']['predicted'], 4)} vs observed "
            f"{round(b['absence']['observed'], 4)} (tolerance {crit['absence_calibration_tolerance']})"]}
    return out


def _qmat(qs: list) -> np.ndarray:
    return np.array(qs, float) if qs else np.empty((0, len(pd_.TAUS)))


def score_ledgers(ledgers: list[dict], lab, final: dict) -> dict:
    """Prospective coverage per mean model and position on fully final weeks, from the ledgers' frozen quantiles."""
    import polars as pl

    final_keys = {key(s, w) for (s, w), ok in final.items() if ok}
    ev = lab.filter(pl.col("key").is_in(list(final_keys)) & pl.col("outcome_status").is_in(list(wl.RESOLVED)))
    out = {"weeks_scored": sorted(f"{s}-{w:02d}" for (s, w), ok in final.items() if ok),
           "weeks_not_final": sorted(f"{s}-{w:02d}" for (s, w), ok in final.items() if not ok),
           "artifacts": sorted({l["artifact"]["content_hash"] for l in ledgers}),
           "outcomes": {s: int(n) for s, n in lab.group_by("outcome_status").len().iter_rows()} if lab.height else {},
           "policy": POSITION_POLICY, "criteria": {"conditional": pd_.VALIDATION_CRITERIA,
                                                   "unconditional": pd_.UNCONDITIONAL_CRITERIA},
           "by_mean": {}}
    for m in MEANS:
        per = {}
        for pos in POSITIONS:
            cond, unc = [], []
            for r in ev.iter_rows(named=True):
                row = ledgers[r["_i"]]["rows"][r["_j"]]
                if row["position"] != pos:
                    continue
                d = row["distributions"][m]
                if "q" not in d:
                    continue
                if r["outcome_status"] == wl.APPEARED:
                    cond.append((r["label"], d["q"], r["key"]))
                if row.get("p_play") is not None:
                    unc.append((r["label"], d["q"], r["key"], row["p_play"], d["mean"],
                                1.0 if r["outcome_status"] == wl.APPEARED else 0.0))
            entry = {"policy": POSITION_POLICY[pos]}
            entry["conditional"] = conditional_block(np.array([c[0] for c in cond], float), _qmat([c[1] for c in cond]),
                                                     np.array([c[2] for c in cond]))
            if POSITION_POLICY[pos]["unconditional"] == "candidate":
                entry["unconditional"] = unconditional_block(
                    np.array([u[0] for u in unc], float), np.array([u[3] for u in unc], float),
                    np.array([u[5] for u in unc], float), np.array([u[4] for u in unc], float),
                    _qmat([u[1] for u in unc]), np.array([u[2] for u in unc]))
            else:
                entry["unconditional"] = {"withheld": POSITION_POLICY[pos]["note"]}
            per[pos] = entry
        out["by_mean"][m] = {"role": "primary (the mean the distributions were validated on)" if m == PRIMARY_MEAN
                             else "secondary (production mean; never part of the validation)", "positions": per}
    return out


def load_ledgers(args) -> list[dict]:
    """Every frozen week, each checked against its own rows hash (a damaged file is refused, not scored)."""
    out = []
    for f in sorted((ledger_dir(args) / "weeks").glob("s*-pregame.json")):
        led = json.loads(f.read_text())
        if _sha_bytes(_canon(led["rows"]).encode()) != led["rows_sha256"]:
            raise ValueError(f"{f.name}: rows do not match their recorded sha256")
        out.append(led)
    return out


def run_score(args, *, now: datetime | None = None, labeller=None) -> dict:
    """Score every fully final frozen week. Never raises; writes scores.json (derived, replaced)."""
    now = now or _utcnow()
    rec = {"kind": "score", "scored_at": now.isoformat(), "warnings": [], "notes": []}
    try:
        ledgers = load_ledgers(args)
        if not ledgers:
            return {**rec, "status": "no_ledgers", "notes": ["no distribution ledger frozen yet: nothing to score"]}
        lab, final = (labeller or _labelled)(args, ledgers)
        scores = {"schema": "point-distribution-scores/v1", "scored_at": now.isoformat(),
                  "ledgers": [f"s{l['season']}w{l['week']:02d}" for l in ledgers], **score_ledgers(ledgers, lab, final)}
        rec.update(status="scored", scores=scores)
        if args.write:
            _replace_json(ledger_dir(args) / "scores.json", scores)
    except Exception as err:          # noqa: BLE001
        rec.update(status="failed", error=f"{err.__class__.__name__}: {err}")
        rec["warnings"].append(f"scoring the distribution ledgers failed: {err}")
    return rec


# ----------------------------------------------------------------------------- commands


def _summary(rec: dict) -> dict:
    out = {k: v for k, v in rec.items() if k != "scores"}
    if rec.get("status") == "scored":
        s = rec["scores"]
        out["weeks_scored"], out["weeks_not_final"] = s["weeks_scored"], s["weeks_not_final"]
        out["coverage80"] = {m: {p: {"conditional": e["conditional"].get("coverage80"),
                                     "conditional_n": e["conditional"].get("n"),
                                     "conditional_bounds": (e["conditional"].get("bounds") or {}).get("status"),
                                     "unconditional": e["unconditional"].get("coverage80")}
                                 for p, e in b["positions"].items()} for m, b in s["by_mean"].items()}
    return out


def _target_week(args, now: datetime) -> int | None:
    """The upcoming week: the earliest REG week with a game still to kick off (refresh_all.py's rule)."""
    import duckdb
    from app.analytics.injury_freshness import scheduled_kickoffs

    con = duckdb.connect(str(args.live_db), read_only=True)
    try:
        weeks = [r[0] for r in con.execute("SELECT DISTINCT week FROM nfl_schedules WHERE season = ? AND "
                                           "game_type = 'REG' ORDER BY week", [args.season]).fetchall()]
        for w in weeks:
            ks = [k for k in scheduled_kickoffs(con, args.season, int(w)).values() if k is not None]
            if ks and max(ks) > now:
                return int(w)
        return None
    finally:
        con.close()


def _last_week(args) -> int | None:
    import duckdb

    con = duckdb.connect(str(args.live_db), read_only=True)
    try:
        row = con.execute("SELECT max(week) FROM nfl_schedules WHERE season = ? AND game_type = 'REG'",
                          [args.season]).fetchone()
    finally:
        con.close()
    return None if row is None or row[0] is None else int(row[0])


def cmd_freeze(args, now: datetime) -> dict:
    week = args.week if args.week is not None else _target_week(args, now)
    if week is None:
        return {"kind": "freeze", "status": "no_upcoming_week", "warnings": [], "notes": []}
    return run_freeze(args, args.season, week, now=now)


def cmd_score(args, now: datetime) -> dict:
    return _summary(run_score(args, now=now))


def cmd_cycle(args, now: datetime) -> dict:
    """learning_cycle.py's stage: score the frozen weeks, record a missed previous week, then freeze the target week
    (right after the cycle's `predict` stage froze that week's pregame forecast)."""
    week = args.week if args.week is not None else _target_week(args, now)
    out = {"score": _summary(run_score(args, now=now))}
    missed = []
    try:
        # every elapsed prospective week before the target (or through the season's last week once it has started)
        # with neither a ledger nor a missed record: a cycle that did not run for weeks still records each of them
        last = week - 1 if week else _last_week(args)
        first = 1 if args.season > FIRST_PROSPECTIVE[0] else FIRST_PROSPECTIVE[1]
        for w in range(first, (last or 0) + 1):
            rec = check_missed(args, args.season, w, now=now)
            if rec:
                missed.append(rec)
    except Exception as err:          # noqa: BLE001
        out["score"]["warnings"].append(f"missed-week check failed: {err}")
    out["missed"] = missed
    out["freeze"] = (run_freeze(args, args.season, week, now=now) if week
                     else {"status": "no_upcoming_week", "warnings": [], "notes": []})
    out["warnings"] = [*out["score"]["warnings"], *out["freeze"]["warnings"]]
    for m in missed:
        out["warnings"].append(f"week {m['week']} started without a frozen distribution ledger (recorded as missed)")
    out["status"] = "failed" if "failed" in (out["score"]["status"], out["freeze"]["status"]) else "ok"
    return out


def cmd_status(args, now: datetime) -> dict:
    d = ledger_dir(args)
    scores = d / "scores.json"
    return {"artifacts": [p.name for p in list_artifacts(args)], "pin": current_pin(args),
            "ledgers": sorted(p.name for p in (d / "weeks").glob("*.json")),
            "missed": sorted(p.name for p in (d / "missed").glob("*.json")),
            "scores": _summary({"status": "scored", "scores": json.loads(scores.read_text())}) if scores.exists() else None}


def cmd_pin(args, now: datetime) -> dict:
    if not args.artifact or not args.reason:
        raise SystemExit("pin needs --artifact FILE and --reason TEXT (every later week uses the new pin)")
    path = Path(args.artifact)
    path = path if path.is_absolute() or path.exists() else research_dir(args) / path
    if path.resolve().parent != research_dir(args).resolve():
        raise SystemExit(f"the artifact must live in {research_dir(args)} (where the ledger verifies it)")
    pin = make_pin(args, path, how="explicit", reason=args.reason, now=now)
    if args.write:
        write_pin(args, pin)
    return {"kind": "pin", "status": "pinned" if args.write else "would_pin", "pin": pin, "warnings": [], "notes": []}


def parse(argv=None):
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["freeze", "score", "cycle", "status", "pin"])
    ap.add_argument("--season", type=int, default=settings.nfl_season)
    ap.add_argument("--week", type=int, help="default: the upcoming week (earliest with a game still to kick off)")
    ap.add_argument("--ifp-dir", type=Path, default=_path("IFP_MODEL_DIR", "data/models"),
                    help="holds research/point_distributions/ (the artifacts and this ledger)")
    ap.add_argument("--weekly-dir", type=Path, default=_path("WEEKLY_MODEL_DIR", "data/models/weekly"),
                    help="the weekly learning output holding the frozen pregame forecasts")
    ap.add_argument("--live-db", type=Path, default=settings.db_path)
    ap.add_argument("--artifact", help="pin: the artifact file (in research/point_distributions/)")
    ap.add_argument("--reason", help="pin: why the pin changes (recorded)")
    ap.add_argument("--now", help="decide at this ISO time; implies --dry-run")
    ap.add_argument("--dry-run", action="store_true", help="decide and print only: nothing is written")
    args = ap.parse_args(argv)
    args.write = not (args.dry_run or args.now)
    return args


def main(argv=None) -> int:
    args = parse(argv)
    now = _utcnow()
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    cmds = {"freeze": cmd_freeze, "score": cmd_score, "cycle": cmd_cycle, "status": cmd_status, "pin": cmd_pin}
    out = cmds[args.command](args, now)
    print(json.dumps(out, indent=1, default=str))
    if args.command == "status":
        return EXIT_OK
    for w in out.get("warnings", []):
        print(f"WARNING: {pd_.DISTRIBUTION_VERSION} ledger: {w}", file=sys.stderr)
    if out.get("status") == "failed":
        return EXIT_FAILED
    return EXIT_WARNINGS if out.get("warnings") else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
