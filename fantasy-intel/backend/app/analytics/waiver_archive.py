"""As-of-time archive of waiver recommendations (issue #346; graded by waiver_feedback.py).

`build_waivers` rewrites team_waiver_recommendations on every analytics run, so nothing it says survives the next run.
After each run this module appends a capture of every team's top ARCHIVE_K recommendations to
waiver_recommendation_snapshots, with the run's inputs (projection source and version, scoring fingerprint, the roster
snapshot it read) and a content hash:

  * append-only: rows are only ever inserted (insert_rows); nothing here updates or deletes;
  * deduplicated: a team's capture is appended only when its content hash differs from that team's latest capture for
    the same week, so a rebuild with unchanged data adds nothing;
  * carried forward: the table is a league table (league_split.LEAGUE_TABLES) and not derived, so a league build copies
    the previous file's captures of the same season before appending (app/sync/build.py `_carry_forward`).

Every team is archived (any member may later view their own team's feedback), each from its own rows only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from ..db.connection import fetch_dicts, insert_rows, table_exists
from .legacy_league import latest_league

TABLE = "waiver_recommendation_snapshots"
RULE_VERSION = "waiver-archive.v1"
ARCHIVE_K = 25           # candidates kept per team and capture (signed off on #346)
HORIZON_WEEKS = 3        # the grading horizon a capture is archived for (waiver_feedback.HORIZON_WEEKS)
CONTENT_FIELDS = ("espn_player_id", "position", "rank", "recommendation", "waiver_score", "opportunity_score",
                  "roster_fit_score", "availability_status", "suggested_drop_id", "components", "reasons",
                  "projection_source", "projection_version", "scoring_fingerprint", "rule_version", "archive_k",
                  "horizon_weeks", "roster_synced_at")


def _canonical(v):
    if isinstance(v, str):
        try:
            return json.loads(v) if v[:1] in "[{" else v
        except ValueError:
            return v
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def content_sha256(rows: list[dict]) -> str:
    """The hash of a team's capture: its rows' CONTENT_FIELDS in rank order, as canonical JSON (no capture time)."""
    body = [{k: _canonical(r.get(k)) for k in CONTENT_FIELDS} for r in sorted(rows, key=lambda r: r["rank"])]
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def scoring_fingerprint(scoring_items) -> str | None:
    items = _canonical(scoring_items)
    if items is None:
        return None
    return hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()[:16]


def _utc_naive(dt):
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def archive_waiver_recommendations(con, provenance: dict | None) -> dict:
    """Append this run's captures (see the module docstring). `provenance` is build_waivers' "provenance"."""
    if not provenance or not table_exists(con, TABLE) or not table_exists(con, "team_waiver_recommendations"):
        return {"skipped": "no waiver run to archive"}
    lg = latest_league(con, ["league_id", "season", "scoring_items"])     # the league build_waivers just evaluated
    if lg is None:
        return {"skipped": "no league"}
    league_id, season = lg["league_id"], lg["season"]
    week, captured_at = provenance["week"], _utc_naive(provenance["computed_at"])
    common = {"projection_source": provenance.get("projection_source"),
              "projection_version": provenance.get("projection_version"),
              "scoring_fingerprint": scoring_fingerprint(lg["scoring_items"]), "rule_version": RULE_VERSION,
              "archive_k": ARCHIVE_K, "horizon_weeks": HORIZON_WEEKS}
    current = fetch_dicts(con, """
        SELECT team_id, espn_player_id, position, recommendation, waiver_score, opportunity_score, roster_fit_score,
               availability_status, suggested_drop_id, components, reasons
        FROM team_waiver_recommendations WHERE league_id = ? AND season = ?
        ORDER BY team_id, waiver_score DESC NULLS LAST, espn_player_id
    """, [league_id, season])
    by_team: dict[int, list[dict]] = {}
    for r in current:
        by_team.setdefault(r["team_id"], []).append(r)
    latest = {r["team_id"]: r["content_sha256"] for r in fetch_dicts(con, f"""
        SELECT team_id, content_sha256 FROM (
            SELECT team_id, content_sha256,
                   row_number() OVER (PARTITION BY team_id ORDER BY captured_at DESC) AS rn
            FROM {TABLE} WHERE league_id = ? AND season = ? AND week = ?) WHERE rn = 1
    """, [league_id, season, week])}
    rostered_at = provenance.get("roster_synced_at") or {}
    out_rows, appended, unchanged = [], 0, 0
    for tid in sorted(by_team):
        rows = [{**r, **common, "rank": i + 1, "roster_synced_at": _utc_naive(rostered_at.get(tid))}
                for i, r in enumerate(by_team[tid][:ARCHIVE_K])]
        digest = content_sha256(rows)
        if latest.get(tid) == digest:
            unchanged += 1
            continue
        snapshot_id = f"{league_id}:{season}:{week}:{tid}:{captured_at.isoformat()}"
        out_rows += [{**r, "league_id": league_id, "season": season, "week": week, "team_id": tid,
                      "captured_at": captured_at, "snapshot_id": snapshot_id, "content_sha256": digest} for r in rows]
        appended += 1
    insert_rows(con, TABLE, out_rows)
    return {"week": week, "teams_appended": appended, "teams_unchanged": unchanged, "rows": len(out_rows)}
