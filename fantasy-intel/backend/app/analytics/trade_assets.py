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
used instead: the projection of the first player beyond the league's starters at that position
(teams x fixed starting slots). Units are projected fantasy points above replacement over the
evaluation window; nothing is weighted, no star premium is added, positions are never compared on
raw points.

A player is "starter-caliber" when his PAR ranks within the league's starters at his position
(teams x fixed starting slots; K and D/ST included only when they are traded). Package labels use
ratios of PAR with display thresholds stated below. They are heuristic, not empirically calibrated
and not a market price; the labels never change the canonical delta and never hide a trade from the
manual analyzer.
"""

from __future__ import annotations

from collections import Counter

from .lineup import FIXED_SLOTS
from .player_availability import ELIGIBLE_UNCONFIRMED, INELIGIBLE, NO_WEEK_LEFT, UNKNOWN
from .team_lineups import RosterPlayer, Window, lineup_relevant, project_team, projection, r

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


def _fixed_counts(slots: list[str]) -> Counter:
    return Counter(s for s in slots if s in FIXED_SLOTS)


class AssetBoard:
    """Replacement baselines, per-player PAR and positional ranks for one league state and window."""

    def __init__(self, state, window: Window):
        self.state, self.window = state, window
        self.weeks = list(window.weeks)
        rostered = [p for ps in state.rosters.values() for p in ps]
        self.teams = len(state.rosters)
        self.starters = {pos: max(1, n * self.teams) for pos, n in _fixed_counts(state.slots).items()}
        self.base: dict[str, dict[int, dict]] = {}
        positions = sorted({p.position for p in rostered if p.position})
        for pos in positions:
            per = {}
            for w in self.weeks:
                fa = [v for p in state.free_agents if p.position == pos and w not in p.unavailable_weeks
                      and (v := projection(state.projections, p.espn_player_id, w)) is not None]
                if fa:
                    per[w] = {"level": max(fa), "source": "best available free agent"}
                else:
                    vals = sorted((v for p in rostered if p.position == pos and w not in p.unavailable_weeks
                                   and (v := projection(state.projections, p.espn_player_id, w)) is not None),
                                  reverse=True)
                    k = self.starters.get(pos, self.teams)
                    per[w] = {"level": vals[k] if len(vals) > k else 0.0,
                              "source": "league-structure replacement (no free agent projected)"}
            self.base[pos] = per
        self.profiles = {p.espn_player_id: self._profile(p) for p in rostered}
        by_pos: dict[str, list] = {}
        for pid, pr in self.profiles.items():
            if pr["par"] is not None:
                by_pos.setdefault(pr["position"], []).append((-pr["par"], pid))
        for pos, items in by_pos.items():
            items.sort()
            for i, (_, pid) in enumerate(items):
                pr = self.profiles[pid]
                pr["position_rank"] = i + 1
                pr["position_rank_of"] = len(items)
                pr["league_starters_at_position"] = self.starters.get(pos)
                pr["starter_caliber"] = pos in self.starters and i + 1 <= self.starters[pos]

    def _profile(self, p: RosterPlayer) -> dict:
        avail = (self.state.availability or {}).get(p.espn_player_id) or {}
        relevant = lineup_relevant(p.position, self.state.slots) and bool(p.pro_team)
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
                "par": r(par) if complete else None, "known_par": r(par),
                "par_regular": r(per["regular"]) if complete else None, "par_playoff": r(per["playoff"]) if complete else None,
                "espn_points_eligible_weeks": r(raw) if complete else None,
                "replacement_per_week": r(sum((base.get(w) or {}).get("level", 0.0) for w in self.weeks) / len(self.weeks))
                if self.weeks else None,
                "replacement_source": sources, "missing_weeks": missing, "nfl_ineligible_weeks": blocked,
                "availability_status": status, "fantasy_ir_slot": p.on_ir,
                "return_unconfirmed": status in (INELIGIBLE, ELIGIBLE_UNCONFIRMED),
                "eligibility_unknown": status == UNKNOWN, "no_eligible_week": status == NO_WEEK_LEFT,
                "position_rank": None, "position_rank_of": None, "league_starters_at_position": None,
                "starter_caliber": False}

    def package(self, mine_send: list[int], their_send: list[int]) -> dict:
        """Asset-balance diagnostic for a package; 'you' = the side sending `mine_send`."""
        a = [self.profiles[x] for x in mine_send]
        b = [self.profiles[x] for x in their_send]
        out = {"you_send": a, "they_send": b, "label": None, "text": None, "note": NOTE,
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
        out.update(label=label, text=LABELS[label], you_send_par=r(pa), they_send_par=r(pb), ratio=round(ratio, 3),
                   you_send_starter_caliber=sa, they_send_starter_caliber=sb, caveats=caveats,
                   per_week={"you_send": r(pa / n), "they_send": r(pb / n)})
        return out


# ------------------------------------------------------------------------- roster needs --

def team_needs(state, window: Window, team_id: int, board: AssetBoard, team: dict | None = None) -> dict:
    """Per position: the team's fixed starters' average projected points per slot-week (canonical
    optimal lineups), its rank among the league's teams, whether it is in the bottom half ("short"),
    and its starter-caliber players who start at most half the weeks ("surplus")."""
    roster = state.rosters[team_id]
    team = team or project_team(roster, state.slots, window, state.projections)
    counts = _fixed_counts(state.slots)
    n = max(1, len(window.weeks))
    starts = Counter(a["player_id"] for w in team["weeks"] for a in w["assignment"] if a["player_id"] is not None)
    out = {}
    for pos, k in counts.items():
        pts = sum(a["points"] or 0.0 for w in team["weeks"] for a in w["assignment"]
                  if a["slot"] == pos and a["player_id"] is not None)
        surplus = [p.espn_player_id for p in roster if p.position == pos and not p.on_ir
                   and board.profiles.get(p.espn_player_id, {}).get("starter_caliber") and starts[p.espn_player_id] * 2 <= n]
        out[pos] = {"slot_points_per_week": r(pts / (n * k)), "slots": k, "surplus": surplus}
    return out


def league_needs(state, window: Window, board: AssetBoard) -> dict[int, dict]:
    teams = {t: team_needs(state, window, t, board) for t in sorted(state.rosters)}
    for pos in _fixed_counts(state.slots):
        order = sorted(teams, key=lambda t: (-(teams[t][pos]["slot_points_per_week"] or 0.0), t))
        for i, t in enumerate(order):
            teams[t][pos]["league_rank"] = i + 1
            teams[t][pos]["teams"] = len(order)
            teams[t][pos]["short"] = i + 1 > len(order) / 2
    return teams
