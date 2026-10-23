"""Cross-league player exposure (issue #209): which NFL players a signed-in manager holds across their own leagues, how
many leagues and teams each one is on, which weeks pile up byes, and how many rostered players carry an injury
designation, read-only (contract portfolio.v1).

Authorization comes first and happens in the /v2 route (leagues_v2.portfolio), exactly as for the action center: the
caller's own leagues (memberships and pins, `mutations.list_my_leagues`) filtered by `authorize_leagues_in`. No league
id is ever taken from the client. The roster read is the caller's saved team in each league (`fi.team_selections`,
the same perspective every league page uses); no other team's roster is read or returned.

Each granted league is read on its own, with the action center's mechanics: its files are pinned through the shared
`league_file_cache` (released in `finally`), LEAGUE_SCOPE is set for the worker thread and `deps.db()` opens the
layered read-only cursor on that league's file and the global file it was built against. Nothing is cached here: every
request reads the files it was authorized for. Fan-out runs on the action center's process-wide pool and admission
slots (`action_center.run_bounded`, `action_center.serve` with kind "portfolio"): at most MAX_LEAGUES leagues, at most
CONCURRENCY at once, BUDGET_S overall; a league still loading then is reported as such, never dropped. Only leagues of
the newest season among the caller's leagues are combined; the others are listed as excluded. Nothing here contacts a
provider or queues a sync.

Identity (who is "the same player" across leagues) follows app/leagues/identity.py: only id-based evidence joins.

  * verified       the league's player_id_map links the player to a GSIS id through an ESPN-id crosswalk
                   (identity.link_from_espn_map_row: nfl_players, ff_playerids, rosters_weekly). Name-only matches
                   (`name_position_team`) and crosswalks the map itself marks as conflicting are not evidence;
  * team_defense   a D/ST (position D/ST, ESPN's -16000 - team id) joins by its NFL team;
  * espn_id        an ESPN league's player without a verified GSIS link still joins other ESPN leagues by ESPN's own
                   player id (one provider's id space), never a Sleeper league;
  * unverified     everything else (no link, a name-only match, a Sleeper player with no ESPN id) stays its own row,
                   one league each;
  * conflict       the league's own map marks the crosswalk conflicting, the same player id is linked to two GSIS
                   ids in different leagues, or one GSIS id is reached from two different player ids: no GSIS link is
                   used for any row involved (an ESPN league's row still joins by ESPN's own id, as above; any
                   other row stays its own).

Honesty rules: counts are exact (distinct leagues and teams); a missing projection, bye week or schedule stays null
with a reason, never 0; injury designations are as of each league's last sync and are never projected to later weeks;
there is no risk score, grade or probability.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from ..config import get_settings
from . import provider_copy
from .deps import LEAGUE_SCOPE, LeagueScope

log = logging.getLogger(__name__)

CONTRACT = "portfolio.v1"
MAX_LEAGUES = 25             # leagues combined per request (the follow limit); the rest are listed, not read
CONCURRENCY = 3              # leagues read at once
BUDGET_S = 20.0              # the whole fan-out; a league not done by then is "not_ready" (still loading)
D_ST_IDS = range(-16999, -15999)    # ESPN's team defense ids (-16000 - pro team id); Sleeper's writer uses the same
HEALTHY = {"", "ACTIVE", "NORMAL"}
NAMES_PER_WEEK = 8           # player names listed in a week's bye cell; the counts are always complete

NOT_SYNCED = {
    "first_sync_queued": "The first sync is queued.",
    "first_sync_running": "The first sync is running.",
    "first_sync_failed": "The first sync failed.",
    "no_data": "This league has no synced data yet.",
}
NO_TEAM = "No team is saved for you in this league, so it has no roster to count."
TEAM_GONE = "Your saved team is not in this league's synced data. Choose your team again."
FILES_UNAVAILABLE = "This league's data could not be loaded. Try again shortly."
TOO_SLOW = "This league's data is still loading. Reload in a moment."
PROVIDER_OFF = "This league's provider is switched off in this deployment."
UNREADABLE = "This league's data could not be read."
OTHER_SEASON = "other_season"
OVER_LIMIT = "over_limit"

REASON = {
    "no_map": "This league's data has no NFL id link for this player.",
    "name_only": "Matched to NFL data by name only, which is not identity evidence.",
    "map_conflict": "This league's NFL id sources disagree for this player, so no NFL link is used.",
    "synthetic": "This player has no ESPN or NFL id in this league's data; not joined across leagues.",
    "dst_unknown": "A team defense without a known NFL team; not joined across leagues.",
    "id_split": "Your leagues link this player id to different NFL players, so no NFL link is used.",
    "gsis_split": "Two different player ids link to the same NFL player, so no NFL link is used.",
    "espn_only": "Joined with your other ESPN leagues by ESPN's player id only; not linked to NFL data.",
    "espn_propagated": "Joined by ESPN's player id: another of your ESPN leagues links it to NFL data.",
}


def _env_number(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return min(hi, max(lo, value))


def limits() -> tuple[int, int, float]:
    """(max leagues, concurrency, budget seconds): bounded whatever the environment says."""
    return (int(_env_number("PORTFOLIO_MAX_LEAGUES", MAX_LEAGUES, 1, MAX_LEAGUES)),
            int(_env_number("PORTFOLIO_CONCURRENCY", CONCURRENCY, 1, 6)),
            _env_number("PORTFOLIO_BUDGET_S", BUDGET_S, 1.0, 60.0))


def _iso(value: Any) -> str | None:
    from .signal_provenance import iso_utc
    return iso_utc(value)


def _section(access, state: str, reason: str | None = None, **extra) -> dict:
    return {"league": access.to_json(), "state": state, "reason": reason, "stale": False, "degraded": None,
            "versions": None, "team": None, "week": None, "final_week": None, "synced_at": None,
            "roster_size": None, "schedule_known": None, "projection": None, "provenance": None, **extra}


# ---- one league ---------------------------------------------------------------------------------------------------------

def bye_weeks(sched: dict) -> dict[str, int | None]:
    """team -> its bye week, only for teams whose regular-season schedule is complete (bye_planner's rule: a missing
    week is a bye only then). A complete team with no missing week maps to None; other teams are absent (unknown)."""
    out: dict[str, int | None] = {}
    if not sched.get("available") or not sched.get("covered"):
        return out
    last = max(sched["covered"])
    for team in sched["complete_teams"]:
        missing = [w for w in range(1, last + 1) if (team, w) not in sched["games"]]
        out[team] = missing[0] if len(missing) == 1 else None
    return out


_ROSTER_SQL = """
    SELECT l.espn_player_id AS player_id, l.lineup_slot, l.lineup_slot_id, l.is_starter,
           fp.full_name, fp.position, fp.pro_team, fp.injury_status,
           {map_cols}
    FROM lineup_snapshots l JOIN fantasy_players fp ON fp.espn_player_id = l.espn_player_id
    {map_join}
    WHERE l.league_id = ? AND l.season = ? AND l.scoring_period = ? AND l.team_id = ?
    ORDER BY l.is_starter DESC, l.lineup_slot_id, fp.full_name
"""


def read_league(team: int) -> dict:
    """The saved team's roster in the league LEAGUE_SCOPE names, with each player's id link, bye week and this week's
    projection. Runs inside the scope; never raises HTTPException for a missing team (returns {"team_gone": True})."""
    from ..analytics import projection_source as ps
    from ..analytics.bye_planner import load_schedule_facts
    from ..analytics.legacy_league import latest_league
    from ..analytics.reserve_slots import is_reserved
    from ..db.connection import table_exists
    from .deps import db, one, rows
    with db() as con:
        lg = latest_league(con, ["league_id", "season", "name", "current_week", "final_scoring_period", "synced_at"])
        if not lg:
            raise HTTPException(503, "This league's data has no league row.")
        league_id, season, week = lg["league_id"], lg["season"], lg["current_week"]
        found = one(con, "SELECT team_id, name FROM fantasy_teams WHERE league_id = ? AND season = ? AND team_id = ?",
                    [league_id, season, team])
        if found is None:
            return {"team_gone": True}
        has_map = table_exists(con, "player_id_map")
        sql = _ROSTER_SQL.format(
            map_cols=("m.gsis_id, m.match_method, m.match_confidence, m.notes" if has_map else
                      "NULL AS gsis_id, NULL AS match_method, NULL AS match_confidence, NULL AS notes"),
            map_join="LEFT JOIN player_id_map m ON m.espn_player_id = l.espn_player_id" if has_map else "")
        roster = rows(con, sql, [league_id, season, week, team]) if week is not None else []
        byes = bye_weeks(load_schedule_facts(con, season))
        sched_known = bool(byes)
        projection: dict[str, Any] = {"source": None, "available": False, "reason": None, "captured_at": None}
        values: dict = {}
        origin: dict = {}
        try:
            source = ps.default_source(con, season)
            w = ps.weekly(con, season, week, source) if week is not None else None
        except Exception as err:  # noqa: BLE001 - a projection failure leaves projections unknown, never zero
            log.warning("portfolio: projections unreadable: %s", type(err).__name__)
            projection["reason"] = "This league's projections could not be read."
            w = None
        if w is not None:
            values, origin = w.get("values") or {}, w.get("origin") or {}
            run = w.get("run") or {}
            projection.update(available=bool(w.get("available")), reason=w.get("reason"),
                              captured_at=_iso(run.get("created_at")) if source == "independent" else _iso(lg["synced_at"]))
        players = []
        for r in roster:
            pid = r["player_id"]
            value = values.get(pid)
            tag = origin.get(pid)
            team_code = r["pro_team"] or None
            players.append({
                "player_id": pid, "name": r["full_name"], "position": r["position"], "nfl_team": team_code,
                "slot": r["lineup_slot"], "is_starter": bool(r["is_starter"]), "reserve": is_reserved(r),
                "injury_status": r["injury_status"] or None,
                "projection": None if value is None else round(float(value), 2),
                "projection_origin": None if value is None else ("model" if tag in ps.MODEL_ORIGINS else "provider"),
                "bye_week": byes.get(team_code) if team_code else None,
                "bye_known": bool(team_code) and team_code in byes,
                "map": {k: r[k] for k in ("gsis_id", "match_method", "match_confidence", "notes")},
            })
        shown = [p["projection_origin"] for p in players if p["projection"] is not None]
        projection["source"] = ("Unavailable" if not shown else ps.served_label(
            ps.OURS if o == "model" else ps.ESPN for o in shown))
        return {"team_gone": False, "league": lg, "team": {"team_id": found["team_id"], "name": found["name"]},
                "players": players, "schedule_known": sched_known, "projection": projection}


def _pin_without_download(files, league_id, data_version, global_version, budget_s):
    from .action_center import _pin_without_download as pin
    return pin(files, league_id, data_version, global_version, budget_s)


def evaluate_league(access) -> dict:
    """One league's section (with its exposures under "_players"). Runs in a worker thread and never raises: anything
    unexpected is that league's own "unavailable" state (only the exception type is logged, never a league id)."""
    try:
        return _evaluate(access)
    except Exception as err:  # noqa: BLE001
        log.warning("portfolio: a league failed: %s", type(err).__name__)
        return _section(access, "unavailable", UNREADABLE)


def _evaluate(access) -> dict:
    from ..db.league_conn import perspective_team
    from ..db.league_split import VERSION_RE
    from .league_scope import cold_load_budget_s, league_file_cache
    from .signal_provenance import coverage, payload, signal
    if access.snapshot is None:
        return _section(access, "not_ready", NOT_SYNCED.get(access.sync_state, NOT_SYNCED["no_data"]))
    if access.provider == "sleeper" and get_settings().sleeper_source == "disabled":
        return _section(access, "unavailable", PROVIDER_OFF)
    team = perspective_team(access.perspective_team_id)
    if team is None:
        return _section(access, "needs_team", NO_TEAM)
    snap = access.snapshot
    if not VERSION_RE.fullmatch(snap.data_version) or not VERSION_RE.fullmatch(snap.global_version):
        return _section(access, "unavailable", FILES_UNAVAILABLE)
    files = league_file_cache()
    try:
        pair = _pin_without_download(files, access.league_id, snap.data_version, snap.global_version,
                                     cold_load_budget_s())
    except Exception as err:  # noqa: BLE001 - R2 errors, a missing object, a full cache: that league's state only
        log.warning("portfolio: league files unavailable: %s", type(err).__name__)
        return _section(access, "unavailable", FILES_UNAVAILABLE)
    if pair is None:
        return _section(access, "not_ready", TOO_SLOW)
    try:
        scope = LeagueScope(access.league_id, pair.league_path, pair.global_path, pair.data_version,
                            pair.global_version, access.visibility_epoch, team, access.provider)
        token = LEAGUE_SCOPE.set(scope)
        try:
            read = read_league(team)
        finally:
            LEAGUE_SCOPE.reset(token)
    except HTTPException as err:
        detail = err.detail if isinstance(err.detail, str) and err.detail else UNREADABLE
        return _section(access, "unavailable", detail if access.provider == "espn" else provider_copy.rewrite(detail))
    finally:
        files.release_pair(pair)
    if read.get("team_gone"):
        return _section(access, "needs_team", TEAM_GONE)
    lg, players, proj = read["league"], read["players"], read["projection"]
    known = sum(1 for p in players if p["projection"] is not None)
    proj_out = {"source": proj["source"], "available": proj["available"], "reason": proj["reason"],
                "known": known, "total": len(players)}
    roster_sig = signal("league", "Your roster", access.provider, lg["synced_at"], pair.data_version,
                        stale_reason="Read from this league's previous files: a newer sync could not be loaded in time."
                        if pair.degraded else None)
    proj_sig = signal("projections", "This week's projections",
                      "fantasy_intel" if proj["source"] in ("Ours", "Ours and ESPN") else access.provider,
                      proj["captured_at"], pair.data_version,
                      coverage=coverage(known, len(players), "rostered players with a projection") if players else None,
                      reason_unavailable=None if known == len(players) else
                      (proj["reason"] or "Some rostered players have no projection this week."))
    section = _section(
        access, "ready", None,
        stale=bool(pair.degraded), degraded=pair.degraded,
        versions={"data_version": pair.data_version, "global_version": pair.global_version},
        team=read["team"], week=lg["current_week"], final_week=lg.get("final_scoring_period"),
        synced_at=_iso(lg["synced_at"]), roster_size=len(players), projection=proj_out,
        provenance=payload(roster_sig, proj_sig), schedule_known=read["schedule_known"],
    )
    if access.provider != "espn":                     # Sleeper's wording, as the league's own pages get it
        for part in ("projection", "provenance"):
            section[part] = provider_copy.rewrite(section[part])
    section["_players"] = players
    return section


# ---- identity --------------------------------------------------------------------------------------------------------

def link(p: dict) -> tuple[str, str | None, str | None]:
    """(status, gsis id or D/ST team, reason) for one exposure, from its league's own id map row only."""
    from ..leagues.identity import link_from_espn_map_row
    pid, m = p["player_id"], p.get("map") or {}
    if p.get("position") == "D/ST":
        if isinstance(pid, int) and pid in D_ST_IDS and p.get("nfl_team"):
            return "team_defense", p["nfl_team"], None
        return "unverified", None, REASON["dst_unknown"]
    if not isinstance(pid, int) or pid <= 0:
        return "unverified", None, REASON["synthetic"]
    if not m.get("match_method"):
        return "unverified", None, REASON["no_map"]
    if (m.get("notes") or "").startswith("Conflicting crosswalks"):
        return "conflict", None, REASON["map_conflict"]
    try:
        lk = link_from_espn_map_row({"espn_player_id": pid, "match_method": m["match_method"],
                                     "gsis_id": m.get("gsis_id"), "nfl_team": None, "notes": m.get("notes")})
    except ValueError:                        # a malformed GSIS id in the map is no evidence
        return "unverified", None, REASON["no_map"]
    if lk.gsis_id:
        return "verified", lk.gsis_id, None
    return "unverified", None, REASON["name_only"] if m["match_method"] == "name_position_team" else REASON["no_map"]


def resolve(exposures: list[dict]) -> None:
    """Sets `key`, `identity` {status, gsis_id, reason} on every exposure (each a dict with league_id, provider and the
    read_league player fields), applying the cross-league conflict rules of the module docstring. Pure."""
    for e in exposures:
        e["_status"], e["_ref"], e["_reason"] = link(e)
    by_id: dict[int, set[str]] = {}
    by_gsis: dict[str, set[int]] = {}
    for e in exposures:
        if e["_status"] == "verified":
            by_id.setdefault(e["player_id"], set()).add(e["_ref"])
            by_gsis.setdefault(e["_ref"], set()).add(e["player_id"])
    for e in exposures:
        if e["_status"] != "verified":
            continue
        if len(by_id[e["player_id"]]) > 1:
            e["_status"], e["_ref"], e["_reason"] = "conflict", None, REASON["id_split"]
        elif len(by_gsis[e["_ref"]]) > 1:
            e["_status"], e["_ref"], e["_reason"] = "conflict", None, REASON["gsis_split"]
    # ESPN's own id joins ESPN leagues: an ESPN exposure with no verified link takes the one GSIS id its ESPN id has
    # in another ESPN league (after the conflict rules above), else it joins by the ESPN id alone.
    espn_gsis: dict[int, set[str]] = {}
    for e in exposures:
        if e["provider"] == "espn" and e["_status"] == "verified":
            espn_gsis.setdefault(e["player_id"], set()).add(e["_ref"])
    for e in exposures:
        pid = e["player_id"]
        status, ref = e["_status"], e["_ref"]
        if status == "verified":
            key, gsis = f"gsis:{ref}", ref
        elif status == "team_defense":
            key, gsis = f"dst:{ref}", None
        elif e["provider"] == "espn" and isinstance(pid, int) and pid > 0:
            found = espn_gsis.get(pid, set())
            if status == "unverified" and len(found) == 1:
                gsis = next(iter(found))
                key, status, e["_reason"] = f"gsis:{gsis}", "verified", REASON["espn_propagated"]
            else:
                key, gsis = f"espn:{pid}", None
                if status == "unverified":
                    status, e["_reason"] = "espn_id", f"{REASON['espn_only']} {e['_reason']}"
        else:
            key, gsis = f"league:{e['league_id']}:{pid}", None
        e["key"] = key
        e["identity"] = {"status": status, "gsis_id": gsis, "reason": e["_reason"]}
        for k in ("_status", "_ref", "_reason"):
            e.pop(k, None)


# ---- the combined view -------------------------------------------------------------------------------------------

def designated(status: str | None) -> bool:
    return (status or "").upper() not in HEALTHY


def _merge_identity(group: list[dict]) -> dict:
    statuses = {e["identity"]["status"] for e in group}
    for pref in ("verified", "team_defense", "conflict", "espn_id", "unverified"):
        if pref in statuses:
            first = next(e for e in group if e["identity"]["status"] == pref)
            return dict(first["identity"])
    return dict(group[0]["identity"])


def aggregate(sections: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """(players, weeks, coverage) from ready sections (each carrying "_players"). Pure: no I/O."""
    ready = [s for s in sections if s["state"] == "ready"]
    exposures: list[dict] = []
    for s in ready:
        lg = s["league"]
        for p in s.get("_players") or []:
            exposures.append({**p, "league_id": lg["league_id"], "provider": lg["provider"], "league_name": lg["name"],
                              "team_id": s["team"]["team_id"], "team_name": s["team"]["name"], "week": s["week"],
                              "synced_at": s["synced_at"], "stale": s["stale"]})
    resolve(exposures)
    groups: dict[str, list[dict]] = {}
    for e in exposures:
        groups.setdefault(e["key"], []).append(e)
    total = len(ready)
    players = []
    for key, group in groups.items():
        group.sort(key=lambda e: (e["synced_at"] or "", e["league_id"]), reverse=True)    # newest data first
        lead = group[0]
        byes = {e["bye_week"] for e in group if e["bye_known"] and e["bye_week"] is not None}
        bye_unknown = any(not e["bye_known"] for e in group)
        if len(byes) == 1:
            bye, bye_reason = next(iter(byes)), None
        elif len(byes) > 1:
            bye, bye_reason = None, "Leagues' schedules disagree on this player's bye week."
        elif bye_unknown:
            bye, bye_reason = None, "No complete NFL schedule for this player's team in this league's data."
        else:
            bye, bye_reason = None, "His team has no bye week in the loaded schedule."
        statuses = sorted({(e["injury_status"] or "").upper() for e in group if designated(e["injury_status"])})
        players.append({
            "key": key, "name": lead["name"], "position": lead["position"], "nfl_team": lead["nfl_team"],
            "identity": _merge_identity(group),
            "leagues": len({e["league_id"] for e in group}), "of": total,
            "teams": len({(e["league_id"], e["team_id"]) for e in group}),
            "starters": sum(1 for e in group if e["is_starter"]),
            "bye_week": bye, "bye_reason": bye_reason,
            "injury": {"designated": sum(1 for e in group if designated(e["injury_status"])), "statuses": statuses},
            "projections": {"known": sum(1 for e in group if e["projection"] is not None),
                            "missing": sum(1 for e in group if e["projection"] is None)},
            "exposures": [{k: e[k] for k in ("league_id", "provider", "league_name", "team_id", "team_name",
                                              "player_id", "slot", "is_starter", "reserve", "injury_status",
                                              "projection", "projection_origin", "week", "bye_week", "synced_at",
                                              "stale")} | {"identity": e["identity"]} for e in group],
        })
    players.sort(key=lambda p: (-p["leagues"], -p["starters"], p["name"] or "", p["key"]))
    weeks = _weeks(ready, exposures, groups)
    status_counts: dict[str, int] = {}
    for p in players:
        status_counts[p["identity"]["status"]] = status_counts.get(p["identity"]["status"], 0) + 1
    cov = {
        "leagues_ready": total, "leagues_total": len(sections), "exposures": len(exposures), "players": len(players),
        "identity": status_counts,
        "projections_known": sum(1 for e in exposures if e["projection"] is not None),
        "projections_missing": sum(1 for e in exposures if e["projection"] is None),
        "bye_unknown": sum(1 for e in exposures if not e["bye_known"]),
    }
    return players, weeks, cov


def _weeks(ready: list[dict], exposures: list[dict], groups: dict[str, list[dict]]) -> list[dict]:
    """One row per NFL week from the earliest ready league's current week to the latest final scoring period: the
    byes among active (non-IR, non-taxi) rostered players in the leagues still playing that week, and the injury
    designations counted only in the week each league's data is from."""
    spans = {}
    for s in ready:
        if isinstance(s["week"], int):
            last = s["final_week"] if isinstance(s["final_week"], int) and s["final_week"] >= s["week"] else s["week"]
            spans[s["league"]["league_id"]] = (s["week"], last)
    if not spans:
        return []
    names = {e["key"]: (groups[e["key"]][0]["name"] or "") for e in exposures}
    out = []
    for w in range(min(a for a, _ in spans.values()), max(b for _, b in spans.values()) + 1):
        playing = {lid for lid, (a, b) in spans.items() if a <= w <= b}
        on_bye = [e for e in exposures if e["league_id"] in playing and not e["reserve"] and e["bye_week"] == w]
        unknown = sum(1 for e in exposures if e["league_id"] in playing and not e["reserve"] and not e["bye_known"])
        current = {lid for lid, (a, _) in spans.items() if a == w}
        hurt = [e for e in exposures if e["league_id"] in current and designated(e["injury_status"])]
        bye_keys = sorted({e["key"] for e in on_bye}, key=lambda k: names[k])
        out.append({
            "week": w, "leagues_playing": len(playing),
            "bye": {"exposures": len(on_bye), "players": len(bye_keys), "leagues": len({e["league_id"] for e in on_bye}),
                    "starters": sum(1 for e in on_bye if e["is_starter"]), "unknown": unknown,
                    "names": [names[k] for k in bye_keys[:NAMES_PER_WEEK]]},
            "injuries": None if not current else {
                "exposures": len(hurt), "players": len({e["key"] for e in hurt}),
                "leagues": len({e["league_id"] for e in hurt}), "leagues_reporting": len(current)},
        })
    return out


# ---- the whole response -------------------------------------------------------------------------------------------

def split(granted: list, max_leagues: int) -> tuple[list, list[dict]]:
    """(the leagues to read, the excluded ones with why): only the newest season among the caller's leagues is
    combined, at most `max_leagues` of it, in GET /v2/leagues order."""
    season = max((a.season for a in granted), default=None)
    same = [a for a in granted if a.season == season]
    excluded = [{"league": a.to_json(), "reason": OTHER_SEASON} for a in granted if a.season != season]
    excluded += [{"league": a.to_json(), "reason": OVER_LIMIT} for a in same[max_leagues:]]
    return same[:max_leagues], excluded


async def build(principal, granted: list, now: datetime | None = None) -> tuple[dict, bool]:
    """(response body, whether any league was read from older files). `granted` is the authorized list."""
    from .action_center import run_bounded
    now = now or datetime.now(timezone.utc)
    max_leagues, concurrency, budget = limits()
    evaluated, excluded = split(granted, max_leagues)
    results = await run_bounded(evaluated, evaluate_league, concurrency, budget,
                                on_error=lambda access: _section(access, "unavailable", UNREADABLE))
    sections = [s if s is not None else _section(evaluated[i], "not_ready", TOO_SLOW) for i, s in enumerate(results)]
    players, weeks, cov = aggregate(sections)
    for s in sections:
        s.pop("_players", None)
    body = {
        "contract": CONTRACT,
        "generated_at": now.isoformat(),
        "limit": max_leagues,
        "season": evaluated[0].season if evaluated else None,
        "leagues": sections,
        "excluded": {"count": len(excluded), "leagues": excluded},
        "players": players,
        "weeks": weeks,
        "coverage": cov,
    }
    return body, any(s.get("stale") for s in sections)


async def serve(principal, granted: list) -> tuple[dict, bool]:
    """`build` behind the action center's admission slots, with one portfolio build per user at a time."""
    from . import action_center
    return await action_center.serve(principal, granted, kind="portfolio", build_fn=build, budget_s=limits()[2])
