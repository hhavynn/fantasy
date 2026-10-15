"""NFL availability for the lineup engine, kept separate from ESPN injury designations and from
fantasy IR slots. Read-only. See docs/v3_trade_intelligence.md ("Injury-aware valuation").

Four different things are tracked independently and never merged:

  * ESPN injury designation (fantasy_players.injury_status: ACTIVE, QUESTIONABLE, OUT,
    INJURY_RESERVE, ...), with its sync time;
  * NFL reserve-list designation (nflverse weekly rosters: `status` RES plus the GSIS
    `status_description_abbr` code), with the latest roster week it was seen in;
  * fantasy IR slot placement (the ESPN lineup slot; RosterPlayer.on_ir);
  * the earliest NFL-eligible week, derived only where a rule makes it certain.

The one rule used: a player placed on Reserve/Injured, Reserve/PUP or Reserve/NFI in season must
miss at least MIN_GAMES of his team's games before he may return. The stint start is the first
weekly roster in the current run that shows him on reserve; that week's game is counted as missed,
and a week with no roster row is assumed possibly on reserve, so the derived earliest week is never
later than the true one (the claimed ineligible weeks are a lower bound). With the team's schedule
the earliest eligible week is the week of its (MIN_GAMES + 1)-th game from the stint start;
without it, MIN_GAMES weeks after the start (a bye only delays a return).

Before that week the player is NFL-ineligible: he cannot start, whatever ESPN projects. From that
week on nothing is assumed: an eligible week is not a return, and ESPN's projection is used as
published. There is no expected-return source in the synced data, so `expected_return_week` is
always None, and no probability is attached to any return scenario.
"""

from __future__ import annotations

from ..db.connection import fetch_dicts, table_columns, table_exists

MIN_GAMES = 4
# GSIS roster status codes (nflverse `status_description_abbr`) that carry the minimum-games rule.
# Mapping from the NFL's published GSIS codes; it needs confirming against real synced rosters.
RULE_CODES = {
    "R01": ("INJURED_RESERVE", "Reserve/Injured"),
    "R48": ("INJURED_RESERVE", "Reserve/Injured (designated for return)"),
    "R04": ("PUP", "Reserve/Physically Unable to Perform"),
    "R05": ("NFI", "Reserve/Non-Football Injury"),
}
RESERVE_STATUSES = {"RES", "PUP", "NFI"}
NOT_ON_RESERVE = {"ACT"}
ESPN_RESERVE = {"INJURY_RESERVE"}

AVAILABLE = "available"
INELIGIBLE = "nfl_ineligible"
ELIGIBLE_UNCONFIRMED = "eligible_return_unconfirmed"
NO_WEEK_LEFT = "no_eligible_week_remaining"
UNKNOWN = "reserve_eligibility_unknown"
RETURNED = "returned_from_reserve"
RESTRICTED = (INELIGIBLE, ELIGIBLE_UNCONFIRMED, NO_WEEK_LEFT, UNKNOWN)

NOTE = ("NFL eligibility, ESPN injury designation and fantasy IR placement are separate. A verified NFL-ineligible "
        "week contributes nothing (ESPN's projection is kept and shown, never counted and never treated as 0). An "
        "earliest eligible week is a minimum, not a return date; after it ESPN's projection is used as published "
        "and no health or role is assumed.")


def _is_reserve(row: dict) -> bool:
    return (row.get("status") or "").upper() in RESERVE_STATUSES or (row.get("abbr") or "") in RULE_CODES


def _weeks(ws) -> str:
    ws = sorted(ws)
    if not ws:
        return ""
    if ws == list(range(ws[0], ws[-1] + 1)) and len(ws) > 1:
        return f"weeks {ws[0]}-{ws[-1]}"
    return ("week " if len(ws) == 1 else "weeks ") + ", ".join(map(str, ws))


def derive(*, espn_status: str | None, on_ir: bool, rows: list[dict], team_games: list[int] | None,
           weeks: tuple[int, ...] | list[int], proj: dict[int, float] | None = None,
           espn_synced_at=None, latest_roster_week: int | None = None) -> dict:
    """One player's availability record. `rows`: his nflverse REG weekly roster rows this season
    ({week, status, abbr, team}); `team_games`: REG weeks his NFL team plays (None = schedule unknown);
    `weeks`: the fantasy weeks being evaluated; `proj`: his ESPN weekly projections (for reporting)."""
    rows = sorted(rows, key=lambda x: x["week"])
    rec = {"espn_injury_status": espn_status, "espn_status_synced_at": str(espn_synced_at) if espn_synced_at else None,
           "fantasy_ir_slot": bool(on_ir), "nfl_designation": None, "nfl_designation_label": None,
           "nfl_designation_source": None, "nfl_latest_roster_week": rows[-1]["week"] if rows else None,
           "nfl_rosters_latest_week": latest_roster_week,
           "designation_since_week": None, "earliest_eligible_week": None, "eligibility_basis": None,
           "expected_return_week": None, "expected_return_source": "none synced (no reliable return-date source)",
           "return_confirmed": False, "status": AVAILABLE, "unavailable_weeks": [], "ignored_projection": [],
           "warnings": []}
    latest = rows[-1] if rows else None
    espn_ir = (espn_status or "").upper() in ESPN_RESERVE
    if latest is not None and _is_reserve(latest):
        # Trailing reserve stint; a week with no row may also have been on reserve (earliest possible start).
        start = 1                       # every row on reserve: the stint may have started at week 1
        for prev in reversed(rows[:-1]):
            if not _is_reserve(prev):
                start = prev["week"] + 1    # the week after his last active row (a gap counts as reserve)
                break
        code = latest.get("abbr")
        kind, label = RULE_CODES.get(code, ("RESERVE_OTHER", f"reserve list ({code or 'type not reported'})"))
        rec.update(nfl_designation=kind, nfl_designation_label=label, designation_since_week=start,
                   nfl_designation_source=f"nflverse weekly roster, week {latest['week']} "
                                          f"(status {latest.get('status')}, code {code or 'none'})")
        if kind == "RESERVE_OTHER":
            rec["status"] = UNKNOWN
            rec["warnings"].append(f"On an NFL reserve list ({label}); no minimum-games rule is applied to this type, so "
                                   "no week is treated as ineligible and the earliest return is unknown.")
        else:
            if team_games is not None:
                later = sorted(w for w in team_games if w >= start)
                if len(later) > MIN_GAMES:
                    earliest = later[MIN_GAMES]
                    basis = (f"{label} since week {start}: must miss {MIN_GAMES} team games "
                             f"({', '.join(map(str, later[:MIN_GAMES]))}); first eligible game week {earliest}")
                else:
                    earliest = None
                    basis = f"{label} since week {start}: fewer than {MIN_GAMES + 1} team games remain this regular season"
            else:
                earliest = start + MIN_GAMES
                basis = (f"{label} since week {start}: at least {MIN_GAMES} games, so no earlier than week {earliest} "
                         "(team schedule unavailable; a bye could delay this)")
            blocked = [w for w in weeks if earliest is None or w < earliest]
            rec.update(earliest_eligible_week=earliest, eligibility_basis=basis, unavailable_weeks=blocked)
            if earliest is None:
                rec["status"] = NO_WEEK_LEFT
                rec["warnings"].append(f"Currently on NFL {label.lower()}; under the minimum-games rule no eligible "
                                       "week remains in the regular season. No lineup contribution is counted.")
            elif blocked:
                rec["status"] = INELIGIBLE
                rec["warnings"].append(f"Currently on NFL {label.lower()}. Earliest eligible return week {earliest}; actual "
                                       f"return not confirmed. Near-term lineup contribution ({_weeks(blocked)}) is "
                                       "unavailable.")
            else:
                rec["status"] = ELIGIBLE_UNCONFIRMED
                rec["warnings"].append(f"On NFL {label.lower()} as of the week-{latest['week']} roster; eligible to "
                                       f"return from week {earliest}, but no activation is confirmed. ESPN's projection "
                                       "is used as published; his return is not established.")
    elif latest is not None and any(_is_reserve(x) for x in rows):
        back = next(x["week"] for x in reversed(rows) if not _is_reserve(x))
        rec.update(return_confirmed=True, status=RETURNED,
                   nfl_designation_source=f"nflverse weekly roster, week {latest['week']} (status {latest.get('status')})")
        rec["warnings"].append(f"Activated from an NFL reserve list (on the active roster from the week-{back} roster).")
    if espn_ir and rec["nfl_designation"] is None:
        rec["nfl_designation"] = "INJURED_RESERVE"
        rec["nfl_designation_label"] = "Injured reserve (per ESPN)"
        rec["nfl_designation_source"] = "ESPN injury status INJURY_RESERVE" + \
            (f" (synced {espn_synced_at})" if espn_synced_at else "")
        if rec["status"] == RETURNED:
            rec["warnings"].append("ESPN still lists INJURY_RESERVE although the NFL roster shows him active; the sources "
                                   "disagree, so no week is treated as ineligible.")
        else:
            rec["status"] = UNKNOWN
            why = (f"the latest NFL roster with him (week {latest['week']}) does not show a reserve designation"
                   if latest is not None else "no NFL roster record gives the placement week")
            rec["warnings"].append(f"ESPN lists him on injured reserve, but {why}, so the earliest eligible week is "
                                   "unknown. No week is treated as ineligible; projected gains that depend on him are "
                                   "not established.")
    if rec["nfl_designation"] and latest_roster_week is not None and weeks and weeks[0] - latest_roster_week > 2:
        rec["warnings"].append(f"NFL roster data was last synced for week {latest_roster_week}; a later activation or "
                               "placement would not be reflected.")
    if on_ir and rec["status"] in (AVAILABLE, RETURNED):
        rec["warnings"].append("In a fantasy IR slot but not NFL-ineligible: the fantasy slot is held as synced (the "
                               "evaluator's IR assumption); activation upside is shown separately.")
    if proj:
        rec["ignored_projection"] = [{"week": w, "espn_projection": proj.get(w)} for w in rec["unavailable_weeks"]
                                     if proj.get(w) is not None]
        stale = [x for x in rec["ignored_projection"] if x["espn_projection"] > 0]
        if stale:
            rec["warnings"].append(f"ESPN still projects {sum(x['espn_projection'] for x in stale):.1f} points in "
                                   f"{_weeks([x['week'] for x in stale])} while he is NFL-ineligible; not counted.")
    return rec


def is_restricted(rec: dict | None) -> bool:
    return bool(rec) and rec["status"] in RESTRICTED


def load_availability(con, season: int, players: list, weeks: tuple[int, ...] | list[int],
                      proj: dict[int, dict[int, float]]) -> dict[int, dict]:
    """Availability records for `players` (RosterPlayer). Missing tables degrade to ESPN-only records."""
    ids = sorted({p.espn_player_id for p in players})
    if not ids:
        return {}
    ph = ", ".join("?" * len(ids))
    synced = {r["espn_player_id"]: r["detail_synced_at"] for r in fetch_dicts(
        con, f"SELECT espn_player_id, detail_synced_at FROM fantasy_players WHERE espn_player_id IN ({ph})", ids)} \
        if "detail_synced_at" in set(table_columns(con, "fantasy_players")) else {}
    rows_by: dict[int, list[dict]] = {}
    latest_week = None
    games: dict[str, list[int]] | None = None
    if table_exists(con, "nfl_rosters_weekly"):
        cols = set(table_columns(con, "nfl_rosters_weekly"))
        if {"week", "status"} <= cols:
            abbr = "r.status_description_abbr" if "status_description_abbr" in cols else "NULL"
            team = "r.team" if "team" in cols else "NULL"
            reg = " AND (r.game_type = 'REG' OR r.game_type IS NULL)" if "game_type" in cols else ""
            via_map = table_exists(con, "player_id_map") and "gsis_id" in cols
            espn_col = "espn_id" in cols
            join = ("LEFT JOIN player_id_map m ON m.gsis_id = r.gsis_id" if via_map else "")
            pid = ("coalesce(m.espn_player_id, TRY_CAST(r.espn_id AS BIGINT))" if via_map and espn_col else
                   "m.espn_player_id" if via_map else "TRY_CAST(r.espn_id AS BIGINT)" if espn_col else None)
            if pid:
                for r in fetch_dicts(con, f"""
                        SELECT {pid} AS espn_player_id, r.week, r.status, {abbr} AS abbr, {team} AS team
                        FROM nfl_rosters_weekly r {join}
                        WHERE r.season = ?{reg} AND {pid} IN ({ph})
                        ORDER BY 1, r.week""", [season, *ids]):
                    rows_by.setdefault(int(r["espn_player_id"]), []).append(r)
            lw = fetch_dicts(con, f"SELECT max(week) AS w FROM nfl_rosters_weekly r WHERE season = ?{reg}", [season])
            latest_week = lw[0]["w"] if lw else None
    if table_exists(con, "nfl_schedules"):
        cols = set(table_columns(con, "nfl_schedules"))
        if {"home_team", "away_team", "week"} <= cols:
            games = {}
            reg = " AND game_type = 'REG'" if "game_type" in cols else ""
            for g in fetch_dicts(con, f"SELECT week, home_team, away_team FROM nfl_schedules WHERE season = ?{reg}",
                                 [season]):
                for t in (g["home_team"], g["away_team"]):
                    games.setdefault(t, []).append(int(g["week"]))
    out = {}
    for p in players:
        rows = rows_by.get(p.espn_player_id, [])
        team = (rows[-1].get("team") if rows else None) or p.pro_team
        out[p.espn_player_id] = derive(
            espn_status=p.injury_status, on_ir=p.on_ir, rows=rows,
            team_games=(games.get(team) if games is not None and team in games else None),
            weeks=weeks, proj=proj.get(p.espn_player_id), espn_synced_at=synced.get(p.espn_player_id),
            latest_roster_week=latest_week)
    return out
