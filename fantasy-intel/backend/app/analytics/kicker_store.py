"""Immutable kicker projection runs (kfp-*): write-once storage, integrity and the consumer read side.

SQL + stdlib only, so the hosted API can import it. Mirrors the skill-position run contract (production_forecasts and
the skill-position run writer) in PARALLEL tables, kicker_projection_runs / kicker_projection_rows:

  * a run is sealed by `content_sha256` over its canonical meta (HASHED_META) and rows (ROW_COLUMNS); run_id,
    created_at, nfl_synced_at and the kickoff/pregame annotations are excluded;
  * `write_run` never updates or deletes: an identical retry returns the newest equal run, a run_id that would
    receive different content raises ImmutableRunError, anything else is a new run;
  * every reader recomputes the hash (`integrity_problem`); a run whose rows were lost or edited is never shown;
  * the artifact gate: a run counts only if its artifact_id is registered for its model version in the
    write-once approved_artifacts table (fail OPEN, marked "unverified", while nothing is registered for the
    version yet, exactly like ifp-2.0.0).

Kicker runs never touch the skill-position run tables, so they cannot change an ifp run's hash, its selection or
any evidence reader of the skill-position models.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Iterable

from ..db.connection import fetch_dicts, insert_rows, table_exists, utcnow

RUNS = "kicker_projection_runs"
ROWS = "kicker_projection_rows"
APPROVED = "approved_artifacts"
MODEL_VERSION = "kfp-1.0.0"
CONSUMER_VERSIONS = (MODEL_VERSION,)

ROW_COLUMNS = ("season", "week", "espn_player_id", "gsis_id", "nfl_team", "opponent", "is_home", "venue_id",
               "coverage_status", "projected_points", "interval_low", "interval_high", "p_play", "expected_points",
               "stat_line", "context", "explanation", "flags", "missing_inputs")
_JSON = {"stat_line", "context", "explanation", "flags", "missing_inputs", "scoring", "model_status", "evaluation",
         "weather_summary"}
_DOUBLE = {"projected_points", "interval_low", "interval_high", "p_play", "expected_points"}
_INT = {"season", "week", "espn_player_id", "data_through_season", "data_through_week", "forecast_horizon",
        "projected_from_week"}
_BOOL = {"is_home"}
HASHED_META = ("season", "week", "model_version", "scoring_fingerprint", "scoring", "artifact_id", "artifact_created_at",
               "data_through_season", "data_through_week", "model_status", "evaluation", "weather_summary",
               "forecast_horizon", "projected_from_week")

ZERO_STATUSES = {"bye", "withheld_unavailable"}
NOT_REGISTERED = "artifact not registered as approved"
MISSING_ROWS = "forecast rows missing"


class ImmutableRunError(ValueError):
    """An attempt to store different content under a run_id that already exists."""


def _canon(col: str, v):
    if v is None:
        return None
    if col in _JSON:
        if isinstance(v, (bytes, bytearray)):
            v = v.decode()
        if isinstance(v, str):
            v = json.loads(v)
        return json.loads(json.dumps(v, default=str, sort_keys=True))
    if col in _DOUBLE:
        return float(v)
    if col in _INT:
        return int(v)
    if col in _BOOL:
        return bool(v)
    return str(v)


def content_sha256(meta: dict, rows: Iterable[dict]) -> str:
    payload = {"meta": {k: _canon(k, meta.get(k)) for k in HASHED_META},
               "rows": [[_canon(c, r.get(c)) for c in ROW_COLUMNS]
                        for r in sorted(rows, key=lambda r: int(r["espn_player_id"]))]}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def integrity_problem(run: dict, rows: list[dict]) -> str | None:
    expected = run.get("row_count")
    if not expected or not rows:
        return f"{MISSING_ROWS}: the run has no stored rows"
    if len(rows) != expected:
        return f"integrity: {len(rows)} stored rows but the run recorded row_count {expected}"
    if not run.get("content_sha256"):
        return "integrity: run has no content hash (unsealed)"
    if content_sha256(run, rows) != run["content_sha256"]:
        return "integrity: stored rows do not match the run's content_sha256"
    return None


# ----------------------------------------------------------------------------- kickoff annotation


def _kickoffs(con, season: int, week: int) -> dict[str, datetime]:
    from .production_forecasts import team_kickoffs

    return {team: ko for (w, team), ko in team_kickoffs(con, season, week).items() if w == week}


def _utc(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)


def is_pregame(created, kickoff) -> bool:
    c, k = _utc(created), _utc(kickoff)
    return c is not None and k is not None and c < k


# ----------------------------------------------------------------------------- write


def write_run(con, meta: dict, rows: list[dict]) -> dict:
    """Store one run, append-only. Returns {run_id, created, row_count, content_sha256}."""
    if not rows:
        raise ValueError("refusing to store a kicker run with no rows")
    run_id, digest = meta["run_id"], content_sha256(meta, rows)
    con.execute("BEGIN TRANSACTION")
    try:
        same = fetch_dicts(con, f"SELECT content_sha256, row_count FROM {RUNS} WHERE run_id = ?", [run_id])
        if same:
            if same[0]["content_sha256"] != digest or same[0]["row_count"] != len(rows):
                raise ImmutableRunError(f"kicker run {run_id} is already stored with different content")
            result = {"run_id": run_id, "created": False}
        else:
            twin = fetch_dicts(con, f"""
                SELECT run_id, content_sha256 FROM {RUNS} WHERE season = ? AND week = ? AND model_version = ?
                  AND scoring_fingerprint IS NOT DISTINCT FROM ? AND artifact_id IS NOT DISTINCT FROM ?
                  AND coalesce(forecast_horizon, 0) = ?
                ORDER BY created_at DESC, run_id DESC LIMIT 1""",
                               [meta["season"], meta["week"], meta["model_version"], meta.get("scoring_fingerprint"),
                                meta.get("artifact_id"), int(meta.get("forecast_horizon") or 0)])
            if twin and twin[0]["content_sha256"] == digest:
                result = {"run_id": twin[0]["run_id"], "created": False}
            else:
                kick = _kickoffs(con, meta["season"], meta["week"])
                created = meta.get("created_at") or utcnow()
                stamped = []
                for r in rows:
                    ko = kick.get(r.get("nfl_team"))
                    stamped.append({**{c: r.get(c) for c in ROW_COLUMNS}, "run_id": run_id,
                                    "kickoff": None if ko is None else ko.astimezone(timezone.utc).replace(tzinfo=None),
                                    "pregame": is_pregame(created, ko)})
                insert_rows(con, RUNS, [{**{k: meta.get(k) for k in HASHED_META}, "run_id": run_id,
                                         "nfl_synced_at": meta.get("nfl_synced_at"), "created_at": created,
                                         "content_sha256": digest, "row_count": len(rows)}])
                insert_rows(con, ROWS, stamped)
                back = fetch_dicts(con, f"SELECT * FROM {ROWS} WHERE run_id = ?", [run_id])
                stored = fetch_dicts(con, f"SELECT * FROM {RUNS} WHERE run_id = ?", [run_id])[0]
                if len(back) != len(rows) or content_sha256(stored, back) != digest:
                    raise RuntimeError(f"kicker run {run_id}: stored rows do not reproduce the content digest")
                result = {"run_id": run_id, "created": True}
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {**result, "row_count": len(rows), "content_sha256": digest}


def register_artifact(con, *, model_version: str, artifact_sha256: str, target_season: int | None,
                      training_cutoff_key: int | None, artifact_created_at: str | None, source: str = "manual_pin") -> bool:
    """Approve a kfp artifact in the shared write-once approved_artifacts table (keyed by model_version, so it can
    never approve an ifp artifact). Returns True when a row was created."""
    if not model_version.startswith("kfp-"):
        raise ValueError("kicker artifacts are registered under a kfp-* model version")
    if not isinstance(artifact_sha256, str) or len(artifact_sha256) != 64 \
            or any(c not in "0123456789abcdef" for c in artifact_sha256):
        raise ValueError("an approved artifact needs the lowercase hex sha256 of the artifact file")
    if source not in ("learning_cycle", "manual_pin"):
        raise ValueError("source must be learning_cycle or manual_pin")
    before = con.execute(f"SELECT count(*) FROM {APPROVED}").fetchone()[0]
    con.execute(f"INSERT INTO {APPROVED} VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [model_version, artifact_sha256, target_season, None, training_cutoff_key, artifact_created_at, source,
                 utcnow()])
    return con.execute(f"SELECT count(*) FROM {APPROVED}").fetchone()[0] > before


# ----------------------------------------------------------------------------- read


def artifact_gate(con, model_version: str):
    """check(run) -> (status, reason): verified | unverified (fail open: nothing registered for the version) |
    rejected. A registered artifact with a training cutoff covers runs of its target season after the cutoff."""
    if not table_exists(con, APPROVED):
        return lambda run: ("unverified", "no approved_artifacts table yet")
    regs: dict[str, list[tuple]] = {}
    for r in fetch_dicts(con, f"SELECT artifact_sha256, target_season, training_cutoff_key FROM {APPROVED} "
                              "WHERE model_version = ?", [model_version]):
        regs.setdefault(r["artifact_sha256"], []).append((r["target_season"], r["training_cutoff_key"]))
    if not regs:
        return lambda run: ("unverified", "no approved artifacts registered for this model yet")

    def check(run):
        targets = regs.get(run.get("artifact_id"))
        if targets is None:
            return "rejected", NOT_REGISTERED
        for season, cutoff in targets:
            if season is None or (run.get("season") == season and (
                    cutoff is None or run["season"] * 100 + run["week"] > cutoff)):
                return "verified", None
        return "rejected", f"{NOT_REGISTERED} for this target week"
    return check


def verified_runs(con, season: int, week: int, *, model_version: str = MODEL_VERSION,
                  scoring_fingerprint: str | None = None):
    """Newest-first (run, rows, problem); problem None = the run may be shown."""
    if not (table_exists(con, RUNS) and table_exists(con, ROWS)):
        return
    where, params = ["season = ?", "week = ?", "model_version = ?"], [season, week, model_version]
    if scoring_fingerprint is not None:
        where.append("scoring_fingerprint = ?")
        params.append(scoring_fingerprint)
    gate = artifact_gate(con, model_version)
    for run in fetch_dicts(con, f"SELECT * FROM {RUNS} WHERE {' AND '.join(where)} "
                                "ORDER BY created_at DESC NULLS LAST, run_id DESC", params):
        status, why = gate(run)
        run = {**run, "artifact_check": status, "artifact_note": why}
        if status == "rejected":
            yield run, [], why
            continue
        rows = fetch_dicts(con, f"SELECT * FROM {ROWS} WHERE run_id = ?", [run["run_id"]])
        yield run, rows, integrity_problem(run, rows)


def league_fingerprint(con, season: int) -> tuple[str | None, str | None]:
    """(kicker scoring fingerprint of the live league, why unusable)."""
    from .kicker_scoring import league_items, resolve_kicker_scoring

    items = league_items(con, season)
    if not items:
        return None, "the league's scoring settings are not synced"
    ks = resolve_kicker_scoring(items)
    if not ks.supported:
        return None, "the league's kicker scoring uses a rule the kicker model does not implement"
    return ks.fingerprint, None


def select_run(con, season: int, week: int) -> dict:
    """The kicker run consumers see: newest kfp run under the league's current kicker scoring whose artifact is
    approved and whose rows verify. {run, rows, scoring_fingerprint, skipped, unavailable_reason}."""
    out = {"run": None, "rows": None, "scoring_fingerprint": None, "skipped": [], "unavailable_reason": None}
    if not (table_exists(con, RUNS) and table_exists(con, ROWS)):
        return {**out, "unavailable_reason": "No kicker projections have been computed."}
    fp, why = league_fingerprint(con, season)
    if fp is None:
        return {**out, "unavailable_reason": f"No kicker projections: {why}."}
    out["scoring_fingerprint"] = fp
    for version in CONSUMER_VERSIONS:
        for run, rows, problem in verified_runs(con, season, week, model_version=version, scoring_fingerprint=fp):
            if problem is None:
                return {**out, "run": _decode_run(run), "rows": [_decode_row(r) for r in rows]}
            out["skipped"].append({"run_id": run["run_id"], "model_version": run["model_version"],
                                   "created_at": str(run["created_at"]), "reason": problem})
    why = (f"No verified {MODEL_VERSION} kicker run under the league's kicker scoring is stored for week {week}"
           + (f" ({'; '.join(s['run_id'] + ': ' + s['reason'] for s in out['skipped'])})" if out["skipped"] else ""))
    return {**out, "unavailable_reason": why + "."}


def _decode_run(run: dict) -> dict:
    return {k: (json.loads(v) if k in _JSON and isinstance(v, str) else v) for k, v in run.items()}


def _decode_row(row: dict) -> dict:
    return {k: (json.loads(v) if k in _JSON and isinstance(v, str) else v) for k, v in row.items()}


def overlay(con, season: int, week: int) -> dict:
    """Per-player values for the weekly source switch: {values {pid: float}, origin {pid: 'ours'|'ours_zero'}, run}.
    Only rows that carry an estimate (projected with expected points) or a credible zero (bye, withheld) appear;
    every other kicker keeps ESPN's number."""
    sel = select_run(con, season, week)
    if sel["run"] is None:
        return {"values": {}, "origin": {}, "run": None, "reason": sel["unavailable_reason"], "skipped": sel["skipped"]}
    values, origin = {}, {}
    for r in sel["rows"]:
        pid = r["espn_player_id"]
        if r["coverage_status"] == "projected" and r.get("expected_points") is not None:
            values[pid], origin[pid] = float(r["expected_points"]), "ours"
        elif r["coverage_status"] in ZERO_STATUSES:
            values[pid], origin[pid] = 0.0, "ours_zero"
    run = sel["run"]
    return {"values": values, "origin": origin, "reason": None, "skipped": sel["skipped"],
            "run": {"run_id": run["run_id"], "model_version": run["model_version"], "created_at": str(run["created_at"]),
                    "artifact_verified": run.get("artifact_check") == "verified",
                    "forecast_horizon": int(run.get("forecast_horizon") or 0)}}
