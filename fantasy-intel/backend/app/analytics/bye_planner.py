"""Roster-wide bye and lineup-coverage planner (issue #177), read-only (contract bye-planner.v1).

For each remaining fantasy week through the final scoring period (the fantasy playoff horizon): can this team fill
every starting slot from its own roster? Computed on request from the synced league file (rosters, roster slots) and
the NFL schedule and weekly rosters; nothing is written, synced or fetched, and no projection is read.

Each rostered player-week has exactly one state, from one of three kinds of evidence:

  * schedule (a fact): the player's NFL team plays that week, or has its bye. A missing game counts as a bye only when
    the team's regular-season schedule is complete (player_availability.complete_schedules: every week but one);
  * availability (verified): a week in the player's `unavailable_weeks` (NFL reserve list, minimum-games rule not met
    yet; analytics/player_availability.py) is out;
  * unknown: no NFL team, no schedule for the week or the team, an NFL reserve designation with no rule, a return that
    is allowed but not confirmed, or an open injury designation of unknown length. An unknown player is never counted
    as able or as unable: he only makes the answer unconfirmed.

Players in an IR or taxi slot are never coverage (analytics/reserve_slots.py), and nothing here forecasts depth that is
not on the roster today.

Coverage is a maximum bipartite matching of players to the league's starting slot instances, with the eligibility the
lineup engine uses (analytics/lineup.eligible), found by augmenting paths in two phases:

  1. a maximum matching that uses only players who certainly can play (every free slot, in lineup order, gets one
     augmenting-path search: a free slot with no augmenting path never gains one later, so one pass is maximum);
  2. the same matching, augmented with unknown players allowed as well.

An augmenting path keeps every vertex that was already matched matched (only its two free endpoints are new), so the
certain players matched in phase 1 all stay matched: the result fills as many slots as any matching can and, among
those, uses as many certain players as any matching can (no matching uses more certain players than a maximum
certain-only matching). Cost: O(slots x edges) per distinct week, never exponential; weeks with the same player states
share one solution. Leagues with more than MAX_STARTING_SLOTS starting slots plan the first ones and report the rest
as not evaluated. A slot is then

  covered     filled by a player who can play;
  unknown     fillable only by an unknown player;
  uncovered   no rostered player can fill it, whatever the unknowns turn out to be.

A week is `short` when a slot is uncovered (a fact about the roster), `unconfirmed` when nothing is uncovered but a slot
is unknown or a slot label cannot be resolved to positions, and `covered` only when every slot is covered. Unknown
evidence therefore never yields `covered` or `short` on its own.
"""

from __future__ import annotations

from dataclasses import replace

from ..db.connection import fetch_dicts, table_columns, table_exists
from ..espn.constants import IR_SLOT, NON_STARTER_SLOTS
from .lineup import FIXED_SLOTS, FLEX_SLOTS, eligible, lineup_slots, parse_eligible_slots, slot_positions
from .player_availability import (
    ELIGIBLE_UNCONFIRMED,
    INELIGIBLE,
    UNKNOWN,
    complete_schedules,
    load_availability,
    roster_weeks,
)
from .reserve_slots import INACTIVE_ROSTER_SLOTS, is_taxi
from .schedule_intelligence import NotFound, NotSynced, _json, _phase, league_info
from .team_lineups import RosterPlayer, lineup_relevant
from .trade_data import evaluation_start_week, kickoff_utc

CONTRACT = "bye-planner.v1"
MAX_STARTING_SLOTS = 30     # more starting slots than any real league; the rest are reported, not solved
BYE_CLUSTER_MIN = 2          # two or more of the team's active, lineup-eligible players on bye in the same week

AVAILABLE, BYE, OUT, UNSURE = "available", "bye", "out", "unknown"
# Where each state's evidence comes from.
STATE_SOURCE = {AVAILABLE: "schedule", BYE: "schedule", OUT: "availability", UNSURE: "unknown"}
# ESPN (and Sleeper, mapped to ESPN's words) designations whose length nobody publishes: the player may miss any
# remaining week, so each week he is unknown rather than able. QUESTIONABLE is a game-day call, not a future absence.
OPEN_DESIGNATIONS = {"OUT", "DOUBTFUL", "INJURY_RESERVE", "IR", "SUSPENSION", "SUSPENDED"}
NON_STARTING = {"BE", *INACTIVE_ROSTER_SLOTS}

STATUS_TEXT = {"covered": "Covered", "short": "Short", "unconfirmed": "Not confirmed"}

SOURCES = {
    "schedule": "Exact: the NFL schedule lists the team's game, or its bye, for the week (a bye only when the team's "
                "whole regular-season schedule is loaded).",
    "availability": "Verified: the NFL weekly roster puts the player on a reserve list whose minimum-games rule has not "
                    "been met by that week.",
    "unknown": "Not established: no NFL team, no schedule for the week or the team, a reserve list with no rule, a "
               "return that is allowed but not confirmed, or an injury designation of unknown length.",
}
UNKNOWN_TEXT = {
    "no_team": "no current NFL team",
    "schedule_missing": "no NFL schedule loaded for the week",
    "team_schedule_incomplete": "the team's NFL schedule is incomplete, so a missing game is not a confirmed bye",
    "availability_unknown": "on an NFL reserve list with no known return rule",
    "return_unconfirmed": "eligible to return, but no activation is confirmed",
    "injury_designation": "injury designation of unknown length",
}
NOTES = [
    ("Coverage counts only players on the roster today: future trades, signings, waiver claims and returns are not "
     "predicted. A player in an IR or taxi slot never counts."),
    "Player teams are the current snapshot, applied to every remaining week.",
    ("A covered slot means a rostered player who can legally start it has a game that week and no open designation; it "
     "says nothing about how many points he will score."),
    ("An injury designation (out, doubtful, injured reserve, suspended) has no published length, so the player is "
     "unknown in every remaining week rather than assumed back or assumed out."),
    "The plan starts at the first week whose NFL games have not kicked off; weeks already under way are not planned.",
]


# ------------------------------------------------------------------ schedule --

def load_schedule_facts(con, season: int) -> dict:
    """REG games of the season: (team, week) -> {opponent, home, kickoff}, the weeks the schedule covers, and the teams
    whose schedule is complete (a missing week is then their bye)."""
    out = {"available": False, "games": {}, "covered": set(), "complete_teams": set(), "teams": set()}
    if not table_exists(con, "nfl_schedules"):
        return out
    cols = set(table_columns(con, "nfl_schedules"))
    if not {"week", "home_team", "away_team"} <= cols:
        return out
    out["available"] = True
    sel = ["week", "home_team", "away_team"] + [c for c in ("gameday", "gametime") if c in cols]
    reg = " AND game_type = 'REG'" if "game_type" in cols else ""
    by_team: dict[str, list[int]] = {}
    for g in fetch_dicts(con, f"SELECT {', '.join(sel)} FROM nfl_schedules WHERE season = ?{reg} "
                              "ORDER BY week, home_team", [season]):
        w = int(g["week"])
        out["covered"].add(w)
        ko = kickoff_utc(g.get("gameday"), g.get("gametime"))
        for team, opp, home in ((g["home_team"], g["away_team"], True), (g["away_team"], g["home_team"], False)):
            out["games"][(team, w)] = {"opponent": opp, "home": home, "kickoff": ko.isoformat() if ko else None}
            by_team.setdefault(team, []).append(w)
    out["teams"] = set(by_team)
    out["complete_teams"] = set(complete_schedules(by_team))
    return out


# ------------------------------------------------------------- player-weeks --

def player_week(p: RosterPlayer, rec: dict | None, week: int, sched: dict) -> dict:
    """{state, code, source, detail, opponent, home, kickoff} for one active player and week."""
    def row(state: str, code: str, detail: str | None = None, game: dict | None = None) -> dict:
        return {"state": state, "code": code, "source": STATE_SOURCE[state], "detail": detail,
                "opponent": (game or {}).get("opponent"), "home": (game or {}).get("home"),
                "kickoff": (game or {}).get("kickoff")}

    team = p.pro_team
    if not team:
        return row(UNSURE, "no_team", UNKNOWN_TEXT["no_team"])
    if week not in sched["covered"]:
        return row(UNSURE, "schedule_missing", f"no NFL schedule loaded for week {week}")
    game = sched["games"].get((team, week))
    if game is None:
        if team in sched["complete_teams"]:
            return row(BYE, "bye", f"{team} has no game in week {week}")
        why = (f"{team} has no game in week {week}, but its regular-season schedule is incomplete, so this is not a "
               "confirmed bye") if team in sched["teams"] else f"no NFL games are loaded for {team}"
        return row(UNSURE, "team_schedule_incomplete", why)
    rec = rec or {}
    if week in p.unavailable_weeks:
        return row(OUT, "nfl_ineligible", rec.get("eligibility_basis") or "NFL reserve list (minimum games not met)",
                   game)
    status = rec.get("status")
    if status == UNKNOWN:
        return row(UNSURE, "availability_unknown", (rec.get("warnings") or [UNKNOWN_TEXT["availability_unknown"]])[0],
                   game)
    if status in (ELIGIBLE_UNCONFIRMED, INELIGIBLE):
        earliest = rec.get("earliest_eligible_week")
        return row(UNSURE, "return_unconfirmed",
                   f"eligible from week {earliest}, but no activation is confirmed" if earliest
                   else UNKNOWN_TEXT["return_unconfirmed"], game)
    if (p.injury_status or "").upper() in OPEN_DESIGNATIONS:
        return row(UNSURE, "injury_designation",
                   f"listed {p.injury_status.replace('_', ' ').lower()} at the last sync; how long is not published",
                   game)
    return row(AVAILABLE, "scheduled", None, game)


# --------------------------------------------------------------- coverage --

def _fits(slot: str, entry: tuple) -> bool:
    """May a match_slots pool entry start in `slot`: (id, position, certain) or (id, position, certain, eligible_slots),
    the provider slot ids a player eligible at two positions carries (lineup.positions_of, issue #253 G11)."""
    return eligible(slot, {"position": entry[1], "eligible_slots": entry[3] if len(entry) > 3 else None})


def match_slots(slots: list[str], pool: list[tuple]) -> list[int | None]:
    """Two-phase maximum matching (module docstring). `pool`: (player id, position, certain[, eligible_slots]) sorted by
    id; returns the matched player id per slot instance (None = empty). Deterministic: slots in lineup order, players by
    id."""
    adj = [[j for j, entry in enumerate(pool) if _fits(s, entry)] for s in slots]
    slot_of: dict[int, int] = {}                 # player index -> slot index
    player_at: list[int | None] = [None] * len(slots)

    def augment(i: int, seen: set[int], allow_unknown: bool) -> bool:
        for j in adj[i]:
            if j in seen or not (allow_unknown or pool[j][2]):
                continue
            seen.add(j)
            if j not in slot_of or augment(slot_of[j], seen, allow_unknown):
                player_at[i], slot_of[j] = j, i
                return True
        return False

    for allow_unknown in (False, True):
        for i in range(len(slots)):
            if player_at[i] is None:
                augment(i, set(), allow_unknown)
    # Presentation only: an earlier slot (fixed slots first, then narrower flex slots) holding an unknown player trades
    # with a later slot holding a certain player when both stay legal. No count changes; certain players sit in the
    # earliest slots they can, as the previous weighted search placed them. Each swap moves a certain player to an
    # earlier slot, so the loop ends within slots^2 swaps.
    swapped = True
    while swapped:
        swapped = False
        for i in range(len(slots)):
            u = player_at[i]
            if u is None or pool[u][2]:
                continue
            for k in range(i + 1, len(slots)):
                c = player_at[k]
                if c is not None and pool[c][2] and _fits(slots[i], pool[c]) and _fits(slots[k], pool[u]):
                    player_at[i], player_at[k] = c, u
                    swapped = True
                    break
    return [pool[j][0] if j is not None else None for j in player_at]


def week_coverage(slots: list[str], players: list[RosterPlayer], states: dict[int, dict],
                  cache: dict | None = None) -> list[dict]:
    """One row per starting slot instance: {slot, player_id, fill} with fill covered | unknown | uncovered.
    `players` are the active lineup-eligible players; `states` their states this week. `cache` (shared across the
    weeks of one request) solves each distinct set of certain and unknown players once."""
    if not slots:
        return []
    pool = sorted((p.espn_player_id, p.position, states[p.espn_player_id]["state"] == AVAILABLE, p.eligible_slots)
                  for p in players if states[p.espn_player_id]["state"] in (AVAILABLE, UNSURE))
    key = tuple(pool)
    if cache is not None and key in cache:
        matched = cache[key]
    else:
        matched = match_slots(slots, pool)
        if cache is not None:
            cache[key] = matched
    certain = {pid for pid, _, ok, _ in pool if ok}
    return [{"slot": s, "player_id": pid,
             "fill": "uncovered" if pid is None else ("covered" if pid in certain else "unknown")}
            for s, pid in zip(slots, matched)]


def plan_week(week: int, slots: list[str], unresolved: list[dict], players: list[RosterPlayer],
              states: dict[int, dict], starters: set[int], *, phase: str | None = None,
              matchup_period: int | None = None, slots_known: bool = True, cache: dict | None = None) -> dict:
    """The coverage, byes, verified absences, unknowns and bench alternatives of one week (pure)."""
    rows = week_coverage(slots, players, states, cache)
    by_state = {s: [p.espn_player_id for p in players if states[p.espn_player_id]["state"] == s]
                for s in (AVAILABLE, BYE, OUT, UNSURE)}
    slot_rows = []
    for label in dict.fromkeys(slots):
        mine = [r for r in rows if r["slot"] == label]
        elig = [p for p in players if eligible(label, p)]
        count = {s: sum(1 for p in elig if states[p.espn_player_id]["state"] == s) for s in (AVAILABLE, BYE, OUT, UNSURE)}
        slot_rows.append({
            "slot": label, "eligible_positions": list(slot_positions(label)), "required": len(mine),
            "covered": sum(1 for r in mine if r["fill"] == "covered"),
            "unknown": sum(1 for r in mine if r["fill"] == "unknown"),
            "uncovered": sum(1 for r in mine if r["fill"] == "uncovered"),
            "available": count[AVAILABLE], "uncertain": count[UNSURE], "bye": count[BYE], "out": count[OUT]})
    total = {k: sum(r[k] for r in slot_rows) for k in ("required", "covered", "unknown", "uncovered")}
    unresolved_n = sum(u["count"] for u in unresolved)
    if total["uncovered"]:
        status = "short"
    elif total["unknown"] or unresolved_n or not slots_known:
        status = "unconfirmed"
    else:
        status = "covered"
    byes = by_state[BYE]
    bench = [{"espn_player_id": p.espn_player_id,
              "slots": [s for s in dict.fromkeys(slots) if eligible(s, p)]}
             for p in players if p.espn_player_id not in starters and states[p.espn_player_id]["state"] == AVAILABLE]
    return {
        "week": week, "phase": phase, "matchup_period": matchup_period,
        "status": status, "status_text": STATUS_TEXT[status],
        "conflict": status != "covered" or len(byes) >= BYE_CLUSTER_MIN,
        **total, "unresolved": unresolved_n,
        "slots": slot_rows,
        "short_slots": [r["slot"] for r in slot_rows if r["uncovered"]],
        "unconfirmed_slots": [r["slot"] for r in slot_rows if r["unknown"]],
        "byes": byes, "bye_cluster": len(byes) >= BYE_CLUSTER_MIN,
        "starters_on_bye": [pid for pid in byes if pid in starters],
        "out": by_state[OUT],
        "uncertain": [{"espn_player_id": pid, "code": states[pid]["code"]} for pid in by_state[UNSURE]],
        "bench_alternatives": [b for b in bench if b["slots"]],
    }


def summarize(weeks: list[dict]) -> dict:
    def ws(pred) -> list[int]:
        return [w["week"] for w in weeks if pred(w)]
    conflicts = ws(lambda w: w["conflict"])
    most = max(weeks, key=lambda w: (len(w["byes"]), -w["week"]), default=None)
    return {"weeks": len(weeks), "covered": ws(lambda w: w["status"] == "covered"),
            "short": ws(lambda w: w["status"] == "short"), "unconfirmed": ws(lambda w: w["status"] == "unconfirmed"),
            "bye_cluster_weeks": ws(lambda w: w["bye_cluster"]), "conflict_weeks": conflicts,
            "first_conflict_week": conflicts[0] if conflicts else None,
            "most_byes": {"week": most["week"], "count": len(most["byes"])} if most and most["byes"] else None}


# ------------------------------------------------------------------- slots --

def starting_slots(roster_slots) -> tuple[list[str], list[dict], bool]:
    """(the starting slots the lineup engine fills, starting slot labels it cannot resolve to positions, whether any
    slot counts were synced at all)."""
    raw = roster_slots if isinstance(roster_slots, dict) else {}
    counts = {}
    for k, v in raw.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            counts[str(k)] = n
    known = set(FIXED_SLOTS) | set(FLEX_SLOTS) | NON_STARTING
    unresolved = [{"slot": k or "?", "count": n, "reason": "No lineup engine fills this slot, so its eligible players "
                                                           "are not established here."}
                  for k, n in sorted(counts.items()) if k not in known]
    # Capped before expanding (a provider can store any count): slots in lineup order up to MAX_STARTING_SLOTS.
    room, capped = MAX_STARTING_SLOTS, {}
    for label in (*FIXED_SLOTS, *FLEX_SLOTS):
        n = counts.get(label, 0)
        capped[label] = min(n, room)
        room -= capped[label]
        if n > capped[label]:
            unresolved.append({"slot": label, "count": n - capped[label],
                               "reason": f"More than {MAX_STARTING_SLOTS} starting slots: the extra slots are not "
                                         "evaluated."})
    return lineup_slots(capped), unresolved, bool(counts)


# ---------------------------------------------------------------------- API --

def build_bye_planner(con, team_id: int | None = None, now=None) -> dict:
    """Read-only. Raises NotSynced or NotFound."""
    lg = league_info(con)
    if lg is None:
        raise NotSynced("League not synced yet (current/final scoring period unknown).")
    season = lg["season"]
    teams = fetch_dicts(con, "SELECT team_id, name, abbrev, is_mine FROM fantasy_teams WHERE league_id = ? AND "
                             "season = ? ORDER BY team_id", [lg["league_id"], season])
    if team_id is None:
        mine = [t for t in teams if t["is_mine"]]
        if not mine:
            raise NotFound("No team is marked as yours (is_mine); pass team_id.")
        team_id = mine[0]["team_id"]
    if team_id not in {t["team_id"] for t in teams}:
        raise NotFound(f"Unknown team {team_id}")
    slot_row = fetch_dicts(con, "SELECT roster_slots FROM leagues WHERE league_id = ? AND season = ?",
                           [lg["league_id"], season])
    slots, unresolved, slots_known = starting_slots(_json(slot_row[0]["roster_slots"]) if slot_row else None)

    current, final = int(lg["current_week"]), int(lg["final_scoring_period"])
    window = evaluation_start_week(con, season, current, final, now)
    start = window["evaluation_start_week"]
    weeks = list(range(start, final + 1)) if start is not None else []

    snap = min(current, final)
    rows = fetch_dicts(con, """
        SELECT l.espn_player_id, l.lineup_slot_id, l.lineup_slot, fp.full_name AS name, fp.position, fp.pro_team,
               coalesce(fp.injury_status, l.injury_status) AS injury_status, fp.eligible_slots
        FROM lineup_snapshots l JOIN fantasy_players fp USING (espn_player_id)
        WHERE l.league_id = ? AND l.season = ? AND l.scoring_period = ? AND l.team_id = ?
        ORDER BY l.espn_player_id""", [lg["league_id"], season, snap, team_id])
    roster = [RosterPlayer(r["espn_player_id"], r["position"], r["pro_team"], r["name"],
                           r["lineup_slot_id"] == IR_SLOT, r["injury_status"], on_taxi=is_taxi(r),
                           eligible_slots=parse_eligible_slots(r["eligible_slots"], r["position"])) for r in rows]
    starters = {r["espn_player_id"] for r in rows
                if r["lineup_slot_id"] not in NON_STARTER_SLOTS and not is_taxi(r)}
    avail = load_availability(con, season, roster, tuple(weeks), {}) if weeks else {}
    roster = [replace(p, unavailable_weeks=frozenset((avail.get(p.espn_player_id) or {}).get("unavailable_weeks") or ()))
              for p in roster]
    sched = load_schedule_facts(con, season)
    loaded_rosters = roster_weeks(con, season)
    active = [p for p in roster if not p.reserved and lineup_relevant(p.position, slots)]
    states = {w: {p.espn_player_id: player_week(p, avail.get(p.espn_player_id), w, sched) for p in active}
              for w in weeks}
    solved: dict = {}           # one matching per distinct set of certain and unknown players (most weeks share one)
    out_weeks = [plan_week(w, slots, unresolved, active, states[w], starters, phase=_phase(w, lg["last_regular_season_week"]),
                           matchup_period=lg["matchup_period_of"].get(w), slots_known=slots_known, cache=solved)
                 for w in weeks]

    order = {p: i for i, p in enumerate((*FIXED_SLOTS[:4], "K", "D/ST"))}
    players = []
    for p in sorted(roster, key=lambda x: (x.reserved, order.get(x.position, 99), x.name or "", x.espn_player_id)):
        rec = avail.get(p.espn_player_id) or {}
        bye = next((w for w in sorted(sched["covered"]) if (p.pro_team, w) not in sched["games"]), None) \
            if p.pro_team in sched["complete_teams"] else None
        players.append({
            "espn_player_id": p.espn_player_id, "name": p.name, "position": p.position, "pro_team": p.pro_team,
            "injury_status": p.injury_status,
            "slot": "ir" if p.on_ir else "taxi" if p.on_taxi else "starter" if p.espn_player_id in starters else "bench",
            "counts": not p.reserved and lineup_relevant(p.position, slots),
            "eligible_slots": [s for s in dict.fromkeys(slots) if eligible(s, p)],
            "bye_week": bye,
            "availability": {k: rec.get(k) for k in ("status", "earliest_eligible_week", "eligibility_basis",
                                                     "nfl_designation_label")} if rec else None,
            "weeks": {str(w): states[w][p.espn_player_id] for w in weeks if p.espn_player_id in states[w]},
        })

    horizon_loaded = [w for w in weeks if w in sched["covered"]]
    # Counted players this season's weekly rosters list (their reserve status is checked); None when nothing is
    # planned, so no record was read. A player with no row is not verified, never assumed off the reserve list.
    found = sum(1 for p in active if (avail.get(p.espn_player_id) or {}).get("nfl_latest_roster_week") is not None) \
        if weeks else None
    return {
        "contract": CONTRACT,
        "league": {k: lg[k] for k in ("league_id", "season", "name", "current_week", "final_scoring_period",
                                      "last_regular_season_week")},
        "team": next(t for t in teams if t["team_id"] == team_id), "teams": teams,
        "query": {"team_id": team_id},
        "window": {"espn_current_week": current, "first_actionable_week": start, "weeks": weeks,
                   "remaining_weeks": start is not None, "uncertain": window.get("uncertain"),
                   "warning": window.get("warning"), "reason": window.get("reason"),
                   "regular_season_through": lg["last_regular_season_week"],
                   "playoff_weeks": [w for w in weeks if _phase(w, lg["last_regular_season_week"]) == "playoffs"]},
        "slots": {"starting": [{"slot": s, "count": slots.count(s), "eligible_positions": list(slot_positions(s))}
                               for s in dict.fromkeys(slots)],
                  "unresolved": unresolved, "known": slots_known},
        "schedule": {"available": sched["available"], "weeks_loaded": horizon_loaded,
                     "weeks_missing": [w for w in weeks if w not in sched["covered"]],
                     "incomplete_teams": sorted({p.pro_team for p in active if p.pro_team
                                                 and p.pro_team not in sched["complete_teams"]})},
        "nfl_rosters": {"available": bool(loaded_rosters), "season": season,
                        "latest_week": loaded_rosters[-1] if loaded_rosters else None,
                        "players_found": found, "players_counted": len(active) if weeks else None},
        "players": players,
        "weeks": out_weeks,
        "summary": summarize(out_weeks),
        "rules": {"bye_cluster_min": BYE_CLUSTER_MIN, "sources": SOURCES, "unknown_reasons": UNKNOWN_TEXT,
                  "statuses": STATUS_TEXT},
        "notes": NOTES,
    }
