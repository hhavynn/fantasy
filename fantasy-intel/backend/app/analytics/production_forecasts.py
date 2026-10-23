"""Immutable production forecasts: the audited read side of the independent projection runs.

SQL + stdlib only (no polars/numpy), so the hosted API can import it.

Storage model (schema v8, docs/independent_projection_engine.md "Live runs and abstention"):

  independent_projection_runs   one row per `project` run (write-once; provenance + integrity columns)
  independent_projection_rows   one row per (run_id, espn_player_id); append-only, never updated or deleted
  independent_player_projections  LEGACY v7 table: one row per (season, week, player), overwritten by the
                                  next run of the same week. Never written or dropped; read only as a
                                  fallback and as the source of the v8 backfill (`migrate_legacy`).

A run is *sealed* by `content_sha256`, the digest of its canonicalised meta and rows (run_id, created_at,
nfl_synced_at, kickoff and pregame excluded: the first two are identities/operational stamps, the last two
are annotations derived from nfl_schedules). Every reader recomputes it through `integrity_problem`: the
consumer view (`latest_run`, `verified_runs`, used by projection_source.select_run) and the evidence reader
(`pregame_forecasts`) alike, so a run whose rows were lost, edited (even with the row count unchanged) or only
partially copied is never shown and never counted; the evidence reader also says why.

Two populations, never mixed: the CONSUMER view is the newest verified run, whenever it was made (a late
update made after some kickoffs is a legitimate live prediction). The EVIDENCE population
(`pregame_forecasts`) is per player the latest verified run created strictly before his own kickoff, from an
artifact fitted before the week's first kickoff. Runs are append-only, so a later run never rewrites a frozen
pregame row, and a post-kickoff row never becomes evidence.

Time rule (CONTRACTS section 2): a row is PREGAME iff run created_at < the kickoff of the player's OWN team's
game; an unknown kickoff is never pregame. The kickoff stored at write time and the one in the schedule now
are both consulted and the EARLIER known one wins, so a flexed game can only make a row less eligible.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Iterable

from ..db.connection import fetch_dicts, table_columns, table_exists
from . import production_pin
from .projection_backtest import _kickoff, _utc

RUNS = "independent_projection_runs"
ROWS = "independent_projection_rows"
LEGACY = "independent_player_projections"

# Row columns shared by the legacy and the run-keyed table, in storage order (run_id first in both).
ROW_COLUMNS = ("season", "week", "espn_player_id", "gsis_id", "position", "nfl_team", "opponent", "is_home",
               "projected_points", "interval_low", "interval_high", "model", "coverage_status", "games_used",
               "latest_usage_season", "latest_usage_week", "missing_inputs", "flags", "explanation", "p_play",
               "expected_points", "participation")
_JSON = {"missing_inputs", "flags", "explanation", "participation"}
_DOUBLE = {"projected_points", "interval_low", "interval_high", "p_play", "expected_points"}
_INT = {"season", "week", "espn_player_id", "games_used", "latest_usage_season", "latest_usage_week"}
_BOOL = {"is_home"}

META_JSON = ("model_status", "evaluation", "participation_status", "participation_evaluation")
META_INT = ("season", "week", "data_through_season", "data_through_week", "forecast_horizon", "projected_from_week")
META_STR = ("model_version", "scoring_fingerprint", "artifact_id", "artifact_created_at", "participation_version")
HASHED_META = META_INT + META_STR + META_JSON
# Rest-of-season runs (docs/independent_projection_engine.md "Rest-of-season projections"): forecast_horizon =
# target week - projected_from_week; NULL (legacy) means 0. Both are in HASHED_META, but only a run with a horizon
# above 0 contributes them to its digest, so every horizon-0 run (stored before or after this change) keeps the
# digest it was sealed with and an unchanged rerun stays idempotent across the upgrade.
HORIZON_META = ("forecast_horizon", "projected_from_week")
HORIZON_NOTE = "rest-of-season forecast (horizon {k})"
# Marker of the flag a horizon >= 1 row carries for an ESPN OUT / DOUBTFUL / QUESTIONABLE status (next game only, not
# applied to the target week). Informational: a horizon-k run's digest ignores it, so a status flip does not make
# every future week's run "different" and store a new copy of all of them.
NEXT_GAME_ONLY = "describes the next game only"
NOT_EVIDENCE = "rest-of-season forecast (horizon {k}), not evidence"


def horizon_of(run: dict | None) -> int:
    """forecast_horizon of a stored run; NULL (a run stored before rest-of-season projections) is horizon 0."""
    v = None if run is None else run.get("forecast_horizon")
    return 0 if v is None else int(v)


def evidence_horizon_sql(con, alias: str = "") -> str:
    """` AND coalesce(forecast_horizon, 0) = 0` for a reader that selects runs itself (evidence only counts the
    validated horizon 0; NULL, a run stored before rest-of-season projections, is horizon 0). Empty when the
    column does not exist yet (a read-only copy that predates it: no run there has a horizon)."""
    if "forecast_horizon" not in table_columns(con, RUNS):
        return ""
    return f" AND coalesce({alias}forecast_horizon, 0) = 0"


def horizon_note(run: dict | None) -> str | None:
    """Run-level experimental label for a rest-of-season run (horizon > 0); None for the validated horizon 0."""
    k = horizon_of(run)
    return HORIZON_NOTE.format(k=k) if k > 0 else None


NOT_APPROVED_ARTIFACT = "artifact not the approved production artifact"     # ifp-1.0.0 sha pin
NOT_REGISTERED = "artifact not registered as approved"                      # ifp-2.x approved_artifacts
NO_TABLE = "no approved_artifacts table yet"                                 # why a run is "unverified"
NO_ROWS = "no approved artifacts registered for this model yet"
APPROVED = "approved_artifacts"
_PIN = object()     # "use the production rule" for the model version (production_pin), as opposed to an explicit sha


def _unchecked(run):
    return "unchecked", None


def artifact_gate(con, model_version: str | None, artifact_id=_PIN):
    """check(run) -> (status, reason) for runs of `model_version`; status is one of:

    * "unchecked"  no artifact rule applies (another version, or ifp-1.0.0 while unpinned): the run counts;
    * "verified"   the run's artifact_id matches the pin / is registered in approved_artifacts: counts;
    * "unverified" ifp-2.x on a database WITHOUT approved_artifacts (an old local database or hosted copy),
                   or whose approved_artifacts has NO row for the model yet (the table exists but nothing has
                   been registered, as right after an upgrade): counts, but is marked unverified (fail OPEN:
                   failing closed would blank production until the owner re-syncs, republishes or re-runs the
                   cycle). Enforcement starts per model with its first registered artifact;
    * "rejected"   excluded, with `reason`.

    An explicit `artifact_id` (a sha, or None for no rule) overrides the per-version default."""
    def pinned(sha):
        def check(run):
            if run.get("artifact_id") == sha:
                return "verified", None
            return "rejected", NOT_APPROVED_ARTIFACT
        return check

    if artifact_id is not _PIN:
        return _unchecked if artifact_id is None else pinned(artifact_id)
    if model_version == production_pin.PINNED_MODEL_VERSION:
        sha = production_pin.APPROVED_ARTIFACT_SHA256
        return _unchecked if sha is None else pinned(sha)
    if model_version not in production_pin.REGISTERED_MODEL_VERSIONS:
        return _unchecked
    if not table_exists(con, APPROVED):
        return lambda run: ("unverified", NO_TABLE)
    targets: dict[str, list[tuple]] = {}
    for r in fetch_dicts(con, f"SELECT artifact_sha256, target_season, target_week, training_cutoff_key FROM {APPROVED} "
                              "WHERE model_version = ?", [model_version]):
        targets.setdefault(r["artifact_sha256"], []).append(
            (r["target_season"], r["target_week"], r["training_cutoff_key"]))

    if not targets:
        return lambda run: ("unverified", NO_ROWS)

    def covers(run, season, week, cutoff) -> bool:
        """May this registered artifact make `run`? No target recorded: any week. A recorded data cutoff: the
        run's season is the artifact's target season and the run's week lies strictly after the cutoff (the
        artifact trained on nothing from that week on), so one weekly artifact also covers the rest-of-season
        runs that reuse it. Without a cutoff, only the exact target week."""
        if season is None or week is None:
            return True
        if cutoff is None:
            return (season, week) == (run.get("season"), run.get("week"))
        return run.get("season") == season and run.get("season") * 100 + run.get("week") > cutoff

    def check(run):
        t = targets.get(run.get("artifact_id"))
        if t is None:
            return "rejected", NOT_REGISTERED
        if any(covers(run, *x) for x in t):
            return "verified", None
        return "rejected", f"{NOT_REGISTERED} for this target week"
    return check


def unapproved_runs(con, season: int, week: int, model_version: str, scoring_fingerprint: str | None) -> int:
    """How many stored runs of `model_version` for the week the artifact rule rejects (they exist, but their
    artifact is not approved), so a reader can say that instead of "no run is stored"."""
    if not table_exists(con, RUNS):
        return 0
    gate = artifact_gate(con, model_version)
    where, params = ["season = ?", "week = ?", "model_version = ?"], [season, week, model_version]
    if scoring_fingerprint is not None:
        where.append("scoring_fingerprint = ?")
        params.append(scoring_fingerprint)
    return sum(gate(r)[0] == "rejected"
               for r in fetch_dicts(con, f"SELECT * FROM {RUNS} WHERE {' AND '.join(where)}", params))


def gate_active(con, model_version: str | None) -> bool:
    """Does a verifying artifact rule apply to `model_version` here (pin set, or approved_artifacts present)?"""
    return artifact_gate(con, model_version)(
        {"artifact_id": None}
    )[0] not in ("unchecked", "unverified")


MISSING_ROWS = "forecast rows missing"


class ImmutableRunError(ValueError):
    """An attempt to store different content under a run_id that already exists."""


# ----------------------------------------------------------------------------- canonical hash


def _canon_json(v):
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        v = v.decode()
    if isinstance(v, str):
        v = json.loads(v)
    return json.loads(json.dumps(v, default=str, sort_keys=True))


def _canon(col: str, v):
    if v is None:
        return None
    if col in _JSON or col in META_JSON:
        return _canon_json(v)
    if col in _DOUBLE:
        return float(v)
    if col in _INT or col in META_INT:
        return int(v)
    if col in _BOOL:
        return bool(v)
    return str(v)


def content_sha256(meta: dict, rows: Iterable[dict]) -> str:
    """Digest of the run's canonicalised meta and rows. Independent of run_id, created_at, nfl_synced_at,
    row order, and of whether values come from Python objects or back from DuckDB."""
    skip = () if horizon_of(meta) > 0 else HORIZON_META
    drop_flags = horizon_of(meta) > 0

    def cell(c, r):
        v = _canon(c, r.get(c))
        if drop_flags and c == "flags" and isinstance(v, list):
            v = [f for f in v if not (isinstance(f, str) and NEXT_GAME_ONLY in f)]
        return v

    payload = {"meta": {k: _canon(k, meta.get(k)) for k in HASHED_META if k not in skip},
               "rows": [[cell(c, r) for c in ROW_COLUMNS] for r in sorted(rows, key=lambda r: int(r["espn_player_id"]))]}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


# ----------------------------------------------------------------------------- kickoffs


def team_kickoffs(con, season: int, week: int | None = None) -> dict[tuple[int, str], datetime]:
    """(week, team) -> aware-UTC kickoff of that team's REG game; unknown/ambiguous kickoffs are absent."""
    if not table_exists(con, "nfl_schedules"):
        return {}
    cols = set(table_columns(con, "nfl_schedules"))
    if not {"gameday", "gametime", "home_team", "away_team", "week"} <= cols:
        return {}
    sql = ("SELECT week, gameday, gametime, home_team, away_team FROM nfl_schedules "
           "WHERE season = ? AND game_type = 'REG'")
    params: list = [season]
    if week is not None:
        sql, params = sql + " AND week = ?", [season, week]
    out: dict[tuple[int, str], datetime] = {}
    for g in fetch_dicts(con, sql, params):
        ko = _kickoff(g)
        if ko is not None:
            for t in (g["home_team"], g["away_team"]):
                out[(int(g["week"]), t)] = ko
    return out


def first_kickoffs(con, season: int) -> dict[int, datetime]:
    out: dict[int, datetime] = {}
    for (week, _), ko in team_kickoffs(con, season).items():
        if week not in out or ko < out[week]:
            out[week] = ko
    return out


def _naive(dt: datetime | None) -> datetime | None:
    return None if dt is None else dt.astimezone(timezone.utc).replace(tzinfo=None)


def annotate(rows: list[dict], season: int, week: int, created_at, kickoffs: dict) -> list[dict]:
    """Kickoff (naive UTC) and pregame flag per row, from the player's own team. Mutates and returns `rows`."""
    created = _utc(created_at)
    for r in rows:
        ko = kickoffs.get((week, r.get("nfl_team")))
        r["kickoff"] = _naive(ko)
        r["pregame"] = bool(ko is not None and created is not None and created < ko)
    return rows


# ----------------------------------------------------------------------------- legacy -> v8 backfill


def migrate_legacy(con) -> dict:
    """Schema v8 backfill. Create-only and idempotent:

    * copies every legacy `independent_player_projections` row whose run exists into
      `independent_projection_rows` under its original run_id (ON CONFLICT DO NOTHING), with the
      player's own kickoff / pregame where nfl_schedules allows;
    * seals each not-yet-sealed run (row_count NULL): row_count, migrated_from_legacy = TRUE and, when
      rows exist, content_sha256 over the copied rows (artifact_id stays NULL: the file was not recorded);
    * runs whose rows were destroyed by the old per-week overwrite keep row_count = 0 and no hash, so every
      reader excludes them with a reason;
    * legacy rows whose run row is missing are counted and reported, never invented.

    The legacy table is only read. Returns (and stores in schema_meta 'projection_rows_backfill') a report."""
    report = {"rows_copied": 0, "runs_sealed": 0, "runs_without_rows": [], "orphan_legacy_rows": 0,
              "orphan_legacy_run_ids": []}
    if not (table_exists(con, RUNS) and table_exists(con, ROWS)):
        return report
    pending = fetch_dicts(con, f"SELECT * FROM {RUNS} WHERE row_count IS NULL ORDER BY created_at, run_id")
    if table_exists(con, LEGACY) and "run_id" in table_columns(con, LEGACY):
        orphans = fetch_dicts(con, f"""SELECT run_id, count(*) AS n FROM {LEGACY}
                                       WHERE run_id IS NULL OR run_id NOT IN (SELECT run_id FROM {RUNS})
                                       GROUP BY 1 ORDER BY 1""")
        report["orphan_legacy_rows"] = sum(o["n"] for o in orphans)
        report["orphan_legacy_run_ids"] = [o["run_id"] for o in orphans]
        if pending:
            kick: dict[tuple, datetime | None] = {}
            for sw in {(r["season"], r["week"]) for r in pending}:
                for (w, team), ko in team_kickoffs(con, sw[0], sw[1]).items():
                    kick[(sw[0], w, team)] = _naive(ko)
            con.execute("CREATE TEMP TABLE _v8_kickoff (season INTEGER, week INTEGER, team VARCHAR, kickoff TIMESTAMP)")
            try:
                if kick:
                    con.executemany("INSERT INTO _v8_kickoff VALUES (?, ?, ?, ?)",
                                    [(s, w, t, k) for (s, w, t), k in kick.items()])
                have = set(table_columns(con, LEGACY))      # an older legacy layout may lack some columns
                cols = ", ".join(ROW_COLUMNS)
                src = ", ".join(f"p.{c}" if c in have else "NULL" for c in ROW_COLUMNS)
                before = con.execute(f"SELECT count(*) FROM {ROWS}").fetchone()[0]
                con.execute(f"""
                    INSERT INTO {ROWS} (run_id, {cols}, kickoff, pregame)
                    SELECT p.run_id, {src}, k.kickoff, coalesce(r.created_at < k.kickoff, false)
                    FROM {LEGACY} p JOIN {RUNS} r ON r.run_id = p.run_id
                    LEFT JOIN _v8_kickoff k ON k.season = p.season AND k.week = p.week AND k.team = p.nfl_team
                    WHERE r.row_count IS NULL AND p.espn_player_id IS NOT NULL
                    ON CONFLICT DO NOTHING""")
                report["rows_copied"] = con.execute(f"SELECT count(*) FROM {ROWS}").fetchone()[0] - before
            finally:
                con.execute("DROP TABLE IF EXISTS _v8_kickoff")
    for run in pending:
        stored = fetch_dicts(con, f"SELECT * FROM {ROWS} WHERE run_id = ?", [run["run_id"]])
        digest = content_sha256(run, stored) if stored else None
        con.execute(f"UPDATE {RUNS} SET row_count = ?, migrated_from_legacy = TRUE, content_sha256 = ? "
                    f"WHERE run_id = ? AND row_count IS NULL", [len(stored), digest, run["run_id"]])
        report["runs_sealed"] += 1
        if not stored:
            report["runs_without_rows"].append(run["run_id"])
    if table_exists(con, "schema_meta"):
        con.execute("DELETE FROM schema_meta WHERE key = 'projection_rows_backfill'")
        con.execute("INSERT INTO schema_meta VALUES ('projection_rows_backfill', ?)", [json.dumps(report)])
    return report


# ----------------------------------------------------------------------------- readers


def rows_source(con) -> str | None:
    """Table consumer readers should read run rows from: the run-keyed table, else the legacy table
    (database or hosted copy not yet migrated), else None."""
    if table_exists(con, ROWS):
        return ROWS
    return LEGACY if table_exists(con, LEGACY) else None


def integrity_problem(run: dict, rows: list[dict]) -> str | None:
    """The one integrity rule every reader applies (consumer selection and pregame evidence alike): the run is
    sealed (row_count and content_sha256 recorded) and its stored rows are exactly the sealed content. None when
    the rows verify, else why not. A row count alone is not proof: an edited value keeps the count."""
    expected = run.get("row_count")
    if not expected or not rows:
        return (f"{MISSING_ROWS}: the run has no stored rows (row_count "
                f"{'NULL' if expected is None else expected}); pre-v8 overwrite or unsealed run")
    if len(rows) != expected:
        return f"integrity: {len(rows)} stored rows but the run recorded row_count {expected}"
    if not run.get("content_sha256"):
        return "integrity: run has no content hash (unsealed)"
    if content_sha256(run, rows) != run["content_sha256"]:
        return "integrity: stored rows do not match the run's content_sha256"
    return None


def verified_runs(con, season: int, week: int, *, model_version: str | None = None,
                  scoring_fingerprint: str | None = None, artifact_id=_PIN):
    """Newest-first (by created_at, then run_id) runs of the week as (run, rows, problem). `run` carries
    `artifact_check` / `artifact_note` from artifact_gate (applied when `model_version` is given); `problem` is
    None when the run may be shown, else the gate's rejection reason (its rows are then not read) or the
    integrity problem. A generator: a caller that stops at the first acceptable run reads no more rows.
    Yields nothing without the run-keyed table: legacy rows cannot be verified, so they are never served."""
    if not (table_exists(con, RUNS) and table_exists(con, ROWS)):
        return
    where, params = ["season = ?", "week = ?"], [season, week]
    if model_version is not None:
        where.append("model_version = ?")
        params.append(model_version)
    if scoring_fingerprint is not None:
        where.append("scoring_fingerprint = ?")
        params.append(scoring_fingerprint)
    gate = _unchecked if model_version is None else artifact_gate(con, model_version, artifact_id)
    for run in fetch_dicts(con, f"SELECT * FROM {RUNS} WHERE {' AND '.join(where)} "
                                "ORDER BY created_at DESC NULLS LAST, run_id DESC", params):
        status, why = gate(run)
        run = {**run, "artifact_check": status, "artifact_note": why, "horizon": horizon_of(run),
               "horizon_note": horizon_note(run)}
        if status == "rejected":
            yield run, [], why
            continue
        rows = fetch_dicts(con, f"SELECT * FROM {ROWS} WHERE run_id = ?", [run["run_id"]])
        yield run, rows, integrity_problem(run, rows)


def latest_run(con, season: int, week: int, *, model_version: str | None = None,
               scoring_fingerprint: str | None = None, artifact_id=_PIN) -> dict | None:
    """Consumer view: the newest run of the week that the artifact rule (artifact_gate) does not reject AND whose
    stored rows verify against its content hash (integrity_problem), or None. A run whose rows were lost, edited
    (even with the row count unchanged) or never sealed is skipped, never shown. The returned run carries
    `artifact_check` ("unchecked" | "verified" | "unverified") and, when unverified, `artifact_note` (why).
    Without the run-keyed table (a database not migrated to schema v8) nothing can be verified, so nothing is
    returned. projection_source.select_run is the consumer policy built on verified_runs.

    CONSUMER selection, deliberately of ANY forecast horizon: the newest verified run for the requested week wins
    whether it is the week's pregame run (horizon 0) or a rest-of-season run made by an earlier cycle (horizon k > 0),
    so a later, better-informed run is never hidden behind an older one. The returned run carries `horizon`
    (forecast_horizon, NULL read as 0) and `horizon_note` (the experimental label, None at horizon 0). Evidence
    readers (pregame_forecasts, eligible_run_ids, the promotion tracker) do the opposite and use horizon 0 only."""
    for run, _, problem in verified_runs(con, season, week, model_version=model_version,
                                         scoring_fingerprint=scoring_fingerprint, artifact_id=artifact_id):
        if problem is None:
            return run
    return None


def _aware(v):
    return _utc(v)


def own_kickoff(row: dict, week: int, now_kick: dict) -> datetime | None:
    """A row's own-team kickoff: the EARLIER of the kickoff stored at write time and the one in the schedule
    now (a flexed game can only make a row less eligible). None when neither is known."""
    known = [k for k in (_aware(row.get("kickoff")), now_kick.get((week, row.get("nfl_team")))) if k is not None]
    return min(known) if known else None


def is_pregame(created, kickoff) -> bool:
    """CONTRACTS section 2: created strictly before the player's own kickoff; unknown is never pregame."""
    created = _aware(created)
    return kickoff is not None and created is not None and created < kickoff


# ----------------------------------------------------------------------------- pregame forecasts


def _assess(con, season: int, weeks: set[int] | None, model_version: str, scoring_fingerprint: str,
            artifact_id=_PIN, include_future_horizons: bool = False) -> tuple[list[dict], list[dict]]:
    """Classify every run of the season: (accepted [{run, rows}], exclusions [{run_id, week, reason}]).
    A rest-of-season run (forecast_horizon > 0) is never evidence: it is excluded first, with the reason
    "rest-of-season forecast (horizon k), not evidence", even when the week's horizon-0 run is missing.
    `include_future_horizons` (False for every evidence reader) exists for audits that list all runs."""
    gate = artifact_gate(con, model_version, artifact_id)
    kick_first = first_kickoffs(con, season)
    sql = f"SELECT * FROM {RUNS} WHERE season = ?"
    params: list = [season]
    if weeks is not None:
        sql += " AND week IN (SELECT unnest(?))"
        params.append(sorted(weeks))
    runs = fetch_dicts(con, sql + " ORDER BY week, created_at, run_id", params)
    if not table_exists(con, ROWS):
        return [], [{"run_id": r["run_id"], "week": r["week"],
                     "reason": "run-keyed forecast table is absent (database not migrated to schema v8)"}
                    for r in runs]
    stored: dict[str, list[dict]] = {}
    if runs:
        for r in fetch_dicts(con, f"SELECT * FROM {ROWS} WHERE run_id IN (SELECT unnest(?))",
                             [[r["run_id"] for r in runs]]):
            stored.setdefault(r["run_id"], []).append(r)
    ok, excluded = [], []
    for run in runs:
        fitted, first = _aware(run.get("artifact_created_at")), kick_first.get(run["week"])
        rows = stored.get(run["run_id"], [])
        if horizon_of(run) > 0 and not include_future_horizons:
            reason = NOT_EVIDENCE.format(k=horizon_of(run))
        elif run["model_version"] != model_version:
            reason = f"model {run['model_version']}, not {model_version}"
        elif run["scoring_fingerprint"] != scoring_fingerprint:
            reason = "made under different league scoring"
        elif gate(run)[0] == "rejected":
            reason = gate(run)[1]
        elif run.get("created_at") is None:
            reason = "run creation time unknown (created_at is NULL), so no row can be shown to be pregame"
        elif (problem := integrity_problem(run, rows)) is not None and problem.startswith(MISSING_ROWS):
            reason = problem
        elif fitted is None or first is None:
            reason = "artifact fit time or week kickoff unknown"
        elif fitted >= first:
            reason = "artifact fitted after the week's first kickoff (it may have trained on that week)"
        elif problem is not None:
            reason = problem
        else:
            ok.append({"run": run, "rows": rows})
            continue
        excluded.append({"run_id": run["run_id"], "week": run["week"], "reason": reason})
    return ok, excluded


def eligible_run_ids(con, season: int, model_version: str, scoring_fingerprint: str,
                     weeks: Iterable[int] | None = None, *, artifact_id=_PIN) -> tuple[list[str], list[dict]]:
    """Runs whose rows may count as evidence (model, scoring, approved artifact,
    artifact-before-first-kickoff, integrity)."""
    if not table_exists(con, RUNS):
        return [], []
    ok, excluded = _assess(con, season, None if weeks is None else set(weeks), model_version, scoring_fingerprint,
                           artifact_id)
    return [a["run"]["run_id"] for a in ok], excluded


def pregame_forecasts(con, season: int, weeks: Iterable[int] | None = None, *,
                      model_version: str, scoring_fingerprint: str, artifact_id=_PIN
                      ) -> tuple[list[dict], list[dict]]:
    """(rows, exclusions). Per (season, week, espn_player_id): the row from the LATEST eligible run whose
    run created_at < that player's own kickoff. Eligible run = model_version match, scoring_fingerprint match,
    artifact_created_at < the week's first kickoff, integrity verified (row_count and content hash), and, once
    the artifact rule of artifact_gate holds (ifp-1.0.0: the sha pin once set; ifp-2.2.0 / ifp-2.1.0 / ifp-2.0.0: registered in approved_artifacts).
    exclusions: [{run_id, week, reason}]. Rows are never returned from another model version or scoring."""
    if not table_exists(con, RUNS):
        return [], []
    ok, excluded = _assess(con, season, None if weeks is None else set(weeks), model_version, scoring_fingerprint,
                           artifact_id)
    now_kick = team_kickoffs(con, season)
    best: dict[tuple[int, int], tuple[tuple, dict]] = {}
    contributed: set[str] = set()
    for a in ok:
        run = a["run"]
        created = _aware(run["created_at"])
        order = (created or datetime.min.replace(tzinfo=timezone.utc), run["run_id"])
        for r in a["rows"]:
            kickoff = own_kickoff(r, run["week"], now_kick)
            if not is_pregame(created, kickoff):
                continue
            contributed.add(run["run_id"])
            key = (run["week"], int(r["espn_player_id"]))
            if key in best and best[key][0] >= order:
                continue
            best[key] = (order, {
                "season": season, "week": run["week"], "espn_player_id": int(r["espn_player_id"]),
                "gsis_id": r["gsis_id"], "position": r["position"], "run_id": run["run_id"],
                "run_created_at": created, "model_version": run["model_version"],
                "artifact_id": run.get("artifact_id"), "artifact_created_at": _aware(run.get("artifact_created_at")),
                "scoring_fingerprint": run["scoring_fingerprint"], "nfl_team": r["nfl_team"], "kickoff": kickoff,
                "pregame": True, "coverage_status": r["coverage_status"],
                "projected_points": r["projected_points"], "p_play": r["p_play"],
                "expected_points": r["expected_points"]})
    for a in ok:
        run = a["run"]
        if run["run_id"] not in contributed:
            excluded.append({"run_id": run["run_id"], "week": run["week"],
                             "reason": "post-kickoff: created at or after every player's own kickoff "
                                       "(or kickoff unknown), so none of its rows is a pregame forecast"})
    excluded.sort(key=lambda e: (e["week"], e["run_id"]))
    return [v[1] for _, v in sorted(best.items())], excluded


# ----------------------------------------------------------------------------- forecast inputs (#225)
#
# A projected row's full point-model inputs, so a change between two forecasts can be split exactly between the
# inputs and the model even across a retrain (projection_attribution). Two create-only, write-once tables beside
# the run tables, never part of a run's content_sha256 (old runs keep their seals; an idempotent retry is unchanged):
#
#   independent_projection_models      one row per (artifact_id, position, model_kind): the ridge's design columns,
#                                      training means and stds, coefficients and intercept (model_sha256 seals it)
#   independent_projection_row_inputs  one row per (run_id, espn_player_id): the raw design row the model was applied
#                                      to (imputed cells hold the training mean and are marked), the model it went
#                                      through, the QB start-share column when the point is the start/no-start mixture,
#                                      and the league re-score factor (scale) or shift
#
# Size (they ship in every league file): a rest-of-season cycle applies one artifact to every remaining week, and a
# player's inputs differ between its target weeks only where the target week enters (opponent, home/away, the
# week's teammate availability). So the first row stored for a player in a cycle keeps the whole vector and a later
# target week stores only the columns that differ (`changed`, `input_values` / `imputed` at those positions) and the
# run it differs from (`base_run_id`); a row differing in more than a third of its columns is stored whole.
# `inputs_digest` (the first 64 bits of a sha256 over the row as stored) catches a changed or truncated copy; the
# decisive check is that the reconstructed inputs rebuild the stored point (`verified_inputs`). Anything that fails
# is "inputs not stored" and the reader keeps its earlier behaviour. Runs written before these tables existed have
# none and are never backfilled.

MODELS = "independent_projection_models"
INPUTS = "independent_projection_row_inputs"
INPUTS_VERSION = "forecast-inputs/v1"
REPRODUCE_TOLERANCE = 0.006       # the stored points are rounded to 2 decimals
DELTA_MAX_SHARE = 1 / 3           # store a row as changes against its cycle's base only below this share of columns


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _floats(v) -> list[float] | None:
    return None if v is None else [float(x) for x in v]


def row_fingerprint(row: dict) -> str:
    """Digest of one forecast row's content cells (ROW_COLUMNS, canonicalised as for content_sha256)."""
    return _digest({c: _canon(c, row.get(c)) for c in ROW_COLUMNS})


def model_sha256(m: dict) -> str:
    return _digest({"artifact_id": str(m["artifact_id"]), "position": str(m["position"]),
                    "model_kind": str(m["model_kind"]), "model_version": m.get("model_version"),
                    "columns": [str(c) for c in m["columns"]], "means": _floats(m["means"]), "stds": _floats(m["stds"]),
                    "coef": _floats(m["coef"]), "intercept": float(m["intercept"])})


def inputs_digest(r: dict) -> int:
    """First 64 bits of the sha256 of one input row as stored (a UBIGINT)."""
    payload = {"run_id": str(r["run_id"]), "espn_player_id": int(r["espn_player_id"]),
               "artifact_id": str(r["artifact_id"]), "position": str(r["position"]), "model_kind": str(r["model_kind"]),
               "base_run_id": r.get("base_run_id"), "changed": None if r.get("changed") is None else
               [int(i) for i in r["changed"]], "input_values": _floats(r["input_values"]),
               "imputed": [bool(x) for x in r["imputed"]], "mixture_column": r.get("mixture_column"),
               "scale": None if r.get("scale") is None else float(r["scale"]),
               "shift": None if r.get("shift") is None else float(r["shift"]), "inputs_version": r.get("inputs_version")}
    return int(_digest(payload)[:16], 16)


MODEL_COLUMNS = ("artifact_id", "position", "model_kind", "model_version", "columns", "means", "stds", "coef",
                 "intercept", "model_sha256")
INPUT_COLUMNS = ("run_id", "espn_player_id", "artifact_id", "position", "model_kind", "base_run_id", "changed",
                 "input_values", "imputed", "mixture_column", "scale", "shift", "inputs_version", "inputs_digest")
_LIST_TYPES = {"columns": "VARCHAR[]", "means": "DOUBLE[]", "stds": "DOUBLE[]", "coef": "DOUBLE[]",
               "input_values": "DOUBLE[]", "imputed": "BOOLEAN[]", "changed": "INTEGER[]"}
_SCALAR_TYPES = {"base_run_id": "VARCHAR", "mixture_column": "VARCHAR", "scale": "DOUBLE", "shift": "DOUBLE",
                 "inputs_digest": "UBIGINT"}


def _insert(con, table: str, columns: tuple[str, ...], rows: list[dict], conflict: str = "") -> None:
    """One INSERT for the batch: the rows as a registered Arrow table (lists stay lists, never JSON text)."""
    import pyarrow as pa       # the connection layer's own dependency (connection._rows_to_arrow)

    arrow = {"columns": pa.list_(pa.string()), "means": pa.list_(pa.float64()), "stds": pa.list_(pa.float64()),
             "coef": pa.list_(pa.float64()), "input_values": pa.list_(pa.float64()), "imputed": pa.list_(pa.bool_()),
             "changed": pa.list_(pa.int32()), "base_run_id": pa.string(), "mixture_column": pa.string(),
             "scale": pa.float64(), "shift": pa.float64(), "inputs_digest": pa.uint64()}
    tmp = f"_inputs_{len(rows)}_{id(rows)}"
    con.register(tmp, pa.table({c: pa.array([r[c] for r in rows], type=arrow.get(c)) for c in columns}))
    try:
        types = {**_LIST_TYPES, **_SCALAR_TYPES}
        sel = ", ".join(f"CAST({c} AS {types[c]})" if c in types else c for c in columns)
        con.execute(f"INSERT INTO {table} ({', '.join(columns)}) SELECT {sel} FROM {tmp} {conflict}")
    finally:
        con.unregister(tmp)


def write_models(con, models: Iterable[dict]) -> int:
    """Insert each model once (ON CONFLICT DO NOTHING: an artifact's models never change). Returns rows offered."""
    rows = [{**m, "model_sha256": model_sha256(m)} for m in models]
    if rows:
        _insert(con, MODELS, MODEL_COLUMNS, rows, "ON CONFLICT DO NOTHING")
    return len(rows)


def write_inputs(con, run_id: str, inputs: Iterable[dict], bases: dict | None = None) -> int:
    """Insert one run's input rows (write-once: a key collision raises), each sealed by inputs_digest. A row given
    whole whose cycle `bases` {espn_player_id: (base_run_id, full row)} holds a vector of the same model is stored as
    its changes against it when few columns differ (module comment, "Size")."""
    rows = []
    for r in inputs:
        r = {"mixture_column": None, "scale": None, "shift": None, "base_run_id": None, "changed": None, **r,
             "run_id": run_id, "inputs_version": r.get("inputs_version") or INPUTS_VERSION}
        base = (bases or {}).get(int(r["espn_player_id"]))
        if base is not None and r["base_run_id"] is None and r["changed"] is None:
            r = _as_changes(r, *base)
        r["inputs_digest"] = inputs_digest(r)
        rows.append(r)
    if rows:
        _insert(con, INPUTS, INPUT_COLUMNS, rows)
    return len(rows)


def _as_changes(r: dict, base_run_id: str, b: dict) -> dict:
    same = all(r[k] == b[k] for k in ("artifact_id", "position", "model_kind", "mixture_column")) \
        and len(r["input_values"]) == len(b["input_values"]) and b.get("base_run_id") is None
    if not same:
        return r
    changed = [i for i, (v, w, iv, iw) in enumerate(zip(r["input_values"], b["input_values"], r["imputed"], b["imputed"]))
               if float(v) != float(w) or bool(iv) != bool(iw)]
    if len(changed) > DELTA_MAX_SHARE * len(r["input_values"]):
        return r
    return {**r, "base_run_id": base_run_id, "changed": changed,
            "input_values": [float(r["input_values"][i]) for i in changed],
            "imputed": [bool(r["imputed"][i]) for i in changed]}


def cycle_bases(con, run: dict, pids: list[int]) -> dict:
    """{espn_player_id: (base_run_id, whole input row)}: per player the newest whole row stored by another run of the
    same cycle (season, artifact, cutoff week, scoring) as `run`, for `write_inputs(..., bases=)`."""
    if not pids or not table_exists(con, INPUTS):
        return {}
    cutoff = run.get("projected_from_week")
    cutoff = run.get("week") if cutoff is None else cutoff
    got = fetch_dicts(con, f"""
        SELECT i.*, r.created_at AS _created FROM {INPUTS} i JOIN {RUNS} r USING (run_id)
        WHERE r.season = ? AND r.artifact_id = ? AND coalesce(r.projected_from_week, r.week) = ?
          AND r.scoring_fingerprint IS NOT DISTINCT FROM ? AND r.run_id <> ? AND i.base_run_id IS NULL
          AND i.espn_player_id IN (SELECT unnest(?))
        ORDER BY r.created_at, r.run_id""",
                      [run["season"], run.get("artifact_id"), cutoff, run.get("scoring_fingerprint"), run["run_id"],
                       sorted(pids)])
    return {int(r["espn_player_id"]): (r["run_id"], r) for r in got}       # newest wins


def linear_parts(model: dict, values: list[float]) -> tuple[float, list[float]]:
    """(constant a, raw weights w) with model output a + sum_i w_i * v_i before the floor: w = coef / std,
    a = intercept - sum_i w_i * mean_i."""
    w = [float(c) / float(s) for c, s in zip(model["coef"], model["stds"])]
    a = float(model["intercept"]) - sum(wi * float(m) for wi, m in zip(w, model["means"]))
    return a, w


def reproduce(model: dict, inp: dict) -> dict | None:
    """The stored point rebuilt from whole inputs: {points, linear, floored, a, w, values, scale, shift}. `linear` is
    the unfloored model output at the inputs (a QB mixture is linear in the start share when neither scenario is
    floored); `floored` says the floor at 0 binds somewhere, so the point is not linear in the inputs. None when the
    inputs do not fit the model (lengths differ, unknown mixture column)."""
    values = _floats(inp["input_values"])
    cols = list(model["columns"])
    if values is None or len(values) != len(cols) or len(inp["imputed"]) != len(cols):
        return None
    a, w = linear_parts(model, values)
    lin = a + sum(wi * v for wi, v in zip(w, values))
    mix = inp.get("mixture_column")
    if mix is None:
        floored, point = lin < 0, max(lin, 0.0)
    else:
        if mix not in cols:
            return None
        i = cols.index(mix)
        pi = values[i]
        f1 = lin + w[i] * (1.0 - pi)
        f0 = lin - w[i] * pi
        floored = f1 < 0 or f0 < 0
        point = pi * max(f1, 0.0) + (1.0 - pi) * max(f0, 0.0)
    scale, shift = inp.get("scale"), inp.get("shift")
    if shift is not None:
        point, floored = max(point + float(shift), 0.0), floored or point + float(shift) < 0
    elif scale is not None:
        point = float(scale) * point
    return {"points": point, "linear": lin, "floored": floored, "a": a, "w": w, "values": values,
            "scale": 1.0 if scale is None else float(scale), "shift": 0.0 if shift is None else float(shift)}


def _whole(r: dict, bases: dict) -> dict | None:
    """The row's whole inputs: itself, or its cycle base with the stored changes applied. None when its base is
    missing, not whole, of another model, or fails its digest."""
    if r.get("base_run_id") is None:
        return r if r.get("changed") is None else None
    b = bases.get((r["base_run_id"], int(r["espn_player_id"])))       # digest already checked by the caller
    if b is None or b.get("base_run_id") is not None \
            or any(b[k] != r[k] for k in ("artifact_id", "position", "model_kind", "mixture_column")):
        return None
    values, imputed = list(_floats(b["input_values"])), [bool(x) for x in b["imputed"]]
    changed = [int(i) for i in r["changed"] or []]
    if len(changed) != len(r["input_values"]) or len(changed) != len(r["imputed"]) \
            or any(i < 0 or i >= len(values) for i in changed):
        return None
    for i, v, m in zip(changed, r["input_values"], r["imputed"]):
        values[i], imputed[i] = float(v), bool(m)
    return {**r, "input_values": values, "imputed": imputed}


def verified_inputs(con, run_ids: list[str], pids: list[int], rows: dict, runs: dict | None = None) -> dict:
    """{(run_id, espn_player_id): {model, inputs, rebuilt}} for the stored inputs that verify: the row's (and its base's)
    digest matches, the model's digest matches and it is the run's own artifact's, and the whole inputs rebuild the
    stored projected_points (2 decimals; `inputs` is the whole row). `rows`: {run_id: {pid: row}} of the run rows
    (projected_points, position) already read; `runs`: {run_id: run meta}, whose artifact_id the inputs must name.
    Three queries; nothing raises."""
    if not run_ids or not pids or not table_exists(con, INPUTS) or not table_exists(con, MODELS):
        return {}
    got = fetch_dicts(con, f"SELECT * FROM {INPUTS} WHERE run_id IN (SELECT unnest(?)) "
                           "AND espn_player_id IN (SELECT unnest(?))", [run_ids, pids])
    need = sorted({r["base_run_id"] for r in got if r.get("base_run_id")})
    bases = {(b["run_id"], int(b["espn_player_id"])): b for b in fetch_dicts(
        con, f"SELECT * FROM {INPUTS} WHERE run_id IN (SELECT unnest(?)) AND espn_player_id IN (SELECT unnest(?))",
        [need, pids])} if need else {}
    keys = sorted({str(r["artifact_id"]) for r in got})
    models = {(str(m["artifact_id"]), str(m["position"]), str(m["model_kind"])): m for m in fetch_dicts(
        con, f"SELECT * FROM {MODELS} WHERE artifact_id IN (SELECT unnest(?))", [keys])} if keys else {}
    sound = {k for k, m in models.items() if m.get("model_sha256") == model_sha256(m)}
    whole_ok = {k for k, b in bases.items() if b.get("inputs_digest") == inputs_digest(b)}
    bases = {k: b for k, b in bases.items() if k in whole_ok}
    out = {}
    for r in got:
        try:
            if r.get("inputs_digest") != inputs_digest(r):
                continue
            if runs is not None and str((runs.get(r["run_id"]) or {}).get("artifact_id")) != str(r["artifact_id"]):
                continue
            whole = _whole(r, bases)
            key = (str(r["artifact_id"]), str(r["position"]), str(r["model_kind"]))
            m = models.get(key)
            if whole is None or m is None or key not in sound:
                continue
            row = (rows.get(r["run_id"]) or {}).get(int(r["espn_player_id"]))
            if row is None or row.get("projected_points") is None or str(row.get("position")) != str(r["position"]):
                continue
            rebuilt = reproduce(m, whole)
            if rebuilt is None:
                continue
            # a re-scored point is the league factor times the reference point already rounded to 2 decimals (or that
            # point plus a shift, rounded again): the rounding it carries grows with the factor
            tol = REPRODUCE_TOLERANCE + 0.005 * (abs(rebuilt["scale"]) if r.get("scale") is not None
                                                 else 1.0 if r.get("shift") is not None else 0.0)
            if abs(rebuilt["points"] - float(row["projected_points"])) > tol:
                continue
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        out[(r["run_id"], int(r["espn_player_id"]))] = {"model": m, "inputs": whole, "rebuilt": rebuilt}
    return out
