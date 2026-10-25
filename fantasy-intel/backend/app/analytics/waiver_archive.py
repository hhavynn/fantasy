"""As-of-time archive of waiver recommendations (issue #346; graded by waiver_feedback.py).

`build_waivers` rewrites team_waiver_recommendations on every analytics run, so nothing it says survives the next run.
After each run this module appends a capture of every team's top ARCHIVE_K recommendations to
waiver_recommendation_snapshots, with the run's inputs (projection source and version, scoring fingerprint, the roster
snapshot it read) and a content hash, and appends the roster each team's capture was evaluated from to
waiver_roster_snapshots (ROSTER_TABLE):

  * append-only: rows are only ever inserted (insert_rows); nothing here updates or deletes;
  * deduplicated: a team's capture (recommendations and roster) is appended only when its content hash differs from that
    team's latest capture for the same week, so a rebuild with unchanged data adds nothing;
  * as of its own roster: the grading reads the capture's archived roster, never roster_history, whose snapshot of a
    day a later sync that day replaces, so a roster seen before a waiver cutoff could be gone after it (Codex #353);
  * carried forward: both tables are league tables (league_split.LEAGUE_TABLES) and not derived, so a league build copies
    the previous file's captures of the same season before appending (app/sync/build.py `_carry_forward`).

Every team is archived (any member may later view their own team's feedback), each from its own rows only.

Known limitation: the laptop path (app/db/league_publish.publish_league) publishes the split of the laptop database,
which does not hold captures a hosted job build appended, so a laptop publish of the same league replaces them, as it
already replaces the jobs' roster_history and lineup_snapshots. Hosted leagues are built by jobs only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from ..db.connection import fetch_dicts, insert_rows, table_exists
from .legacy_league import latest_league

TABLE = "waiver_recommendation_snapshots"
ROSTER_TABLE = "waiver_roster_snapshots"
RULE_VERSION = "waiver-archive.v1"
ARCHIVE_K = 25           # candidates kept per team and capture (signed off on #346)
HORIZON_WEEKS = 3        # the grading horizon a capture is archived for (waiver_feedback.HORIZON_WEEKS)
# What a capture says. The input stamps (projection_version, roster_synced_at) are provenance only: they change on every
# sync, so hashing them would append a full capture per build (hourly on game days) with nothing new in it. A changed
# roster or projection that changes a recommendation changes these fields, and is appended.
CONTENT_FIELDS = ("espn_player_id", "position", "rank", "recommendation", "waiver_score", "opportunity_score",
                  "roster_fit_score", "availability_status", "suggested_drop_id", "projection_source",
                  "scoring_fingerprint", "rule_version", "archive_k", "horizon_weeks")


def _canonical(v):
    if isinstance(v, str):
        try:
            return json.loads(v) if v[:1] in "[{" else v
        except ValueError:
            return v
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def content_sha256(rows: list[dict], roster: list[dict] = ()) -> str:
    """The hash of a team's capture: its rows' CONTENT_FIELDS in rank order and its roster's (player, slot) pairs, as
    canonical JSON (no time or stamp). A roster-only change is a new capture: the grading reads the capture's roster."""
    body = {"rows": [{k: _canonical(r.get(k)) for k in CONTENT_FIELDS} for r in sorted(rows, key=lambda r: r["rank"])],
            "roster": sorted([int(r["espn_player_id"]), r.get("lineup_slot_id")] for r in roster)}
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
    if not provenance or not all(table_exists(con, t) for t in (TABLE, ROSTER_TABLE, "team_waiver_recommendations",
                                                                 "lineup_snapshots")):
        return {"skipped": "no waiver run to archive"}
    league_id, season = provenance["league_id"], provenance["season"]     # the partition build_waivers just wrote
    lg = latest_league(con, ["league_id", "season", "scoring_items"])
    if lg is None or (lg["league_id"], lg["season"]) != (league_id, season):
        return {"skipped": "the league evaluated is not the latest league"}
    week, captured_at = provenance["week"], _utc_naive(provenance["computed_at"])
    common = {"projection_source": provenance.get("projection_source"),
              "projection_version": provenance.get("projection_version"),
              "scoring_fingerprint": scoring_fingerprint(lg["scoring_items"]), "rule_version": RULE_VERSION,
              "archive_k": ARCHIVE_K, "horizon_weeks": HORIZON_WEEKS}
    current = fetch_dicts(con, """
        SELECT team_id, espn_player_id, position, recommendation, waiver_score, opportunity_score, roster_fit_score,
               availability_status, suggested_drop_id
        FROM team_waiver_recommendations WHERE league_id = ? AND season = ?
        ORDER BY team_id, waiver_score DESC NULLS LAST, espn_player_id
    """, [league_id, season])
    by_team: dict[int, list[dict]] = {}
    for r in current:
        by_team.setdefault(r["team_id"], []).append(r)
    rosters: dict[int, list[dict]] = {}
    for r in fetch_dicts(con, """
        SELECT team_id, espn_player_id, lineup_slot_id FROM lineup_snapshots
        WHERE league_id = ? AND season = ? AND scoring_period = ? AND espn_player_id IS NOT NULL
        ORDER BY team_id, espn_player_id
    """, [league_id, season, week]):                  # the rows build_waivers evaluated each team from
        rosters.setdefault(r["team_id"], []).append(r)
    latest = {r["team_id"]: r["content_sha256"] for r in fetch_dicts(con, f"""
        SELECT team_id, content_sha256 FROM (
            SELECT team_id, content_sha256,
                   row_number() OVER (PARTITION BY team_id ORDER BY captured_at DESC) AS rn
            FROM {TABLE} WHERE league_id = ? AND season = ? AND week = ?) WHERE rn = 1
    """, [league_id, season, week])}
    rostered_at = provenance.get("roster_synced_at") or {}
    out_rows, roster_rows, appended, unchanged = [], [], 0, 0
    for tid in sorted(by_team):
        rows = [{**r, **common, "rank": i + 1, "roster_synced_at": _utc_naive(rostered_at.get(tid))}
                for i, r in enumerate(by_team[tid][:ARCHIVE_K])]
        roster = rosters.get(tid, [])
        digest = content_sha256(rows, roster)
        if latest.get(tid) == digest:
            unchanged += 1
            continue
        snapshot_id = f"{league_id}:{season}:{week}:{tid}:{captured_at.isoformat()}"
        key = {"league_id": league_id, "season": season, "week": week, "team_id": tid, "captured_at": captured_at,
               "snapshot_id": snapshot_id}
        out_rows += [{**r, **key, "content_sha256": digest} for r in rows]
        roster_rows += [{**key, "espn_player_id": r["espn_player_id"], "lineup_slot_id": r["lineup_slot_id"]}
                        for r in roster]
        appended += 1
    insert_rows(con, TABLE, out_rows)
    insert_rows(con, ROSTER_TABLE, roster_rows)
    return {"week": week, "teams_appended": appended, "teams_unchanged": unchanged, "rows": len(out_rows),
            "roster_rows": len(roster_rows)}
