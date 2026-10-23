"""Multi-week kicker and D/ST streaming planner (issue #181), read-only (contract kdst-streaming.v1).

For each position the league starts (K, D/ST) and the next 2-4 actionable weeks: which free agent to stream into the
one slot each week, or whether to keep the current roster, with the alternatives, the gaps and the reasons. Computed on
request from the synced league (rosters, free agents and their status, the league's weekly projections as the trade and
waiver engines load them: trade_data.load_league_state), the NFL schedule (byes, opponents) and the stored K / D/ST
model runs (kicker_store / dst_store, shown beside the plan, never its input). Nothing is written, synced or fetched.

Candidates. A free agent (provider status FREEAGENT or WAIVERS at the last sync, on no roster) at the position, with an
NFL team and no open injury designation. A rostered player is never a candidate. Each candidate-week is one of

  plays    the team has a game and the player has a projection (a projected 0 is a measured 0);
  bye      the team has no game and its regular-season schedule is complete: he scores 0 and cannot be added that week;
  out      verified NFL-ineligible that week (player_availability): the same;
  unknown  no NFL schedule for the week or the team, or a game without a projection. Never a 0: an unknown week can
           neither be planned nor held through.

Eligibility is kept apart from points. A player kept from an earlier planned add is `held`. Only this week's add of a player listed FREEAGENT by ESPN is `free_agent`
(confirmed at the last sync); a WAIVERS player needs a claim (`waiver_claim`); a later week's add assumes he is still
available then (`hypothetical`); a provider that does not publish waiver status (Sleeper lists every unrostered player
as available) is `unconfirmed`.

Sequence. One streaming slot per position. The state of a week is who holds the slot: the current roster (`hold`; an
empty slot when the team rosters nobody at the position) or one candidate. Moving to a different candidate is one
add/drop; the current roster's player, once dropped, is not planned back. A player can be added only in a week he
plays, and is never held through a week whose value is unknown; holding him through a bye leaves the slot empty (a
listed gap, 0). Objective: projected points minus `move_cost` per add/drop, with at most `max_moves` add/drops.

Solver: dynamic programming over weeks x holder x add/drops used, with the best two predecessors per add/drop count so a
switch is O(1): O(weeks x candidates x moves), never a search over tied values. Ties prefer fewer add/drops, then
staying, then the lower candidate index (candidates are ordered by projection then id), so the result is deterministic.
The candidate pool is bounded (MAX_POOL per position, the best by weekly projection); `solve` itself is polynomial for
any pool.

Horizon. A week is planned only if the NFL schedule is loaded for it and at least MIN_PROJECTED candidates are projected
there, covering at least MIN_COVERAGE of the candidates with a game. The plan stops at the first week that fails: that
week and every later one are listed as beyond the data horizon, with no player and no number. Weeks two or more weeks
out are flagged `outlook` (future K and D/ST projections are matchup-driven and noisy).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..db.connection import fetch_dicts
from . import dst_store, kicker_store
from .bye_planner import OPEN_DESIGNATIONS, load_schedule_facts
from .schedule_intelligence import NotFound, NotSynced, _json, _phase, league_info
from .team_lineups import (
    STREAM_POSITIONS,
    STREAM_RANK,
    projection,
    r,
    source_label,
    streaming_baseline,
)

CONTRACT = "kdst-streaming.v1"
MIN_WEEKS, MAX_WEEKS, DEFAULT_WEEKS = 2, 4, 3
MAX_MOVES_LIMIT = MAX_WEEKS          # one add/drop a week at most is ever useful
MAX_MOVE_COST = 10.0
MAX_POOL = 40                        # candidates per position the solver sees (best weekly projections first)
MIN_PROJECTED = STREAM_RANK          # the streaming baseline needs its #3 player: fewer projected = thin week
MIN_COVERAGE = 0.5                   # projected share of the candidates with a game
OUTLOOK_FROM = 2                     # weeks this far ahead of the first actionable week are an outlook
ALTERNATIVES = 3
EXCLUDED_LISTED = 20
OFFENSE = ("QB", "RB", "WR", "TE", "K")

PLAYS, BYE, OUT, UNKNOWN, EMPTY = "plays", "bye", "out", "unknown", "empty"
HOLD = "hold"

ELIGIBILITY = {
    "free_agent": "Listed as a free agent at the last sync: can be added now, before his game locks.",
    "waiver_claim": "On waivers at the last sync: needs a claim, and waiver order decides who gets him.",
    "hypothetical": "A later week's add: assumes he is still available then. Not confirmed.",
    "unconfirmed": "The provider does not publish waiver status: availability is not confirmed.",
    "held": "Held from an earlier week of the plan: no new add/drop.",
}
NOTES = [
    ("Only free agents count: a player on any roster is never a candidate. Availability is the provider's status at "
     "the last sync; claims by other managers before your move are not predicted."),
    "One streaming slot per position. Adding a different player is one add/drop; which player to drop is your call.",
    "A missing projection is unknown, never 0: such a week is not planned, and a player is not held through it.",
    "A bye is a real 0. A player is never added for his bye week; holding him through it leaves the slot empty.",
    ("Future K and D/ST projections are matchup-driven and noisy, so weeks two or more ahead are an outlook, and the "
     "plan stops at the first week with too few projected free agents."),
    ("The model columns (Fantasy Intel's kicker and D/ST runs) are shown for comparison; the plan uses the league's "
     "projections."),
]


# ------------------------------------------------------------------------------------------------ cells --

@dataclass(frozen=True)
class Cell:
    """One holder-week: state (plays | bye | out | unknown | empty) and points (None = unknown, never 0)."""
    state: str
    points: float | None
    detail: str | None = None
    code: str | None = None       # why: no_team | schedule_missing | team_schedule_incomplete | no_projection | ...

    @property
    def known(self) -> bool:
        return self.points is not None

    @property
    def addable(self) -> bool:
        return self.state == PLAYS and self.points is not None


SCHEDULE_UNKNOWN = {"schedule_missing", "team_schedule_incomplete"}


def player_cell(pro_team: str | None, unavailable: frozenset, pid: int, week: int, sched: dict, proj) -> Cell:
    if not pro_team:
        return Cell(UNKNOWN, None, "no current NFL team", "no_team")
    if week not in sched["covered"]:
        return Cell(UNKNOWN, None, f"no NFL schedule loaded for week {week}", "schedule_missing")
    if (pro_team, week) not in sched["games"]:
        if pro_team in sched["complete_teams"]:
            return Cell(BYE, 0.0, f"{pro_team} has no game in week {week}", "bye")
        return Cell(UNKNOWN, None, f"{pro_team}'s schedule is incomplete, so a missing game is not a confirmed bye",
                    "team_schedule_incomplete")
    if week in unavailable:
        return Cell(OUT, 0.0, "NFL reserve list (minimum games not met)", "nfl_ineligible")
    v = projection(proj, pid, week)
    if v is None:
        return Cell(UNKNOWN, None, f"no projection for week {week}", "no_projection")
    return Cell(PLAYS, v)


# ----------------------------------------------------------------------------------------------- solver --

@dataclass(frozen=True)
class Plan:
    holders: tuple[int, ...]      # per week: 0 = hold (current roster / empty slot), i >= 1 = candidate i - 1
    moves: int
    points: float                 # projected points of the slot over the weeks
    score: float                  # points - cost x moves


def _better(a: tuple, b: tuple | None) -> bool:
    """Strictly better DP entry: higher score (to 1e-9), then fewer moves, then lower holder / predecessor index."""
    if b is None:
        return True
    if abs(a[0] - b[0]) > 1e-9:
        return a[0] > b[0]
    return a[1:] < b[1:]


def solve(hold: list[Cell], cand: list[list[Cell]], max_moves: int | None = None, cost: float = 0.0) -> Plan | None:
    """Best one-slot sequence (module docstring). `hold[t]` is the current roster's cell in week t, `cand[i][t]`
    candidate i's. None when no sequence is feasible (e.g. holding is unknown and no add/drop is allowed).

    best[s][m] = (score, back) for holder s (0 = hold, i + 1 = candidate i) after m add/drops. A candidate is entered
    only in a week he plays (addable) and held only through weeks with a known value; hold is never re-entered."""
    weeks, n = len(hold), len(cand)
    if weeks == 0:
        return None
    cap = weeks if max_moves is None else max(0, min(max_moves, weeks))
    cell = lambda s, t: hold[t] if s == 0 else cand[s - 1][t]
    NEG = None
    layers: list[list[list]] = []
    prev = [[NEG] * (cap + 1) for _ in range(n + 1)]
    prev[0][0] = (0.0, 0.0)               # the virtual start: holding before week 0, no add/drop
    for t in range(weeks):
        # Best two predecessors per add/drop count, so a switch into s skips s itself in O(1).
        top: list[list[tuple]] = []
        for m in range(cap + 1):
            two: list[tuple] = []                 # (score, holder), best first; ties keep the lower holder
            for s in range(n + 1):
                e = prev[s][m]
                if e is None:
                    continue
                if not two or _better((e[0], s), (two[0][0], two[0][1])):
                    two = [(e[0], s)] + two[:1]
                elif len(two) < 2 or _better((e[0], s), (two[1][0], two[1][1])):
                    two = [two[0], (e[0], s)]
            top.append(two)
        cur = [[NEG] * (cap + 1) for _ in range(n + 1)]
        for s in range(n + 1):
            c = cell(s, t)
            if not c.known:
                continue
            pts = c.points
            for m in range(cap + 1):
                best = None
                stay = prev[s][m]
                if stay is not None:
                    best = (stay[0] + pts, 0, -1, (s, m))          # staying ranks before any switch on a tie
                if s > 0 and m > 0 and c.addable:
                    for score, p in top[m - 1]:
                        if p == s:
                            continue
                        cand_entry = (score + pts - cost, 1, p, (p, m - 1))
                        if best is None or _better(cand_entry[:3], best[:3]):
                            best = cand_entry
                        break
                if best is not None:
                    cur[s][m] = (best[0], best[3])
        layers.append(cur)
        prev = cur
    end = None
    for s in range(n + 1):
        for m in range(cap + 1):
            e = prev[s][m]
            if e is not None and (end is None or _better((e[0], m, s), (end[0], end[1], end[2]))):
                end = (e[0], m, s)
    if end is None:
        return None
    score, moves, s = end
    holders, m = [], moves
    for t in range(weeks - 1, -1, -1):
        holders.append(s)
        s, m = layers[t][s][m][1]
    holders.reverse()
    points = sum(cell(h, t).points for t, h in enumerate(holders))
    return Plan(tuple(holders), moves, points, score)


# ------------------------------------------------------------------------------------------------ build --

def _provider(con, league_id, season) -> str:
    rows = fetch_dicts(con, "SELECT settings_raw FROM leagues WHERE league_id = ? AND season = ?", [league_id, season])
    settings = (_json(rows[0]["settings_raw"]) if rows else None) or {}
    return "sleeper" if isinstance(settings, dict) and settings.get("provider") == "sleeper" else "espn"


def _model_rows(con, season: int, position: str, weeks: list[int]) -> dict:
    """{week: {"run": {...} | None, "reason": str | None, "rows": {pid: row}}} from the stored, verified model runs."""
    store = kicker_store if position == "K" else dst_store
    out = {}
    for w in weeks:
        sel = store.select_run(con, season, w)
        run = sel["run"]
        out[w] = {"run": None if run is None else {
                      "model_version": run["model_version"], "forecast_horizon": int(run.get("forecast_horizon") or 0),
                      "artifact_verified": run.get("artifact_check") == "verified"},
                  "reason": sel["unavailable_reason"],
                  "rows": {row["espn_player_id"]: row for row in (sel["rows"] or [])}}
    return out


def _model_cell(models: dict, week: int, pid: int, position: str) -> dict | None:
    m = models.get(week) or {}
    if m.get("run") is None:
        return None
    row = m["rows"].get(pid)
    if row is None:
        return {"expected_points": None, "coverage_status": "not_in_run", "implied_total": None,
                "model_version": m["run"]["model_version"], "forecast_horizon": m["run"]["forecast_horizon"]}
    ctx = row.get("context") if isinstance(row.get("context"), dict) else {}
    implied = ((ctx.get("team") or {}).get("o_implied") if position == "K" else ctx.get("opponent_implied_total"))
    exp = row.get("expected_points")
    return {"expected_points": r(float(exp)) if exp is not None and row.get("coverage_status") == "projected" else None,
            "coverage_status": row.get("coverage_status"),
            "implied_total": r(float(implied)) if implied is not None else None,
            "model_version": m["run"]["model_version"], "forecast_horizon": m["run"]["forecast_horizon"]}


def _game(sched: dict, team: str | None, week: int) -> dict:
    g = sched["games"].get((team, week)) if team else None
    return {"opponent": (g or {}).get("opponent"), "home": (g or {}).get("home"), "kickoff": (g or {}).get("kickoff")}


def _conflicts(position: str, opponent: str | None, roster: list) -> list[dict]:
    """Your own rostered players this start plays against: a D/ST facing your offense, a kicker facing your D/ST."""
    if not opponent:
        return []
    against = OFFENSE if position == "D/ST" else ("D/ST",)
    return [{"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position}
            for p in roster if p.position in against and p.pro_team == opponent and not p.reserved]


def _eligibility(status: str | None, provider: str, horizon: int, re_add: bool, held: bool = False) -> str:
    if held:
        return "held"
    if provider != "espn":
        return "unconfirmed"
    if horizon > 0 or re_add:
        return "hypothetical"
    return "waiver_claim" if status == "WAIVERS" else "free_agent"


def plan_position(position: str, weeks: list[int], *, free_agents: list, roster: list, statuses: dict, proj,
                  sched: dict, provider: str, models: dict, max_moves: int | None, cost: float,
                  phase_of, slot_count: int) -> dict:
    """The plan, horizon, current-week advice and alternatives for one position (pure apart from its inputs)."""
    incumbents = sorted((p for p in roster if p.position == position and not p.reserved),
                        key=lambda p: p.espn_player_id)
    pool, excluded = [], []
    for p in sorted((p for p in free_agents if p.position == position), key=lambda p: p.espn_player_id):
        status = (p.injury_status or "").upper()
        if status in OPEN_DESIGNATIONS:
            excluded.append((p, f"listed {status.replace('_', ' ').lower()} at the last sync; how long is not published"))
        elif not p.pro_team:
            excluded.append((p, "no current NFL team"))
        else:
            pool.append(p)
    cells = {p.espn_player_id: [player_cell(p.pro_team, p.unavailable_weeks, p.espn_player_id, w, sched, proj)
                                for w in weeks] for p in pool}
    level, ref = streaming_baseline(pool, [position], weeks, proj)

    # Horizon: the first week that is thin ends the plan.
    meta, planned = [], []
    for t, w in enumerate(weeks):
        # Every candidate not on a confirmed bye or verified out, including those whose game is unknown: a partly
        # loaded week must not shrink the denominator to the teams that happen to be loaded.
        with_game = [p for p in pool if cells[p.espn_player_id][t].state in (PLAYS, UNKNOWN)]
        projected = [p for p in with_game if cells[p.espn_player_id][t].addable]
        unsched = sorted({p.pro_team for p in pool if cells[p.espn_player_id][t].code in SCHEDULE_UNKNOWN})
        loaded = w in sched["covered"]
        reason = None
        if not loaded:
            reason = f"No NFL schedule is loaded for week {w}, so byes and opponents are unknown."
        elif unsched:
            # Strict: a week is planned only when every candidate's game or bye that week is established.
            reason = (f"The NFL schedule for week {w} is incomplete ({', '.join(unsched[:6])}"
                      f"{f' and {len(unsched) - 6} more' if len(unsched) > 6 else ''}): games and byes are unknown.")
            loaded = False
        elif len(projected) < MIN_PROJECTED:
            n_text = ("No free agent is" if not projected else
                      f"Only {len(projected)} free {'agent is' if len(projected) == 1 else 'agents are'}")
            reason = f"{n_text} projected for week {w} (at least {MIN_PROJECTED} needed)."
        elif len(projected) < MIN_COVERAGE * len(with_game):
            reason = (f"Only {len(projected)} of {len(with_game)} free agents not on a bye are projected for week {w}.")
        thin = reason is not None or (planned != list(weeks[:t]))
        if reason is None and thin:
            reason = "Beyond the data horizon: an earlier week is not planned."
        meta.append({"week": w, "horizon": t, "phase": phase_of(w), "outlook": t >= OUTLOOK_FROM,
                     "schedule_loaded": loaded, "candidates_with_game": len(with_game),
                     "projected_candidates": len(projected), "planned": not thin, "reason": reason,
                     "baseline": ({"points": r(level[position][w]), **{k: ref[position][w][k] for k in
                                  ("espn_player_id", "name", "pro_team", "rank", "available_projected")}}
                                  if w in level.get(position, {}) and not thin else None)})
        if not thin:
            planned.append(w)
    T = len(planned)

    # Bounded pool: each planned week's best projections in turn (1st of every week, then 2nd, ...), so every week
    # keeps its own top players; the solver order is by best weekly projection, then id.
    usable = [p for p in pool if any(c.addable for c in cells[p.espn_player_id][:T])]
    used_ids = {p.espn_player_id for p in usable}
    unprojected = [p for p in pool if p.espn_player_id not in used_ids]
    by_week = [sorted((p for p in usable if cells[p.espn_player_id][t].addable),
                      key=lambda p: (-cells[p.espn_player_id][t].points, p.espn_player_id)) for t in range(T)]
    picked: dict[int, object] = {}
    for k in range(max((len(b) for b in by_week), default=0)):
        for b in by_week:
            if k < len(b) and len(picked) < MAX_POOL:
                picked.setdefault(b[k].espn_player_id, b[k])
        if len(picked) >= MAX_POOL:
            break

    def rank_key(p):
        known = [c.points for c in cells[p.espn_player_id][:T] if c.addable]
        return (-max(known), -sum(known), p.espn_player_id)
    chosen = sorted(picked.values(), key=rank_key)

    # The current roster's cell per week: the best incumbent whose week is known. An incumbent with an open designation
    # or an unknown week is left out (named in `detail`); the hold is unknown only when no incumbent is known.
    def hold_of(t: int):
        w = weeks[t]
        if not incumbents:
            return Cell(EMPTY, 0.0, "no player rostered at the position", "empty"), None
        known, skipped = [], []
        for p in incumbents:
            c = player_cell(p.pro_team, p.unavailable_weeks, p.espn_player_id, w, sched, proj)
            label = p.name or str(p.espn_player_id)
            if (p.injury_status or "").upper() in OPEN_DESIGNATIONS:
                skipped.append(f"{label} is listed {p.injury_status.replace('_', ' ').lower()}")
            elif not c.known:
                skipped.append(f"{label}: {c.detail}")
            else:
                known.append((c, p))
        note = f"not counted: {'; '.join(skipped)}" if skipped else None
        if not known:
            return Cell(UNKNOWN, None, note, "no_known_incumbent"), None
        c, p = max(known, key=lambda cp: (cp[0].points, cp[0].state == PLAYS, -cp[1].espn_player_id))
        detail = "; ".join(x for x in (c.detail, note) if x) or None
        return Cell(c.state, c.points, detail, c.code), p
    holds = [hold_of(t) for t in range(len(weeks))]
    hold = [c for c, _ in holds]
    best_hold = [p for _, p in holds]

    plan = solve(hold[:T], [cells[p.espn_player_id][:T] for p in chosen], max_moves, cost) if T else None

    def person(p) -> dict:
        return {"espn_player_id": p.espn_player_id, "name": p.name, "pro_team": p.pro_team,
                "injury_status": p.injury_status, "provider_status": statuses.get(p.espn_player_id)}

    def start_row(p, t: int, c: Cell, *, re_add: bool = False, action: str | None = None) -> dict:
        w = weeks[t]
        g = _game(sched, p.pro_team, w)
        return {**person(p), "points": r(c.points), "state": c.state, "detail": c.detail, **g,
                "eligibility": _eligibility(statuses.get(p.espn_player_id), provider, t, re_add, action == "keep"),
                "conflicts": _conflicts(position, g["opponent"], roster), "model": _model_cell(models, w, p.espn_player_id, position),
                **({"action": action} if action else {})}

    out_weeks = []
    seen_before: set[int] = set()
    for t, w in enumerate(weeks):
        row = {**meta[t]}
        h = hold[t]
        inc = best_hold[t]
        if t >= T or plan is None:
            # Not planned: no player and no number (the hold included), only the week's reason.
            row["hold"] = {"player": None, "points": None, "state": None, "detail": None, "opponent": None,
                           "home": None, "kickoff": None}
            row.update(action=None, start=None, alternatives=[], gap=None, reasons=[])
            out_weeks.append(row)
            continue
        row["hold"] = {"player": person(inc) if inc else None, "points": r(h.points), "state": h.state,
                       "detail": h.detail, **(_game(sched, inc.pro_team, w) if inc else
                                               {"opponent": None, "home": None, "kickoff": None})}
        s = plan.holders[t]
        prev_s = plan.holders[t - 1] if t else 0
        if s == 0:
            action = "hold"
            start = None
            pts = h.points
        else:
            p = chosen[s - 1]
            c = cells[p.espn_player_id][t]
            action = "keep" if prev_s == s else "add"
            start = start_row(p, t, c, re_add=action == "add" and p.espn_player_id in seen_before, action=action)
            pts = c.points
            seen_before.add(p.espn_player_id)
        if s and prev_s and prev_s != s:
            seen_before.add(chosen[prev_s - 1].espn_player_id)
        planned_id = chosen[s - 1].espn_player_id if s else None
        alts = sorted(((cells[p.espn_player_id][t].points, p) for p in usable
                       if p.espn_player_id != planned_id and cells[p.espn_player_id][t].addable),
                      key=lambda vp: (-vp[0], vp[1].espn_player_id))[:ALTERNATIVES]
        base = level.get(position, {}).get(w)
        gap = None
        if pts is not None and pts == 0 and (s == 0 and h.state in (BYE, OUT, EMPTY) or s and start["state"] in (BYE, OUT)):
            gap = {"code": "empty_slot", "text": (f"Week {w}: the slot is empty (" +
                                                  (start["detail"] if s else h.detail or "no player") + ").")}
        elif base is not None and pts is not None and pts < base:
            gap = {"code": "below_baseline", "text": f"Week {w}: {r(base - pts)} below the streaming baseline."}
        reasons = []
        if action == "hold":
            reasons.append(f"Keep your current {position}" + (f" ({inc.name})" if inc else "")
                           + (f": {r(h.points)} projected." if h.state == PLAYS else f": {h.detail}."))
        elif action == "keep":
            reasons.append(f"Keep {start['name']}: {r(pts)} projected, no add/drop.")
        else:
            reasons.append(f"Add {start['name']}: {r(pts)} projected"
                           + (f", {r(pts - h.points):+} vs keeping your current {position}"
                              if h.known and incumbents else "") + ".")
        if base is not None and pts is not None:
            reasons.append(f"Streaming baseline (#{ref[position][w]['rank']} available): {r(base)}.")
        if start and start["conflicts"]:
            reasons.append("Plays against your " + ", ".join(f"{x['name']} ({x['position']})" for x in start["conflicts"])
                           + ".")
        if row["outlook"]:
            reasons.append(f"Outlook: {t} weeks ahead, where K and D/ST projections are least reliable.")
        row.update(action=action, start=start, gap=gap, reasons=reasons,
                   points=r(pts), alternatives=[{**start_row(p, t, cells[p.espn_player_id][t]),
                                                 "delta": r(v - pts) if pts is not None else None} for v, p in alts])
        out_weeks.append(row)

    # The best streamer this week, apart from the plan (one week only), and the best confirmed one.
    this_week = None
    if weeks and meta[0]["planned"]:
        ranked = sorted(((cells[p.espn_player_id][0].points, p) for p in usable if cells[p.espn_player_id][0].addable),
                        key=lambda vp: (-vp[0], vp[1].espn_player_id))
        best = ranked[0][1] if ranked else None
        conf = next((p for _, p in ranked if _eligibility(statuses.get(p.espn_player_id), provider, 0, False)
                     == "free_agent"), None)
        this_week = {"week": weeks[0],
                     "best": start_row(best, 0, cells[best.espn_player_id][0]) if best else None,
                     "best_confirmed": (start_row(conf, 0, cells[conf.espn_player_id][0])
                                        if conf and best and conf.espn_player_id != best.espn_player_id else None),
                     "hold_points": r(hold[0].points)}

    hold_total = sum(c.points for c in hold[:T]) if T and all(c.known for c in hold[:T]) else None
    status = ("planned" if plan is not None else "no_plan") if T else "no_weeks"
    return {
        "position": position, "slots": slot_count, "status": status,
        "incumbents": [person(p) for p in incumbents],
        "weeks": out_weeks,
        "planned_weeks": planned, "capped_weeks": [m["week"] for m in meta if not m["planned"]],
        "plan": None if plan is None else {
            "moves": plan.moves, "move_cost": cost, "cost_total": r(plan.moves * cost), "projected_points": r(plan.points),
            "net_points": r(plan.score), "hold_points": r(hold_total),
            "gain_vs_hold": r(plan.score - hold_total) if hold_total is not None else None,
            "hold_unknown_weeks": [weeks[t] for t in range(T) if not hold[t].known]},
        "no_plan_reason": (None if plan is not None or not T else
                           "Keeping your current player cannot be evaluated (a week has no projection) and no add/drop "
                           "is allowed." if max_moves == 0 else "No feasible streaming sequence for these weeks."),
        "this_week": this_week,
        "pool": {"free_agents": len(pool) + len(excluded), "candidates": len(chosen), "cap": MAX_POOL,
                 "unprojected": len(unprojected), "excluded": len(excluded)},
        "excluded": [{**person(p), "reason": why} for p, why in excluded[:EXCLUDED_LISTED]],
        "model": {str(w): {"run": (models.get(w) or {}).get("run"), "reason": (models.get(w) or {}).get("reason")}
                  for w in weeks},
    }


class BadParam(ValueError):
    """An out-of-range request parameter (the API answers 422 with this message)."""


def build_kdst_streaming(con, team_id: int | None = None, weeks: int = DEFAULT_WEEKS, max_moves: int | None = None,
                         move_cost: float = 0.0, now: datetime | None = None) -> dict:
    """Read-only. Raises NotSynced, NotFound or BadParam (an out-of-range parameter)."""
    from .trade_data import load_league_state

    if not MIN_WEEKS <= weeks <= MAX_WEEKS:
        raise BadParam(f"weeks must be between {MIN_WEEKS} and {MAX_WEEKS}")
    if max_moves is not None and not 0 <= max_moves <= MAX_MOVES_LIMIT:
        raise BadParam(f"max_moves must be between 0 and {MAX_MOVES_LIMIT}")
    if not 0 <= move_cost <= MAX_MOVE_COST:
        raise BadParam(f"move_cost must be between 0 and {MAX_MOVE_COST:g}")
    lg = league_info(con)
    state = load_league_state(con, now=now) if lg is not None else None
    if lg is None or state is None:
        raise NotSynced("League not synced yet (current/final scoring period unknown).")
    season = state.season
    teams = fetch_dicts(con, "SELECT team_id, name, abbrev, is_mine FROM fantasy_teams WHERE league_id = ? AND "
                             "season = ? ORDER BY team_id", [state.league_id, season])
    if team_id is None:
        mine = [t for t in teams if t["is_mine"]]
        if not mine:
            raise NotFound("No team is marked as yours (is_mine); pass team_id.")
        team_id = mine[0]["team_id"]
    if team_id not in {t["team_id"] for t in teams}:
        raise NotFound(f"Unknown team {team_id}")
    window = state.window()
    horizon = list(window.weeks[:weeks])
    sched = load_schedule_facts(con, season)
    provider = _provider(con, state.league_id, season)
    roster = state.rosters.get(team_id, [])
    phase_of = lambda w: _phase(w, lg["last_regular_season_week"])
    positions = []
    for pos in STREAM_POSITIONS:
        count = state.slots.count(pos)
        if not count:
            positions.append({"position": pos, "slots": 0, "status": "not_started", "weeks": [], "plan": None,
                              "this_week": None, "incumbents": [], "planned_weeks": [], "capped_weeks": [],
                              "no_plan_reason": f"The league does not start a {pos}.", "pool": None, "excluded": [],
                              "model": {}})
            continue
        positions.append(plan_position(pos, horizon, free_agents=state.free_agents, roster=roster,
                                       statuses=state.free_agent_status, proj=state.projections, sched=sched,
                                       provider=provider, models=_model_rows(con, season, pos, horizon),
                                       max_moves=max_moves, cost=float(move_cost), phase_of=phase_of, slot_count=count))
    reason = state.start_week_reason or {}
    return {
        "contract": CONTRACT,
        "league": {k: lg[k] for k in ("league_id", "season", "name", "current_week", "final_scoring_period",
                                      "last_regular_season_week")},
        "team": next(t for t in teams if t["team_id"] == team_id), "teams": teams,
        "query": {"team_id": team_id, "weeks": weeks, "max_moves": max_moves, "move_cost": float(move_cost)},
        "provider": provider,
        "projection_source": source_label(state.projection_source),
        "window": {"espn_current_week": int(lg["current_week"]), "first_actionable_week": state.start_week,
                   "weeks": horizon, "remaining_weeks": state.start_week is not None,
                   "uncertain": reason.get("uncertain"), "warning": reason.get("warning"),
                   "reason": reason.get("reason")},
        "schedule": {"available": sched["available"], "weeks_missing": [w for w in horizon if w not in sched["covered"]]},
        "positions": positions,
        "rules": {"min_projected": MIN_PROJECTED, "min_coverage": MIN_COVERAGE, "outlook_from": OUTLOOK_FROM,
                  "max_pool": MAX_POOL, "stream_rank": STREAM_RANK, "weeks": [MIN_WEEKS, MAX_WEEKS],
                  "max_moves_limit": MAX_MOVES_LIMIT, "max_move_cost": MAX_MOVE_COST, "eligibility": ELIGIBILITY},
        "notes": NOTES,
    }
