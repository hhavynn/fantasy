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
are annotations derived from nfl_schedules). `pregame_forecasts` recomputes it, so a run whose rows were
lost, edited or only partially copied is excluded with a reason instead of being used as evidence.

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
from .projection_backtest import _kickoff, _utc

RUNS = "independent_projection_runs"
ROWS = "independent_projection_rows"
LEGACY = "independent_player_projections"

# Row columns shared by the legacy and the run-keyed table, in storage order (run_id first in both).
ROW_COLUMNS = ("season", "week", "espn_player_id", "gsis_id", "position", "nfl_team", "opponent", "is_home",
               "projected_points", "interval_low", "interval_high", "model", "coverage_status", "games_used",
               "latest_usage_season", "latest_usage_week", "missing_inputs", "flags", "explanation", "p_play",
               "expected_points", "participation")
ANNOTATIONS = ("kickoff", "pregame")
_JSON = {"missing_inputs", "flags", "explanation", "participation"}
_DOUBLE = {"projected_points", "interval_low", "interval_high", "p_play", "expected_points"}
_INT = {"season", "week", "espn_player_id", "games_used", "latest_usage_season", "latest_usage_week"}
_BOOL = {"is_home"}

META_JSON = ("model_status", "evaluation", "participation_status", "participation_evaluation")
META_INT = ("season", "week", "data_through_season", "data_through_week")
META_STR = ("model_version", "scoring_fingerprint", "artifact_id", "artifact_created_at", "participation_version")
HASHED_META = META_INT + META_STR + META_JSON


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
    payload = {"meta": {k: _canon(k, meta.get(k)) for k in HASHED_META},
               "rows": [[_canon(c, r.get(c)) for c in ROW_COLUMNS]
                        for r in sorted(rows, key=lambda r: int(r["espn_player_id"]))]}
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
    if table_exists(con, LEGACY):
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
                cols = ", ".join(ROW_COLUMNS)
                src = ", ".join(f"p.{c}" for c in ROW_COLUMNS)
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


def latest_run(con, season: int, week: int, *, model_version: str | None = None,
               scoring_fingerprint: str | None = None) -> dict | None:
    """Consumer view: the newest run (by created_at) for the week that still has all of its rows.
    A run whose rows are missing (destroyed by the pre-v8 overwrite) is skipped, not shown as an empty
    forecast. With only the legacy table (unmigrated copy) it is the newest run, as before."""
    if not table_exists(con, RUNS) or rows_source(con) is None:
        return None
    where, params = ["season = ?", "week = ?"], [season, week]
    if model_version is not None:
        where.append("model_version = ?")
        params.append(model_version)
    if scoring_fingerprint is not None:
        where.append("scoring_fingerprint = ?")
        params.append(scoring_fingerprint)
    runs = fetch_dicts(con, f"SELECT * FROM {RUNS} WHERE {' AND '.join(where)} ORDER BY created_at DESC, run_id DESC",
                       params)
    if not table_exists(con, ROWS):
        return runs[0] if runs else None
    for run in runs:
        n = run.get("row_count")
        if n and con.execute(f"SELECT count(*) FROM {ROWS} WHERE run_id = ?", [run["run_id"]]).fetchone()[0] == n:
            return run
    return None


# ----------------------------------------------------------------------------- pregame forecasts


def _aware(v):
    return _utc(v)


def _assess(con, season: int, weeks: set[int] | None, model_version: str, scoring_fingerprint: str
            ) -> tuple[list[dict], list[dict]]:
    """Classify every run of the season: (accepted [{run, rows}], exclusions [{run_id, week, reason}])."""
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
        expected = run.get("row_count")
        if run["model_version"] != model_version:
            reason = f"model {run['model_version']}, not {model_version}"
        elif run["scoring_fingerprint"] != scoring_fingerprint:
            reason = "made under different league scoring"
        elif not expected or not rows:
            reason = ("forecast rows missing: the run has no stored rows (row_count "
                      f"{'NULL' if expected is None else expected}); pre-v8 overwrite or unsealed run")
        elif fitted is None or first is None:
            reason = "artifact fit time or week kickoff unknown"
        elif fitted >= first:
            reason = "artifact fitted after the week's first kickoff (it may have trained on that week)"
        elif len(rows) != expected:
            reason = f"integrity: {len(rows)} stored rows but the run recorded row_count {expected}"
        elif not run.get("content_sha256"):
            reason = "integrity: run has no content hash (unsealed)"
        elif content_sha256(run, rows) != run["content_sha256"]:
            reason = "integrity: stored rows do not match the run's content_sha256"
        else:
            ok.append({"run": run, "rows": rows})
            continue
        excluded.append({"run_id": run["run_id"], "week": run["week"], "reason": reason})
    return ok, excluded


def eligible_run_ids(con, season: int, model_version: str, scoring_fingerprint: str,
                     weeks: Iterable[int] | None = None) -> tuple[list[str], list[dict]]:
    """Runs whose rows may count as evidence (model, scoring, artifact-before-first-kickoff, integrity)."""
    if not table_exists(con, RUNS):
        return [], []
    ok, excluded = _assess(con, season, None if weeks is None else set(weeks), model_version, scoring_fingerprint)
    return [a["run"]["run_id"] for a in ok], excluded


def pregame_forecasts(con, season: int, weeks: Iterable[int] | None = None, *,
                      model_version: str, scoring_fingerprint: str) -> tuple[list[dict], list[dict]]:
    """(rows, exclusions). Per (season, week, espn_player_id): the row from the LATEST eligible run whose
    run created_at < that player's own kickoff. Eligible run = model_version match, scoring_fingerprint match,
    artifact_created_at < the week's first kickoff, integrity verified (row_count and content hash).
    exclusions: [{run_id, week, reason}]. Rows are never returned from another model version or scoring."""
    if not table_exists(con, RUNS):
        return [], []
    ok, excluded = _assess(con, season, None if weeks is None else set(weeks), model_version, scoring_fingerprint)
    now_kick = team_kickoffs(con, season)
    best: dict[tuple[int, int], tuple[tuple, dict]] = {}
    contributed: set[str] = set()
    for a in ok:
        run = a["run"]
        created = _aware(run["created_at"])
        order = (created or datetime.min.replace(tzinfo=timezone.utc), run["run_id"])
        for r in a["rows"]:
            known = [k for k in (_aware(r.get("kickoff")), now_kick.get((run["week"], r.get("nfl_team"))))
                     if k is not None]
            kickoff = min(known) if known else None
            if kickoff is None or created is None or not created < kickoff:
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
