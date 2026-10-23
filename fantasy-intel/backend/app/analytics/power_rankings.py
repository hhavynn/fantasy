"""League Power Rankings (read-only): every roster in the league ranked on a visible composite of four
components, with week-over-week movement recomputed point-in-time. See docs/power_rankings.md.

Distinct from the player rankings (/rankings: players, not rosters) and from the standings and
playoff odds (/playoff-race: actual records and a projected scenario). Nothing here is a win
probability, a forecast of results or a claim about who makes the playoffs.

Components (each reuses an existing engine, unchanged):
  * strength     - projected optimal-lineup points per week over the remaining weeks, on comparable weeks
                   only (team_value.league_ranks: the exact weekly optimizer, team_lineups.project_team).
  * depth        - injury cover: the projected points the rest of the roster recovers per lost starter-week,
                   from explicit removal scenarios (team_lineups.roster_marginal_value), own roster only.
  * replacement  - positional replacement options: per starting position, how far the best bench player
                   projects above the best free agent (team_lineups.replacement_market), floored at 0 and
                   summed over positions. 0 at a position = no backup better than the waiver wire.
  * all_play     - all-play winning percentage over completed regular-season periods (playoff_race.all_play).
K and D/ST are left out of depth and replacement: every team can stream them from waivers.

Each component is scaled within the league (best ranked team 100, worst 0) and the composite is the
weighted mean of those scores (WEIGHTS). The scale is relative to this league this week, so a composite
is not comparable across leagues; ranks are.

Editions. The rankings for week E are what the league looked like before E's first kickoff:
  * current  - E = the first fantasy week that has not kicked off (trade_data.evaluation_start_week),
               the canonical league state (current rosters, current ESPN projections, NFL availability).
  * previous - P = E - 1, recomputed point-in-time: rosters from the newest roster_history snapshot synced
               before P's first kickoff (history_roster_problem), else from lineup_snapshots at scoring period P
               when they provably are the rosters at P's first kickoff (kickoff_roster_problem),
               ESPN projections from the newest espn_projection_history snapshot synced before P's first
               kickoff, free agents = league players on no roster at P, all-play over periods that ended
               before P. NFL availability records are current-only, so they are not applied to P (ESPN's
               projections already carry injuries). When any input is missing, movement is not shown and
               the reason is given; nothing is estimated.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from ..db.connection import fetch_dicts, table_columns, table_exists
from ..espn.constants import IR_SLOT
from . import production_forecasts as pf
from .lineup import FIXED_SLOTS, slots_left_out, unsupported_of
from .playoff_race import NotSynced, RaceData, all_play, classify_matchups, load_race_data
from .projection_backtest import _utc
from .reserve_slots import is_taxi
from .team_lineups import (STREAM_POSITIONS, RosterPlayer, project_team, r, replacement_market, source_label,
                           roster_marginal_value, starters)
from .team_value import NotFound, league_ranks, load_extras, position_strength
from .trades import LeagueState, with_streaming

CONTRACT = "power-rankings/v1"
COMPONENTS = ("strength", "depth", "replacement", "all_play")
WEIGHTS = {"strength": 0.45, "all_play": 0.30, "depth": 0.15, "replacement": 0.10}
LABELS = {"strength": "Lineup strength", "depth": "Depth and injury cover", "replacement": "Replacement options",
          "all_play": "All-play"}
UNITS = {"strength": "projected points per week", "depth": "points recovered per lost starter-week",
         "replacement": "points per week above the waiver wire", "all_play": "winning percentage"}
DESCRIPTIONS = {
    "strength": "Average projected points of the team's optimal starting lineup per remaining week (ESPN weekly "
                "projections, the league's slots, re-optimized every week). Comparable weeks only.",
    "depth": "Remove one starter at a time and re-optimize the rest of the roster: the projected points the "
             "remaining players recover, averaged over every week that starter would have started. Own roster only.",
    "replacement": "At each starting position (QB, RB, WR, TE), how many points per week the best bench player projects "
                   "above the best free agent, never below 0, summed over positions.",
    "all_play": "Every completed regular-season week, each team's score against every other team's score that week.",
}
METHOD = [
    "Each component is scaled within the league: the best ranked team scores 100, the worst 0 (all equal: 50).",
    "Power score = " + " + ".join(f"{int(w * 100)}% {LABELS[k].lower()}" for k, w in WEIGHTS.items()) + ". "
    "A component no team has yet (no completed weeks) is left out and the other weights are scaled up.",
    "Equal power scores share a rank; lineup strength breaks the tie only for display order.",
    "Last week's ranks are recomputed from what was known before last week's first kickoff: that week's rosters, "
    "the last ESPN projection snapshot stored before kickoff and the all-play record up to then.",
    "These are rankings, not win probabilities or playoff odds.",
]
SKIP_POSITIONS = set(STREAM_POSITIONS)


@dataclass
class Previous:
    """Point-in-time inputs for the previous edition, or why there are none."""
    week: int | None
    state: LeagueState | None
    reason: str | None = None
    projection_snapshot: str | None = None
    projection_synced_at: str | None = None
    roster_source: str | None = None           # roster_history | lineup_snapshots
    roster_synced_at: str | None = None


# ------------------------------------------------------------------ pure math --

def all_play_before(regular: list[dict], team_ids: list[int], before_week: int | None) -> dict[int, dict]:
    """playoff_race.all_play over final regular-season periods whose every scoring period is before
    `before_week` (None = every final period)."""
    keep = [m for m in regular if m["status"] == "final" and
            (before_week is None or (m.get("weeks") and max(m["weeks"]) < before_week))]
    return all_play(keep, team_ids)


def scale(values: dict[int, float | None]) -> dict[int, float | None]:
    """League-relative 0-100: best 100, worst 0, all equal 50. None stays None and is ignored."""
    known = {t: v for t, v in values.items() if v is not None}
    if not known:
        return {t: None for t in values}
    lo, hi = min(known.values()), max(known.values())
    return {t: (None if v is None else 50.0 if hi - lo < 1e-9 else round(100 * (v - lo) / (hi - lo), 1))
            for t, v in values.items()}


def composite(scores: dict[str, dict[int, float | None]], weights: dict[str, float] = WEIGHTS) -> tuple[dict, dict]:
    """(team -> weighted mean of its component scores, effective weights). A component no team has is
    dropped league-wide and the rest renormalized; a team missing a component other teams have gets
    its own renormalized mean (and the caller flags it)."""
    live = {k: w for k, w in weights.items() if any(v is not None for v in scores.get(k, {}).values())}
    total = sum(live.values())
    effective = {k: (round(w / total, 4) if k in live else 0.0) for k, w in weights.items()} if total else \
        {k: 0.0 for k in weights}
    teams = sorted({t for per in scores.values() for t in per})
    out = {}
    for t in teams:
        parts = [(live[k], scores[k][t]) for k in live if scores[k].get(t) is not None]
        w = sum(p[0] for p in parts)
        out[t] = round(sum(p[0] * p[1] for p in parts) / w, 1) if w else None
    return out, effective


def rank(values: dict[int, float | None]) -> dict[int, int]:
    """Competition ranks, higher is better; equal values share a rank; None is unranked."""
    known = sorted(((v, t) for t, v in values.items() if v is not None), key=lambda x: (-x[0], x[1]))
    out, prev, cur = {}, None, 0
    for i, (v, t) in enumerate(known, start=1):
        if v != prev:
            cur, prev = i, v
        out[t] = cur
    return out


def movement(current: dict[int, int], previous: dict[int, int] | None) -> dict[int, int | None]:
    """Places gained since the previous edition (positive = rose). None without a previous rank."""
    previous = previous or {}
    return {t: (previous[t] - rk if t in previous else None) for t, rk in current.items()}


# ------------------------------------------------------------------ components --

def depth_cover(roster: list[RosterPlayer], base: dict, slots: list[str], window, proj, weeks: list[int]) -> dict:
    """Removal scenarios over `weeks`: for each rostered starter (not K/D/ST), the points the rest of the
    roster recovers in each week he starts = his lineup points minus his marginal value that week."""
    by_week = {w["week"]: w for w in base["weeks"] if w["week"] in weeks}
    rostered = {p.espn_player_id: p for p in roster}
    recovered = own = 0.0
    starter_weeks = 0
    players = []
    for pid in sorted({q for w in by_week.values() for q in starters(w)}):
        p = rostered.get(pid)
        if p is None or p.reserved or p.position in SKIP_POSITIONS:   # streamers and K/D/ST are not depth
            continue
        mv = roster_marginal_value(roster, pid, slots, window, proj, base)
        rec = pts = 0.0
        n = 0
        for wk in mv["weeks"]:
            w = by_week.get(wk["week"])
            if w is None or not wk["started"] or wk["marginal"] is None:
                continue
            mine = next(a["points"] for a in w["assignment"] if a["player_id"] == pid)
            rec += mine - wk["marginal"]
            pts += mine
            n += 1
        if n:
            recovered, own, starter_weeks = recovered + rec, own + pts, starter_weeks + n
            players.append({"espn_player_id": pid, "name": p.name, "position": p.position, "weeks_started": n,
                            "marginal": r(pts - rec), "recovered": r(rec)})
    players.sort(key=lambda x: (-(x["marginal"] or 0), x["espn_player_id"]))
    return {"value": r(recovered / starter_weeks) if starter_weeks else None,
            "share": round(recovered / own, 4) if own > 0 else None,
            "starter_weeks": starter_weeks, "players": players}


def replacement_options(base: dict, market: dict, slots: list[str], weeks: list[int]) -> dict:
    """Per starting position (QB/RB/WR/TE the league starts): best bench projection minus best free agent
    projection, each week, floored at 0, averaged over `weeks`; value = the sum over positions."""
    positions = [s for s in FIXED_SLOTS if s in slots and s not in SKIP_POSITIONS]
    mk = {w["week"]: w["positions"] for w in market["weeks"]}
    rows = []
    for pos in positions:
        edges, bench_best, fa_best = [], [], []
        for w in base["weeks"]:
            if w["week"] not in weeks:
                continue
            bench = [b["points"] for b in w["bench"] if b.get("position") == pos and b.get("points") is not None]
            fa = ((mk.get(w["week"]) or {}).get(pos) or {}).get("best_available_projection")
            b, f = (max(bench) if bench else None), fa
            bench_best.append(b)
            fa_best.append(f)
            edges.append(max(0.0, (b or 0.0) - (f or 0.0)) if b is not None else 0.0)
        n = len(edges)
        known_b, known_f = [x for x in bench_best if x is not None], [x for x in fa_best if x is not None]
        rows.append({"position": pos, "edge": r(sum(edges) / n) if n else None,
                     "best_bench": r(sum(known_b) / len(known_b)) if known_b else None,
                     "best_free_agent": r(sum(known_f) / len(known_f)) if known_f else None,
                     "has_backup": bool(known_b)})
    vals = [x["edge"] for x in rows if x["edge"] is not None]
    return {"value": r(sum(vals)) if vals and weeks else None, "positions": rows,
            "covered": sum(1 for x in rows if round(x["edge"] or 0, 1) > 0), "of": len(rows)}


def edition(state: LeagueState, regular: list[dict], team_ids: list[int], team_names: dict[int, str],
            all_play_before_week: int | None, detail: bool = True) -> dict:
    """Raw component values for every team under one league state. `detail` adds the per-team breakdowns."""
    window = state.window()
    slots, proj = state.slots, state.projections
    projections = {t: (project_team(state.rosters[t], slots, window, proj) if state.rosters.get(t) else None)
                   for t in team_ids}
    ranks = league_ranks(projections, window, team_names) if window.weeks else None
    comparable = ranks["phases"]["all"]["weeks"] if ranks else []
    strength = {x["team_id"]: x["average_weekly"] for x in ranks["phases"]["all"]["teams"]} if ranks else {}
    market = replacement_market(state.free_agents, slots, window, proj, top_n=1,
                                projection_source=source_label(state.projection_source, state.projections)) \
        if comparable else None
    ap = all_play_before(regular, team_ids, all_play_before_week)
    teams = {}
    for t in team_ids:
        base = projections[t]
        ranked = t in strength
        depth = depth_cover(state.rosters[t], base, slots, window, proj, comparable) if ranked else None
        repl = replacement_options(base, market, slots, comparable) if ranked else None
        g = ap[t]["wins"] + ap[t]["losses"] + ap[t]["ties"]
        teams[t] = {"strength": strength.get(t), "depth": depth["value"] if depth else None,
                    "replacement": repl["value"] if repl else None, "all_play": ap[t]["pct"] if g else None,
                    "all_play_record": {k: ap[t][k] for k in ("wins", "losses", "ties")}, "all_play_periods": ap[t]["periods"],
                    "unranked_reason": None if ranked else ("no roster snapshot" if base is None else
                                                            f"no comparable week: {slots_left_out(unsupported_of(slots))}"
                                                            if unsupported_of(slots) else
                                                            "no comparable week with a complete projection"),
                    **({"_depth": depth, "_replacement": repl} if detail else {})}
    pos = position_strength(projections, slots, window, {"all": comparable}) if detail and comparable else None
    return {"weeks": list(window.weeks), "comparable_weeks": comparable,
            "excluded_weeks": ranks["excluded_weeks"] if ranks else [], "teams": teams, "positions": pos,
            **({"unsupported_slots": list(unsupported_of(slots))} if unsupported_of(slots) else {}),
            "all_play_periods": sorted({p for x in ap.values() for p in x["periods"]})}


def score_edition(ed: dict) -> dict:
    """Scaled component scores, composite and ranks of one edition (only teams with lineup strength rank)."""
    ranked = [t for t, x in ed["teams"].items() if x["strength"] is not None]
    scores = {k: scale({t: ed["teams"][t][k] for t in ranked}) for k in COMPONENTS}
    comp, effective = composite(scores)
    ranks = rank(comp)
    comp_ranks = {k: rank({t: ed["teams"][t][k] for t in ranked}) for k in COMPONENTS}
    return {"scores": scores, "composite": comp, "weights": effective, "ranks": ranks, "component_ranks": comp_ranks}


# ------------------------------------------------------------------ previous --

def _prior_projections(con, season: int, week: int, final: int, kickoff: datetime) -> tuple[dict, str | None, str | None, str | None]:
    """ESPN projections for weeks week..final from the newest espn_projection_history snapshot whose latest
    row was synced before `week`'s first kickoff: (projections, snapshot_date, synced_at, reason if none)."""
    if not table_exists(con, "espn_projection_history"):
        return {}, None, None, "no ESPN projection history is stored"
    snaps = fetch_dicts(con, """SELECT snapshot_date, max(synced_at) AS synced_at FROM espn_projection_history
                                WHERE season = ? GROUP BY snapshot_date ORDER BY snapshot_date DESC""", [season])
    pick = next((s for s in snaps if (_utc(s["synced_at"]) or kickoff) < kickoff), None)
    if pick is None:
        first = snaps[-1]["snapshot_date"] if snaps else None
        return {}, None, None, (f"no ESPN projection snapshot was stored before week {week}'s first kickoff"
                                + (f" (history starts {first})" if first else ""))
    proj: dict[int, dict[int, float]] = {}
    for row in fetch_dicts(con, """SELECT espn_player_id, scoring_period, projected_points FROM espn_projection_history
                                   WHERE snapshot_date = ? AND season = ? AND scoring_period BETWEEN ? AND ?
                                     AND projected_points IS NOT NULL""", [pick["snapshot_date"], season, week, final]):
        proj.setdefault(row["espn_player_id"], {})[row["scoring_period"]] = float(row["projected_points"])
    return proj, str(pick["snapshot_date"]), _iso(pick["synced_at"]), None


def kickoff_roster_problem(rows: list[dict], week: int, kickoff: datetime) -> str | None:
    """Why a stored week-`week` roster snapshot cannot be shown to be the rosters at that week's first kickoff,
    or None when it can. The sync rewrites a period's rows, so a snapshot synced after kickoff may include later
    moves. It is accepted when every row was synced before kickoff, or when its acquisition dates are recorded and
    none is at or after kickoff (an add or a trade after kickoff always carries a later date). Moves are never
    reversed from the transaction log: ESPN omits other teams' trade legs and some adds there."""
    synced = [_utc(r.get("synced_at")) for r in rows]
    if synced and all(t is not None and t < kickoff for t in synced):
        return None
    dates = [_utc(r.get("acquisition_date")) for r in rows]
    known = [d for d in dates if d is not None]
    if not known:
        return (f"the stored week {week} rosters were synced after its first kickoff and carry no acquisition dates, "
                "so they cannot be shown to be the rosters at kickoff")
    late = sorted({r["team_id"] for r, d in zip(rows, dates) if d is not None and d >= kickoff})
    if late:
        n = len(late)
        return (f"{n} {'team' if n == 1 else 'teams'} changed {'its roster' if n == 1 else 'rosters'} after week "
                f"{week}'s first kickoff, so the stored week {week} rosters are no longer the rosters at kickoff")
    return None


def history_roster_problem(period_rows: list[dict], week: int, synced_at: datetime, kickoff: datetime) -> str | None:
    """Why a roster_history snapshot synced at `synced_at` (before `week`'s first kickoff) is not the rosters at
    kickoff, or None when nothing shows a move in between. The evidence is the week's lineup_snapshots rows: a
    player there acquired at or after the snapshot and before kickoff joined a roster the snapshot does not show.
    (A player added in that gap and dropped again before the week's last sync leaves no trace there.)"""
    late = sorted({r["team_id"] for r in period_rows
                   if (d := _utc(r.get("acquisition_date"))) is not None and synced_at <= d < kickoff})
    if not late:
        return None
    n = len(late)
    return (f"{n} {'team' if n == 1 else 'teams'} changed {'its roster' if n == 1 else 'rosters'} between the last "
            f"roster snapshot stored before week {week}'s first kickoff and that kickoff")


def _history_roster(con, st: LeagueState, week: int, kickoff: datetime) -> tuple[list[dict], str | None]:
    """Rows of the newest roster_history snapshot synced before `week`'s first kickoff and after the previous
    week's first kickoff (an older one predates that week's waivers), and its synced_at. ([], None) if none."""
    if not table_exists(con, "roster_history"):
        return [], None
    floor = pf.first_kickoffs(con, st.season).get(week - 1)
    snaps = fetch_dicts(con, """SELECT snapshot_date, max(synced_at) AS synced_at FROM roster_history
                                WHERE league_id = ? AND season = ? GROUP BY snapshot_date
                                ORDER BY snapshot_date DESC""", [st.league_id, st.season])
    pick = next((s for s in snaps if (t := _utc(s["synced_at"])) is not None and t < kickoff
                 and (floor is None or t > floor)), None)
    if pick is None:
        return [], None
    rows = fetch_dicts(con, """SELECT h.team_id, h.espn_player_id, h.lineup_slot_id, fp.full_name, fp.position,
                                      fp.pro_team, fp.injury_status
                               FROM roster_history h JOIN fantasy_players fp USING (espn_player_id)
                               WHERE h.snapshot_date = ? AND h.league_id = ? AND h.season = ?
                               ORDER BY h.team_id, h.espn_player_id""",
                       [pick["snapshot_date"], st.league_id, st.season])
    return rows, pick["synced_at"]


def load_previous(con, race: RaceData) -> Previous:
    """The previous edition's point-in-time league state (week E - 1), or why it cannot be rebuilt.
    Rosters: the newest roster_history snapshot before P's first kickoff when nothing shows a later move
    (history_roster_problem); otherwise lineup_snapshots at P when kickoff_roster_problem accepts them."""
    st = race.state
    if st.start_week is None:
        return Previous(None, None, "the season has no remaining weeks")
    week = st.start_week - 1
    if week < 1:
        return Previous(None, None, "there is no earlier week")
    kickoff = pf.first_kickoffs(con, st.season).get(week)
    if kickoff is None:
        return Previous(week, None, f"week {week}'s first kickoff time is unknown")
    snapshots = table_exists(con, "lineup_snapshots")
    label = "l.lineup_slot" if snapshots and "lineup_slot" in table_columns(con, "lineup_snapshots") else \
        "NULL AS lineup_slot"      # the taxi label (reserve_slots.is_taxi); absent = read as before
    period = fetch_dicts(con, f"""SELECT l.team_id, l.espn_player_id, l.lineup_slot_id, {label}, fp.full_name,
                                         fp.position, fp.pro_team, coalesce(fp.injury_status, l.injury_status) AS injury_status,
                                         l.acquisition_date, l.synced_at
                                  FROM lineup_snapshots l JOIN fantasy_players fp USING (espn_player_id)
                                  WHERE l.league_id = ? AND l.season = ? AND l.scoring_period = ?
                                  ORDER BY l.team_id, l.espn_player_id""", [st.league_id, st.season, week]) \
        if snapshots else None
    rows, synced_at = _history_roster(con, st, week, kickoff)
    gap = history_roster_problem(period or [], week, _utc(synced_at), kickoff) if rows else None
    if rows and not gap:
        source = "roster_history"
    else:
        if period is None:
            return Previous(week, None, gap or "no weekly roster snapshots are stored")
        if not period:
            return Previous(week, None, gap or f"no roster snapshot is stored for week {week}")
        problem = kickoff_roster_problem(period, week, kickoff)
        if problem:
            return Previous(week, None, gap or problem)
        rows, source = period, "lineup_snapshots"
        synced_at = max((r["synced_at"] for r in period if r.get("synced_at") is not None), default=None)
    rosters: dict[int, list[RosterPlayer]] = {t: [] for t in st.rosters}
    for row in rows:
        rosters.setdefault(row["team_id"], []).append(RosterPlayer(
            row["espn_player_id"], row["position"], row["pro_team"], row["full_name"],
            row["lineup_slot_id"] == IR_SLOT, row["injury_status"], on_taxi=is_taxi(row)))   # roster_history: no label
    proj, snap, synced, reason = _prior_projections(con, st.season, week, st.final_week, kickoff)
    if reason:
        return Previous(week, None, reason)
    then = {p.espn_player_id for ps in rosters.values() for p in ps}
    universe = {p.espn_player_id: p for p in st.free_agents}
    universe.update({p.espn_player_id: p for ps in st.rosters.values() for p in ps})
    free_agents = [replace(p, unavailable_weeks=frozenset(), on_ir=False, on_taxi=False) for pid, p in sorted(universe.items())
                   if pid not in then]
    prior = replace(st, rosters=rosters, free_agents=free_agents, projections=proj, start_week=week,
                    availability={}, first_projection_week=week)
    return Previous(week, prior, None, snap, synced, source, _iso(synced_at))


# ------------------------------------------------------------------ build --

def _iso(t):
    return t.isoformat() if hasattr(t, "isoformat") else t


def load_power_inputs(con, now: datetime | None = None) -> tuple[RaceData, Previous, dict]:
    race = load_race_data(con, now)
    return race, load_previous(con, race), load_extras(con, race)


def _component_row(k: str, t: int, cur: dict, sc: dict, prev_ed: dict | None, prev_sc: dict | None) -> dict:
    p = prev_ed["teams"].get(t) if prev_ed else None
    return {"value": cur["teams"][t][k], "score": sc["scores"][k].get(t), "rank": sc["component_ranks"][k].get(t),
            "previous_value": p[k] if p else None,
            "previous_score": prev_sc["scores"][k].get(t) if prev_sc else None}


def build_power_rankings(race: RaceData, previous: Previous | None, extras: dict | None = None,
                         team_id: int | None = None, kdst_streaming: bool = True) -> dict:
    """Read-only. Raises NotFound for an unknown team_id; NotSynced is raised by the loaders."""
    if not race.teams:
        raise NotSynced("No fantasy teams synced for the league.")
    meta = {t["team_id"]: t for t in race.teams}
    if team_id is not None and team_id not in meta:
        raise NotFound(f"Unknown team {team_id}")
    focus = team_id if team_id is not None else next((t["team_id"] for t in race.teams if t["is_mine"]), None)
    team_ids = sorted(meta)
    names = {t: m["name"] for t, m in meta.items()}
    state = with_streaming(race.state) if kdst_streaming else race.state
    week = state.start_week
    regular, _, _ = classify_matchups(race, {})
    extras = extras or {}
    out = {"contract": CONTRACT,
           "league": {k: race.league.get(k) for k in ("league_id", "season", "name", "current_week",
                                                      "last_regular_season_week", "final_scoring_period")},
           "focus_team_id": focus, "kdst_streaming": kdst_streaming,
           "components": [{"key": k, "label": LABELS[k], "unit": UNITS[k], "weight": WEIGHTS[k],
                           "description": DESCRIPTIONS[k]} for k in COMPONENTS],
           "method": METHOD,
           "freshness": {"league_synced_at": _iso(extras.get("league_synced_at")),
                         "projections_synced_at": _iso(extras.get("projections_synced_at"))}}
    if week is None:
        return {**out, "week": None, "empty": True, "teams": [], "weights": WEIGHTS,
                "message": "No remaining fantasy weeks: every week has kicked off, so there is no lineup to rank.",
                "previous": {"available": False, "week": None, "reason": "the season has no remaining weeks"}}

    cur = edition(state, regular, team_ids, names, None)
    sc = score_edition(cur)
    prev_ed = prev_sc = None
    prev_info = {"available": False, "week": previous.week if previous else None,
                 "reason": previous.reason if previous else "not loaded"}
    if previous is not None and previous.state is not None:
        pstate = with_streaming(previous.state) if kdst_streaming else previous.state
        prev_ed = edition(pstate, regular, team_ids, names, previous.week, detail=False)
        prev_sc = score_edition(prev_ed)
        if prev_sc["ranks"]:
            prev_info = {"available": True, "week": previous.week, "reason": None,
                         "projection_snapshot": previous.projection_snapshot,
                         "projection_synced_at": previous.projection_synced_at,
                         "roster_week": previous.week, "roster_source": previous.roster_source,
                         "roster_synced_at": previous.roster_synced_at,
                         "comparable_weeks": prev_ed["comparable_weeks"],
                         "all_play_periods": prev_ed["all_play_periods"], "weights": prev_sc["weights"]}
        else:
            prev_ed = prev_sc = None
            prev_info["reason"] = f"no team had a complete week {previous.week} projection in the stored snapshot"
    moves = movement(sc["ranks"], prev_sc["ranks"] if prev_sc else None)

    pos = cur["positions"]
    groups = [g for g in dict.fromkeys(state.slots)]
    rows = []
    for t in team_ids:
        x, m = cur["teams"][t], meta[t]
        depth, repl = x["_depth"], x["_replacement"]
        strengths = []
        if pos and t in sc["ranks"]:
            for g in groups:
                rk = pos["all"][g]["ranks"].get(t)
                if rk is not None:
                    strengths.append({"slot": g, "rank": rk, "average": pos["all"][g]["average"].get(t)})
        ordered = sorted(strengths, key=lambda s: (s["rank"], groups.index(s["slot"])))
        rows.append({
            "team_id": t, "name": m["name"], "abbrev": m.get("abbrev"), "is_mine": bool(m["is_mine"]),
            "rank": sc["ranks"].get(t), "previous_rank": prev_sc["ranks"].get(t) if prev_sc else None,
            "movement": moves.get(t), "power_score": sc["composite"].get(t),
            "previous_power_score": prev_sc["composite"].get(t) if prev_sc else None,
            "record": {"wins": m.get("wins"), "losses": m.get("losses"), "ties": m.get("ties") or 0},
            "points_for": m.get("points_for"),
            "components": {k: _component_row(k, t, cur, sc, prev_ed, prev_sc) for k in COMPONENTS},
            "all_play": {**x["all_play_record"], "pct": x["all_play"]},
            "depth": {k: depth[k] for k in ("share", "starter_weeks")} | {"key_players": depth["players"][:3]}
            if depth else None,
            "replacement": {k: repl[k] for k in ("positions", "covered", "of")} if repl else None,
            "positions": {"strongest": ordered[:2], "weakest": ordered[::-1][:2]} if ordered else None,
            "unranked_reason": x["unranked_reason"],
        })
    rows.sort(key=lambda x: (x["rank"] is None, x["rank"] or 0, -(x["components"]["strength"]["value"] or 0),
                             x["team_id"]))
    window_note = None
    if cur["excluded_weeks"]:
        n = len(cur["excluded_weeks"])
        why = (f"{slots_left_out(cur['unsupported_slots'])}, so no lineup total is complete"
               if cur.get("unsupported_slots") else "some team's projection is incomplete there")
        window_note = (f"{n} remaining {'week is' if n == 1 else 'weeks are'} left out of lineup strength, depth and "
                       f"replacement because {why}.")
    return {**out, "week": week, "empty": not sc["ranks"], "teams": rows, "weights": sc["weights"],
            "window": {"weeks": cur["weeks"], "comparable_weeks": cur["comparable_weeks"],
                       "excluded_weeks": cur["excluded_weeks"], "note": window_note,
                       "all_play_periods": cur["all_play_periods"]},
            "previous": prev_info,
            "message": None if sc["ranks"] else
            (f"No team can be ranked: {slots_left_out(cur['unsupported_slots'])}, so no lineup total is complete."
             if cur.get("unsupported_slots") else "No team has a complete lineup projection to rank.")}
