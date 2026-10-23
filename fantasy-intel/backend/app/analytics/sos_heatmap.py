"""Position-specific strength-of-schedule heatmap (issue #188; read-only, contract sos-heatmap.v1).

One grid per offensive position (QB, RB, WR, TE): rows are NFL offenses (or the players of a roster, the league's free
agents or one player, each on his current NFL team), columns are the remaining fantasy weeks of one horizon (the rest
of the regular season, or the fantasy-playoff weeks from the league's settings), and each cell is that week's opponent
defense graded on the position's primary metric.

Everything defensive comes from schedule_intelligence, unchanged: the same nflverse inputs (nfl_schedules,
nfl_defense_week), the same presumed-complete rule, no lookahead (a week-W cell uses only the opponent's games before
week W), the same metric per position and the same rank bands. This module adds only

  * the horizon: the remaining weeks of the regular season or of the fantasy playoffs, from the league's settings
    (unknown settings are said, never guessed);
  * a neutral baseline per week: the pooled value of the defenses that can be ranked at that cutoff (sum of their
    numerators over the sum of their denominators), so each graded cell also shows how far it sits from neutral;
  * row tallies over the horizon (weeks per band, the graded weeks' average rank): descriptive counts, never a
    composite score or a projection.

Rules carried over: missing is never zero (a cell without evidence has no rank, value or difference, with the reason);
an opponent with fewer than MIN_DEF_GAMES presumed-complete games, or a week with fewer than MIN_RANKED_DEFENSES
rankable defenses, gets no grade at all; a week without a loaded NFL schedule, or a player without a current NFL team,
gets no opponent. Ranks compare defenses within one position's metric only: a QB grid and an RB grid use different
metrics and are not comparable. Nothing here is written, synced or fetched.
"""

from __future__ import annotations

import re
from datetime import datetime

from ..db.connection import fetch_dicts, table_exists
from ..espn.constants import IR_SLOT
from .context import MIN_DEF_GAMES
from .schedule_intelligence import (
    LABEL_RULE, LABELS, METRICS, MIN_RANKED_DEFENSES, OBSERVATION_NOTE, PRIMARY_METRIC, SKILL_POSITIONS,
    DefenseCutoffs, NotFound, NotSynced, _identity, _phase, _roster, completion_warnings, league_info, load_schedule,
    nfl_input_runs,
)
from .trade_data import evaluation_start_week

CONTRACT = "sos-heatmap.v1"
HORIZONS = ("ros", "playoffs")
VIEWS = ("teams", "roster", "free_agents")
# Free agents per request, in FREE_AGENT_ORDER.
MAX_FREE_AGENTS = 40
FREE_AGENT_ORDER = ("Available players: free agents or on waivers as the provider's last sync reported them (a provider "
                    "that reports no waiver status lists every unrostered player as a free agent). Players with an NFL "
                    "team come first, then by the provider's ownership share where synced, then by season fantasy "
                    "points where synced, then by name.")
GRADED = ("permissive", "middle", "tougher")
_NFL_TEAM = re.compile(r"^[A-Z]{2,3}$")

SOURCE_NAME = "nflverse play-by-play aggregated per defense and week, with the nflverse schedule"
BASELINE_RULE = (f"Neutral baseline for a week: the pooled value of every defense that can be ranked before that week "
                 f"(at least {MIN_DEF_GAMES} presumed-complete games): the sum of their numerators over the sum of their "
                 f"denominators. It is shown only when at least {MIN_RANKED_DEFENSES} defenses can be ranked. A cell's "
                 "difference is its opponent's value minus that baseline; positive means offenses did better than "
                 "neutral against that defense.")
SUMMARY_RULE = ("Row tallies count the horizon's weeks per band. The average rank is over graded weeks only (shown with "
                "how many), never over weeks without evidence. Descriptive counts, not a score or a projection.")
ROLE_NOTE = ("A player's row uses his current NFL team for every remaining week. The grid describes the opponent "
             "defenses only: it says nothing about the player's role, usage or health, and future trades, releases "
             "and signings are not predicted.")


class BadParam(ValueError):
    """A request parameter outside its allowed values (the message is safe to return)."""


# ------------------------------------------------------------------ horizon --

def horizon_weeks(horizon: str, start: int | None, final: int, last_regular: int | None) -> dict:
    """The remaining weeks of one horizon. `start` is the first actionable week (None: none remain)."""
    remaining = list(range(start, final + 1)) if start is not None else []
    playoff_weeks = list(range(last_regular + 1, final + 1)) if last_regular is not None else None
    out = {"key": horizon, "weeks": [], "known": True, "reason": None, "source": "league settings",
           "regular_season_through": last_regular, "playoff_weeks": playoff_weeks, "final_scoring_period": final,
           "first_actionable_week": start}
    if horizon == "ros":
        out["label"] = "Rest of regular season"
        if last_regular is None:
            out["weeks"] = remaining
            out["known"] = False
            out["reason"] = ("The league settings do not establish where the regular season ends, so this shows every "
                             f"remaining week through the final scoring period (week {final}), playoffs possibly included.")
        else:
            out["weeks"] = [w for w in remaining if w <= last_regular]
            if remaining and not out["weeks"]:
                out["reason"] = f"The regular season ended with week {last_regular}; only fantasy-playoff weeks remain."
    else:
        out["label"] = "Fantasy playoffs"
        if last_regular is None:
            out["known"] = False
            out["reason"] = ("The league settings do not establish the fantasy-playoff weeks, so no playoff window is "
                             "shown. Use the rest-of-season view.")
        else:
            out["weeks"] = [w for w in remaining if w > last_regular]
            if not playoff_weeks:
                out["reason"] = (f"The league settings end the regular season at the final scoring period (week {final}): "
                                 "there are no fantasy-playoff weeks.")
            elif remaining and not out["weeks"]:
                out["reason"] = "Every fantasy-playoff week is already under way or over."
    if start is None:
        out["reason"] = (f"No remaining fantasy weeks: every week through the final scoring period (week {final}) has "
                         "kicked off.")
    return out


# ----------------------------------------------------------------- evidence --

def week_baseline(defs: DefenseCutoffs, week: int, metric: str) -> dict:
    """The neutral baseline before `week`: pooled over the defenses that can be ranked at that cutoff."""
    cut = defs.at(week)
    m = METRICS[metric]
    num = den = 0.0
    pool = 0
    for team, s in cut["samples"].items():
        v, _ = cut["metrics"].get(team, {}).get(metric, (None, None))
        if v is None or s["games"] < MIN_DEF_GAMES:
            continue
        num += s[m["numerator"]]
        den += s[m["denominator"]]
        pool += 1
    if pool < MIN_RANKED_DEFENSES or not den:
        reason = (f"only {pool} defenses have >= {MIN_DEF_GAMES} presumed-complete games before week {week} "
                  f"(< {MIN_RANKED_DEFENSES})")
        return {"week": week, "value": None, "ranked_defenses": pool, "through_week": cut["through_week"],
                "reason": reason}
    return {"week": week, "value": num / den, "ranked_defenses": pool, "through_week": cut["through_week"],
            "reason": None}


def team_cell(team: str, week: int, sched: dict, defs: DefenseCutoffs, position: str, baseline: dict,
              last_regular: int | None) -> dict:
    """One NFL offense's week: the opponent and its evidence on the position's primary metric, or why there is none."""
    base = {"week": week, "phase": _phase(week, last_regular)}
    if week not in sched["covered"]:
        return {**base, "status": "schedule_missing", "label": "unavailable", "label_text": LABELS["unavailable"],
                "reason": f"no NFL regular-season schedule is loaded for week {week}"}
    g = sched["games"].get((team, week))
    if g is None:
        return {**base, "status": "bye", "label": "bye", "label_text": LABELS["bye"],
                "reason": f"{team} has no game in week {week}"}
    ev = defs.describe(g["opponent"], week, position)
    p = next(x for x in ev["metrics"] if x["metric"] == PRIMARY_METRIC[position])
    graded = ev["label"] in GRADED
    if graded:
        reason = None
    elif p["value"] is None or p["rank"] is None:
        reason = p["missing_reason"] or "no evidence"
    else:
        reason = (f"only {p['ranked_defenses']} defenses can be ranked before week {week} (< {MIN_RANKED_DEFENSES}), "
                  "so no defense is graded")
    delta = p["value"] - baseline["value"] if graded and baseline["value"] is not None else None
    return {**base, "status": "scheduled", "opponent": g["opponent"], "home": g["home"],
            "kickoff_known": g["kickoff"] is not None,
            "label": ev["label"], "label_text": ev["label_text"],
            "rank": p["rank"] if graded else None, "ranked_defenses": p["ranked_defenses"],
            "value": p["value"], "baseline": baseline["value"], "delta": delta,
            "games": ev["games"], "plays": p["denominator"],
            "through_week": ev["through_week"], "excluded_games": len(ev["excluded_weeks"]),
            "reason": reason}


def summarize(cells: list[dict]) -> dict:
    """Tallies over a row's horizon cells: weeks per band and the graded weeks' average rank (never over gaps)."""
    counts = {k: 0 for k in LABELS}
    for c in cells:
        counts[c["label"]] += 1
    ranks = [c["rank"] for c in cells if c.get("rank") is not None]
    deltas = [c["delta"] for c in cells if c.get("delta") is not None]
    return {"weeks": len(cells), "counts": counts, "graded_weeks": len(ranks),
            "games": sum(1 for c in cells if c["status"] == "scheduled"),
            "average_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
            "average_delta": sum(deltas) / len(deltas) if deltas else None}


def unknown_team_cells(weeks: list[int], last_regular: int | None) -> list[dict]:
    return [{"week": w, "phase": _phase(w, last_regular), "status": "team_unknown", "label": "unavailable",
             "label_text": LABELS["unavailable"], "reason": "no current NFL team, so no opponent can be assigned"}
            for w in weeks]


# ------------------------------------------------------------------ players --

def _rostered(con, lg: dict) -> dict[int, int]:
    """espn_player_id -> fantasy team for the league's current lineup snapshot."""
    week = min(int(lg["current_week"]), int(lg["final_scoring_period"]))
    return {r["espn_player_id"]: r["team_id"] for r in fetch_dicts(
        con, "SELECT espn_player_id, team_id FROM lineup_snapshots WHERE league_id = ? AND season = ? AND "
             "scoring_period = ?", [lg["league_id"], lg["season"], week])}


def _free_agents(con, position: str, rostered: dict[int, int], nfl_team: str | None) -> tuple[list[dict], int]:
    """Available players (FREEAGENT or WAIVERS, on no fantasy team, not in a roster snapshot) at the position: the
    first MAX_FREE_AGENTS in FREE_AGENT_ORDER, and how many there are in all."""
    params: list = [position]
    team_sql = ""
    if nfl_team:
        team_sql = " AND pro_team = ?"
        params.append(nfl_team)
    rows = fetch_dicts(con, f"""
        SELECT espn_player_id, full_name AS name, position, pro_team, injury_status, availability_status,
               percent_owned, NULL AS lineup_slot_id
        FROM fantasy_players
        WHERE availability_status IN ('FREEAGENT', 'WAIVERS') AND coalesce(on_team_id, 0) = 0 AND position = ?{team_sql}
        ORDER BY pro_team IS NULL, percent_owned DESC NULLS LAST, season_points DESC NULLS LAST, full_name,
                 espn_player_id""", params)
    rows = [r for r in rows if r["espn_player_id"] not in rostered]
    return rows[:MAX_FREE_AGENTS], len(rows)


def _player_row(p: dict, ident: dict, team_cells: dict[str, list[dict]], weeks: list[int], last_regular: int | None,
                rostered: dict[int, int]) -> dict:
    team = p["pro_team"]
    notes = []
    if not team:
        notes.append("No current NFL team in the provider's data (free agent or unknown): no opponent is assigned.")
    latest = ident.get("nflverse_latest_team")
    uncertain = bool(team and latest and latest != team)
    if uncertain:
        notes.append(f"The provider lists {team}; the latest nflverse weekly roster (week "
                     f"{ident['nflverse_latest_week']}) lists {latest}. The provider's team is used; verify a recent move.")
    if team and team not in team_cells:
        notes.append(f"{team} is not in the stored NFL schedule, so no opponent is assigned.")
    cells = team_cells.get(team) if team else None
    if cells is None:
        cells = unknown_team_cells(weeks, last_regular)
    return {"kind": "player", "nfl_team": team, "team_uncertain": uncertain,
            "player": {"espn_player_id": p["espn_player_id"], "name": p["name"], "position": p["position"],
                       "pro_team": team, "injury_status": p.get("injury_status"),
                       "on_ir": p.get("lineup_slot_id") == IR_SLOT,
                       "fantasy_team_id": rostered.get(p["espn_player_id"]),
                       "availability_status": p.get("availability_status"),
                       "percent_owned": p.get("percent_owned")},
            "notes": notes, "cells": cells, "summary": summarize(cells)}


# ---------------------------------------------------------------------- API --

def build_sos_heatmap(con, position: str = "QB", horizon: str = "ros", view: str = "teams", team_id: int | None = None,
                      nfl_team: str | None = None, player_id: int | None = None, now: datetime | None = None) -> dict:
    """Read-only. Raises NotSynced, NotFound or BadParam."""
    position = (position or "").upper()
    if position not in SKILL_POSITIONS:
        raise BadParam(f"position must be one of {', '.join(SKILL_POSITIONS)}")
    if horizon not in HORIZONS:
        raise BadParam("horizon must be ros or playoffs")
    if view not in VIEWS:
        raise BadParam("view must be teams, roster or free_agents")
    if nfl_team is not None:
        nfl_team = nfl_team.strip().upper() or None
        if nfl_team is not None and not _NFL_TEAM.match(nfl_team):
            raise BadParam("nfl_team must be an NFL team abbreviation such as KC")

    lg = league_info(con)
    if lg is None:
        raise NotSynced("League not synced yet (current/final scoring period unknown).")
    season = lg["season"]
    teams = fetch_dicts(con, "SELECT team_id, name, abbrev, is_mine FROM fantasy_teams WHERE league_id = ? AND "
                             "season = ? ORDER BY team_id", [lg["league_id"], season])
    if team_id is not None and team_id not in {t["team_id"] for t in teams}:
        raise NotFound(f"Unknown team {team_id}")

    player = None
    if player_id is not None:
        found = fetch_dicts(con, "SELECT espn_player_id, full_name AS name, position, pro_team, injury_status, "
                                 "availability_status, percent_owned, NULL AS lineup_slot_id FROM fantasy_players "
                                 "WHERE espn_player_id = ?", [player_id])
        if not found:
            raise NotFound(f"Unknown player {player_id}")
        player = found[0]
        if player["position"] not in SKILL_POSITIONS:
            raise BadParam("strength of schedule covers QB, RB, WR and TE")
        position = player["position"]           # a player is graded on his own position's metric only
        view_used = "player"
    else:
        view_used = view
    if view_used == "roster" and team_id is None:
        mine = [t for t in teams if t["is_mine"]]
        if not mine:
            raise NotFound("No team is marked as yours (is_mine); pass team_id.")
        team_id = mine[0]["team_id"]

    current, final = int(lg["current_week"]), int(lg["final_scoring_period"])
    last_regular = lg["last_regular_season_week"]
    window = evaluation_start_week(con, season, current, final, now)
    hz = horizon_weeks(horizon, window["evaluation_start_week"], final, last_regular)
    weeks = hz["weeks"]

    runs = nfl_input_runs(con, season)
    fetches = {k: runs[k]["started_at"] if runs[k] else None for k in ("schedules", "defense")}
    sched = load_schedule(con, season, fetches)
    defense_rows = fetch_dicts(con, "SELECT * FROM nfl_defense_week WHERE season = ? ORDER BY week, defteam",
                               [season]) if table_exists(con, "nfl_defense_week") else []
    defs = DefenseCutoffs(defense_rows, sched["presumed_complete"], sched["excluded"])
    metric = PRIMARY_METRIC[position]
    baselines = {w: week_baseline(defs, w, metric) for w in weeks}

    nfl_teams = sorted({team for team, _ in sched["games"]})
    if nfl_team is not None and sched["available"] and nfl_team not in nfl_teams:
        raise BadParam(f"{nfl_team} is not a team in the stored {season} NFL schedule")
    team_cells = {t: [team_cell(t, w, sched, defs, position, baselines[w], last_regular) for w in weeks]
                  for t in nfl_teams}

    rostered = _rostered(con, lg)
    rows_total = None
    if view_used == "teams":
        rows = [{"kind": "team", "nfl_team": t, "team_uncertain": False, "player": None, "notes": [],
                 "cells": team_cells[t], "summary": summarize(team_cells[t])}
                for t in nfl_teams if nfl_team is None or t == nfl_team]
    else:
        if view_used == "player":
            players = [player]
        elif view_used == "roster":
            players = [p for p in _roster(con, lg, team_id) if p["position"] == position
                       and (nfl_team is None or p["pro_team"] == nfl_team)]
            players.sort(key=lambda p: (p["name"] or "", p["espn_player_id"]))
        else:
            players, rows_total = _free_agents(con, position, rostered, nfl_team)
        ident = _identity(con, season, [p["espn_player_id"] for p in players])
        rows = [_player_row(p, ident[p["espn_player_id"]], team_cells, weeks, last_regular, rostered) for p in players]
    if rows_total is None:
        rows_total = len(rows)

    cells = [c for r in rows for c in r["cells"]]
    scheduled = sum(1 for c in cells if c["status"] == "scheduled")
    graded = sum(1 for c in cells if c.get("rank") is not None)
    through = max((b["through_week"] for b in baselines.values() if b["through_week"] is not None), default=None)
    has_values = any(c.get("value") is not None for c in cells) or any(b["value"] is not None for b in baselines.values())
    m = METRICS[metric]

    return {
        "contract": CONTRACT,
        "league": {k: lg[k] for k in ("league_id", "season", "name", "current_week", "final_scoring_period",
                                      "last_regular_season_week")},
        "query": {"position": position, "horizon": horizon, "view": view_used, "team_id": team_id,
                  "nfl_team": nfl_team, "player_id": player_id},
        "team": next((t for t in teams if t["team_id"] == team_id), None), "teams": teams,
        "nfl_teams": nfl_teams,
        "position": position,
        "metric": {"key": metric, "label": m["label"], "unit": m["unit"], "kind": m["kind"],
                   "numerator": m["numerator"], "denominator": m["denominator"], "definition": m["definition"]},
        "horizon": hz,
        "window": {"espn_current_week": current, "first_actionable_week": window["evaluation_start_week"],
                   "uncertain": window.get("uncertain"), "warning": window.get("warning"),
                   "reason": window.get("reason")},
        "baselines": [baselines[w] for w in weeks],
        "rows": rows,
        "rows_total": rows_total,
        "rows_shown": len(rows),
        "free_agent_limit": MAX_FREE_AGENTS if view_used == "free_agents" else None,
        "free_agent_order": FREE_AGENT_ORDER if view_used == "free_agents" else None,
        "coverage": {"cells": len(cells), "scheduled": scheduled, "graded": graded},
        "source": {"name": SOURCE_NAME if has_values else None,
                   "available": has_values,
                   "reason": None if has_values else
                   ("No presumed-complete defensive games are stored for this season." if not defense_rows else
                    "No opponent in view has presumed-complete defensive evidence yet."),
                   "through_week": through,
                   "defense_weeks_loaded": sorted({r["week"] for r in defense_rows}),
                   "schedule_available": sched["available"],
                   "weeks_without_schedule": [w for w in weeks if w not in sched["covered"]]},
        "schedule": {"available": sched["available"],
                     "warnings": completion_warnings(season, runs, sched, defense_rows)},
        "labels": LABELS,
        "label_rule": LABEL_RULE,
        "baseline_rule": BASELINE_RULE,
        "summary_rule": SUMMARY_RULE,
        "min_games": MIN_DEF_GAMES,
        "min_ranked_defenses": MIN_RANKED_DEFENSES,
        "notes": [
            f"Comparable only within {position}: each defense is ranked on {m['label']} among defenses with at "
            f"least {MIN_DEF_GAMES} presumed-complete games before the week (rank 1 = most permissive). Other "
            "positions use other metrics, so their grids are not comparable with this one.",
            ROLE_NOTE, OBSERVATION_NOTE,
            "No lookahead: each week uses only the opponent's games presumed complete before that week; future weeks "
            "use every game presumed complete so far.",
        ],
    }
