"""Import a normalized external-usage CSV into DuckDB.

Behaviour (see docs/external_utilization_contract.md):
  * exact duplicate file (same sha256 + provider + dataset already imported)
    -> recorded as a `duplicate_noop` import; nothing else changes
  * any hard validation error -> the whole file is `rejected` (no partial imports);
    the import row and every issue are recorded for inspection
  * otherwise rows are upserted by (provider, dataset, season, week, source_player_key);
    a corrected file replaces values and the superseded values go to
    external_player_week_usage_revisions
  * unmatched / ambiguous players are stored with gsis_id NULL, never dropped
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ..db.connection import fetch_dicts, replace_where, table_exists, upsert_rows, utcnow
from .contract import METRICS, ParsedFile, RowIssue, file_sha256, parse_rows
from .identity import build_index, canonical_team, resolve

ROW_COLUMNS = ["provider", "dataset", "season", "week", "source_player_key", "provider_player_id", "gsis_id",
               "player_name", "team", "position", *METRICS, "mapping_method", "mapping_confidence",
               "mapping_reason", "source_updated_at", "imported_at", "import_id"]
KEY = ["provider", "dataset", "season", "week", "source_player_key"]
CORRECTION_ROUTES_REL, CORRECTION_ROUTES_ABS = 0.30, 5


def read_csv(data: bytes) -> tuple[list[str], list[dict]]:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader.fieldnames or []), list(reader)


def _issue_rows(import_id: str, issues: list[RowIssue], season: int | None, now: datetime) -> list[dict]:
    return [{"import_id": import_id, "severity": i.severity, "check_name": i.check_name, "detail": i.detail,
             "season": season, "week": i.week, "row_number": i.row_number, "source_player_key": i.source_player_key,
             "player_name": i.player_name, "provider_player_id": i.provider_player_id, "created_at": now}
            for i in issues]


def coverage_issues(con, rows: list[dict]) -> list[RowIssue]:
    """Warn when a week covers fewer teams than played that week (partial file)."""
    if not table_exists(con, "nfl_schedules"):
        return []
    out = []
    by_week = defaultdict(set)
    for r in rows:
        if r["team"]:
            by_week[(r["season"], r["week"])].add(canonical_team(r["team"]))
    for (season, week), teams in sorted(by_week.items()):
        played = {t for (t,) in con.execute("""
            SELECT home_team FROM nfl_schedules WHERE season=? AND week=? UNION
            SELECT away_team FROM nfl_schedules WHERE season=? AND week=?""", [season, week, season, week]).fetchall()}
        if played and len(teams & played) < len(played):
            missing = sorted(played - teams)
            out.append(RowIssue("warning", "partial_week_coverage",
                                f"season {season} week {week}: {len(teams & played)}/{len(played)} teams covered; "
                                f"missing {missing[:8]}{'...' if len(missing) > 8 else ''}", week=week))
    return out


def import_external_usage(con, path: Path, provider: str | None = None, dataset: str | None = None,
                          now: datetime | None = None) -> dict:
    now = now or utcnow()
    data = Path(path).read_bytes()
    digest = file_sha256(data)
    import_id = f"ext-{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    header, records = read_csv(data)
    parsed: ParsedFile = parse_rows(header, records, provider=provider, dataset=dataset)
    rows = parsed.rows
    providers = sorted({r["provider"] for r in rows if r["provider"]})
    datasets = sorted({r["dataset"] for r in rows if r["dataset"]})
    seasons = sorted({r["season"] for r in rows if r["season"]})
    weeks = sorted({r["week"] for r in rows if r["week"]})
    if len(providers) > 1 or len(datasets) > 1:
        parsed.issues.append(RowIssue("error", "mixed_provider_or_dataset",
                                      f"one file must contain one provider and one dataset (got {providers}, {datasets})"))
    if not rows and not parsed.errors:
        parsed.issues.append(RowIssue("error", "empty_file", "no data rows"))
    prov = providers[0] if providers else provider
    ds = datasets[0] if datasets else dataset
    season = seasons[0] if len(seasons) == 1 else None
    base = {"import_id": import_id, "provider": prov, "dataset": ds, "source_filename": Path(path).name,
            "source_file_hash": digest, "season": season, "weeks": weeks, "imported_at": now,
            "source_updated_at": max((r["source_updated_at"] for r in rows if r["source_updated_at"]), default=None),
            "row_count": len(records), "matched_rows": 0, "unmatched_rows": 0, "ambiguous_rows": 0,
            "duplicate_of": None}

    con.execute("BEGIN TRANSACTION")
    try:
        dup = con.execute("""SELECT import_id FROM external_usage_imports WHERE source_file_hash = ? AND provider
                             IS NOT DISTINCT FROM ? AND dataset IS NOT DISTINCT FROM ? AND status = 'imported'
                             ORDER BY imported_at LIMIT 1""", [digest, prov, ds]).fetchone()
        if dup:
            rec = {**base, "warning_count": 0, "error_count": 0, "status": "duplicate_noop", "duplicate_of": dup[0],
                   "message": f"identical file already imported as {dup[0]}; nothing changed"}
            upsert_rows(con, "external_usage_imports", [rec], ["import_id"])
            con.execute("COMMIT")
            return rec
        if parsed.errors:
            rec = {**base, "warning_count": len(parsed.issues) - len(parsed.errors), "error_count": len(parsed.errors),
                   "status": "rejected", "message": f"{len(parsed.errors)} hard validation error(s); file not imported"}
            upsert_rows(con, "external_usage_imports", [rec], ["import_id"])
            replace_where(con, "external_usage_issues", _issue_rows(import_id, parsed.issues, season, now),
                          "import_id = ?", [import_id])
            con.execute("COMMIT")
            return rec

        idx = build_index(con, set(seasons))
        issues = list(parsed.issues)
        out_rows = []
        for r in rows:
            r = {**r, **resolve(r, idx), "imported_at": now, "import_id": import_id}
            if r["mapping_method"] in ("unmatched", "ambiguous"):
                issues.append(RowIssue("warning", "ambiguous_identity" if r["mapping_method"] == "ambiguous"
                                       else "missing_gsis_match", r["mapping_reason"], row_number=r["row_number"],
                                       week=r["week"], source_player_key=r["source_player_key"],
                                       player_name=r["player_name"], provider_player_id=r["provider_player_id"]))
            out_rows.append({c: r.get(c) for c in ROW_COLUMNS})

        by_gsis = defaultdict(list)
        for r in out_rows:
            if r["gsis_id"]:
                by_gsis[(r["season"], r["week"], r["gsis_id"])].append(r["source_player_key"])
        for (s, w, g), keys in by_gsis.items():
            if len(keys) > 1:
                issues.append(RowIssue("warning", "duplicate_gsis_mapping",
                                       f"{len(keys)} source rows map to {g} in season {s} week {w}: {keys}; "
                                       f"conflicting values will be left NULL", week=w))

        issues += correction_issues(con, out_rows, import_id, now)
        issues += coverage_issues(con, out_rows)
        upsert_rows(con, "external_player_week_usage", out_rows, KEY)
        rec = {**base,
               "matched_rows": sum(1 for r in out_rows if r["gsis_id"]),
               "unmatched_rows": sum(1 for r in out_rows if r["mapping_method"] == "unmatched"),
               "ambiguous_rows": sum(1 for r in out_rows if r["mapping_method"] == "ambiguous"),
               "warning_count": len(issues), "error_count": 0, "status": "imported",
               "message": f"{len(out_rows)} rows imported"}
        upsert_rows(con, "external_usage_imports", [rec], ["import_id"])
        replace_where(con, "external_usage_issues", _issue_rows(import_id, issues, season, now),
                      "import_id = ?", [import_id])
        con.execute("COMMIT")
        return rec
    except Exception:
        con.execute("ROLLBACK")
        raise


def correction_issues(con, new_rows: list[dict], import_id: str, now: datetime) -> list[RowIssue]:
    """Archive superseded values and flag suspicious corrections."""
    if not new_rows:
        return []
    existing = {}
    provider, dataset = new_rows[0]["provider"], new_rows[0]["dataset"]
    for r in fetch_dicts(con, "SELECT * FROM external_player_week_usage WHERE provider = ? AND dataset = ?",
                         [provider, dataset]):
        existing[tuple(r[k] for k in KEY)] = r
    out, revisions = [], []
    compare = [*METRICS, "gsis_id"]
    for r in new_rows:
        old = existing.get(tuple(r[k] for k in KEY))
        if old is None or all(old.get(c) == r.get(c) for c in compare):
            continue
        revisions.append({"provider": provider, "dataset": dataset, "season": r["season"], "week": r["week"],
                          "source_player_key": r["source_player_key"], "import_id": old["import_id"],
                          "superseded_by": import_id, "superseded_at": now,
                          "previous": json.dumps({c: old.get(c) for c in compare}, default=str)})
        a, b = old.get("routes"), r.get("routes")
        if a is not None and b is not None and abs(a - b) > CORRECTION_ROUTES_ABS \
                and abs(a - b) > CORRECTION_ROUTES_REL * max(a, b, 1):
            out.append(RowIssue("warning", "suspicious_provider_correction",
                                f"routes changed {a} -> {b} vs import {old['import_id']}", week=r["week"],
                                source_player_key=r["source_player_key"], player_name=r["player_name"],
                                provider_player_id=r["provider_player_id"]))
        if old.get("gsis_id") and r.get("gsis_id") and old["gsis_id"] != r["gsis_id"]:
            out.append(RowIssue("warning", "identity_changed_in_correction",
                                f"gsis_id changed {old['gsis_id']} -> {r['gsis_id']}", week=r["week"],
                                source_player_key=r["source_player_key"], player_name=r["player_name"]))
    if revisions:
        upsert_rows(con, "external_player_week_usage_revisions", revisions,
                    ["provider", "dataset", "season", "week", "source_player_key", "superseded_by"])
    return out


def add_reviewed_mapping(con, provider: str, source_player_key: str, gsis_id: str, note: str | None = None,
                         provider_player_id: str | None = None, now: datetime | None = None) -> int:
    """Record a reviewed mapping and re-resolve the provider's stored rows. Returns rows updated."""
    now = now or utcnow()
    if not con.execute("SELECT count(*) FROM nfl_players WHERE gsis_id = ?", [gsis_id]).fetchone()[0]:
        raise ValueError(f"gsis_id {gsis_id} is not in the nflverse player universe")
    upsert_rows(con, "external_player_map", [{
        "provider": provider, "source_player_key": source_player_key, "provider_player_id": provider_player_id,
        "gsis_id": gsis_id, "match_method": "manual", "match_confidence": "reviewed", "reviewed_at": now,
        "note": note}], ["provider", "source_player_key"])
    rows = fetch_dicts(con, """SELECT * FROM external_player_week_usage WHERE provider = ? AND source_player_key = ?
                               AND (gsis_id IS NULL OR mapping_method <> 'gsis_id')""", [provider, source_player_key])
    idx = build_index(con, {r["season"] for r in rows})
    updated = [{**r, **resolve({**r, "gsis_id": None}, idx)} for r in rows]
    upsert_rows(con, "external_player_week_usage", updated, KEY)
    return len(updated)
