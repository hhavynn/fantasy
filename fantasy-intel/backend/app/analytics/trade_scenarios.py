"""Compare saved trade scenarios side by side (issue #183). Hypothetical and read-only.

A manager keeps up to MAX_SCENARIOS candidate trades on their device (frontend/lib/trade-scenarios.ts) and sends
them here only to be checked against the league's current files:

    scenarios=TA:TB:A1.A2:B1.B2,TA:TB:A1:B1      team A id, team B id, the players each team sends

Every scenario is valued with the trade analyzer's own canonical computation (trades.side_lineups: incoming players
active, exact forced drops, optimal lineups before and after for every remaining week), over ONE evaluation window
(trades.evaluation_window) on ONE league state, so all comparable rows share the same scoring horizon and data
version by construction.

What this never does:
  * value a stale scenario: a player who is no longer on the roster that sends him is reported as moved, and the
    scenario gets no deltas (it is not silently re-valued with whoever is there now);
  * add scenarios together: each one is measured alone against the current rosters. They are alternatives, so a
    player shared by two scenarios is a conflict (at most one of them can happen as saved), never a sum;
  * turn a missing projection into 0: an incomplete delta is None, its known part is reported separately;
  * grade, rank by acceptance or attach a probability.
Nothing here reads or writes the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .team_lineups import source_label, stream_summary
from .trades import (
    EvaluationUnavailable,
    LeagueState,
    TradeError,
    assumptions,
    evaluation_window,
    result_label,
    side_lineups,
    validate,
)

CONTRACT = "trade-scenario-compare/v1"
MAX_SCENARIOS = 5
# A bound on the request, not the analyzer's per-side limit (settings.trade_max_players_per_side, checked per scenario).
MAX_IDS_PER_SIDE = 12
ID_BOUND = 10**12            # ESPN player ids are positive; team defenses are negative (-16002)
TEAM_BOUND = 999_999

_INT = r"-?\d{1,13}"
_SCENARIO = re.compile(rf"^(\d{{1,6}}):(\d{{1,6}}):({_INT}(?:\.{_INT})*):({_INT}(?:\.{_INT})*)$")

ALTERNATIVES_NOTE = ("Each scenario is evaluated alone against the current rosters. They are alternatives: their deltas "
                     "never add up, and scenarios that share a player cannot both happen as saved.")
SHARED_NOTE = ("All scenarios share one evaluation window and one data snapshot; a missing projection leaves a delta "
               "unknown (its known part is shown separately), never 0.")
STALE_NOTE = ("A player in this scenario is no longer on the roster that sends him, so it was not re-valued. Rebuild it "
              "in the analyzer from the current rosters.")


class ScenarioError(ValueError):
    """A request that is not a well-formed scenario list (422)."""


@dataclass(frozen=True)
class ScenarioRequest:
    team_a: int
    team_b: int
    a_sends: tuple[int, ...]
    b_sends: tuple[int, ...]

    def key(self) -> tuple:
        """The same trade whichever side is called A: teams and packages, order-insensitive."""
        one = (self.team_a, tuple(sorted(self.a_sends)))
        two = (self.team_b, tuple(sorted(self.b_sends)))
        return tuple(sorted((one, two)))

    def players(self) -> list[int]:
        return list(self.a_sends) + list(self.b_sends)


def parse_scenario(text: str) -> ScenarioRequest:
    m = _SCENARIO.match(text or "")
    if not m:
        raise ScenarioError("A scenario is TEAM_A:TEAM_B:A_SENDS:B_SENDS, with player ids separated by dots.")
    ta, tb = int(m.group(1)), int(m.group(2))
    a = tuple(int(x) for x in m.group(3).split("."))
    b = tuple(int(x) for x in m.group(4).split("."))
    if ta > TEAM_BOUND or tb > TEAM_BOUND:
        raise ScenarioError("A team id is out of range.")
    if len(a) > MAX_IDS_PER_SIDE or len(b) > MAX_IDS_PER_SIDE:
        raise ScenarioError(f"A scenario names at most {MAX_IDS_PER_SIDE} players per side.")
    if any(x == 0 or abs(x) > ID_BOUND for x in a + b):
        raise ScenarioError("A player id is out of range.")
    return ScenarioRequest(ta, tb, a, b)


def parse_scenarios(text: str) -> list[ScenarioRequest]:
    """The comma-separated scenario list, in its saved order (1 to MAX_SCENARIOS)."""
    parts = [p for p in (text or "").split(",")]
    if not text or not parts:
        raise ScenarioError("Name at least one scenario.")
    if len(parts) > MAX_SCENARIOS:
        raise ScenarioError(f"Compare at most {MAX_SCENARIOS} scenarios at once.")
    return [parse_scenario(p) for p in parts]


def _where(state: LeagueState) -> dict[int, int]:
    """Player id -> the team that rosters him now."""
    return {p.espn_player_id: t for t, ps in state.rosters.items() for p in ps}


def moved_players(state: LeagueState, req: ScenarioRequest) -> list[dict]:
    """Players no longer on the roster that sends them: where each one is now (another team, or None for a player
    on no roster in this league's files)."""
    where = _where(state)
    out = []
    for team, sends in ((req.team_a, req.a_sends), (req.team_b, req.b_sends)):
        for pid in sends:
            now = where.get(pid)
            if now != team:
                out.append({"espn_player_id": pid, "expected_team_id": team, "current_team_id": now,
                            "current_team_name": state.team_names.get(now) if now is not None else None})
    return out


def _short_term(cmp: dict) -> dict | None:
    """The first evaluated week's delta: the trade's effect on the next lineup a manager sets."""
    if not cmp["weeks"]:
        return None
    w = cmp["weeks"][0]
    return {"week": w["week"], "delta": w["delta"], "known_delta": w["known_delta"],
            "projection_complete": w["projection_complete"]}


def _side(state: LeagueState, team: int, sends: tuple[int, ...], receives: tuple[int, ...], core: dict) -> dict:
    cmp, res = core["compare"], core["resolution"]
    bt, at = core["before_team"]["summary"], core["after_team"]["summary"]
    return {
        "team_id": team, "team_name": state.team_names.get(team),
        "sends": list(sends), "receives": list(receives),
        "short_term": _short_term(cmp),
        "regular_season_delta": cmp["regular_season_delta"], "playoff_delta": cmp["playoff_delta"],
        "ros_delta": cmp["ros_delta"], "known_ros_delta": cmp["known_ros_delta"],
        "weeks_incomplete": cmp["weeks_incomplete"], "projection_complete": cmp["projection_complete"],
        "missing_players": sorted(set(bt["missing_players"]) | set(at["missing_players"])),
        "forced_drops": [{k: d[k] for k in ("espn_player_id", "name", "position", "is_incoming")}
                         for d in res["forced_drops"]],
    }


def _evaluate(state: LeagueState, window, req: ScenarioRequest, max_per_side: int) -> dict:
    """One scenario: stale (moved players, no deltas), invalid or unavailable (a reason, no deltas), or evaluated."""
    if req.team_a not in state.rosters or req.team_b not in state.rosters:
        unknown = next(t for t in (req.team_a, req.team_b) if t not in state.rosters)
        return {"status": "invalid", "reason": f"Team {unknown} is not in this league."}
    moved = moved_players(state, req)
    if moved:
        return {"status": "stale", "reason": STALE_NOTE, "moved": moved}
    a, b = list(req.a_sends), list(req.b_sends)
    try:
        validate(state, req.team_a, req.team_b, a, b, max_per_side)
        by = {p.espn_player_id: p for t in (req.team_a, req.team_b) for p in state.rosters[t]}
        # Both sides of one trade, valued together: the same canonical computation as the analyzer's deltas.
        core_a = side_lineups(state, window, req.team_a, a, [by[x] for x in b])
        core_b = side_lineups(state, window, req.team_b, b, [by[x] for x in a])
    except EvaluationUnavailable as err:
        return {"status": "unavailable", "reason": str(err)}
    except TradeError as err:
        return {"status": "invalid", "reason": str(err)}
    side_a = _side(state, req.team_a, req.a_sends, req.b_sends, core_a)
    side_b = _side(state, req.team_b, req.b_sends, req.a_sends, core_b)
    return {"status": "evaluated", "reason": None, "team_a": side_a, "team_b": side_b,
            "result_label": result_label(side_a["ros_delta"], side_b["ros_delta"])}


def compare_scenarios(state: LeagueState, requests: list[ScenarioRequest], start_week: int | None = None,
                      max_per_side: int = 6) -> dict:
    """Every scenario over one window on one state. Raises NoRemainingWeeksError / TradeError for the shared horizon
    (a bad start_week fails the whole comparison: rows over different horizons would not be comparable)."""
    if not 1 <= len(requests) <= MAX_SCENARIOS:
        raise ScenarioError(f"Compare between 1 and {MAX_SCENARIOS} scenarios.")
    window = evaluation_window(state, start_week)
    done: dict[tuple, tuple[int, dict]] = {}
    rows = []
    for i, req in enumerate(requests):
        first = done.get(req.key())
        if first is None:
            out = _evaluate(state, window, req, max_per_side)
            done[req.key()] = (i, out)
            dup = None
        else:
            dup, out = first
            # The same trade, possibly with the sides named the other way round: re-orient, never re-value.
            if out["status"] == "evaluated" and out["team_a"]["team_id"] != req.team_a:
                out = {**out, "team_a": out["team_b"], "team_b": out["team_a"],
                       "result_label": result_label(out["team_b"]["ros_delta"], out["team_a"]["ros_delta"])}
        rows.append({"index": i, "request": {"team_a_id": req.team_a, "team_b_id": req.team_b,
                                             "team_a_sends": list(req.a_sends), "team_b_sends": list(req.b_sends)},
                     "shape": f"{len(req.a_sends)}-for-{len(req.b_sends)}", "duplicate_of": dup,
                     "moved": [], **out})
    # Shared players: alternatives that cannot both happen as saved. Duplicates are reported as such, not as conflicts.
    holders: dict[int, list[int]] = {}
    for row, req in zip(rows, requests):
        if row["duplicate_of"] is None:
            for pid in dict.fromkeys(req.players()):
                holders.setdefault(pid, []).append(row["index"])
    conflicts = [{"espn_player_id": pid, "scenarios": idx} for pid, idx in holders.items() if len(idx) > 1]
    for row in rows:
        row["conflicts_with"] = sorted({j for c in conflicts if row["index"] in c["scenarios"]
                                        for j in c["scenarios"] if j != row["index"]})
    evaluated = [x for x in rows if x["status"] == "evaluated"]
    return {
        "contract": CONTRACT,
        "submitted": False,
        "max_scenarios": MAX_SCENARIOS,
        "evaluation": {"start_week": window.start, "final_week": window.final,
                       "last_regular_season_week": window.last_regular, "weeks": list(window.weeks),
                       "short_term_week": window.weeks[0] if window.weeks else None,
                       "start_week_overridden": start_week is not None,
                       "earliest_actionable_week": state.start_week},
        "scenarios": rows,
        "conflicts": conflicts,
        "summary": {"scenarios": len(rows), "evaluated": len(evaluated),
                    "incomplete": sum(1 for x in evaluated
                                      if not (x["team_a"]["projection_complete"] and x["team_b"]["projection_complete"])),
                    "stale": sum(1 for x in rows if x["status"] == "stale"),
                    "invalid": sum(1 for x in rows if x["status"] in ("invalid", "unavailable"))},
        # The source is named only when it supplied values; with no projections at all it is unavailable.
        "projection_source": source_label(state.projection_source) if state.projections else None,
        "trade_impact_source": "exact optimal-lineup simulation",
        "kdst_streaming": stream_summary(state.projections, window),
        "notes": [ALTERNATIVES_NOTE, SHARED_NOTE],
        "assumptions": assumptions(state),
    }
