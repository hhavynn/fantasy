"""Availability timeline and roster digest (issue #176, docs/availability_timeline.md): GET
/api/injury-impact/availability-timeline.

Read-only. For one fantasy team's current roster (the perspective team, `team_id`, else the owner's), every
availability event the app already ingests, in order, each with its source and its time:

  official     the NFL injury report (nflverse `load_injuries`: game status, practice participation, injury,
               `date_modified`) and the NFL weekly roster status (nflverse `load_rosters_weekly`: active, reserve,
               suspended ..., week only, no time);
  provider     the league provider's current player status (ESPN, or Sleeper mapped to ESPN's words), with the time
               we synced it: the provider publishes no time of its own;
  third_party  owner-curated news summaries (availability_news.json) with the reports they cite. Interpretation,
               never a designation: they supplement, never replace, an official or provider status;
  schedule     the week's kickoff (nflverse schedules), so a report modified after kickoff is visibly late.

No new source, scrape or paid feed. Practice participation is what nflverse publishes: one value per report row (the
week's final practice listed), not each practice day; daily DNP/LP/FP progression is unavailable and never invented.

Truthful missing data: a player with no report this week is `unknown` (with the reason: not listed, no report for his
team yet, no report synced at all, no NFL id), never healthy or full practice. Every timestamp is UTC ISO-8601 with
`Z`; a time the source does not publish is null, and the event says what its time means (`at_basis`).

Consistency with lineup advice: the headline level is availability.availability() over the same inputs the roster,
Start/Sit and Overview read (provider status, IR / taxi slot, the week's NFL report: the most severe wins), so this
page never states a different level than the lineup tools; where the sources disagree it says so and names both.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType

from ..db.connection import fetch_dicts, table_columns, table_exists, utcnow
from . import availability_news as news
from . import injury_freshness as fresh
from .availability import AVAILABLE, DOUBTFUL, ESPN_STATUS, IR, NFL_REPORT_STATUS, OUT, QUESTIONABLE, SEVERITY, \
    SUSPENDED, TAXI, availability
from .legacy_league import latest_league
from .projections import league_window
from .reserve_slots import is_taxi

VERSION = "availability-timeline/v1"
HISTORY_WEEKS = 3            # the window: the current week and the 3 before it

NFLVERSE_INJURIES_URL = "https://github.com/nflverse/nflverse-data/releases/tag/injuries"
NFLVERSE_ROSTERS_URL = "https://github.com/nflverse/nflverse-data/releases/tag/weekly_rosters"
NFLVERSE_SCHEDULES_URL = "https://github.com/nflverse/nflverse-data/releases/tag/schedules"

# Constant tables are read-only (MappingProxyType): no module-level mutable state.
DESIGNATED = MappingProxyType({QUESTIONABLE: "questionable", DOUBTFUL: "doubtful", OUT: "out", IR: "ir",
                               SUSPENDED: "suspended", TAXI: "taxi"})
LEVEL_RANK = MappingProxyType({"suspended": 0, "ir": 1, "out": 2, "doubtful": 3, "questionable": 4, "taxi": 5,
                               "listed": 6, "unknown": 7})
LEVEL_LABEL = MappingProxyType({"suspended": "Suspended", "ir": "Injured reserve", "out": "Out", "doubtful": "Doubtful",
                                "questionable": "Questionable", "taxi": "Taxi squad",
                                "listed": "Listed, no game status", "unknown": "Unknown"})
PROVIDER_LABEL = MappingProxyType({"ACTIVE": "No designation", "NORMAL": "No designation",
                                   "QUESTIONABLE": "Questionable", "DAY_TO_DAY": "Day to day", "DOUBTFUL": "Doubtful",
                                   "OUT": "Out", "INJURY_RESERVE": "Injured reserve", "SUSPENSION": "Suspended"})
PRACTICE_LABEL = MappingProxyType({"DNP": "Did not participate", "LP": "Limited", "FP": "Full"})
ROSTER_LABEL = MappingProxyType({"ACT": "Active", "RES": "Reserve list", "INA": "Inactive",
                                 "PUP": "Physically unable to perform", "NFI": "Non-football injury list",
                                 "SUS": "Suspended", "EXE": "Commissioner exempt", "CUT": "Released", "RET": "Retired",
                                 "TRD": "Traded", "TRT": "Traded", "DEV": "Practice squad", "UFA": "Free agent"})
RESERVE = ("RES", "PUP", "NFI", "SUS", "EXE")
KIND_RANK = MappingProxyType({"roster_status": 0, "third_party": 1, "official_report": 2, "provider_status": 3,
                              "kickoff": 4})

UNKNOWN_TEXT = MappingProxyType({
    "not_listed": "Not on his team's week {w} NFL injury report. Not being listed is not a clean bill of health: "
                  "availability unknown.",
    "no_team_report": "His team has no week {w} NFL injury report synced yet: availability unknown.",
    "not_published": "No week {w} NFL injury report is synced yet: availability unknown.",
    "no_feed": "The NFL injury report has never been synced: availability unknown.",
    "unmapped": "He is not matched to an NFL player id, so no NFL report can be read for him: availability unknown.",
})
PRACTICE_GAP = ("Practice participation comes from the NFL injury report via nflverse: one value per report row (the "
                "final practice listed that week), not each practice day. Daily DNP / LP / FP progression is not in "
                "any integrated feed, so it is never shown or inferred; without a listing it is unknown.")
METHOD = MappingProxyType({
    "window": f"The current week and the {HISTORY_WEEKS} before it.",
    "order": ("Events are ordered by week, then by their own time: the provider's publication time (NFL report "
              "date_modified) when it has one, else the time we retrieved it; weekly roster statuses carry a week "
              "only and come first in their week. Times are UTC and shown in US Eastern time."),
    "replacement": ("Within a week the newest NFL report row is current and older rows are superseded; rows with no "
                    "time that disagree are marked conflicting, and the current one is chosen exactly as the lineup "
                    "tools choose it (newest, then most severe). A newer week's report replaces the previous week's."),
    "headline": ("The level is the one the lineup tools read: the most severe of the provider status, an IR or taxi "
                 "slot and the week's NFL report. Without any of them the level is unknown, never healthy."),
    "third_party": ("Owner-curated news summaries cite their reports and supplement, never replace, an official or "
                    "provider status."),
    "practice": PRACTICE_GAP,
    "watchlist": "No player watchlist exists yet, so the digest covers the selected team's roster only.",
})


class AvailabilityTimelineUnavailable(Exception):
    """No synced league (or its current week is unknown): the API answers 503."""


def _iso(v) -> str | None:
    t = fresh.naive_utc(v)
    return None if t is None else t.isoformat() + "Z"


def practice_code(raw: str | None) -> str | None:
    """nflverse practice_status text -> DNP | LP | FP | OTHER, or None when absent."""
    t = (raw or "").strip().lower()
    if not t:
        return None
    if "did not" in t or t == "dnp":
        return "DNP"
    if "limited" in t or t == "lp":
        return "LP"
    if "full" in t or t == "fp":
        return "FP"
    return "OTHER"


def _official_level(status: str | None) -> str | None:
    return NFL_REPORT_STATUS.get(status) if status else None


def _provider_level(status: str | None) -> str | None:
    lvl = ESPN_STATUS.get(status.upper()) if status else None
    return None if lvl in (None, AVAILABLE) else lvl


# ----------------------------------------------------------------------------- reads (one query each)


def _league(con, team_id: int | None) -> tuple[dict, int, int, list[dict], int | None]:
    lg = latest_league(con, ["league_id", "season", "name"])
    if lg is None:
        raise AvailabilityTimelineUnavailable("ESPN league not synced yet. Run scripts/sync_espn.py.")
    season = lg["season"]
    lw = league_window(con, season)
    if lw is None:
        raise AvailabilityTimelineUnavailable("The league's current scoring period is unknown. Run scripts/sync_espn.py.")
    teams = fetch_dicts(con, "SELECT team_id, name, is_mine FROM fantasy_teams WHERE league_id = ? AND season = ? "
                             "ORDER BY team_id", [lg["league_id"], season]) if table_exists(con, "fantasy_teams") else []
    if team_id is not None and not any(t["team_id"] == team_id for t in teams):
        raise LookupError(f"Team {team_id} is not in {lg['name'] or 'this league'} ({season}).")
    mine = team_id if team_id is not None else next((t["team_id"] for t in teams if t["is_mine"]), None)
    return lg, season, lw.current, teams, mine


def _roster(con, league_id: int, season: int, week: int, team_id: int | None) -> list[dict]:
    """The team's roster for the scoring period, with the provider's current status and the player's NFL id."""
    if team_id is None or not all(table_exists(con, t) for t in ("lineup_snapshots", "fantasy_players")):
        return []
    has_map = table_exists(con, "player_id_map")
    join = ("LEFT JOIN (SELECT espn_player_id, any_value(gsis_id) AS gsis_id FROM player_id_map WHERE gsis_id IS NOT "
            "NULL AND lower(coalesce(notes, '')) NOT LIKE '%conflict%' GROUP BY 1) m "
            "ON m.espn_player_id = l.espn_player_id") if has_map else ""
    gsis = "m.gsis_id" if has_map else "NULL"
    return fetch_dicts(con, f"""
        SELECT l.espn_player_id, l.lineup_slot, l.lineup_slot_id, l.is_starter, fp.full_name, fp.position,
               fp.pro_team, fp.injury_status, coalesce(fp.detail_synced_at, fp.synced_at) AS provider_synced_at,
               {gsis} AS gsis_id
        FROM lineup_snapshots l JOIN fantasy_players fp ON fp.espn_player_id = l.espn_player_id {join}
        WHERE l.league_id = ? AND l.season = ? AND l.scoring_period = ? AND l.team_id = ?
        ORDER BY l.is_starter DESC, l.lineup_slot_id, fp.full_name""", [league_id, season, week, team_id])


def _in(n: int) -> str:
    return ", ".join("?" * n)


def _report_rows(con, season: int, weeks: tuple[int, int], gsis: list[str]) -> tuple[list[dict], dict]:
    """(nflverse report rows of these players in the window, feed facts). Times converted to naive UTC in SQL."""
    facts = {"available": False, "season_rows": 0, "week_rows": {}, "team_week_rows": {}, "with_time": 0}
    if not table_exists(con, "nfl_injuries"):
        return [], facts
    cols = set(table_columns(con, "nfl_injuries"))
    if not {"season", "week", "team", "gsis_id", "report_status"} <= cols:
        return [], facts
    gt = "AND game_type = 'REG'" if "game_type" in cols else ""
    dm = fresh._naive_utc_column(con, "nfl_injuries", "date_modified") if "date_modified" in cols else "NULL"
    pr = "practice_status" if "practice_status" in cols else "NULL"
    inj = "report_primary_injury" if "report_primary_injury" in cols else "NULL"
    pinj = "practice_primary_injury" if "practice_primary_injury" in cols else "NULL"
    n, with_time = con.execute(f"SELECT count(*), count({dm}) FROM nfl_injuries WHERE season = ? {gt}",
                               [season]).fetchone()
    facts.update(available=bool(n), season_rows=int(n or 0), with_time=int(with_time or 0))
    for team, week, cnt in con.execute(f"SELECT team, week, count(*) FROM nfl_injuries WHERE season = ? AND week "
                                       f"BETWEEN ? AND ? {gt} GROUP BY 1, 2", [season, *weeks]).fetchall():
        facts["team_week_rows"][(team, week)] = int(cnt)
        facts["week_rows"][week] = facts["week_rows"].get(week, 0) + int(cnt)
    if not gsis:
        return [], facts
    rows = fetch_dicts(con, f"""
        SELECT week, team, gsis_id, report_status, {pr} AS practice_status, {inj} AS injury,
               {pinj} AS practice_injury, {dm} AS date_modified
        FROM nfl_injuries WHERE season = ? AND week BETWEEN ? AND ? {gt} AND gsis_id IN ({_in(len(gsis))})""",
                       [season, *weeks, *gsis])
    for r in rows:
        r["date_modified"] = fresh.naive_utc(r["date_modified"])
    return rows, facts


def _roster_status(con, season: int, weeks: tuple[int, int], gsis: list[str]) -> dict[str, dict[int, str]]:
    """gsis -> {week: NFL weekly roster status}, from the week before the window (to see a change into it)."""
    if not gsis or not table_exists(con, "nfl_rosters_weekly"):
        return {}
    cols = set(table_columns(con, "nfl_rosters_weekly"))
    if not {"season", "week", "gsis_id", "status"} <= cols:
        return {}
    gt = "AND game_type = 'REG'" if "game_type" in cols else ""
    out: dict[str, dict[int, str]] = {}
    for r in fetch_dicts(con, f"""SELECT gsis_id, week, min(status) AS status FROM nfl_rosters_weekly
                                  WHERE season = ? AND week BETWEEN ? AND ? {gt} AND gsis_id IN ({_in(len(gsis))})
                                  GROUP BY 1, 2""", [season, weeks[0] - 1, weeks[1], *gsis]):
        if r["status"]:
            out.setdefault(r["gsis_id"], {})[int(r["week"])] = str(r["status"])
    return out


def _retrievals(con, season: int, weeks: tuple[int, int]) -> tuple[dict, datetime | None, dict]:
    """({(team, week): latest nflverse retrieval}, latest nflverse feed retrieval, {team: latest provider retrieval})
    from the write-once retrieval log (our clock)."""
    if not table_exists(con, fresh.TABLE):
        return {}, None, {}
    team_week = {(t, w): fresh.naive_utc(at) for t, w, at in con.execute(
        f"SELECT team, week, max(retrieved_at) FROM {fresh.TABLE} WHERE source = ? AND scope = ? AND season = ? "
        "AND week BETWEEN ? AND ? GROUP BY 1, 2", [fresh.SOURCE_NFLVERSE, fresh.SCOPE_TEAM_WEEK, season, *weeks]
    ).fetchall()}
    feed = con.execute(f"SELECT max(retrieved_at) FROM {fresh.TABLE} WHERE source = ? AND scope = ? AND season = ?",
                       [fresh.SOURCE_NFLVERSE, fresh.SCOPE_FEED, season]).fetchone()[0]
    provider = {t: fresh.naive_utc(at) for t, at in con.execute(
        f"SELECT team, max(retrieved_at) FROM {fresh.TABLE} WHERE source = ? AND scope = ? AND season = ? GROUP BY 1",
        [fresh.SOURCE_ESPN, fresh.SCOPE_TEAM_CURRENT, season]).fetchall()}
    return team_week, fresh.naive_utc(feed), provider


# ----------------------------------------------------------------------------- events


def _order_key(r: dict):
    """Newest report row first, exactly as availability.injury_order_sql orders a week (the lineup tools' choice)."""
    t = r["date_modified"]
    sev = {"Out": 3, "Doubtful": 2, "Questionable": 1}.get(r["report_status"], 0)
    return (t is None, -(t.timestamp() if t else 0), -sev, r["practice_status"] is None, r["practice_status"] or "",
            r["injury"] is None, r["injury"] or "")


def _official_events(rows: list[dict], gsis: str, *, kickoffs: dict, retrieved: dict, current: int) -> list[dict]:
    """One event per report row; relations say which row is current and which ones it replaced."""
    by_week: dict[int, list[dict]] = {}
    for r in rows:
        by_week.setdefault(int(r["week"]), []).append(r)
    events, tops = [], {}
    for week in sorted(by_week):
        ranked = sorted(by_week[week], key=_order_key)
        top = ranked[0]
        for i, r in enumerate(ranked):
            eid = f"nfl-{week}-{gsis}-{i}"
            if i == 0:
                relation, replaced_by = ("current" if week == current else "latest_for_week"), None
                tops[week] = eid
            elif r["date_modified"] is not None and top["date_modified"] is not None \
                    and r["date_modified"] < top["date_modified"]:
                relation, replaced_by = "superseded", tops[week]
            elif (r["report_status"], practice_code(r["practice_status"])) != \
                    (top["report_status"], practice_code(top["practice_status"])):
                relation, replaced_by = "conflicting", None
            else:
                relation, replaced_by = "duplicate", tops[week]
            ko = fresh.naive_utc(kickoffs.get((r["team"], week)))
            mod = r["date_modified"]
            code = practice_code(r["practice_status"])
            status = r["report_status"]
            parts = [f"Game status: {status}" if status else "No game status"]
            if code:
                parts.append(f"practice: {PRACTICE_LABEL.get(code, r['practice_status'])}"
                             + (f" ({code})" if code in PRACTICE_LABEL else ""))
            else:
                parts.append("practice: not reported")
            injury = r["injury"] or r["practice_injury"]
            if injury:
                parts.append(f"injury: {injury}")
            events.append({
                "id": eid, "kind": "official_report", "week": week,
                "at": _iso(mod or retrieved.get((r["team"], week))),
                "at_basis": "provider_published" if mod else ("retrieved" if retrieved.get((r["team"], week))
                                                               else "week_only"),
                "published_at": _iso(mod), "retrieved_at": _iso(retrieved.get((r["team"], week))), "date": None,
                "source": {"type": "official", "label": "NFL injury report (via nflverse)",
                           "url": NFLVERSE_INJURIES_URL, "urls": []},
                "title": f"Week {week} NFL injury report", "detail": "; ".join(parts) + ".",
                "status": status, "practice": {"code": code, "raw": r["practice_status"]}, "injury": injury,
                "team": r["team"], "relation": relation, "replaced_by": replaced_by,
                "after_kickoff": None if mod is None or ko is None else mod >= ko,
            })
    # A newer week's report replaces the latest row of every earlier week.
    weeks = sorted(tops)
    for e in events:
        if e["relation"] == "latest_for_week":
            later = [w for w in weeks if w > e["week"]]
            e["relation"] = "earlier_week"
            e["replaced_by"] = tops[later[0]] if later else None
    return events


def _roster_events(statuses: dict[int, str], gsis: str, window: tuple[int, int]) -> list[dict]:
    """A weekly roster status change (or a non-active status with nothing before it) inside the window."""
    out = []
    for week in range(window[0], window[1] + 1):
        st = statuses.get(week)
        if st is None:
            continue
        prev = statuses.get(week - 1)
        if prev == st or (prev is None and st == "ACT"):
            continue
        back = prev in RESERVE and st == "ACT"
        label = ROSTER_LABEL.get(st, st)
        detail = (f"{ROSTER_LABEL.get(prev, prev)} ({prev}) to {label} ({st})" if prev else f"{label} ({st})")
        out.append({
            "id": f"roster-{week}-{gsis}", "kind": "roster_status", "week": week, "at": None, "at_basis": "week_only",
            "published_at": None, "retrieved_at": None, "date": None,
            "source": {"type": "official", "label": "NFL weekly roster (via nflverse)", "url": NFLVERSE_ROSTERS_URL,
                       "urls": []},
            "title": "Back on the active roster" if back else f"Week {week} roster status: {label}",
            "detail": detail + ". The weekly roster carries a week, not a time.",
            "status": st, "practice": None, "injury": None, "team": None,
            "relation": "current" if week == window[1] else "earlier_week", "replaced_by": None,
            "after_kickoff": None, "return_to_play": back,
        })
    return out


def _news_event(e: news.Entry, gsis: str) -> dict:
    if e.kind == "active":
        what = f"Reported eligible to play from week {e.from_week}"
    elif e.return_window:
        what = f"Reported out from week {e.from_week}, return reported for weeks {e.return_window[0]}-" \
               f"{e.return_window[1]}"
    else:
        what = f"Reported out from week {e.from_week} for the rest of the season"
    try:
        day = date.fromisoformat(e.reported_on).isoformat()
    except ValueError:
        day = None
    return {
        "id": f"news-{e.season}-{gsis}", "kind": "third_party", "week": e.from_week,
        "at": None, "at_basis": "reported_date" if day else "week_only", "published_at": None, "retrieved_at": None,
        "date": day,
        "source": {"type": "third_party", "label": "Owner-curated news summary", "url": None, "urls": list(e.sources)},
        "title": what, "detail": e.note, "status": e.kind, "practice": None, "injury": None, "team": e.team,
        "relation": "supplements", "replaced_by": None, "after_kickoff": None,
    }


def _sort_key(e: dict):
    t = fresh.naive_utc(e["at"])
    if t is None and e["date"]:
        t = datetime.fromisoformat(e["date"])
    return (e["week"], t or datetime.min, KIND_RANK[e["kind"]], e["id"])


# ----------------------------------------------------------------------------- payload


def build_availability_timeline(con, *, team_id: int | None = None, now: datetime | None = None,
                                news_path: Path | None = None) -> dict:
    """The availability timeline and weekly digest for one team's roster. LookupError for a team_id outside the
    league (404); AvailabilityTimelineUnavailable when the league or its current week is unknown (503)."""
    lg, season, week, teams, mine = _league(con, team_id)
    now = fresh.naive_utc(now) or utcnow()
    window = (max(1, week - HISTORY_WEEKS), week)
    roster = _roster(con, lg["league_id"], season, week, mine)
    defenses = [p for p in roster if (p["position"] or "") in ("D/ST", "DST", "DEF")]
    players = [p for p in roster if p not in defenses]
    gsis_ids = sorted({p["gsis_id"] for p in players if p["gsis_id"]})

    rows, facts = _report_rows(con, season, window, gsis_ids)
    statuses = _roster_status(con, season, window, gsis_ids)
    team_week_ret, feed_ret, provider_ret = _retrievals(con, season, window)
    kickoffs: dict[tuple[str, int], object] = {}
    for w in range(window[0], window[1] + 1):
        for t, ko in fresh.scheduled_kickoffs(con, season, w).items():
            kickoffs[(t, w)] = ko
    games = {g["team"]: g for g in fresh.scheduled_games(con, season, week)}
    covered = bool(games)
    diag = fresh.diagnostics(con, season, week, now)
    fresh_of = {t["team"]: t for t in diag["teams"]}
    entries = [e for e in news.load(news_path) if e.season == season]

    rows_of: dict[str, list[dict]] = {}
    for r in rows:
        rows_of.setdefault(r["gsis_id"], []).append(r)

    out = []
    for p in players:
        g = p["gsis_id"]
        team = p["pro_team"]
        events = _official_events(rows_of.get(g, []), g, kickoffs=kickoffs, retrieved=team_week_ret,
                                  current=week) if g else []
        cur = next((e for e in events if e["relation"] == "current"), None)
        conflict = any(e["relation"] == "conflicting" and e["week"] == week for e in events)
        if g:
            events += _roster_events(statuses.get(g, {}), g, window)
        for e in entries:
            if (g and e.gsis_id == g) or (e.espn_player_id is not None and e.espn_player_id == p["espn_player_id"]):
                events.append(_news_event(e, g or str(p["espn_player_id"])))
        prov_raw = p["injury_status"]
        prov_at = fresh.naive_utc(p["provider_synced_at"]) or provider_ret.get(team)
        if prov_raw:
            events.append({
                "id": f"provider-{p['espn_player_id']}", "kind": "provider_status", "week": week, "at": _iso(prov_at),
                "at_basis": "retrieved" if prov_at else "week_only", "published_at": None,
                "retrieved_at": _iso(prov_at), "date": None,
                "source": {"type": "provider", "label": "ESPN player status", "url": None, "urls": []},
                "title": f"ESPN status: {PROVIDER_LABEL.get(prov_raw.upper(), prov_raw)}",
                "detail": "ESPN publishes no time for a status; the time shown is when it was synced.",
                "status": prov_raw, "practice": None, "injury": None, "team": team, "relation": "current",
                "replaced_by": None, "after_kickoff": None,
            })
        gm = games.get(team) if team else None
        ko = fresh.naive_utc(gm["kickoff"]) if gm else None
        if gm and ko is not None:
            events.append({
                "id": f"kickoff-{week}-{team}", "kind": "kickoff", "week": week, "at": _iso(ko), "at_basis": "scheduled",
                "published_at": None, "retrieved_at": None, "date": None,
                "source": {"type": "schedule", "label": "NFL schedule (via nflverse)", "url": NFLVERSE_SCHEDULES_URL,
                           "urls": []},
                "title": f"Kickoff vs {gm['opponent']}", "detail": "Reports modified at or after kickoff are marked late.",
                "status": None, "practice": None, "injury": None, "team": team, "relation": "context",
                "replaced_by": None, "after_kickoff": None,
            })
        events.sort(key=_sort_key)

        # Coverage of this week's official report for him.
        if cur is not None:
            coverage = "listed"
        elif not facts["available"]:
            coverage = "no_feed"
        elif not g:
            coverage = "unmapped"
        elif not facts["week_rows"].get(week):
            coverage = "not_published"
        elif team and facts["team_week_rows"].get((team, week)):
            coverage = "not_listed"
        else:
            coverage = "no_team_report"

        av = availability(prov_raw, p["lineup_slot_id"], cur["status"] if cur else None, taxi=is_taxi(p))
        level = DESIGNATED.get(av["status"]) or ("listed" if cur is not None else "unknown")
        off_lvl, prov_lvl = _official_level(cur["status"] if cur else None), _provider_level(prov_raw)
        if level == "unknown":
            text = UNKNOWN_TEXT[coverage].format(w=week)
            if not prov_raw or prov_raw.upper() in ("ACTIVE", "NORMAL"):
                text += " ESPN lists no designation either."
        elif level == "listed":
            text = f"On the week {week} NFL injury report without a game status."
        else:
            text = f"{LEVEL_LABEL[level]}: " + ", ".join(_source_words(x) for x in av["sources"]) + "."
        disagreement = None
        if off_lvl and prov_raw and (prov_lvl or AVAILABLE) != off_lvl:
            disagreement = (f"Sources disagree: the NFL report says {cur['status']}, ESPN says "
                            f"{PROVIDER_LABEL.get(prov_raw.upper(), prov_raw)}. The lineup tools read the more severe "
                            f"({LEVEL_LABEL[level]}).")

        code = practice_code(cur["practice"]["raw"]) if cur else None
        practice = ({"code": code, "label": PRACTICE_LABEL.get(code, cur["practice"]["raw"]), "raw": cur["practice"]["raw"],
                     "scope": "final_report", "reason": None} if code else
                    {"code": "unknown", "label": "Unknown", "raw": None, "scope": None,
                     "reason": "not_in_listing" if cur is not None else coverage})

        # Last week's current row (the top row of week - 1) against this week's.
        prev = next((e for e in events if e["kind"] == "official_report" and e["week"] == week - 1
                     and e["relation"] == "earlier_week"), None)
        change = None
        if cur and prev is None:
            change = {"kind": "new_listing", "from": None, "to": cur["status"] or "Listed"}
        elif cur and prev and (prev["status"], prev["practice"]["code"]) != (cur["status"], cur["practice"]["code"]):
            change = {"kind": "status_change", "from": _report_label(prev), "to": _report_label(cur)}
        elif cur is None and prev is not None and coverage == "not_listed":
            change = {"kind": "dropped_off_report", "from": _report_label(prev), "to": None}
        this_week = [e for e in events if e["week"] == week and e["kind"] in ("official_report", "roster_status")]
        high_signal = bool(change) or any(e.get("return_to_play") or e["status"] in RESERVE
                                          for e in this_week if e["kind"] == "roster_status") or \
            (level in ("questionable", "doubtful", "out") and cur is None)    # a provider designation, no report yet

        tf = fresh_of.get(team) if team else None
        started = bool(ko and now >= ko)
        out.append({
            "espn_player_id": p["espn_player_id"], "gsis_id": g, "full_name": p["full_name"],
            "position": p["position"], "team": team, "lineup_slot": p["lineup_slot"],
            "is_starter": bool(p["is_starter"]),
            "headline": {"level": level, "label": LEVEL_LABEL[level], "text": text, "sources": av["sources"],
                         "coverage": coverage, "disagreement": disagreement, "conflicting_reports": conflict},
            "lineup": {"status": av["status"], "sources": av["sources"]},
            "practice": practice,
            "change": change, "high_signal": high_signal,
            "game": {"week": week, "opponent": gm["opponent"] if gm else None, "kickoff": _iso(ko),
                     "started": started, "on_bye": None if not covered else gm is None},
            "freshness": None if tf is None else {"status": tf["status"], "reason": tf["reason"],
                                                  "source_used": tf["source_used"],
                                                  "evidence_retrieved_at": _iso(tf["evidence_retrieved_at"])},
            "events": events,
        })

    out.sort(key=lambda x: (LEVEL_RANK[x["headline"]["level"]], not x["high_signal"], not x["is_starter"],
                            x["full_name"] or ""))
    team_row = next((t for t in teams if t["team_id"] == mine), None)
    counts = {
        "players": len(out), "team_defenses_skipped": len(defenses),
        "designated": sum(1 for x in out if x["headline"]["level"] not in ("listed", "unknown")),
        "listed": sum(1 for x in out if x["headline"]["coverage"] == "listed"),
        "unknown": sum(1 for x in out if x["headline"]["level"] == "unknown"),
        "high_signal": sum(1 for x in out if x["high_signal"]),
        "practice_known": sum(1 for x in out if x["practice"]["code"] != "unknown"),
        "conflicts": sum(1 for x in out if x["headline"]["conflicting_reports"]),
        "disagreements": sum(1 for x in out if x["headline"]["disagreement"]),
    }
    prov_times = [fresh.naive_utc(p["provider_synced_at"]) for p in roster if p["provider_synced_at"]]
    return {
        "version": VERSION, "season": season, "week": week, "generated_at": now.isoformat() + "Z",
        "window": {"from_week": window[0], "to_week": window[1]},
        "league_context": {"available": bool(teams), "league_name": lg["name"], "my_team_id": mine,
                           "team_name": team_row["name"] if team_row else None,
                           "perspective": "query" if team_id is not None else "owner_default"},
        "sources": _sources(facts, feed_ret, max(prov_times, default=None), entries, covered),
        "practice": {"daily_available": False, "granularity": "final_report", "text": PRACTICE_GAP},
        "method": dict(METHOD),
        "counts": counts,
        "notices": _notices(facts, week, out, mine, roster, covered),
        "players": out,
    }


def _source_words(src: str) -> str:
    """'ESPN INJURY_RESERVE' -> 'ESPN status Injured reserve'; other availability sources read as words already."""
    head, _, code = src.partition(" ")
    if head == "ESPN" and code.upper() in PROVIDER_LABEL:
        return f"ESPN status {PROVIDER_LABEL[code.upper()]}"
    return src


def _report_label(e: dict) -> str:
    code = e["practice"]["code"]
    return " / ".join(x for x in (e["status"] or "No game status", code) if x)


def _sources(facts: dict, feed_ret, provider_at, entries: list, covered: bool) -> list[dict]:
    rows, timed = facts["season_rows"], facts["with_time"]
    return [
        {"key": "nfl_report", "type": "official", "label": "NFL injury report (via nflverse)", "url": NFLVERSE_INJURIES_URL,
         "available": facts["available"], "retrieved_at": _iso(feed_ret),
         "publication_time": "missing" if not rows else "available" if timed == rows else "partial" if timed else "missing",
         "text": "Game status, final practice participation and injury per player-week, with the provider's "
                 "date_modified when published. Practice is one value per report row, not each practice day."},
        {"key": "nfl_roster", "type": "official", "label": "NFL weekly roster (via nflverse)", "url": NFLVERSE_ROSTERS_URL,
         "available": None, "retrieved_at": None, "publication_time": "missing",
         "text": "Active, reserve, suspended and similar roster statuses per week; a week, never a time."},
        {"key": "provider", "type": "provider", "label": "ESPN player status", "url": None,
         "available": provider_at is not None, "retrieved_at": _iso(provider_at), "publication_time": "missing",
         "text": "The league provider's current status. ESPN publishes no time; the time is when it was synced."},
        {"key": "news", "type": "third_party", "label": "Owner-curated news summaries", "url": None,
         "available": bool(entries), "retrieved_at": None, "publication_time": "date_only",
         "text": "Reviewed summaries of public reports, each citing them. Interpretation, never a designation."},
        {"key": "schedule", "type": "schedule", "label": "NFL schedule (via nflverse)", "url": NFLVERSE_SCHEDULES_URL,
         "available": covered, "retrieved_at": None, "publication_time": "scheduled",
         "text": "This week's kickoffs, to mark reports modified after kickoff."},
    ]


def _notices(facts: dict, week: int, players: list[dict], mine: int | None, roster: list, covered: bool) -> list[dict]:
    out = []
    if mine is None:
        out.append({"tone": "info", "code": "no_team",
                    "text": "No team is selected and the league has no default team, so there is no roster to "
                            "follow. Pick a team."})
    elif not roster:
        out.append({"tone": "info", "code": "no_roster",
                    "text": f"No roster is synced for this team in week {week}, so there is nothing to follow yet."})
    if not facts["available"]:
        out.append({"tone": "caution", "code": "no_nfl_report",
                    "text": "The NFL injury report has never been synced: official designations and practice "
                            "participation are unknown for every player."})
    elif not facts["week_rows"].get(week):
        out.append({"tone": "caution", "code": "report_not_published",
                    "text": f"No week {week} NFL injury report rows are synced yet. Players without a provider "
                            "designation are unknown until it is."})
    if any(x["headline"]["conflicting_reports"] for x in players):
        out.append({"tone": "caution", "code": "conflicting_reports",
                    "text": "Some players have NFL report rows this week that disagree and carry no time to order "
                            "them. Both are shown; the current one is chosen as the lineup tools choose it."})
    if any(e["after_kickoff"] for x in players for e in x["events"]):
        out.append({"tone": "info", "code": "after_kickoff",
                    "text": "Some report rows were modified at or after their game's kickoff; they are marked late."})
    if not covered:
        out.append({"tone": "info", "code": "no_schedule",
                    "text": "The NFL schedule for this week is not synced, so kickoffs are unknown."})
    out.append({"tone": "info", "code": "practice_granularity", "text": PRACTICE_GAP})
    return out


__all__ = ["VERSION", "HISTORY_WEEKS", "AvailabilityTimelineUnavailable", "build_availability_timeline",
           "practice_code"]
