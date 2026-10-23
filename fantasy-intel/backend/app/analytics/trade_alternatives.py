"""Trade vs. free agency: what a trade adds beyond one available player. Hypothetical, pure and
read-only (no database, no ESPN). See docs/v3_1_trade_vs_free_agency.md.

The canonical trade delta (trades.side_lineups, the same computation evaluate_trade and discovery
use) answers "how many projected starting-lineup points does this trade add or remove?". This module
answers the companion question for each manager independently: "could I get most of that without
making the trade?". It never changes, weights or replaces the canonical delta.

Every scenario is scored with the same exact weekly optimal lineup (team_lineups.project_team) on
ESPN's projections over the same evaluation window:

  A  no trade                      the current roster (= the canonical "before")
  B  the proposed trade            the canonical "after": incoming players active, forced drops resolved
  C  no trade + one acquisition    the current roster plus the single best available player F, with the
                                   legal drop it needs (trades.choose_forced_drops: the trade evaluator's rule)
  E  trade + the same F            the trade applied together with F, forced drops by the same rule
  D  trade + its own best player   the post-trade roster plus its own best available player (diagnostic)

  L(X) = projected starting points of roster X summed over the window

  canonical_gain                 = L(B) - L(A)   the canonical delta, untouched
  free_agent_alternative_gain    = L(C) - L(A)   0 (and F = none) when no available player improves the lineup
  trade_advantage                = L(E) - L(C)   what the trade still adds once the free-agent move is made
  available_without_trade        = canonical_gain - trade_advantage
                                                 the part of the trade's gain that move already provides
  trade_instead_of_free_agent    = L(B) - L(C)   making the trade instead of the free-agent move (diagnostic)
  trade_advantage_if_both_add    = L(D) - L(C)   each world takes its own best acquisition (diagnostic)

trade_advantage is the headline because the free-agent move is open to the manager with or without the
trade. When the trade and F fill the same need (an empty D/ST slot) it shrinks to what the trade adds
beyond F; when they are independent (an RB trade, a D/ST pickup) it stays the full canonical gain,
whereas L(B) - L(C) would wrongly turn negative.

Rules:
  * One acquisition, one player, over the whole window. No multi-transaction plan, no weekly churn.
  * Only players in the synced ESPN free-agent pool. ESPN FREEAGENT players are the headline
    alternative; WAIVERS players are a separate, explicitly non-guaranteed waiver-claim alternative.
  * A candidate is scored only when ESPN projects him in every window week he is NFL-eligible
    (missing is unknown, never 0). Verified NFL-ineligible weeks count as no contribution, exactly as
    in the lineup engine. Free agents never go to IR.
  * Roster capacity and ESPN position limits are enforced: a full roster must drop someone, chosen
    by the forced-drop rule (fewest removals, then the largest remaining starting total), and the
    drop's lineup cost is inside L(C). A candidate that could only be kept by cutting himself is no
    acquisition.
  * Each manager's alternatives are independent counterfactuals. When both managers' best
    alternative is the same player, that is flagged: only one of them could add him.
  * K and D/ST get an extra per-week "streaming frontier": the best available player at the position
    each week added for free. It is an optimistic illustration, never a plan and never in any metric.

Search: exact. The no-drop gain L(R + c) - L(R) bounds a candidate's gain with any drop (the weekly
lineup value is monotone in the roster with non-negative projections), so candidates are solved in
decreasing order of that bound and the search stops once the bound is below the best exact gain.
Same-position candidates weakly dominated week by week are skipped (team_lineups._pareto: exact). A
negative projection voids the bound and every remaining candidate is solved. Totals come from the
discovery solver (bit-identical to project_week); reported numbers come from project_team/compare.
"""

from __future__ import annotations

from collections import Counter

from .lineup import is_flex, slot_positions
from .team_lineups import (PROJECTION_SOURCE, RosterPlayer, source_label, Window, _pareto, compare, lineup_relevant, project_team,
                           project_week, projection, r, slot_filled, starters)
from .trade_discovery import _Totals
from .trades import EvaluationUnavailable, LeagueState, TradeError, choose_forced_drops, legal_roster, side_lineups

EPS = 1e-6
STREAMING_POSITIONS = ("K", "D/ST")
IMMEDIATE = ("free_agent",)             # confirmed free agents only; waivers and unknown availability reported separately
KINDS = {"FREEAGENT": "free_agent", "WAIVERS": "waiver_claim"}
DELTA_KEYS = ("ros_delta", "regular_season_delta", "playoff_delta", "known_ros_delta", "projection_complete")
ACQUISITION_NOTES = {
    "free_agent": "ESPN free agent: can be added without a waiver claim (check ESPN transaction timing and game locks).",
    "waiver_claim": "On ESPN waivers: a claim is not guaranteed to succeed (priority, FAAB or other claims).",
    "unknown": "ESPN availability type not synced: verify that he can be added.",
}
FORMULAS = {
    "L(X)": "projected starting points of roster X over the evaluation window: ESPN weekly projections, the exact "
            "weekly optimal lineup, bench points never counted",
    "canonical_gain": "L(trade roster) - L(current roster): the canonical trade delta, unchanged",
    "free_agent_alternative_gain": "L(current roster + best available free agent - required drop) - L(current roster); "
                                   "0 when no available player improves the lineup",
    "trade_advantage": "L(trade + the same free agent - required drops) - L(current roster + that free agent - "
                       "required drop): what the trade still adds once the free-agent move is made",
    "available_without_trade": "canonical_gain - trade_advantage: the part of the trade's gain the free-agent move "
                               "already provides",
    "share_available_without_trade": "available_without_trade / canonical_gain (only when canonical_gain > 0)",
    "trade_instead_of_free_agent": "L(trade roster) - L(current roster + best available free agent - required drop): "
                                   "diagnostic, making the trade instead of the free-agent move",
    "trade_advantage_if_both_add": "L(trade roster + its own best free agent - drop) - L(current roster + its best free "
                                   "agent - drop): diagnostic, each world takes its own best acquisition",
}
MAJORITY_RULE = ("'Most of this gain is available without trading' means available_without_trade is more than half of "
                 "the canonical gain; no other threshold is used.")
FRONTIER_LABEL = ("Optimistic streaming illustration, not a plan: the best available player at this position each week "
                  "is added for free (no drop, no waiver competition, perfect foresight of every week's projection). "
                  "It is never part of any metric above.")
NOTE = ("Trade vs. free agency compares the canonical trade delta with one hypothetical acquisition of a currently "
        "available player. The canonical delta never includes it. Availability changes, waivers may not clear, and "
        "both managers cannot add the same player.")
ASSUMPTIONS = [
    "Same engine as the trade: ESPN weekly projections, unmodified, and the exact weekly optimal lineup.",
    "One acquisition over the whole evaluation window: a single available player added now and kept.",
    "Only players in the synced ESPN free-agent pool; FREEAGENT players are the headline alternative, WAIVERS "
    "players are a separate claim that may fail.",
    "A full roster drops a player by the trade evaluator's forced-drop rule; the drop's lineup cost is included.",
    "Free agents without an ESPN projection in an eligible week are not scored (missing is unknown, not 0).",
    "Each manager's alternative is independent; both cannot add the same player.",
]
LIMITATIONS = [
    "The free-agent pool is today's synced snapshot; players can be added by other managers at any time.",
    "Only one acquisition is modeled; real managers may make several moves or stream weekly.",
    "A dropped player's value outside this team's lineup (as a trade asset) is not measured.",
    "ESPN undroppable lists, game locks and transaction timing are not modeled.",
    "The K/D/ST streaming frontier assumes perfect weekly foresight and free adds; it is an upper illustration.",
]


def _active(players: list[RosterPlayer]) -> list[int]:
    return [p.espn_player_id for p in players if not p.reserved]    # IR and taxi players never start


def _brief(p: RosterPlayer) -> dict:
    return {"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position, "pro_team": p.pro_team}


def _deltas(c: dict) -> dict:
    return {k: c[k] for k in DELTA_KEYS}


def _known_only(d: dict, unknown: bool) -> dict:
    """A comparison whose roster move drops a player of unknown value has no canonical total."""
    return {k: (v if k == "known_ros_delta" else False if k == "projection_complete" else None) for k, v in d.items()} \
        if unknown else d


def _sub(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else r(a - b)


class FreeAgencyBoard:
    """The available-player pool, a fast totals solver over it and per-roster caches for one league
    state and window. Build once per request (discovery shares one across all shown rows)."""

    def __init__(self, state: LeagueState, window: Window):
        self.state, self.window, self.weeks = state, window, window.weeks
        statuses = state.free_agent_status or {}
        self.kind = {p.espn_player_id: KINDS.get(statuses.get(p.espn_player_id), "unknown") for p in state.free_agents}
        rostered = {p.espn_player_id for ps in state.rosters.values() for p in ps}
        self.pool, self.excluded_unprojected = [], Counter()
        for p in state.free_agents:
            if p.espn_player_id in rostered or not lineup_relevant(p.position, state.slots):
                continue
            if all(projection(state.projections, p.espn_player_id, w) is not None
                   for w in self.weeks if w not in p.unavailable_weeks):
                self.pool.append(p)
            elif p.pro_team:
                self.excluded_unprojected[p.position] += 1
        everyone = {p.espn_player_id: p for ps in state.rosters.values() for p in ps} | \
                   {p.espn_player_id: p for p in self.pool}
        self.T = _Totals(state, self.weeks, set(everyone), {i: p.position for i, p in everyone.items()},
                         {i: p.pro_team for i, p in everyone.items()},
                         {i: p.unavailable_weeks for i, p in everyone.items() if p.unavailable_weeks})
        self.negative = self.T.negative
        self._totals: dict[tuple, float] = {}
        self._best: dict[tuple, dict] = {}
        self._teams: dict[tuple, dict] = {}

    def unprojected(self, p: RosterPlayer) -> bool:
        """An active lineup-relevant player with an NFL team and no ESPN projection in some eligible week:
        what he is worth is unknown, so dropping him has an unknown cost (never a measured 0)."""
        return not p.reserved and bool(p.pro_team) and lineup_relevant(p.position, self.state.slots) and any(
            projection(self.state.projections, p.espn_player_id, w) is None
            for w in self.weeks if w not in p.unavailable_weeks)

    # ---- totals
    def total(self, players) -> float:
        key = tuple(sorted(_active(players)))
        if key not in self._totals:
            self._totals[key] = sum(self.T.vec(list(key)))
        return self._totals[key]

    def drop_search(self, players, legal_base, incoming: set[int]) -> dict:
        """trades.choose_forced_drops on `players` with self.total's values (same memo): a week a drop leaves alone
        keeps the full roster's value (LineupTotals.removal_total, exact)."""
        known = self.T.removal_total(_active(players), self._totals)
        return choose_forced_drops(players, legal_base, self.state, incoming, lambda ps: known(_active(ps)),
                                   self.negative, known.dropping, known.bound)

    def team(self, players: list[RosterPlayer]) -> dict:
        key = tuple(sorted((p.espn_player_id, p.on_ir, p.on_taxi) for p in players))
        if key not in self._teams:
            self._teams[key] = project_team(players, self.state.slots, self.window, self.state.projections)
        return self._teams[key]

    # ---- one acquisition
    def best_acquisition(self, team_id: int, roster: list[RosterPlayer], kinds=IMMEDIATE,
                         positions: tuple[str, ...] | None = None) -> dict:
        """The single available player (of `kinds`, optionally only `positions`) whose acquisition, with
        the legal drop it needs, maximises the roster's known starting total. Legality is judged against
        the team's current roster (state.rosters[team_id]), as in the trade evaluator."""
        key = (team_id, tuple(sorted((p.espn_player_id, p.on_ir, p.on_taxi) for p in roster)), tuple(kinds), positions)
        if key in self._best:
            return self._best[key]
        st = self.state
        legal_base = st.rosters[team_id]
        have = {p.espn_player_id for p in roster}
        cands = [c for c in self.pool if c.espn_player_id not in have and self.kind[c.espn_player_id] in kinds
                 and (positions is None or c.position in positions)]
        kept = _pareto(cands, self.weeks, st.projections)
        base = self.total(roster)
        ub = {c.espn_player_id: self.total(roster + [c]) - base for c in kept}
        order = sorted(kept, key=lambda c: (-ub[c.espn_player_id], c.espn_player_id))
        best, stats = None, Counter()
        for c in order:
            bound = ub[c.espn_player_id]
            if not self.negative and (bound <= EPS or (best is not None and bound < best[0][0] - EPS)):
                break                  # no remaining candidate can improve the lineup or beat (or tie) the best
            stats["solved"] += 1
            after = roster + [c]
            drops: tuple = ()
            if legal_roster(after, st, legal_base):
                total = base + bound
            else:
                try:
                    res = self.drop_search(after, legal_base, {c.espn_player_id})
                except EvaluationUnavailable:
                    stats["evaluation_unavailable"] += 1
                    continue
                except TradeError:
                    stats["no_legal_roster"] += 1
                    continue
                if False not in res["best"]:
                    stats["only_by_cutting_candidate"] += 1
                    continue
                _, drops, total = res["best"][False]
                drops = self._display_drop(after, legal_base, c, drops, total)
            gain = total - base
            k = (round(gain, 9), -c.espn_player_id)
            if gain > EPS and (best is None or k > best[0]):
                best = (k, c, drops, total, base + bound)
        search = {"candidates": len(cands), "after_dominance": len(kept), "solved": stats["solved"],
                  "no_legal_roster": stats["no_legal_roster"], "evaluation_unavailable": stats["evaluation_unavailable"],
                  "only_by_cutting_candidate": stats["only_by_cutting_candidate"],
                  "excluded_unprojected": sum(v for p, v in self.excluded_unprojected.items()
                                              if positions is None or p in positions),
                  "method": "exact (every candidate solved: a negative projection voids the bound)" if self.negative
                  else "exact (branch and bound on the no-drop gain)"}
        before_team = self.team(roster)
        if best is None:
            # Every candidate that could have improved the lineup failed on roster legality: no legal
            # acquisition. Otherwise no available player improves it (not acquiring is the alternative).
            illegal = stats["no_legal_roster"] + stats["only_by_cutting_candidate"] + stats["evaluation_unavailable"]
            reason = "no_legal_acquisition" if illegal and illegal == stats["solved"] else "no_improving_player"
            out = {"player": None, "reason": reason, "acquisition_type": None, "drops": [], "requires_drop": False,
                   "drop_lineup_cost": None, "drop_cost_unknown": False,
                   "gain": _deltas(compare(before_team, before_team, self.window)),
                   "weeks_started": [], "search": search, "_team": before_team}
        else:
            _, c, drops, total, no_drop_total = best
            kind = self.kind[c.espn_player_id]
            team = self.team([p for p in roster if p not in drops] + [c])
            unknown = any(self.unprojected(p) for p in drops)
            out = {"player": {**_brief(c), "acquisition_type": kind}, "reason": None, "acquisition_type": kind,
                   "acquisition_note": ACQUISITION_NOTES[kind],
                   "drops": [_brief(p) for p in drops], "requires_drop": bool(drops),
                   "drop_lineup_cost": r(no_drop_total - total) if drops and not unknown else None,
                   "drop_cost_unknown": unknown,
                   "gain": _known_only(_deltas(compare(before_team, team, self.window)), unknown),
                   "weeks_started": [w["week"] for w in team["weeks"] if c.espn_player_id in starters(w)],
                   "search": search, "_team": team}
        self._best[key] = out
        return out

    def _display_drop(self, after, legal_base, c, drops, total):
        """Among single drops with the same lineup cost as the forced-drop rule's choice (it breaks such
        ties by player id), show the one with the fewest known projected points over the window: the
        player a manager would actually cut. Never changes any total."""
        if len(drops) != 1:
            return drops
        st = self.state

        def own(p):
            if self.unprojected(p):
                return float("inf")       # never prefer cutting a player whose value is unknown
            return sum(v for w in self.weeks if (v := projection(st.projections, p.espn_player_id, w)) is not None
                       and w not in p.unavailable_weeks)
        same = [p for p in after if p is not c and not p.reserved
                and legal_roster([q for q in after if q is not p], st, legal_base)
                and abs(self.total([q for q in after if q is not p]) - total) <= EPS]
        return (min(same, key=lambda p: (own(p), p.espn_player_id)),) if same else drops

    # ---- scenario E: the trade together with a given acquisition
    def trade_with(self, team_id: int, pre: list[RosterPlayer], incoming: set[int], fa: RosterPlayer) -> dict:
        """The post-swap roster (`pre`: outgoing removed, incoming active, before forced drops) plus `fa`,
        made legal by the trade evaluator's forced-drop rule with `fa` counted as incoming (so he is cut
        only when nothing else is legal). Both `fa` and all incoming traded assets must survive in the
        resolved roster for the combined scenario to be legally retained. If either cannot be retained,
        returns available=False with a structured reason and does not claim the combination was completed."""
        players = pre + [fa]
        st = self.state
        drops: tuple = ()
        if not legal_roster(players, st, st.rosters[team_id]):
            try:
                res = self.drop_search(players, st.rosters[team_id], incoming | {fa.espn_player_id})
            except EvaluationUnavailable as err:
                return {"available": False, "reason": "evaluation_unavailable", "team": None, "drops": [],
                        "drop_cost_unknown": False, "retained_player_ids": [], "error": str(err)}
            except TradeError as err:
                return {"available": False, "reason": "no_legal_roster", "team": None, "drops": [],
                        "drop_cost_unknown": False, "retained_player_ids": [], "error": str(err)}
            if False not in res["best"]:
                # Any legal drop set cuts at least one incoming player or the free agent
                best_illegal = res["best"].get(True)
                drop_combo = best_illegal[1] if best_illegal else ()
                drop_pids = {p.espn_player_id for p in drop_combo}
                if fa.espn_player_id in drop_pids and any(p in incoming for p in drop_pids):
                    reason = "cannot_retain_combination"
                elif fa.espn_player_id in drop_pids:
                    reason = "free_agent_dropped"
                elif any(p in incoming for p in drop_pids):
                    reason = "traded_asset_dropped"
                else:
                    reason = "cannot_retain_combination"
                retained_pids = [p.espn_player_id for p in players if p not in drop_combo]
                return {"available": False, "reason": reason, "team": None,
                        "drops": [_brief(p) for p in drop_combo],
                        "drop_cost_unknown": any(self.unprojected(p) for p in drop_combo),
                        "retained_player_ids": retained_pids}
            _, drops, total = res["best"][False]
        retained = [p for p in players if p not in drops]
        retained_ids = [p.espn_player_id for p in retained]
        if fa.espn_player_id not in retained_ids:
            return {"available": False, "reason": "free_agent_dropped", "team": None,
                    "drops": [_brief(p) for p in drops],
                    "drop_cost_unknown": any(self.unprojected(p) for p in drops),
                    "retained_player_ids": retained_ids}
        if any(p not in retained_ids for p in incoming):
            return {"available": False, "reason": "traded_asset_dropped", "team": None,
                    "drops": [_brief(p) for p in drops],
                    "drop_cost_unknown": any(self.unprojected(p) for p in drops),
                    "retained_player_ids": retained_ids}
        return {"available": True, "reason": None, "team": self.team(retained),
                "drops": [_brief(p) for p in drops],
                "drop_cost_unknown": any(self.unprojected(p) for p in drops),
                "retained_player_ids": retained_ids}

    # ---- K / D/ST streaming frontier (illustration only)
    def frontier(self, roster: list[RosterPlayer], position: str) -> dict:
        n = sum(1 for s in self.state.slots if s == position)
        have = {p.espn_player_id for p in roster}
        base = self.team(roster)
        weeks, total, complete = [], 0.0, True
        for w, bw in zip(self.weeks, base["weeks"]):
            avail = sorted(((v, p) for p in self.state.free_agents
                            if p.position == position and self.kind[p.espn_player_id] == "free_agent"
                            and p.espn_player_id not in have and w not in p.unavailable_weeks
                            and (v := projection(self.state.projections, p.espn_player_id, w)) is not None),
                           key=lambda t: (-t[0], t[1].espn_player_id))[:n]
            plus = project_week(roster + [p for _, p in avail], self.state.slots, w, self.state.projections)
            gain = plus["_total"] - bw["_total"]
            ok = bw["projection_complete"]
            complete = complete and ok
            total += gain
            incumbent_slots = [(a["points"] if slot_filled(a) else 0.0) for a in bw["assignment"] if a["slot"] == position]
            if not incumbent_slots:
                starter_pts = 0.0
            elif any(pts is None for pts in incumbent_slots):
                starter_pts = None
            else:
                starter_pts = r(sum(pts for pts in incumbent_slots if pts is not None))
            weeks.append({"week": w, "phase": bw["phase"], "starter_points": starter_pts,
                          "starter_slots": incumbent_slots,
                          "best_available": [{**_brief(p), "points": v, "acquisition_type": self.kind[p.espn_player_id]}
                                             for v, p in avail],
                          "gain": r(gain) if ok else None, "known_gain": r(gain)})
        return {"position": position, "slot_count": n, "label": FRONTIER_LABEL, "weeks": weeks,
                "total_gain": r(total) if complete else None, "known_total_gain": r(total)}


# ------------------------------------------------------------------------- per side --

def slot_breakdown(teams: dict[str, dict]) -> list[dict]:
    """Known projected points by starting slot (FLEX separate) per scenario, and empty slot-weeks:
    where each lineup's points land. The optimizer may move players between slots, so this is an
    attribution of the optimum, not a causal split."""
    first = next(iter(teams.values()))["weeks"]
    order = {s: i for i, s in enumerate(dict.fromkeys(a["slot"] for a in first[0]["assignment"]))} if first else {}
    rows: dict[str, dict] = {}
    for name, team in teams.items():
        for w in team["weeks"]:
            for a in w["assignment"]:
                row = rows.setdefault(a["slot"], {"slot": a["slot"], **{f"{k}_points": 0.0 for k in teams},
                                                  **{f"{k}_empty_weeks": 0 for k in teams}})
                if not slot_filled(a):
                    row[f"{name}_empty_weeks"] += 1
                else:
                    row[f"{name}_points"] += a["points"] or 0.0
    out = []
    for row in sorted(rows.values(), key=lambda x: order.get(x["slot"], 99)):
        for k in teams:
            row[f"{k}_points"] = r(row[f"{k}_points"])
        base = row["before_points"]
        for k in teams:
            if k != "before":
                row[f"{k}_delta"] = r(row[f"{k}_points"] - base)
        out.append(row)
    return out


def _who(label: str) -> tuple[str, str]:
    """(subject, possessive) for statements: You/your, or a team name."""
    return ("You", "your") if label == "You" else (label, f"{label}'s")


def _name(p: dict | None) -> str:
    return (p or {}).get("name") or f"#{(p or {}).get('espn_player_id')}"


def statements(label: str, cmp: dict, alt: dict, waiver: dict | None, position_alts: list[dict],
               slots: list[dict], combined: dict | None = None, unknown_alt: dict | None = None) -> list[str]:
    """Plain, calculated evidence for one side. The only qualitative rule is the majority rule."""
    _, whose = _who(label)
    g, a = cmp["canonical_gain"]["ros_delta"], cmp["free_agent_alternative_gain"]["ros_delta"]
    t, av = cmp["trade_advantage"]["ros_delta"], cmp["available_without_trade"]["ros_delta"]
    p = alt["player"]
    out = []
    if combined is not None and not combined.get("available"):
        reason = combined.get("reason")
        if reason == "free_agent_dropped":
            out.append(f"The trade cannot be legally combined with adding {_name(p)}: roster limits would require "
                       f"dropping {_name(p)}, so trade advantage beyond free agency is unavailable.")
        elif reason == "traded_asset_dropped":
            out.append(f"The trade cannot be legally combined with adding {_name(p)}: retaining {_name(p)} would require "
                       "dropping an acquired traded player, so trade advantage beyond free agency is unavailable.")
        else:
            out.append(f"The trade cannot be legally combined with adding {_name(p)} under roster limits; "
                       "trade advantage beyond free agency is unavailable.")
    elif g is None or a is None or (p is not None and t is None):
        out.append("Replacement comparison unavailable: incomplete ESPN projections (no canonical total).")
    elif p is None:
        if alt["reason"] == "no_legal_acquisition":
            out.append(f"No legal free-agent acquisition improves {whose} lineup (roster or position limits).")
        elif g > 0:
            out.append(f"No available free agent improves {whose} lineup: the trade's {g:+.1f} is not available "
                       "through free agency.")
        else:
            out.append(f"The trade does not improve {whose} lineup ({g:+.1f}), and no available free agent would.")
    elif g <= 0:
        out.append(f"The trade does not improve {whose} lineup ({g:+.1f}); adding {_name(p)} ({p['position']}) alone "
                   f"would ({a:+.1f}).")
    elif av is not None and av < -EPS and t is not None:
        out.append(f"The trade and free-agent pickup interact favorably: adding {_name(p)} gives {a:+.1f}, and "
                   f"the trade adds {t:+.1f} beyond it (higher than its {g:+.1f} canonical gain alone).")
    elif t <= 0:
        out.append(f"All of this projected gain is available without trading: adding {_name(p)} ({p['position']}) gives "
                   f"{a:+.1f}, and with that move made the trade adds {t:+.1f}.")
    elif 2 * av > g:
        out.append(f"Most of this projected gain is available without trading: adding {_name(p)} ({p['position']}) gives "
                   f"{a:+.1f}; with that move made, the trade adds only {t:+.1f} of its {g:+.1f}.")
    else:
        out.append(f"The trade offers an improvement beyond available replacements: {t:+.1f} of its {g:+.1f} remains "
                   f"after the best free-agent move ({_name(p)}, {p['position']}: {a:+.1f} on its own).")
        if a > 0 and 2 * av <= a:
            out.append(f"Adding {_name(p)} is worth {a:+.1f} with or without this trade: most of it does not overlap "
                       "with the trade.")
    if p is not None and g is not None and a is not None:
        if alt["drops"]:
            cost = alt["drop_lineup_cost"]
            out.append(f"Adding {_name(p)} would require dropping "
                       f"{', '.join(_name(d) for d in alt['drops'])}"
                       + (f" (lineup cost {cost:.1f}, included above)." if cost is not None else "."))
        else:
            out.append(f"{_name(p)} fits in an open active roster spot (no drop needed).")
        if alt["acquisition_type"] == "unknown":
            out.append(ACQUISITION_NOTES["unknown"])
    if unknown_alt and unknown_alt.get("player") is not None and p is None:
        up = unknown_alt["player"]
        ug = (unknown_alt.get("gain") or {}).get("ros_delta")
        if ug is not None:
            out.append(f"An available player with unconfirmed availability status ({_name(up)}, {up['position']}) would add {ug:+.1f}, "
                       "but availability status is unconfirmed (verify that he can be added).")
    if waiver and waiver["player"] is not None:
        wg = waiver["gain"]["ros_delta"]
        if wg is not None:
            out.append(f"A waiver claim on {_name(waiver['player'])} ({waiver['player']['position']}) would add {wg:+.1f}, "
                       "but a claim is not guaranteed to succeed.")
    # Position by position (the incoming players' positions). A position no flex slot accepts (K, D/ST,
    # QB without OP) is filled only by its own players, so its slot points compare directly: what the
    # trade adds there versus the best available player there, with the same majority rule. A
    # flex-eligible position's points move between its slots and FLEX, so only the whole-lineup gain
    # of its best available player is stated.
    by_slot = {s["slot"]: s for s in slots}
    flexible = {pos for s in by_slot if is_flex(s) for pos in slot_positions(s)}
    for pa in position_alts:
        pos, pl = pa["position"], pa["player"]
        whole = pa["gain"]["ros_delta"]
        if pl is None or pa.get("same_as_headline") and pos in flexible:
            continue
        if pos in flexible:
            if whole is not None:
                out.append(f"Best available {pos}: adding {_name(pl)} alone would add {whole:+.1f} to {whose} lineup.")
            continue
        row = by_slot.get(pos)
        t_slot, a_slot = (row or {}).get("trade_delta"), pa.get("slot_delta")
        if t_slot is None or a_slot is None or t_slot <= 0:
            continue
        lead = f"An available {pos} could provide a similar lineup improvement: " if 2 * a_slot > t_slot else ""
        out.append(f"{lead}at {pos} the trade adds {t_slot:+.1f}; adding {_name(pl)} adds {a_slot:+.1f} there"
                   + (f" ({whole:+.1f} to the whole lineup)." if whole is not None else "."))
    return [s[0].upper() + s[1:] for s in dict.fromkeys(out)]


def side_alternatives(board: FreeAgencyBoard, team_id: int, core: dict, incoming: list[RosterPlayer],
                      label: str, frontier_positions: tuple[str, ...] = ()) -> dict:
    """Scenarios A-D for one manager. `core` is trades.side_lineups output (the canonical side)."""
    w = board.window
    before, after = core["before"], core["after"]
    a_team, b_team = core["before_team"], core["after_team"]
    alt = board.best_acquisition(team_id, before, kinds=("free_agent",))
    waiver = board.best_acquisition(team_id, before, kinds=("waiver_claim",))
    unknown = board.best_acquisition(team_id, before, kinds=("unknown",))
    with_trade = board.best_acquisition(team_id, after, kinds=("free_agent",))
    c_team, d_team = alt["_team"], with_trade["_team"]
    combined = None                       # scenario E: the trade made together with the same acquisition
    if alt["player"] is not None:
        fa = next((p for p in board.pool if p.espn_player_id == alt["player"]["espn_player_id"]), None)
        if fa is not None:
            try:
                combined = board.trade_with(team_id, core["pre"], {p.espn_player_id for p in incoming}, fa)
            except TradeError as err:         # includes EvaluationUnavailable: no result is invented
                combined = {"available": False, "reason": "no_legal_roster", "team": None, "drops": [],
                            "drop_cost_unknown": False, "retained_player_ids": [], "error": str(err)}
    if combined is not None and not combined.get("available"):
        e_team = None
    elif combined is not None:
        e_team = combined.get("team")
    else:
        e_team = b_team
    none = {k: None for k in DELTA_KEYS} | {"projection_complete": False}
    cu, du = alt["drop_cost_unknown"], with_trade["drop_cost_unknown"]
    eu = cu or bool(combined and combined.get("drop_cost_unknown"))
    cmp = {
        "canonical_gain": _deltas(core["compare"]),
        "free_agent_alternative_gain": alt["gain"],
        "trade_advantage": _known_only(_deltas(compare(c_team, e_team, w)), eu) if e_team is not None else none,
        "trade_instead_of_free_agent": _known_only(_deltas(compare(c_team, b_team, w)), cu),
        "trade_plus_best_free_agent_gain": _known_only(_deltas(compare(a_team, d_team, w)), du),
        "trade_advantage_if_both_add": _known_only(_deltas(compare(c_team, d_team, w)), cu or du),
    }
    g, t = cmp["canonical_gain"], cmp["trade_advantage"]
    cmp["available_without_trade"] = {k: _sub(g[k], t[k]) for k in DELTA_KEYS if k != "projection_complete"} | \
        {"projection_complete": g["projection_complete"] and t["projection_complete"]}
    ga, av = g["ros_delta"], cmp["available_without_trade"]["ros_delta"]
    cmp["share_available_without_trade"] = round(av / ga, 3) if ga is not None and av is not None and ga > 0 else None

    # Interaction classification
    t_ros = t["ros_delta"]
    if ga is None or t_ros is None or av is None:
        interaction_type = "unavailable"
        interaction_note = None
    elif ga <= 0:
        interaction_type = "non_positive_gain"
        interaction_note = None
    elif av < -EPS:
        interaction_type = "positive_synergy"
        interaction_note = "These roster moves interact favorably, so a simple percentage of replaceable gain would be misleading."
    elif av > ga + EPS:
        interaction_type = "negative_interaction"
        interaction_note = "These roster moves interact, so a simple percentage of replaceable gain would be misleading."
    elif abs(av) <= EPS:
        interaction_type = "independent"
        interaction_note = None
    else:
        interaction_type = "ordinary_overlap"
        interaction_note = None

    cmp["interaction_type"] = interaction_type
    cmp["interaction_note"] = interaction_note

    a = alt["gain"]["ros_delta"]
    slots = slot_breakdown({"before": a_team, "trade": b_team, "alternative": c_team})
    by_slot = {s["slot"]: s for s in slots}
    position_alts = []
    head_pos = (alt["player"] or {}).get("position")
    for pos in sorted({p.position for p in incoming if p.position} - {head_pos}):
        pa = board.best_acquisition(team_id, before, kinds=("free_agent",), positions=(pos,))
        row = None
        if pa["player"] is not None:
            row = {s["slot"]: s for s in slot_breakdown({"before": a_team, "alternative": pa["_team"]})}.get(pos)
        position_alts.append({"position": pos, **_public(pa), "slot_delta": row["alternative_delta"] if row else None})
    if head_pos in {p.position for p in incoming} and alt["player"] is not None and head_pos in by_slot:
        position_alts.insert(0, {"position": head_pos, **_public(alt), "slot_delta": by_slot[head_pos]["alternative_delta"],
                                 "same_as_headline": True})
    waiver_out = _public(waiver) if waiver["player"] is not None and (
        a is None or (waiver["gain"]["ros_delta"] or 0) > a + EPS) else None
    unknown_out = _public(unknown) if unknown["player"] is not None and (
        a is None or (unknown["gain"]["ros_delta"] or 0) > a + EPS) else None
    stmts = statements(label, cmp, alt, waiver_out, position_alts, slots, combined=combined, unknown_alt=unknown_out)
    return {
        "team_id": team_id, "team_name": board.state.team_names.get(team_id), "label": label,
        "comparison": cmp,
        "free_agent_alternative": _public(alt),
        "trade_with_free_agent": None if combined is None else
        {k: v for k, v in combined.items() if k != "team"},
        "waiver_claim_alternative": waiver_out,
        "unknown_availability_alternative": unknown_out,
        "position_alternatives": position_alts,
        "with_trade_alternative": _public(with_trade),
        "slot_breakdown": slots,
        "streaming_frontier": [{"position": pos, "no_trade": board.frontier(before, pos),
                                "after_trade": board.frontier(after, pos)}
                               for pos in frontier_positions if pos in board.state.slots],
        "statements": stmts,
        "projection_complete": all(v["projection_complete"] for k, v in cmp.items() if isinstance(v, dict)),
    }


def _public(x: dict) -> dict:
    return {k: v for k, v in x.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------- a trade --

def analyze(state: LeagueState, team_a: int, team_b: int, a_sends: list[int], b_sends: list[int],
            window: Window | None = None, board: FreeAgencyBoard | None = None,
            labels: dict[int, str] | None = None) -> dict:
    """Trade vs. free agency for both managers of one trade. Read-only; never mutates `state`, never
    touches the canonical delta (recomputed here with trades.side_lineups, the shared computation)."""
    window = window or state.window()
    board = board or FreeAgencyBoard(state, window)
    a_sends, b_sends = [int(x) for x in a_sends], [int(x) for x in b_sends]
    names = {p.espn_player_id: p for t in (team_a, team_b) for p in state.rosters[t]}
    moved = {names[x].position for x in a_sends + b_sends}
    frontier = tuple(p for p in STREAMING_POSITIONS if p in moved)
    labels = labels or {}
    sides = {}
    for key, team, sends, gets in (("team_a", team_a, a_sends, b_sends), ("team_b", team_b, b_sends, a_sends)):
        incoming = [names[x] for x in gets]
        before_team = board.team(state.rosters[team])
        core = side_lineups(state, window, team, sends, incoming, before_team)
        sides[key] = side_alternatives(board, team, core, incoming,
                                       labels.get(team) or state.team_names.get(team) or f"Team {team}", frontier)
    pa, pb = sides["team_a"]["free_agent_alternative"]["player"], sides["team_b"]["free_agent_alternative"]["player"]
    contested = None
    if pa and pb and pa["espn_player_id"] == pb["espn_player_id"]:
        contested = {"player": pa, "note": f"Both managers' best alternative is {_name(pa)}: only one of them could add "
                                          "him, so the two alternatives are not simultaneously available."}
    return {
        "label": "Trade vs. free agency (hypothetical; never part of the canonical delta)",
        "note": NOTE, "formulas": FORMULAS, "majority_rule": MAJORITY_RULE,
        "evaluation": {"start_week": window.start, "final_week": window.final,
                       "last_regular_season_week": window.last_regular, "weeks": list(window.weeks)},
        "team_a": sides["team_a"], "team_b": sides["team_b"], "contested": contested,
        "pool": {"available_players_scored": len(board.pool),
                 "excluded_unprojected": dict(sorted(board.excluded_unprojected.items())),
                 "acquisition_types": dict(sorted(Counter(board.kind[p.espn_player_id] for p in board.pool).items()))},
        "projection_source": source_label(state.projection_source, state.projections),
        "assumptions": ASSUMPTIONS, "limitations": LIMITATIONS,
    }


def consistent_with(result: dict, evaluation: dict) -> bool:
    """Do the canonical gains shown here equal the trade evaluator's deltas (both sides)?"""
    return all(result[s]["comparison"]["canonical_gain"][k] == evaluation[s]["deltas"][k]
               for s in ("team_a", "team_b") for k in DELTA_KEYS if k != "projection_complete")


def for_evaluation(state: LeagueState, evaluation: dict, team_a: int, team_b: int, a_sends, b_sends,
                   start_week: int | None = None, board: FreeAgencyBoard | None = None,
                   labels: dict[int, str] | None = None) -> dict:
    """analyze() for an already-evaluated trade (the manual evaluator's result, or discovery's detail
    sides), with a check that its canonical gains equal that evaluation's deltas."""
    try:
        out = analyze(state, team_a, team_b, a_sends, b_sends, window=state.window(start_week), board=board,
                      labels=labels)
    except TradeError as err:
        return {"available": False, "reason": str(err)}
    return {**out, "available": True, "consistent_with_canonical": consistent_with(out, evaluation)}
