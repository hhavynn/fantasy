"""Asset-package balance and roster needs for Trade Discovery: an independent diagnostic, kept
apart from the canonical lineup delta. Pure (no database). See docs/v3_trade_intelligence.md.

The canonical trade value is what a deal does to each team's optimal starting lineups. That says
nothing about what each manager gives up as an asset: a player buried on one bench can still be a
starter anywhere else. This module measures assets independently of roster congestion:

  points above replacement (PAR) = sum over the remaining weeks the player is NFL-eligible of
                                   max(0, ESPN projection - replacement level at his position)

Replacement level is what a manager can actually get for free: the best currently available free
agent's ESPN projection at that position that week (a streaming baseline, also NFL-availability
aware). Where no free agent at the position is projected that week, the league-structure level is
used instead: the projection of the first player beyond the league's starters at that position that
week (see "league starters" below). Units are projected fantasy points above replacement over the
evaluation window; nothing is weighted, no star premium is added, positions are never compared on
raw points.

League starters at a position come from one league reference assignment (league_reference): every
team's exact optimal projected lineup each week (team_lineups.project_team, i.e. lineup.assign_lineup
over the league's real slots, the same lineups the trade evaluator scores). A fixed slot (QB, RB, WR,
TE, K, D/ST) is demand for its own position, teams x count, whether or not it is filled. Each flexible
slot (FLEX, RB/WR, WR/TE, OP) is counted once, for the position of the player who actually fills it in
that team-week, and not at all when it is empty: a superflex OP filled by a QB adds a QB starter, one
filled by an RB adds an RB starter, never both. The weekly counts set the league-structure replacement
level; their mean over the window (flex seats apportioned by largest remainder, never more than the
mean filled flex slots, rounded) is the season count used for starter-caliber ranks. Flex demand is
dynamic: it moves with projections, byes, injuries and roster changes. A league with no flexible slot
gets exactly teams x fixed slots, as before.

A player is "starter-caliber" when his PAR ranks within the league's starters at his position (K and
D/ST included only when they are traded). Package labels use ratios of PAR with display thresholds
stated below. They are heuristic, not empirically calibrated and not a market price; the labels never
change the canonical delta and never hide a trade from the manual analyzer.
"""

from __future__ import annotations

from collections import Counter
from statistics import median

from .lineup import FIXED_SLOTS, is_flex
from .player_availability import ELIGIBLE_UNCONFIRMED, INELIGIBLE, NO_WEEK_LEFT, UNKNOWN
from .team_lineups import RosterPlayer, Window, lineup_relevant, project_team, projection, r, slot_filled

# Display thresholds (heuristic, documented; see module docstring and the V3 docs).
BALANCED_RATIO = 2 / 3          # smaller package >= 2/3 of the larger: "relatively balanced"
SIGNIFICANT_RATIO = 0.4         # smaller package < 40% of the larger: "significant imbalance"
REPLACEMENT_PER_WEEK = 1.0      # both packages under 1 point above replacement per remaining week

LABELS = {
    "balanced": "Asset packages appear relatively balanced",
    "they_give_more": "They give up more asset value (moderate imbalance)",
    "you_give_more": "You give up more asset value (moderate imbalance)",
    "significant_they_give_more": "Significant asset imbalance: they surrender far more",
    "significant_you_give_more": "Significant asset imbalance: you surrender far more",
    "they_surrender_multiple_strong": "They surrender multiple strong assets for fewer",
    "you_surrender_multiple_strong": "You surrender multiple strong assets for fewer",
    "replacement_level": "Replacement-level package (little value above free agents on either side)",
    "insufficient_data": "Insufficient data to assess asset balance",
}
AGAINST_THEM = ("significant_they_give_more", "they_surrender_multiple_strong")
NOTE = ("Asset balance is a diagnostic separate from the lineup delta: ESPN points above the best available "
        "free agent at each player's position, over the weeks he is NFL-eligible, independent of whether he "
        "starts for his current team. Labels use stated heuristic thresholds; they are not a market price, not "
        "calibrated, and not an acceptance estimate.")
FLEX_NOTE = ("League starter counts include flexible slots (FLEX, RB/WR, WR/TE, OP/superflex): each one is counted "
             "once, for the position of the player who fills it in each team's optimal projected lineup, averaged over "
             "the remaining weeks. Flex demand is dynamic: it moves with projections, byes, injuries and roster "
             "changes, so these counts are a current reference, not a fixed rule.")


def _fixed_counts(slots: list[str]) -> Counter:
    return Counter(s for s in slots if s in FIXED_SLOTS)


def _round_half_up(total: int, n: int) -> int:
    """total / n rounded half up, in integers (no float tie ambiguity)."""
    return (2 * total + n) // (2 * n)


def league_reference(lineups: dict[int, dict], slots: list[str], weeks: list[int]) -> dict:
    """League-wide starter demand per position from every team's exact optimal lineup (project_team output).

    Fixed slots count teams x count for their own position. Each filled flex slot counts once, for the position of
    the player (or never, for a flex slot left empty) in that team-week's optimal assignment; the K/D/ST waiver
    streamer only ever fills fixed slots. Returns per-week counts (`weeks`), the per-position flex starts summed over
    the window (`flex_starts`), their season apportionment (`flex_seats`, see _flex_seats) and the season counts
    (`starters`): fixed + flex seats. `incomplete_team_weeks` counts team-weeks whose optimal lineup was solved from
    known projections only (project_team's projection_complete is False); the flex counts are then uncertain.
    A position is in `starters` when it has a fixed slot (at least 1) or at least one flex seat."""
    teams = len(lineups)
    fixed = {pos: n * teams for pos, n in _fixed_counts(slots).items()}
    flex_slots = sum(1 for s in slots if is_flex(s)) * teams
    by_week: dict[int, dict[str, int]] = {}
    flex_starts: Counter = Counter()
    incomplete = 0
    rows = {t: {x["week"]: x for x in lineups[t]["weeks"]} for t in sorted(lineups)}
    for w in weeks:
        flex = Counter()
        for t in rows:
            row = rows[t].get(w) or {}
            incomplete += row.get("projection_complete") is False
            for a in row.get("assignment", ()):
                if a["is_flex"] and slot_filled(a) and a["position"]:
                    flex[a["position"]] += 1
        flex_starts.update(flex)
        by_week[w] = {pos: fixed.get(pos, 0) + flex.get(pos, 0) for pos in sorted(set(fixed) | set(flex))}
    seats = _flex_seats(flex_starts, len(weeks))
    starters = {pos: max(1, k) for pos, k in fixed.items()}
    for pos, share in seats.items():
        if pos in starters:
            starters[pos] += share
        elif share >= 1:
            starters[pos] = share
    return {"teams": teams, "fixed": fixed, "flex_slots": flex_slots, "weeks": by_week,
            "flex_starts": dict(sorted(flex_starts.items())), "flex_seats": seats,
            "starters": dict(sorted(starters.items())), "note": FLEX_NOTE if flex_slots else None,
            "incomplete_team_weeks": incomplete}


def _flex_seats(flex_starts: Counter, n: int) -> dict[str, int]:
    """Season flex starters per position: the weekly mean of filled flex slots, rounded half up, apportioned to the
    positions by largest remainder (ties: position name). Rounding each position on its own could count one slot twice
    (an OP that is a QB half the weeks and an RB the other half would add one QB and one RB); this never hands out more
    flex starters than the league's mean filled flex slots, rounded."""
    if not n or not flex_starts:
        return {}
    seats = {pos: k // n for pos, k in flex_starts.items()}
    left = _round_half_up(sum(flex_starts.values()), n) - sum(seats.values())
    for pos in sorted((p for p in flex_starts if flex_starts[p] % n), key=lambda p: (-(flex_starts[p] % n), p))[:left]:
        seats[pos] += 1
    return dict(sorted(seats.items()))


class AssetBoard:
    """Replacement baselines, per-player PAR and positional ranks for one league state and window.

    Positional PAR ranks are tie-aware (standard competition rank: 1 + count of players with strictly
    higher PAR using full-precision PAR with a 1e-9 tolerance). When ties span the league starter
    threshold, all tied players receive the same rank and qualify as starter-caliber (rank <= starters),
    even if that means more players qualify than the nominal starter count. Display order remains
    deterministic (descending by PAR, breaking ties by ESPN player ID ascending), but display ordering
    is strictly decoupled from value classification.
    """

    def __init__(self, state, window: Window):
        self.state, self.window = state, window
        self.weeks = list(window.weeks)
        rostered = [p for ps in state.rosters.values() for p in ps]
        self.teams = len(state.rosters)
        # The league reference assignment: every team's canonical optimal lineups (also reused by team_needs).
        self.lineups = {t: project_team(state.rosters[t], state.slots, window, state.projections)
                        for t in sorted(state.rosters)}
        self._roster_refs = {t: state.rosters[t] for t in self.lineups}
        self.reference = league_reference(self.lineups, state.slots, self.weeks)
        self.starters = self.reference["starters"]
        self.base: dict[str, dict[int, dict]] = {}
        positions = sorted({p.position for p in rostered if p.position})
        for pos in positions:
            per = {}
            for w in self.weeks:
                fa = [v for p in state.free_agents if p.position == pos and w not in p.unavailable_weeks
                      and (v := projection(state.projections, p.espn_player_id, w)) is not None]
                if fa:
                    per[w] = {"level": max(fa), "source": "best available free agent", "measured": True}
                else:
                    vals = sorted((v for p in rostered if p.position == pos and w not in p.unavailable_weeks
                                   and (v := projection(state.projections, p.espn_player_id, w)) is not None),
                                  reverse=True)
                    # A lineup-eligible position uses that week's starters, even a flex-only one whose season
                    # share rounded to 0; only a position no slot accepts falls back to one per team.
                    lineup_pos = pos in self.starters or lineup_relevant(pos, state.slots)
                    k = self.reference["weeks"][w].get(pos, 0) if lineup_pos else self.teams
                    # `measured` False: no projected player beyond the starters, so the 0.0 level is a stand-in, not a
                    # measurement (the trade values board reports such a week as unknown; the engines here keep 0.0).
                    per[w] = {"level": vals[k] if len(vals) > k else 0.0,
                              "source": "league-structure replacement (no free agent projected)",
                              "measured": len(vals) > k}
            self.base[pos] = per
        self.profiles = {p.espn_player_id: self._profile(p) for p in rostered}
        by_pos: dict[str, list[tuple[float, int]]] = {}
        for pid, pr in self.profiles.items():
            raw_par = pr.pop("_raw_par", None)
            if raw_par is not None:
                by_pos.setdefault(pr["position"], []).append((raw_par, pid))
        for pos, items in by_pos.items():
            # Deterministic presentation ordering: highest PAR first, ties broken by ESPN player ID
            items.sort(key=lambda x: (-x[0], x[1]))
            known_raws = [x[0] for x in items]
            for raw_par, pid in items:
                pr = self.profiles[pid]
                # Tie-aware competition rank: 1 + number of players with strictly higher PAR
                rank = 1 + sum(1 for other_raw in known_raws if other_raw > raw_par + 1e-9)
                pr["position_rank"] = rank
                pr["position_rank_of"] = len(items)
                pr["league_starters_at_position"] = self.starters.get(pos)
                # All tied players spanning the cutoff receive starter_caliber=True
                pr["starter_caliber"] = pos in self.starters and rank <= self.starters[pos]

    def _profile(self, p: RosterPlayer) -> dict:
        avail = (self.state.availability or {}).get(p.espn_player_id) or {}
        relevant = lineup_relevant(p, self.state.slots) and bool(p.pro_team)
        par, raw, missing, blocked, per = 0.0, 0.0, [], [], {"regular": 0.0, "playoff": 0.0}
        base = self.base.get(p.position, {})
        for w in self.weeks:
            if w in p.unavailable_weeks:
                blocked.append(w)
                continue
            v = projection(self.state.projections, p.espn_player_id, w)
            if v is None:
                if relevant:
                    missing.append(w)
                continue
            lvl = (base.get(w) or {}).get("level", 0.0)
            gain = max(0.0, v - lvl)
            par += gain
            raw += v
            ph = self.window.phase(w)
            if ph in per:
                per[ph] += gain
        complete = not missing
        sources = sorted({(base.get(w) or {}).get("source") for w in self.weeks if w in base} - {None})
        status = avail.get("status")
        return {"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position,
                "par": r(par) if complete else None, "known_par": r(par), "_raw_par": par if complete else None,
                "par_regular": r(per["regular"]) if complete else None, "par_playoff": r(per["playoff"]) if complete else None,
                "espn_points_eligible_weeks": r(raw) if complete else None,
                "replacement_per_week": r(sum((base.get(w) or {}).get("level", 0.0) for w in self.weeks) / len(self.weeks))
                if self.weeks else None,
                "replacement_source": sources, "missing_weeks": missing, "nfl_ineligible_weeks": blocked,
                "availability_status": status, "fantasy_ir_slot": p.on_ir, **({"fantasy_taxi_slot": True} if p.on_taxi else {}),
                "return_unconfirmed": status in (INELIGIBLE, ELIGIBLE_UNCONFIRMED),
                "eligibility_unknown": status == UNKNOWN, "no_eligible_week": status == NO_WEEK_LEFT,
                "position_rank": None, "position_rank_of": None, "league_starters_at_position": None,
                "starter_caliber": False}

    def package(self, mine_send: list[int], their_send: list[int]) -> dict:
        """Asset-balance diagnostic for a package; 'you' = the side sending `mine_send`."""
        a = [self.profiles[x] for x in mine_send]
        b = [self.profiles[x] for x in their_send]
        note = NOTE + (" " + self.reference["note"] if self.reference["note"] else "")
        out = {"you_send": a, "they_send": b, "label": None, "text": None, "note": note,
               "thresholds": {"balanced_ratio": round(BALANCED_RATIO, 3), "significant_ratio": SIGNIFICANT_RATIO,
                              "replacement_per_week": REPLACEMENT_PER_WEEK},
               "you_send_par": None, "they_send_par": None, "ratio": None, "caveats": []}
        if any(x["par"] is None for x in a + b) or any(x["eligibility_unknown"] for x in a + b):
            why = [f"{x['name']}: no ESPN projection in week(s) {', '.join(map(str, x['missing_weeks']))}"
                   for x in a + b if x["missing_weeks"]] + \
                  [f"{x['name']}: NFL eligibility unknown" for x in a + b if x["eligibility_unknown"]]
            out.update(label="insufficient_data", text=LABELS["insufficient_data"], caveats=why)
            return out
        pa, pb = sum(x["par"] for x in a), sum(x["par"] for x in b)
        n = max(1, len(self.weeks))
        sa, sb = sum(x["starter_caliber"] for x in a), sum(x["starter_caliber"] for x in b)
        ratio = 1.0 if max(pa, pb) == 0 else min(pa, pb) / max(pa, pb)
        if pa / n < REPLACEMENT_PER_WEEK and pb / n < REPLACEMENT_PER_WEEK:
            label = "replacement_level"
        elif sb >= 2 and sb > sa and pb > pa:
            label = "they_surrender_multiple_strong"
        elif sa >= 2 and sa > sb and pa > pb:
            label = "you_surrender_multiple_strong"
        elif ratio >= BALANCED_RATIO:
            label = "balanced"
        elif ratio >= SIGNIFICANT_RATIO:
            label = "they_give_more" if pb > pa else "you_give_more"
        else:
            label = "significant_they_give_more" if pb > pa else "significant_you_give_more"
        caveats = [f"{x['name']}: value counted only from week {x['nfl_ineligible_weeks'][-1] + 1}; return not confirmed"
                   for x in a + b if x["return_unconfirmed"] and x["nfl_ineligible_weeks"]]
        caveats += [f"{x['name']}: on an NFL reserve list, eligible but not confirmed active"
                    for x in a + b if x["return_unconfirmed"] and not x["nfl_ineligible_weeks"]]
        caveats += [f"{x['name']}: in a fantasy IR slot; his value assumes he plays every week he is not NFL-restricted"
                    for x in a + b if x["fantasy_ir_slot"] and not x["return_unconfirmed"]]
        if self.reference["flex_slots"] and self.reference["incomplete_team_weeks"]:
            caveats.append(f"League flex starter counts use known projections only for "
                           f"{self.reference['incomplete_team_weeks']} team-week(s) with an unprojected player; "
                           "starter-caliber labels may shift when those projections arrive")
        out.update(label=label, text=LABELS[label], you_send_par=r(pa), they_send_par=r(pb), ratio=round(ratio, 3),
                   you_send_starter_caliber=sa, they_send_starter_caliber=sb, caveats=caveats,
                   per_week={"you_send": r(pa / n), "they_send": r(pb / n)})
        return out


# ------------------------------------------------------------------------- roster needs --

def team_needs(state, window: Window, team_id: int, board: AssetBoard, team: dict | None = None) -> dict:
    """Per position: the team's fixed starters' average projected points per slot-week (canonical
    optimal lineups) and its starter-caliber players who start at most half the weeks ("surplus"; a start
    in a flex slot counts, and starter-caliber is the board's flex-aware league reference).
    The average is unknown (None) when a rostered player at the position lacks an ESPN projection in
    any week: an empty or partial slot is never scored as a measured 0."""
    roster = state.rosters[team_id]
    if team is None and board.state is state and board.window == window and board._roster_refs.get(team_id) is roster:
        team = board.lineups[team_id]                  # the same canonical lineups, already computed
    team = team or project_team(roster, state.slots, window, state.projections)
    counts = _fixed_counts(state.slots)
    n = max(1, len(window.weeks))
    pos_of = {p.espn_player_id: p.position for p in roster}
    starts = Counter(a["player_id"] for w in team["weeks"] for a in w["assignment"] if a["player_id"] is not None)
    out = {}
    for pos, k in counts.items():
        missing = any(pos_of.get(pid) == pos for w in team["weeks"] for pid in w["missing_projection_ids"])
        pts = sum(a["points"] or 0.0 for w in team["weeks"] for a in w["assignment"]
                  if a["slot"] == pos and slot_filled(a))         # the K/D/ST waiver streamer counts too
        surplus = [p.espn_player_id for p in roster if p.position == pos and not p.reserved
                   and board.profiles.get(p.espn_player_id, {}).get("starter_caliber") and starts[p.espn_player_id] * 2 <= n]
        out[pos] = {"slot_points_per_week": None if missing else r(pts / (n * k)), "_raw": None if missing else pts / (n * k),
                    "slots": k, "surplus": surplus, "incomplete": missing}
    return out


def league_needs(state, window: Window, board: AssetBoard) -> dict[int, dict]:
    """Ranks are tie-aware (1 + teams strictly better, among teams with a known value); "short" means
    strictly below the league median of the known values, so equal teams are never split by id."""
    teams = {t: team_needs(state, window, t, board) for t in sorted(state.rosters)}
    for pos in _fixed_counts(state.slots):
        known = {t: teams[t][pos]["_raw"] for t in teams if teams[t][pos]["_raw"] is not None}
        med = median(known.values()) if known else None
        for t in teams:
            row = teams[t][pos]
            v = row.pop("_raw")
            row["teams"] = len(known)
            row["league_rank"] = None if v is None else 1 + sum(1 for x in known.values() if x > v + 1e-9)
            row["short"] = v is not None and med is not None and v < med - 1e-9
    return teams
