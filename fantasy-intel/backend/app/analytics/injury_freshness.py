"""Injury retrieval freshness: when did we actually fetch each team's injury evidence, and is it
still good enough before that team's kickoff? Diagnostics only: nothing here feeds a model.

Two clocks, never mixed up and never invented:
  retrieved_at           OUR clock, recorded by the sync at download time (table `injury_retrievals`).
  provider_published_at  the PROVIDER's own timestamp (nflverse `date_modified`). ESPN status has none,
                         so it stays NULL (counted, never imputed).

The log is append-only. A re-fetch of identical content is a new observation with the same
content_sha (it proves the feed was still saying the same thing at that time); nothing is
overwritten. This module is stdlib + duckdb only (no polars) and is read-only except for
`record_observations`, which is only ever called by the sync functions right after their commit.

Source selection mirrors the participation model's ESPN-status fallback without importing or
changing it: a team uses the nflverse week-W report when it has a designation (report_status) that was
not modified at/after its kickoff, otherwise the ESPN status snapshot, otherwise nothing. A team with
no report at all is `missing`; it is never read as healthy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from ..db.connection import table_columns, table_exists, utcnow

SOURCE_NFLVERSE, SOURCE_ESPN = "nflverse", "espn_status"
SCOPE_TEAM_WEEK, SCOPE_TEAM_CURRENT, SCOPE_FEED = "team_week", "team_current", "feed"
SOURCES = (SOURCE_NFLVERSE, SOURCE_ESPN)
SCOPES = (SCOPE_TEAM_WEEK, SCOPE_TEAM_CURRENT, SCOPE_FEED)
TABLE = "injury_retrievals"
ESPN_DESIGNATIONS = ("OUT", "DOUBTFUL", "QUESTIONABLE")

FRESH, STALE, MISSING, POST_KICKOFF_ONLY = "fresh", "stale", "missing", "post_kickoff_only"
STATUSES = (FRESH, STALE, MISSING, POST_KICKOFF_ONLY)
SOURCE_NONE = "none"


@dataclass(frozen=True)
class Thresholds:
    """A team is `stale` when the evidence it would use was retrieved more than `stale_after_hours`
    before the reference time (now, or kickoff once the game started) and kickoff is within
    `window_hours` (or unknown). Farther out, an old retrieval is not yet a problem."""
    stale_after_hours: float = 24.0
    window_hours: float = 72.0


DEFAULT_THRESHOLDS = Thresholds()


# ----------------------------------------------------------------------------- time


def naive_utc(value: Any) -> datetime | None:
    """Repository TIMESTAMPs are naive UTC. Accepts aware/naive datetimes and ISO strings."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def label_pregame(retrieved_at: Any, kickoff: Any) -> str:
    """'pregame' only when the retrieval finished strictly before kickoff; a retrieval at or after
    kickoff is 'post_kickoff'; with no known kickoff nothing can be called pregame."""
    r, k = naive_utc(retrieved_at), naive_utc(kickoff)
    if r is None or k is None:
        return "unknown_kickoff" if k is None else "unknown_retrieval"
    return "pregame" if r < k else "post_kickoff"


def _hours(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds() / 3600.0


# ----------------------------------------------------------------------------- observations


@dataclass(frozen=True)
class Observation:
    source: str
    scope: str
    season: int
    week: int | None
    team: str | None
    retrieved_at: datetime
    content_sha: str
    row_count: int
    rows_with_publication_time: int = 0
    provider_published_at: datetime | None = None
    fetch_started_at: datetime | None = None
    sync_run_id: str | None = None

    def validate(self) -> "Observation":
        if self.source not in SOURCES or self.scope not in SCOPES:
            raise ValueError(f"unknown source/scope: {self.source}/{self.scope}")
        if naive_utc(self.retrieved_at) is None:
            raise ValueError("retrieved_at is required (our clock at download time); it is never derived later")
        if self.source == SOURCE_ESPN and (self.provider_published_at is not None or self.rows_with_publication_time):
            raise ValueError("ESPN status carries no provider publication time; it must stay NULL")
        if self.provider_published_at is not None and not self.rows_with_publication_time:
            raise ValueError("a publication time needs at least one row that carries one")
        return self

    def retrieval_id(self) -> str:
        key = "|".join(str(x) for x in (self.source, self.scope, self.season, self.week, self.team,
                                          naive_utc(self.retrieved_at).isoformat(), self.content_sha,
                                          self.sync_run_id))
        return hashlib.sha256(key.encode()).hexdigest()[:32]


def content_sha(rows: Iterable[Mapping[str, Any]]) -> str:
    """Order-independent sha256 of the rows exactly as downloaded."""
    lines = sorted(json.dumps(dict(r), sort_keys=True, default=str) for r in rows)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def nflverse_observations(rows: Iterable[Mapping[str, Any]], season: int, *, retrieved_at: datetime,
                          fetch_started_at: datetime | None = None, sync_run_id: str | None = None,
                          ) -> list[Observation]:
    """One feed-level observation (the whole downloaded injury frame, so 'we asked and the team had no
    rows' is provable) plus one per (week, team) of REG rows. `rows`: the downloaded frame as dicts."""
    rows = list(rows)
    out = [_nflverse_obs(rows, SCOPE_FEED, season, None, None, retrieved_at, fetch_started_at, sync_run_id)]
    groups: dict[tuple, list] = {}
    for r in rows:
        if (r.get("game_type") in (None, "REG")) and r.get("week") is not None and r.get("team"):
            groups.setdefault((int(r["week"]), str(r["team"])), []).append(r)
    for (week, team), grp in sorted(groups.items()):
        out.append(_nflverse_obs(grp, SCOPE_TEAM_WEEK, season, week, team, retrieved_at, fetch_started_at,
                                 sync_run_id))
    return out


def _nflverse_obs(rows, scope, season, week, team, retrieved_at, started, run_id) -> Observation:
    pubs = [t for t in (naive_utc(r.get("date_modified")) for r in rows) if t is not None]
    return Observation(SOURCE_NFLVERSE, scope, int(season), week, team, naive_utc(retrieved_at), content_sha(rows),
                       len(rows), len(pubs), max(pubs) if pubs else None, naive_utc(started), run_id).validate()


def espn_status_observations(rows: Iterable[Mapping[str, Any]], season: int, *, retrieved_at: datetime,
                             fetch_started_at: datetime | None = None, sync_run_id: str | None = None,
                             ) -> list[Observation]:
    """One observation per NFL team of the ESPN player rows just fetched (espn_player_id, pro_team,
    injury_status). A current status snapshot, so `week` is NULL; there is no provider timestamp."""
    groups: dict[str, list] = {}
    for r in rows:
        if r.get("pro_team"):
            groups.setdefault(str(r["pro_team"]), []).append(
                {"espn_player_id": r.get("espn_player_id"), "injury_status": r.get("injury_status")})
    return [Observation(SOURCE_ESPN, SCOPE_TEAM_CURRENT, int(season), None, team, naive_utc(retrieved_at),
                        content_sha(grp), len(grp), 0, None, naive_utc(fetch_started_at), sync_run_id).validate()
            for team, grp in sorted(groups.items())]


def record_observations(con, observations: Iterable[Observation], *, recorded_at: datetime | None = None) -> dict:
    """Append observations. Write-once per retrieval_id: an id that already exists is left untouched
    (ON CONFLICT DO NOTHING), nothing is ever updated or deleted. Returns {"inserted", "ignored"}."""
    if not table_exists(con, TABLE):
        raise RuntimeError(f"{TABLE} does not exist; open the database with init_db first")
    stamp = naive_utc(recorded_at) or utcnow()
    inserted = ignored = 0
    for o in observations:
        o.validate()
        before = con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
        con.execute(
            f"INSERT INTO {TABLE} (retrieval_id, source, scope, season, week, team, fetch_started_at, retrieved_at, "
            "content_sha, row_count, rows_with_publication_time, provider_published_at, sync_run_id, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (retrieval_id) DO NOTHING",
            [o.retrieval_id(), o.source, o.scope, o.season, o.week, o.team, naive_utc(o.fetch_started_at),
             naive_utc(o.retrieved_at), o.content_sha, o.row_count, o.rows_with_publication_time,
             naive_utc(o.provider_published_at), o.sync_run_id, stamp])
        if con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] > before:
            inserted += 1
        else:
            ignored += 1
    return {"inserted": inserted, "ignored": ignored}


# ----------------------------------------------------------------------------- schedule


def scheduled_games(con, season: int, week: int) -> list[dict]:
    """REG games of the week: [{team, opponent, kickoff (aware UTC | None)}], one row per team.
    Teams on a bye have no row. Kickoff None = ambiguous or missing clock (never guessed)."""
    from .projection_backtest import _kickoff

    if not table_exists(con, "nfl_schedules"):
        return []
    cur = con.execute("SELECT gameday, gametime, home_team, away_team FROM nfl_schedules "
                      "WHERE season = ? AND week = ? AND game_type = 'REG'", [season, week])
    out = []
    for gameday, gametime, home, away in cur.fetchall():
        ko = _kickoff({"gameday": gameday, "gametime": gametime})
        out += [{"team": home, "opponent": away, "kickoff": ko}, {"team": away, "opponent": home, "kickoff": ko}]
    return sorted(out, key=lambda g: g["team"])


def scheduled_kickoffs(con, season: int, week: int) -> dict[str, datetime | None]:
    return {g["team"]: g["kickoff"] for g in scheduled_games(con, season, week)}


# ----------------------------------------------------------------------------- reads


def _retrievals(con, source: str, season: int, scope: str, week: int | None = None) -> dict[str | None, list[dict]]:
    """Retrieval rows by team, oldest first."""
    if not table_exists(con, TABLE):
        return {}
    sql = (f"SELECT team, retrieved_at, content_sha, row_count, rows_with_publication_time, provider_published_at, "
           f"sync_run_id, retrieval_id FROM {TABLE} WHERE source = ? AND scope = ? AND season = ?")
    params: list = [source, scope, season]
    if week is not None:
        sql += " AND week = ?"
        params.append(week)
    out: dict = {}
    for team, r_at, sha, n, npub, pub, run, rid in con.execute(sql + " ORDER BY retrieved_at", params).fetchall():
        out.setdefault(team, []).append({"retrieved_at": naive_utc(r_at), "content_sha": sha, "row_count": n,
                                         "rows_with_publication_time": npub,
                                         "provider_published_at": naive_utc(pub), "sync_run_id": run,
                                         "retrieval_id": rid})
    return out


def _latest(rows: list[dict] | None, before: datetime | None = None) -> dict | None:
    """Latest retrieval; with `before`, the latest strictly before it."""
    ok = [r for r in (rows or []) if before is None or r["retrieved_at"] < before]
    return ok[-1] if ok else None


def _naive_utc_column(con, table: str, column: str) -> str:
    """SQL expression returning `column` as a naive-UTC TIMESTAMP. The nflverse sync stores a UTC polars
    Datetime as TIMESTAMP WITH TIME ZONE; fetching that into Python needs pytz (not installed) and would
    otherwise follow the session TimeZone. `timezone('UTC', x)` converts in SQL, independent of the
    session TimeZone. (`::TIMESTAMP` would convert in the session TimeZone, so it is not used.)"""
    row = con.execute("SELECT data_type FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
                      [table, column]).fetchone()
    if row and "WITH TIME ZONE" in str(row[0]).upper():
        return f"timezone('UTC', {column})"
    return column


def _nflverse_reports(con, season: int, week: int, kickoffs: Mapping[str, Any]) -> dict[str, dict]:
    """Per team, what the nflverse week-W injury table holds, counted exactly like the model's loader:
    REG rows with a gsis_id; a designation (report_status) counts only if the row was not modified at
    or after the team's kickoff (those rows are dropped before the model sees them)."""
    if not table_exists(con, "nfl_injuries"):
        return {}
    cols = set(table_columns(con, "nfl_injuries"))
    if not {"season", "week", "team", "gsis_id", "report_status"} <= cols:
        return {}
    gt = "AND game_type = 'REG'" if "game_type" in cols else ""
    dm = _naive_utc_column(con, "nfl_injuries", "date_modified") if "date_modified" in cols else "NULL"
    out: dict[str, dict] = {}
    for team, status, practice, modified in con.execute(
            f"SELECT team, report_status, {'practice_status' if 'practice_status' in cols else 'NULL'}, {dm} "
            f"FROM nfl_injuries WHERE season = ? AND week = ? AND gsis_id IS NOT NULL {gt}", [season, week]).fetchall():
        d = out.setdefault(team, {"rows": 0, "designations": 0, "post_kickoff_designations": 0, "practice_only": 0})
        d["rows"] += 1
        ko, mod = naive_utc(kickoffs.get(team)), naive_utc(modified)
        if status is not None:
            if mod is not None and ko is not None and mod >= ko:
                d["post_kickoff_designations"] += 1
            else:
                d["designations"] += 1
        elif practice is not None:
            d["practice_only"] += 1
    return out


def _espn_designated(con) -> dict[str, int]:
    """Players per ESPN pro team with an OUT/DOUBTFUL/QUESTIONABLE status and a high-confidence,
    non-conflicting id mapping (`notes` without 'conflict', as the projection store does): the rows
    the fallback would turn into designations. Current table state."""
    if not all(table_exists(con, t) for t in ("fantasy_players", "player_id_map")):
        return {}
    cur = con.execute("""
        SELECT fp.pro_team, count(*) FROM fantasy_players fp JOIN player_id_map m USING (espn_player_id)
        WHERE m.match_confidence = 'high' AND m.gsis_id IS NOT NULL
          AND lower(coalesce(m.notes, '')) NOT LIKE '%conflict%'
          AND upper(fp.injury_status) IN ('OUT', 'DOUBTFUL', 'QUESTIONABLE') GROUP BY 1""")
    return {t: int(n) for t, n in cur.fetchall() if t}


# ----------------------------------------------------------------------------- freshness


def team_freshness(con, season: int, week: int, now: datetime, *, kickoffs: Mapping[str, Any],
                   thresholds: Thresholds = DEFAULT_THRESHOLDS) -> list[dict]:
    """One row per scheduled team (`kickoffs` keys; value None = unknown kickoff). Read-only.

    Keys: team, kickoff, hours_to_kickoff, game_started, source_used (nflverse | espn_status | none),
    status (fresh | stale | missing | post_kickoff_only), reason, evidence_retrieved_at (the retrieval of
    the source that would be used, strictly before kickoff once the game started), hours_since_retrieval
    (measured to now, or to kickoff once started), plus the latest nflverse / ESPN retrievals, the
    nflverse report counts, and the provider publication time (None when the provider gave none)."""
    t_now = naive_utc(now)
    if t_now is None:
        raise ValueError("now is required")
    nfl = _retrievals(con, SOURCE_NFLVERSE, season, SCOPE_TEAM_WEEK, week)
    espn = _retrievals(con, SOURCE_ESPN, season, SCOPE_TEAM_CURRENT)
    feed = _latest(_retrievals(con, SOURCE_NFLVERSE, season, SCOPE_FEED).get(None))
    reports = _nflverse_reports(con, season, week, kickoffs)
    designated = _espn_designated(con)
    out = []
    for team in sorted(kickoffs, key=lambda t: (naive_utc(kickoffs[t]) or datetime.max, t)):
        ko = naive_utc(kickoffs[team])
        started = ko is not None and t_now >= ko
        cutoff = ko if started else None          # once started only retrievals before kickoff count
        rep = reports.get(team, {"rows": 0, "designations": 0, "post_kickoff_designations": 0, "practice_only": 0})
        nfl_all, espn_all = _latest(nfl.get(team)), _latest(espn.get(team))
        nfl_ev, espn_ev = _latest(nfl.get(team), cutoff), _latest(espn.get(team), cutoff)
        synced = rep["designations"] > 0
        if synced:
            source, ev = SOURCE_NFLVERSE, nfl_ev
        elif espn_ev is not None:
            source, ev = SOURCE_ESPN, espn_ev
        else:
            source, ev = SOURCE_NONE, None
        hours_to_ko = None if ko is None else round(_hours(ko, t_now), 2)
        ref = ko if started else t_now
        age = None if ev is None else round(_hours(ref, ev["retrieved_at"]), 2)
        if source == SOURCE_NONE:
            later = bool(started and (nfl_all or espn_all or rep["rows"]))
            status, reason = (POST_KICKOFF_ONLY, "evidence_only_after_kickoff") if later else (
                MISSING, "practice_report_only_no_designation_and_no_espn_status" if rep["practice_only"]
                else "no_report_and_no_espn_status_retrieval")
        elif ev is None:       # nflverse designations exist but no verified pregame retrieval of them
            late = started and nfl_all is not None
            status, reason = (POST_KICKOFF_ONLY, "nflverse_retrieved_only_after_kickoff") if late else (
                STALE, "nflverse_report_without_verified_retrieval")
        elif age > thresholds.stale_after_hours and (hours_to_ko is None or hours_to_ko <= thresholds.window_hours):
            status, reason = STALE, f"retrieved_{age:.0f}h_before_{'kickoff' if started else 'now'}"
        else:
            status = FRESH
            reason = ("kickoff_unknown_age_only" if ko is None else
                      "kickoff_beyond_window" if hours_to_ko > thresholds.window_hours and
                      age > thresholds.stale_after_hours else "within_threshold")
        out.append({
            "team": team, "kickoff": None if ko is None else ko.replace(tzinfo=timezone.utc).isoformat(),
            "hours_to_kickoff": hours_to_ko, "game_started": started,
            "source_used": source, "status": status, "reason": reason,
            "evidence_retrieved_at": None if ev is None else ev["retrieved_at"].isoformat(),
            "hours_since_retrieval": age,
            "provider_published_at": None if ev is None or ev["provider_published_at"] is None
            else ev["provider_published_at"].isoformat(),
            "provider_publication_time": "available" if ev and ev["provider_published_at"] else "null",
            "pregame_label": None if ev is None else label_pregame(ev["retrieved_at"], ko),
            "latest_nflverse_retrieval": None if nfl_all is None else nfl_all["retrieved_at"].isoformat(),
            "latest_espn_status_retrieval": None if espn_all is None else espn_all["retrieved_at"].isoformat(),
            "latest_nflverse_feed_retrieval": None if feed is None else feed["retrieved_at"].isoformat(),
            "nflverse_report": rep, "nflverse_designation_synced": synced,
            "espn_designated_players": designated.get(team, 0),
            "fallback_adds_no_designations": source == SOURCE_ESPN and designated.get(team, 0) == 0,
            "espn_fresher_than_unused_nflverse": bool(
                synced and espn_ev and nfl_ev and espn_ev["retrieved_at"] > nfl_ev["retrieved_at"]),
        })
    return out


def summarize(teams: list[dict]) -> dict:
    """Coverage of the slate: counts by status and source, teams with a report, and how many used
    evidence carry a provider publication time (NULL ones are counted, never imputed)."""
    by_status = {s: sorted(t["team"] for t in teams if t["status"] == s) for s in STATUSES}
    used = [t for t in teams if t["source_used"] != SOURCE_NONE]
    return {
        "teams": len(teams),
        "by_status": {s: len(v) for s, v in by_status.items()},
        "teams_by_status": by_status,
        "by_source": {s: sum(1 for t in teams if t["source_used"] == s) for s in (SOURCE_NFLVERSE, SOURCE_ESPN, SOURCE_NONE)},
        "teams_with_nflverse_designations": sum(1 for t in teams if t["nflverse_designation_synced"]),
        "teams_without_any_report": sum(1 for t in teams if t["source_used"] == SOURCE_NONE),
        "espn_fallback_teams": sorted(t["team"] for t in teams if t["source_used"] == SOURCE_ESPN),
        "espn_fallback_without_designations": sorted(t["team"] for t in teams if t["fallback_adds_no_designations"]),
        "unknown_kickoff": sorted(t["team"] for t in teams if t["kickoff"] is None),
        "publication_time": {"used_evidence": len(used),
                             "with_provider_time": sum(1 for t in used if t["provider_published_at"]),
                             "null_provider_time": sum(1 for t in used if not t["provider_published_at"])},
    }


def diagnostics(con, season: int, week: int, now: datetime, *, kickoffs: Mapping[str, Any] | None = None,
                thresholds: Thresholds = DEFAULT_THRESHOLDS) -> dict:
    """Report for the slate: per-team freshness, summary, and the upcoming games whose injury
    evidence is missing or outdated (earliest kickoff first). Read-only."""
    games = scheduled_games(con, season, week)
    opp = {g["team"]: g["opponent"] for g in games}
    ko = dict(kickoffs) if kickoffs is not None else {g["team"]: g["kickoff"] for g in games}
    teams = team_freshness(con, season, week, now, kickoffs=ko, thresholds=thresholds)
    issues = [{"team": t["team"], "opponent": opp.get(t["team"]), "kickoff": t["kickoff"], "status": t["status"],
               "reason": t["reason"], "hours_to_kickoff": t["hours_to_kickoff"]}
              for t in teams if t["status"] in (STALE, MISSING) and not t["game_started"]]
    return {"season": season, "week": week, "now": naive_utc(now).replace(tzinfo=timezone.utc).isoformat(),
            "thresholds": {"stale_after_hours": thresholds.stale_after_hours, "window_hours": thresholds.window_hours},
            "log_present": table_exists(con, TABLE), "summary": summarize(teams), "teams": teams,
            "upcoming_issues": issues}


# ----------------------------------------------------------------------------- integration views

MANIFEST_VIEW_SCHEMA = "injury-retrievals/v1"
LOG_UNAVAILABLE = "injury retrieval log unavailable"


def _utc_iso(value: datetime | None) -> str | None:
    t = naive_utc(value)
    return None if t is None else t.replace(tzinfo=timezone.utc).isoformat()


def manifest_view(con, season: int, week: int, captured_at: Any, *,
                  kickoffs: Mapping[str, Any] | None = None) -> dict:
    """What the source snapshot manifest records about WHEN each team's week-W nflverse injury rows were
    retrieved (our clock), from the append-only log. Pure SELECT; never writes, never imputes.

    The log row must describe the table content actually captured. The nflverse sync REPLACES the season
    on every run, so the content is that of the latest successful nfl sync (`sync_runs`, source 'nfl',
    status 'ok'), and only that run's own retrievals can describe it. Hence:
      * no table, no successful nfl sync run, or that run finished at/after `captured_at` (a backdated
        capture, or content newer than the capture): no team gets an entry;
      * otherwise a team gets its retrieval written by THAT run (`sync_run_id` equal to the run id), and
        only if it was retrieved strictly before `captured_at` and strictly before the team's kickoff.
        A run whose log write failed, or that did not list the team, leaves the team without an entry,
        and `known_by` then falls back to the sync-level time (conservative).
    Fail closed on an unverifiable newer sync: `run_nfl_sync` COMMITs the new content before it records the
    retrievals and calls `finish('ok')`. If the process died in between, the table holds NEWER content than
    the latest 'ok' run describes. So when any nfl sync started after that run (and before the capture) has a
    status other than 'ok'/'error' ('running' left by a crash, anything unknown; 'error' is only set after a
    rollback), or when a logged nflverse retrieval after it belongs to a sync that never recorded a status
    (a crash from before the 'running' marker existed), no team gets an entry.
    A team with an unknown kickoff never gets an entry. Nothing is guessed.
    `log`: 'present', 'unavailable' (old database without the table, or an unreadable log) or
    'unverifiable: ...' (the reason no entry was recorded)."""
    cut = naive_utc(captured_at)
    if cut is None:
        raise ValueError("captured_at is required")
    view: dict = {"schema": MANIFEST_VIEW_SCHEMA, "log": "unavailable", "teams": {}}
    try:
        if not table_exists(con, TABLE):
            return view
        ko = dict(kickoffs) if kickoffs is not None else scheduled_kickoffs(con, season, week)
        nfl = _retrievals(con, SOURCE_NFLVERSE, season, SCOPE_TEAM_WEEK, week)
        run = latest_nfl_sync_run(con)
    except Exception:
        return view
    view["log"] = "present"
    if run is None or run["finished_at"] is None or run["finished_at"] >= cut:
        return view
    try:
        why = unverifiable_after(con, season, run, cut)
    except Exception:
        why = f"cannot read sync history after {run['run_id']}"
    if why:
        view["log"] = f"unverifiable: {why}"
        return view
    for team in sorted(ko):
        kick = naive_utc(ko[team])
        mine = [r for r in nfl.get(team, []) if r["sync_run_id"] == run["run_id"]]
        hit = _latest(mine, cut)
        if hit is None or kick is None or hit["retrieved_at"] >= kick:
            continue
        view["teams"][team] = {"source": SOURCE_NFLVERSE, "retrieved_at": _utc_iso(hit["retrieved_at"]),
                               "provider_published_at": _utc_iso(hit["provider_published_at"]),
                               "content_sha": hit["content_sha"], "retrieval_id": hit["retrieval_id"]}
    return view


def latest_nfl_sync_run(con) -> dict | None:
    """The latest successful nflverse sync (the one that wrote the current nfl_* tables), or None."""
    if not table_exists(con, "sync_runs"):
        return None
    row = con.execute("SELECT run_id, finished_at, started_at FROM sync_runs WHERE source = 'nfl' AND status = 'ok' "
                      "ORDER BY finished_at DESC NULLS LAST, started_at DESC LIMIT 1").fetchone()
    return None if row is None else {"run_id": row[0], "finished_at": naive_utc(row[1]),
                                     "started_at": naive_utc(row[2])}


def unverifiable_after(con, season: int, run: dict, cut: datetime) -> str | None:
    """Why the table content may be newer than `run` (the latest 'ok' nfl sync), or None. See manifest_view."""
    started = run["started_at"]
    if started is None:
        return f"start time of {run['run_id']} unknown"
    bad = con.execute("SELECT run_id, status FROM sync_runs WHERE source = 'nfl' AND run_id <> ? AND started_at > ? "
                      "AND started_at < ? AND (status IS NULL OR status NOT IN ('ok', 'error')) "
                      "ORDER BY started_at LIMIT 1", [run["run_id"], started, cut]).fetchone()
    if bad:
        return f"unfinished nfl sync {bad[0]} (status {bad[1]}) after {run['run_id']}"
    if not table_exists(con, TABLE):
        return None
    orphan = con.execute(
        f"SELECT sync_run_id FROM {TABLE} WHERE source = ? AND season = ? AND retrieved_at >= ? AND retrieved_at < ? "
        "AND sync_run_id IS NOT NULL AND sync_run_id <> ? AND sync_run_id NOT IN (SELECT run_id FROM sync_runs) "
        "LIMIT 1", [SOURCE_NFLVERSE, season, started, cut, run["run_id"]]).fetchone()
    if orphan:
        return f"nfl retrieval of sync {orphan[0]}, which has no recorded status, after {run['run_id']}"
    return None


def readiness_notes(con, season: int, week: int, now: datetime) -> dict:
    """Informational injury-freshness warnings for the readiness report and the learning-cycle report.
    They never block or change a forecast: no caller may use them as a gate or a model input.
    Keys: log_available, warnings (strings), upcoming_issues (missing / stale, game not started),
    post_kickoff_only, fallback_adds_no_designations (teams whose ESPN fallback contributes zero
    designations). An absent log table, or any error reading it, yields one 'unavailable' warning."""
    out = {"log_available": False, "warnings": [], "upcoming_issues": [], "post_kickoff_only": [],
           "fallback_adds_no_designations": []}
    try:
        if con is None or not table_exists(con, TABLE):
            out["warnings"].append(f"{LOG_UNAVAILABLE} (no {TABLE} table in this database); "
                                   "pregame injury freshness not checked")
            return out
        diag = diagnostics(con, season, week, now)
    except Exception as err:
        out["warnings"].append(f"{LOG_UNAVAILABLE} (freshness check failed: {type(err).__name__}); "
                               "pregame injury freshness not checked")
        return out
    out["log_available"] = True
    out["upcoming_issues"] = diag["upcoming_issues"]
    out["post_kickoff_only"] = sorted(t["team"] for t in diag["teams"] if t["status"] == POST_KICKOFF_ONLY)
    out["fallback_adds_no_designations"] = diag["summary"]["espn_fallback_without_designations"]
    for status, label in ((MISSING, "no injury evidence retrieved"), (STALE, "stale injury evidence")):
        hit = [i for i in diag["upcoming_issues"] if i["status"] == status]
        if hit:
            out["warnings"].append(f"{label} for {len(hit)} team(s) before kickoff: "
                                   + ", ".join(f"{i['team']} ({i['reason']})" for i in hit[:8])
                                   + (" ..." if len(hit) > 8 else "") + "; informational only, the forecast is unchanged")
    if out["post_kickoff_only"]:
        out["warnings"].append(f"injury evidence retrieved only after kickoff for {', '.join(out['post_kickoff_only'])}; "
                               "informational only")
    if out["fallback_adds_no_designations"]:
        out["warnings"].append("ESPN status fallback adds no designations for "
                               f"{', '.join(out['fallback_adds_no_designations'])} (the model sees an empty injury "
                               "list for them); informational only")
    return out


# ----------------------------------------------------------------------------- refresh


class RefreshRefused(RuntimeError):
    pass


def plan_refresh(con, season: int, week: int, now: datetime, *, kickoffs: Mapping[str, Any],
                 thresholds: Thresholds = DEFAULT_THRESHOLDS) -> dict:
    """Which teams need a refresh BEFORE their kickoff. Teams whose game already started are never
    refreshable (a retrieval now could not be pregame) and are listed under `not_refreshable`.
    A team with an unknown kickoff is listed with a caveat: its retrieval can never be labeled pregame.
    `sources`: nflverse for every refreshable team; also espn_status when the team has no nflverse
    designation yet (the fallback in use)."""
    teams = team_freshness(con, season, week, now, kickoffs=kickoffs, thresholds=thresholds)
    refresh, blocked, ok = [], [], []
    for t in teams:
        if t["game_started"]:
            blocked.append({"team": t["team"], "kickoff": t["kickoff"], "status": t["status"],
                            "reason": "kicked_off: a retrieval now would be post-kickoff, never labeled pregame"})
        elif t["status"] in (STALE, MISSING):
            srcs = [SOURCE_NFLVERSE] + ([SOURCE_ESPN] if not t["nflverse_designation_synced"] else [])
            refresh.append({"team": t["team"], "kickoff": t["kickoff"], "hours_to_kickoff": t["hours_to_kickoff"],
                            "status": t["status"], "reason": t["reason"], "sources": srcs,
                            "refresh_before": t["kickoff"],
                            "caveat": "kickoff unknown: a retrieval cannot be labeled pregame"
                            if t["kickoff"] is None else None})
        else:
            ok.append(t["team"])
    sources = sorted({s for r in refresh for s in r["sources"]})
    return {"season": season, "week": week, "now": naive_utc(now).replace(tzinfo=timezone.utc).isoformat(),
            "refreshable": refresh, "not_refreshable": blocked, "up_to_date": ok, "sources": sources,
            "uses": "existing sync functions only (nflverse via nflreadpy, ESPN league API); no new feed"}


def execute_refresh(plan: dict, *, sync_nfl: Callable[[], Any], sync_espn: Callable[[], Any],
                    now_fn: Callable[[], datetime] = utcnow) -> dict:
    """Run the existing syncs the plan asks for. Refuses when no team can still be refreshed pregame.
    Each source's sync runs independently and failures are reported per source in `errors` (the other
    source still runs). Afterwards each team is labeled by comparing the refresh's completion time with its kickoff, so a
    refresh that overran a kickoff is reported as post_kickoff, not pregame."""
    if not plan["refreshable"]:
        raise RefreshRefused("no team needs a pregame refresh (all up to date, or already kicked off)")
    started = naive_utc(now_fn())
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    # Each source runs independently: one failing sync must not stop the other from refreshing.
    for source, fn in ((SOURCE_NFLVERSE, sync_nfl), (SOURCE_ESPN, sync_espn)):
        if source not in plan["sources"]:
            continue
        try:
            fn()
            results[source] = "ran"
        except Exception as err:
            results[source] = "error"
            errors[source] = f"{type(err).__name__}: {err}"
    finished = naive_utc(now_fn())
    # A team whose planned source failed has no new retrieval from it: never labeled pregame.
    labels = {r["team"]: "refresh_failed" if any(src in errors for src in r["sources"])
              else label_pregame(finished, r["kickoff"]) for r in plan["refreshable"]}
    return {"started_at": started.isoformat(), "finished_at": finished.isoformat(), "ran": results,
            "errors": errors, "labels": labels, "note": "evidence is only pregame for teams labeled 'pregame'; "
                                      "re-run `report` to see the recorded retrievals"}
