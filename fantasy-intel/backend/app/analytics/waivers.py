"""Waiver intelligence: player side x my-roster side x league side.

All scores are explainable weighted sums; every component is stored.
League interest is LOW / MEDIUM / HIGH (no fake probabilities).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean

from ..db.connection import fetch_dicts, replace_where, table_exists, utcnow
from ..espn.constants import FANTASY_POSITIONS, IR_SLOT
from .availability import ESPN_STATUS, NFL_REPORT_STATUS, availability, latest_injuries
from .lineup import FIXED_SLOTS, assign_lineup, lineup_gain, lineup_slots
from .player_scores import percentile_scores
from .waiver_roster import plan_roster_move
from .waiver_decisions import acquisition_utility, classify_action, evaluate_streaming, resource_diagnostics
from .waiver_data import (attach_roster_evidence, comparable_free_agents, load_waiver_context, select_candidates,
                          stale_snapshot, streaming_evidence, streaming_floor)

DEPTH_EXTRA = {"RB": 1, "WR": 1}          # bench depth wanted beyond actual starters
# How useful a non-starting player at the position is (flex/bye/injury cover).
DEPTH_UTILITY = {"RB": 1.0, "WR": 1.0, "TE": 0.3, "QB": 0.3, "K": 0.15, "D/ST": 0.15}
# Opportunity penalty by availability status (points on the 0-10 scale).
INJURY_ADJ = {"QUESTIONABLE": -0.5, "DOUBTFUL": -2.0, "OUT": -3.0, "IR": -3.0, "SUSPENDED": -3.0}

W_OPP, W_FIT = 0.55, 0.45
CLAIM_SCORE, WAIT_SCORE = 6.0, 4.5
CLAIM_MARGIN = 0.5                         # value points the add must beat the drop by
CLAIM_MIN_FIT = 3.0                        # a CLAIM must actually help *my* roster
INTERESTED = 0.55
INTEREST_HIGH, INTEREST_MED = 0.45, 0.25

BASIS_TEXT = {
    "xfp": "recent expected-fantasy-points (xFP) percentile",
    "targets+carries": "recent targets+carries percentile (xFP unavailable)",
    "espn_points": "recent ESPN league-points percentile (no NFL usage data)",
    "unavailable": "no usage or scoring data",
}


# --------------------------------------------------------------- helpers --

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class PlayerValue:
    espn_player_id: int
    position: str | None
    value: float | None
    opportunity: float | None
    production: float | None
    trend_label: str | None
    classification: str | None
    basis: str                      # xfp | targets+carries | espn_points | unavailable
    gsis_id: str | None = None
    games: int | None = None
    extras: dict = field(default_factory=dict)


# ------------------------------------------------------- lineup + needs --

def team_state(roster: list[dict], values: dict[int, PlayerValue], roster_slots: dict[str, int]) -> dict:
    """Availability-weighted players and the team's optimal current lineup.

    roster rows: espn_player_id, position, lineup_slot_id, injury_status, nfl_report_status, full_name.
    """
    players = []
    for p in roster:
        av = availability(p.get("injury_status"), p.get("lineup_slot_id"), p.get("nfl_report_status"))
        v = values.get(p["espn_player_id"])
        raw = v.value if v is not None and v.value is not None else None
        players.append({
            "espn_player_id": p["espn_player_id"], "position": p.get("position"), "full_name": p.get("full_name"),
            "injury_status": p.get("injury_status"), "nfl_report_status": p.get("nfl_report_status"),
            "lineup_slot_id": p.get("lineup_slot_id"), "raw_value": raw,
            "value": round((raw or 0.0) * av["weight"], 3), "available": av["weight"] > 0, "availability": av,
        })
    slots = lineup_slots(roster_slots)
    return {"players": players, "slots": slots, "roster_slots": roster_slots, "lineup": assign_lineup(players, slots)}


def _empty_slots(state: dict, pos: str) -> int:
    """Dedicated slots for `pos` that no eligible player could fill (holes count as 0 value)."""
    return sum(1 for a in state["lineup"] if a["slot"] == pos and a["player_id"] is None)


def position_starter_values(state: dict, pos: str) -> list[float]:
    """Values of every lineup starter whose *player* position is `pos`, whatever slot he
    occupies (fixed, FLEX, RB/WR, WR/TE, OP), plus 0 for each unfilled dedicated `pos` slot.
    Each assigned player sits in exactly one slot, so nothing is counted twice."""
    vals = [a["value"] for a in state["lineup"] if a["player_id"] is not None and a["position"] == pos]
    return vals + [0.0] * _empty_slots(state, pos)


def _has_value_data(state: dict, pos: str) -> bool:
    return any(p["raw_value"] is not None for p in state["players"] if p["position"] == pos)


def league_starter_averages(states: dict[int, dict], roster_slots: dict[str, int]) -> dict[str, float]:
    """Mean value of each team's starters at a position (fixed and flex-assigned), averaged
    across teams that have value data at that position and start someone there."""
    out = {}
    for pos in FIXED_SLOTS:
        per_team = [mean(v) for st in states.values() if _has_value_data(st, pos)
                    for v in [position_starter_values(st, pos)] if v]
        if per_team:
            out[pos] = round(mean(per_team), 2)
    return out


def team_needs(state: dict, roster_slots: dict[str, int], league_starter_avg: dict[str, float]) -> dict[str, dict]:
    """Need per position (0..1) from the actual lineup assignment (flex counted once)."""
    out = {}
    for pos in FANTASY_POSITIONS:
        fixed = int(roster_slots.get(pos, 0))
        flex_filled = sum(1 for a in state["lineup"] if a["is_flex"] and a["position"] == pos)
        starters = fixed + flex_filled
        players = [p for p in state["players"] if p["position"] == pos]
        weights = [p["availability"]["weight"] for p in players]
        eff = round(sum(weights), 2)
        target = (starters + DEPTH_EXTRA.get(pos, 0)) if starters else 0
        starter_vals = position_starter_values(state, pos)
        has_data = _has_value_data(state, pos)
        starter_value = mean(starter_vals) if starter_vals and has_data else None
        weakest = min(starter_vals) if starter_vals and has_data else None
        shortage = clamp((target - eff) / target) if target else 0.0
        gap = 0.0
        if starter_value is not None and league_starter_avg.get(pos) is not None:
            gap = clamp((league_starter_avg[pos] - starter_value) / 4.0)
        need = round(clamp(0.55 * clamp(shortage * 2) + 0.45 * gap), 3) if starters or fixed else 0.0
        out[pos] = {
            "position": pos, "starters_required": starters, "rostered": len(players),
            "healthy": sum(1 for w in weights if w == 1.0), "unavailable": sum(1 for w in weights if w == 0.0),
            "effective_available": eff,
            "starter_value": round(starter_value, 2) if starter_value is not None else None,
            "need_score": need,
            "details": {
                "fixed_slots": fixed, "flex_slots_filled": flex_filled, "depth_target": target,
                "weakest_starter_value": round(weakest, 2) if weakest is not None else None,
                "shortage": round(shortage, 3), "quality_gap": round(gap, 3),
                "league_avg_starter_value": league_starter_avg.get(pos),
                "availability": [{"espn_player_id": p["espn_player_id"], "name": p["full_name"],
                                  "status": p["availability"]["status"], "weight": p["availability"]["weight"]}
                                 for p in players],
                "starter_values": [round(v, 2) for v in starter_vals],
                "formula": "need = 0.55*min(1, 2*shortage) + 0.45*quality_gap; quality uses every lineup "
                           "starter at the position (fixed + flex); shortage uses availability weights "
                           "(AVAILABLE 1, QUESTIONABLE 0.75, DOUBTFUL 0.15, OUT/IR/SUSPENDED 0)",
            },
        }
    return out


# --------------------------------------------------------- suggested drop --

def _empty_fixed(players: list[dict], slots: list[str]) -> int:
    return sum(1 for a in assign_lineup(players, slots) if a["player_id"] is None and not a["is_flex"])


def suggest_drop(state: dict, candidate: dict | None = None) -> dict | None:
    """Lowest rest-of-season-value bench player whose removal (with the candidate
    added) leaves no fixed lineup slot unfillable. Never a current starter or IR."""
    starters = {a["player_id"] for a in state["lineup"] if a["player_id"] is not None}
    base = state["players"] + ([candidate] if candidate else [])
    holes_before = _empty_fixed(base, state["slots"])
    bench = [p for p in state["players"]
             if p["espn_player_id"] not in starters and p["lineup_slot_id"] != IR_SLOT]
    for p in sorted(bench, key=lambda p: (p["raw_value"] if p["raw_value"] is not None else 0.0, p["espn_player_id"])):
        remaining = [q for q in base if q["espn_player_id"] != p["espn_player_id"]]
        if _empty_fixed(remaining, state["slots"]) > holes_before:
            continue  # would leave a fixed slot unfillable
        return {"espn_player_id": p["espn_player_id"], "name": p["full_name"], "position": p["position"],
                "value": round(p["raw_value"] or 0.0, 2), "value_known": p["raw_value"] is not None,
                "availability": p["availability"]["status"]}
    return None


# --------------------------------------------------------- league interest --

def manager_interest(team_id: int, pos: str, needs: dict[int, dict], profiles: dict[int, dict],
                     league_pos_share: dict[str, float]) -> tuple[float, list[str], dict]:
    prof = profiles.get(team_id, {})
    need = needs.get(team_id, {}).get(pos, {}).get("need_score", 0.0)
    reasons = []
    share = prof.get("add_share", {}).get(pos)
    league_share = league_pos_share.get(pos)
    if prof.get("adds_total", 0) >= 5 and share is not None and league_share:
        ratio = share / league_share
        affinity = clamp(ratio / 2)
        if ratio >= 1.3:
            reasons.append(f"adds {pos} at {ratio:.1f}x the league rate")
    else:
        ratio, affinity = None, 0.5
    aggression = prof.get("aggression_pct", 0.5)
    resource = prof.get("resource", 0.5)
    if need >= 0.5:
        reasons.append(f"{pos} need {need:.2f}")
    if aggression >= 0.75:
        reasons.append("one of the most active waiver managers")
    if prof.get("resource_note"):
        reasons.append(prof["resource_note"])
    score = 0.4 * need + 0.25 * affinity + 0.2 * aggression + 0.15 * resource
    return round(score, 3), reasons, {"need": need, "affinity": round(affinity, 3), "pos_add_ratio": ratio,
                                      "aggression": aggression, "resource": resource}


def interest_label(score: float) -> str:
    return "HIGH" if score >= INTEREST_HIGH else "MEDIUM" if score >= INTEREST_MED else "LOW"


# --------------------------------------------------------------- evaluate --

def evaluate_candidate(c: dict, cv: PlayerValue, *, my_team_id: int, needs: dict[int, dict], my_state: dict,
                       profiles: dict[int, dict], league_pos_share: dict[str, float], team_names: dict[int, str],
                       scarcity: dict[str, int], context_reasons: list[str] | None = None,
                       decision_context: dict | None = None) -> dict:
    pos = c["position"]
    reasons: list[str] = []
    av = availability(c.get("injury_status"), None, c.get("nfl_report_status"))
    injured = av["weight"] == 0.0

    # Player side
    basis = cv.basis if cv.basis in BASIS_TEXT else "unavailable"
    base = cv.opportunity if cv.opportunity is not None else cv.value
    trend_adj = {"UP": 1.0, "DOWN": -1.0}.get(cv.trend_label or "", 0.0)
    injury_adj = INJURY_ADJ.get(av["status"], 0.0)
    opportunity = round(clamp((base or 0.0) + trend_adj + injury_adj, 0, 10), 1) if base is not None else None
    if base is not None:
        reasons.append(f"Opportunity {base:.1f}/10 at {pos}: {BASIS_TEXT[basis]}")
    else:
        reasons.append(f"Opportunity unknown: {BASIS_TEXT['unavailable']}")
    if trend_adj:
        reasons.append(f"Role {'rising' if trend_adj > 0 else 'falling'} ({trend_adj:+.0f})")
    reasons += context_reasons or []
    if injury_adj:
        reasons.append(f"Currently {av['status']} ({', '.join(av['sources'])}; {injury_adj:+.1f})")

    # My roster side: does he actually enter my optimal lineup?
    my_need = needs.get(my_team_id, {}).get(pos, {})
    need = my_need.get("need_score", 0.0)
    cand_value = cv.value or 0.0
    cand = {"espn_player_id": c["espn_player_id"], "position": pos, "full_name": c.get("full_name"),
            "lineup_slot_id": None, "raw_value": cv.value, "value": round(cand_value * av["weight"], 3),
            "available": av["weight"] > 0, "availability": av}
    lg = lineup_gain(my_state["players"], my_state["slots"], cand)
    upgrade = clamp(lg["gain"] / 3.0)
    ctx = decision_context or {}
    cand.update(projections=ctx.get("projections", {}).get(c["espn_player_id"], {}),
                bye_weeks=ctx.get("candidate_byes", []), rising=cv.trend_label == "UP")
    def roster_move(player):
        cache = ctx.get("move_cache")
        pid = player["espn_player_id"]
        if cache is None:
            return plan_roster_move(my_state, player, ctx.get("constraints"))
        if pid not in cache:
            cache[pid] = plan_roster_move(my_state, player, ctx.get("constraints"))
        return cache[pid]

    move = roster_move(cand)
    drop = move["drop"]
    margin = cand_value - drop["value"] if drop else None
    bench_upgrade = clamp(margin / 4.0) if margin is not None else 0.0
    depth_utility = DEPTH_UTILITY.get(pos, 0.5)
    fit = 10.0 * (0.35 * need + 0.35 * upgrade + 0.30 * bench_upgrade * depth_utility)
    if drop is not None and margin is not None and margin <= 0:
        fit *= 0.5
        reasons.append(f"Not clearly better than your weakest bench option ({drop['name']}, value {drop['value']:.1f})")
    fit = round(clamp(fit, 0, 10), 1)
    if need >= 0.5:
        reasons.append(f"Fills a real need at {pos} (need {need:.2f})")
    if lg["enters_lineup"]:
        reasons.append(f"Would enter your lineup at {lg['slot']} (+{lg['gain']:.1f} lineup value)")
    elif depth_utility < 0.5 and need < 0.5:
        reasons.append(f"Wouldn't start for you and a backup {pos} rarely plays")
    scarcity_count = scarcity.get(pos, 0)
    scarcity_bonus = 0.5 if scarcity_count <= 2 else 0.25 if scarcity_count <= 5 else 0.0
    if fit < CLAIM_MIN_FIT:
        scarcity_bonus = 0.0   # scarcity only matters if he'd help my roster
    if scarcity_bonus:
        reasons.append(f"Few startable {pos}s available ({scarcity_count})")

    # League side
    others = []
    for tid in needs:
        if tid == my_team_id:
            continue
        score, why, comp = manager_interest(tid, pos, needs, profiles, league_pos_share)
        others.append({"team_id": tid, "name": team_names.get(tid), "interest": score, "reasons": why,
                       "components": comp})
    others.sort(key=lambda o: (-o["interest"], o["team_id"]))
    attractiveness = clamp(0.8 * (opportunity or 0) / 10 + 0.2 * clamp((c.get("percent_change") or 0) / 10))
    interested = [o for o in others if o["interest"] >= INTERESTED]
    max_interest = others[0]["interest"] if others else 0.0
    interest_score = round(attractiveness * (0.6 * max_interest + 0.4 * min(1.0, len(interested) / 4)), 3)
    league_interest = interest_label(interest_score)
    if others and others[0]["reasons"] and league_interest != "LOW":
        reasons.append(f"Competition: {others[0]['name']} ({'; '.join(others[0]['reasons'])})")

    waiver_score = round(W_OPP * (opportunity or 0) + W_FIT * fit + scarcity_bonus, 2)
    if injured:
        rec = "WAIT" if (base or 0) >= 6 else "IGNORE"
        reasons.append("Unavailable now: stash only if you have the roster room" if rec == "WAIT"
                       else "Unavailable and low role")
    elif waiver_score >= CLAIM_SCORE and fit >= CLAIM_MIN_FIT and (margin is None or margin >= CLAIM_MARGIN):
        rec = "CLAIM"
    elif waiver_score >= WAIT_SCORE:
        rec = "WAIT"
    else:
        rec = "IGNORE"
    utility = acquisition_utility(my_state, cand, move, need=need, opportunity=opportunity)
    stream = evaluate_streaming(pos, ctx.get("streaming", {}), required=int(my_state.get("roster_slots", {}).get(pos, 0))) if pos in ("K", "D/ST") else None
    stream_benefit = bool(stream and stream["category"] in ("SHORT_TERM_STREAMER", "MULTIWEEK_HOLD"))
    if stream_benefit:
        utility["ranking_utility"] = max(utility["ranking_utility"], stream["gain"])
    alternative = None
    for choice in ctx.get("alternatives", []):
        alt_cand = choice["player"]
        alt_move = roster_move(alt_cand)
        if alt_move["status"] not in ("ADD_WITH_DROP", "ADD_WITHOUT_DROP"):
            continue
        alt_utility = acquisition_utility(my_state, alt_cand, alt_move, need=need, opportunity=None)
        alt_stream_gain = 0
        if stream_benefit and stream["weeks"]:
            w = stream["weeks"][0]["week"]
            points = alt_cand["projections"].get(w)
            if points is not None and stream.get("incumbent_points") is not None:
                alt_stream_gain = points - stream["incumbent_points"]
        if ((alt_utility["meaningful"] or alt_stream_gain >= 1)
                and max(alt_utility["ranking_utility"], alt_stream_gain) >= utility["ranking_utility"] - 0.5):
            alternative = choice["resource"]
            break
    resource = resource_diagnostics(c.get("availability_status"), ctx.get("rules", {"is_faab": False}),
                                    ctx.get("team", {}), meaningful=utility["meaningful"] or stream_benefit, alternative=alternative)
    known_injury = ((c.get("injury_status") or "").upper() in ESPN_STATUS or c.get("nfl_report_status") in NFL_REPORT_STATUS)
    usable = av["weight"] > 0 if known_injury else None
    if ctx.get("stale"):
        usable = None
    decision = classify_action(utility, move, resource, available=usable, actionable=ctx.get("actionable", True), streaming=stream)
    # Strong-but-unproven depth belongs on the watchlist, never a resource-spending claim.
    if decision["action"] == "NOT_AN_UPGRADE" and not stream and (cv.value or 0) >= 4 and (opportunity or 0) >= 6:
        decision.update(action="MONITOR", recommendation="WAIT")
    rec = decision["recommendation"]
    risks = list(ctx.get("warnings", [])) + list(move.get("warnings", []))
    if move["status"] not in ("ADD_WITH_DROP", "ADD_WITHOUT_DROP"):
        risks.append(move["summary"])
    if utility["starter_gain"] is None:
        risks.append("Historical roster comparison is incomplete; unknown value is not zero.")
    if av["status"] != "AVAILABLE":
        risks.append(f"Current availability is {av['status']}; verify status before acting.")
    if not known_injury:
        risks.append("Injury status is unknown; verify current availability.")
    elif ctx.get("stale"):
        risks.append("Player availability/injury snapshot is older than 72 hours or undated; refresh ESPN before acting.")
    if not cand["projections"]:
        risks.append("No forward ESPN projection evidence; lineup values are historical heuristics.")
    if utility["forward"]["missing_weeks"]:
        risks.append("Some forward lineup comparisons are incomplete; missing projections are not zero.")
    if stream and not stream["complete"]:
        risks.append(stream["summary"])
    if stream_benefit and drop and drop["forward"]["total_gain"] is not None and drop["forward"]["total_gain"] < 0:
        risks.append(f"Short-term stream sacrifices {-drop['forward']['total_gain']:.1f} projected points over the remaining horizon; reassess next week.")
    decision.update(version=3, roster_move=move, utility=utility, resources=resource, streaming=stream,
                    risks=risks, confidence="LIMITED" if risks else "SUPPORTED",
                    coverage=ctx.get("coverage"), weeks=ctx.get("weeks", []))
    reasons += [utility["summary"], move["summary"], resource["summary"]]

    return {
        "espn_player_id": c["espn_player_id"],
        "position": pos,
        "availability_status": c.get("availability_status"),
        "opportunity_score": opportunity,
        "roster_fit_score": fit,
        "league_interest": league_interest,
        "interest_score": interest_score,
        "waiver_score": waiver_score,
        "recommendation": rec,
        "suggested_drop_id": drop["espn_player_id"] if drop else None,
        "components": {
            "player": {"basis": basis, "basis_description": BASIS_TEXT[basis], "base_opportunity": base,
                       "trend_label": cv.trend_label, "trend_adj": trend_adj, "availability": av,
                       "injury_adj": injury_adj, "value": cv.value, "classification": cv.classification,
                       "percent_owned": c.get("percent_owned"), "percent_change": c.get("percent_change"),
                       **cv.extras},
            "roster": {"need": need, "my_starter_value": my_need.get("starter_value"),
                       "my_weakest_starter_value": (my_need.get("details") or {}).get("weakest_starter_value"),
                       "lineup_gain": lg["gain"], "enters_lineup": lg["enters_lineup"], "lineup_slot": lg["slot"],
                       "displaced_id": lg["displaced_id"],
                       "upgrade": round(upgrade, 3), "bench_upgrade": round(bench_upgrade, 3),
                       "depth_utility": depth_utility,
                       "fit_formula": "10*(0.35*need + 0.35*clamp(lineup_gain/3) + 0.30*upgrade_over_drop*depth_utility)",
                       "suggested_drop": drop, "value_margin_vs_drop": round(margin, 2) if margin is not None else None},
            "league": {"attractiveness": round(attractiveness, 3), "max_manager_interest": max_interest,
                       "managers_interested": len(interested), "scarcity_available_startable": scarcity_count,
                       "scarcity_bonus": scarcity_bonus},
            "decision": decision,
            "formula": "waiver_score = 0.55*opportunity + 0.45*fit + scarcity_bonus (legacy heuristic only); "
                       "V3 actions use legal roster moves, demonstrated lineup/depth benefit and transaction resources",
        },
        "reasons": reasons,
        "competitors": [{k: o[k] for k in ("team_id", "name", "interest", "reasons")} for o in others[:3]],
    }


# ------------------------------------------------------------------ loader --

def load_values(con, season: int, current_week: int) -> dict[int, PlayerValue]:
    values: dict[int, PlayerValue] = {}
    for r in fetch_dicts(con, """
        SELECT m.espn_player_id, fp.position, s.* EXCLUDE (espn_player_id, position)
        FROM player_scores s JOIN player_id_map m ON m.gsis_id = s.gsis_id
        JOIN fantasy_players fp ON fp.espn_player_id = m.espn_player_id
        WHERE s.season = ?
    """, [season]):
        values[r["espn_player_id"]] = PlayerValue(
            r["espn_player_id"], r["position"], r["value_score"], r["opportunity_score"], r["production_score"],
            r["role_trend"], r["market_signal"], r["opportunity_basis"] or "unavailable", r["gsis_id"], r["games"])
    # Fallback: recent ESPN league points percentile within position.
    pts = fetch_dicts(con, """
        SELECT p.espn_player_id, fp.position, avg(p.actual_points) AS ppg, count(*) AS n
        FROM (SELECT *, row_number() OVER (PARTITION BY espn_player_id ORDER BY scoring_period DESC) rn
              FROM espn_player_weekly_points
              WHERE season = ? AND actual_points IS NOT NULL AND scoring_period < ?) p
        JOIN fantasy_players fp USING (espn_player_id)
        WHERE rn <= 3 GROUP BY 1, 2 HAVING count(*) >= 2
    """, [season, current_week])
    by_pos: dict[str, dict[int, float]] = defaultdict(dict)
    for r in pts:
        by_pos[r["position"]][r["espn_player_id"]] = r["ppg"]
    for pos, d in by_pos.items():
        for pid, pct in percentile_scores(d).items():
            if pid not in values or values[pid].value is None:
                values[pid] = PlayerValue(pid, pos, pct, None, pct, None, None, "espn_points",
                                          extras={"recent_league_ppg": round(d[pid], 2)})
    return values


def load_profiles(con, league_id: int, season: int, teams: list[dict], is_faab: bool,
                  faab_budget: float | None) -> tuple[dict[int, dict], dict[str, float]]:
    metrics = defaultdict(dict)
    for r in fetch_dicts(con, "SELECT team_id, metric, value, numerator FROM manager_metrics WHERE league_id=? AND season=?",
                         [league_id, season]):
        metrics[r["team_id"]][r["metric"]] = r
    apw = {t: m.get("adds_per_week", {}).get("value") or 0 for t, m in metrics.items()}
    apw_pct = {t: v / 10 for t, v in percentile_scores(apw).items()}
    profiles = {}
    totals = defaultdict(float)
    for t in teams:
        tid = t["team_id"]
        m = metrics.get(tid, {})
        shares = {}
        for pos in FANTASY_POSITIONS:
            key = pos.lower().replace("/", "")
            shares[pos] = m.get(f"{key}_add_share", {}).get("value")
            totals[pos] += m.get(f"{key}_adds", {}).get("value") or 0
        if is_faab and faab_budget:
            remaining = max(0.0, faab_budget - t["faab_spent"]) if t.get("faab_spent") is not None else None
            resource = remaining / faab_budget if remaining is not None else 0.5
            note = f"${remaining:.0f} FAAB left" if remaining is not None and resource >= 0.6 else None
        else:
            rank = t.get("waiver_rank")
            resource = 1 - (rank - 1) / max(1, len(teams) - 1) if rank else 0.5
            note = f"waiver priority #{rank}" if rank and rank <= 3 else None
        profiles[tid] = {"add_share": shares, "adds_total": m.get("adds_total", {}).get("value") or 0,
                         "aggression_pct": round(apw_pct.get(tid, 0.5), 3), "resource": round(resource, 3),
                         "resource_note": note}
    all_adds = sum(totals.values())
    league_share = {pos: (totals[pos] / all_adds if all_adds else None) for pos in FANTASY_POSITIONS}
    return profiles, league_share


def build_waivers(con, kdst_streaming: bool = True) -> dict:
    """Waiver recommendations for your team. `kdst_streaming` credits holding a K or D/ST only for future weeks
    it beats the weekly streaming baseline (waiver_data.streaming_floor)."""
    lg = fetch_dicts(con, "SELECT * FROM leagues ORDER BY synced_at DESC LIMIT 1")
    if not lg:
        return {"skipped": "no ESPN league synced"}
    lg = lg[0]
    league_id, season, week = lg["league_id"], lg["season"], lg["current_week"] or 1
    decision_ctx = load_waiver_context(con, lg)
    decision_ctx["move_cache"] = {}  # per analytics build, fixed roster/settings; never shared across requests
    slots = json.loads(lg["roster_slots"]) if isinstance(lg["roster_slots"], str) else (lg["roster_slots"] or {})
    teams = fetch_dicts(con, "SELECT * FROM fantasy_teams WHERE league_id=? AND season=?", [league_id, season])
    mine = [t["team_id"] for t in teams if t["is_mine"]]
    if not mine:
        return {"skipped": "your team is not identified (set ESPN_MY_TEAM_ID)"}
    my_team_id = mine[0]
    team_names = {t["team_id"]: t["name"] for t in teams}

    values = load_values(con, season, week)
    reports = current_week_reports(con, season, week)
    roster_rows = fetch_dicts(con, """
        SELECT l.team_id, l.espn_player_id, l.lineup_slot_id, l.is_starter, fp.position, fp.full_name,
               coalesce(fp.injury_status, l.injury_status) AS injury_status, m.gsis_id, fp.pro_team, fp.detail_synced_at,
               l.synced_at AS roster_synced_at
        FROM lineup_snapshots l JOIN fantasy_players fp USING (espn_player_id)
        LEFT JOIN player_id_map m USING (espn_player_id)
        WHERE l.league_id=? AND l.season=? AND l.scoring_period = ?
    """, [league_id, season, week])
    rosters: dict[int, list[dict]] = defaultdict(list)
    for r in roster_rows:
        r["nfl_report_status"] = reports.get(r["gsis_id"])
        rosters[r["team_id"]].append(r)
    states = {tid: team_state(rosters.get(tid, []), values, slots) for tid in team_names}
    attach_roster_evidence(states[my_team_id], rosters.get(my_team_id, []), decision_ctx)
    decision_ctx["constraints"]["snapshot_complete"] = bool(rosters.get(my_team_id)) and all(
        not stale_snapshot(r["roster_synced_at"], decision_ctx["now"]) for r in rosters[my_team_id])
    if not decision_ctx["constraints"]["snapshot_complete"]:
        decision_ctx["warnings"].append("Roster snapshot is missing, undated or older than 72 hours; active capacity cannot be confirmed.")
    lsa = league_starter_averages(states, slots)
    needs = {tid: team_needs(states[tid], slots, lsa) for tid in team_names}
    now = utcnow()
    need_rows = [{"league_id": league_id, "season": season, "team_id": tid, **n, "computed_at": now}
                 for tid, per in needs.items() for n in per.values()]
    replace_where(con, "team_position_needs", need_rows, "league_id=? AND season=?", [league_id, season])

    profiles, league_share = load_profiles(con, league_id, season, teams, bool(lg["is_faab"]), lg["faab_budget"])
    cands = fetch_dicts(con, """
        SELECT fp.*, m.gsis_id FROM fantasy_players fp LEFT JOIN player_id_map m USING (espn_player_id)
        WHERE availability_status IN ('FREEAGENT', 'WAIVERS') AND position IN ('QB','RB','WR','TE','K','D/ST')
        AND coalesce(fp.on_team_id, 0) = 0
    """)
    rostered_ids = {r["espn_player_id"] for r in roster_rows}
    pool = [c for c in cands if c["espn_player_id"] not in rostered_ids]
    rostered_conflicts = len(cands) - len(pool)
    if kdst_streaming:
        states[my_team_id]["stream"] = streaming_floor(pool, decision_ctx, slots)
    depth = _latest_depth(con, season)
    cands, coverage = select_candidates(pool, values, decision_ctx["projections"], depth, decision_ctx["weeks"])
    candidates_by_id = {p["espn_player_id"]: p for p in cands}
    coverage["excluded_rostered_conflicts"] = rostered_conflicts
    scarcity = {pos: sum(1 for c in cands if c["position"] == pos and c["espn_player_id"] in values
                         and (values[c["espn_player_id"]].value or 0) >= (lsa.get(pos) or 99))
                for pos in FANTASY_POSITIONS}
    contexts = load_player_contexts(con, season)
    out = []
    for c in cands:
        c["nfl_report_status"] = reports.get(c.get("gsis_id"))
        cv = values.get(c["espn_player_id"]) or PlayerValue(c["espn_player_id"], c["position"], None, None, None,
                                                            None, None, "unavailable")
        if cv.gsis_id and cv.gsis_id in depth:
            cv.extras = {**cv.extras, "depth_chart": depth[cv.gsis_id]}
        synced = c.get("detail_synced_at")
        stale = stale_snapshot(synced, decision_ctx["now"])
        pos = c["position"]
        # A comparable freely available option makes preserving rolling priority concrete.
        alternatives = []
        for alternative in comparable_free_agents(c, values, cands, decision_ctx, reports):
            alt_row = candidates_by_id[alternative["espn_player_id"]]
            alt_value = values.get(alt_row["espn_player_id"])
            raw = alt_value.value if alt_value else None
            alt_cand = {**alt_row, "raw_value": raw, "value": raw, "available": True, "lineup_slot_id": None,
                        "rising": bool(alt_value and alt_value.trend_label == "UP"),
                        "bye_weeks": [w for w in decision_ctx["weeks"] if w in decision_ctx["covered"] and alt_row.get("pro_team") and (alt_row["pro_team"], w) not in decision_ctx["games"]],
                        "projections": decision_ctx["projections"].get(alt_row["espn_player_id"], {})}
            alternatives.append({"player": alt_cand, "resource": alternative})
        per = {**decision_ctx, "coverage": coverage, "team": next(t for t in teams if t["team_id"] == my_team_id),
               "stale": stale, "alternatives": alternatives,
               "candidate_byes": [w for w in decision_ctx["weeks"] if w in decision_ctx["covered"] and c.get("pro_team") and (c["pro_team"], w) not in decision_ctx["games"]]}
        if pos in ("K", "D/ST"):
            per["streaming"] = streaming_evidence(c, states[my_team_id], decision_ctx, pool)
        row = evaluate_candidate(c, cv, my_team_id=my_team_id, needs=needs, my_state=states[my_team_id],
                                 profiles=profiles, league_pos_share=league_share, team_names=team_names,
                                 scarcity=scarcity, context_reasons=context_reasons(contexts.get(cv.gsis_id)), decision_context=per)
        row.update(league_id=league_id, season=season, computed_at=now)
        out.append(row)
    replace_where(con, "waiver_recommendations", out, "league_id=? AND season=?", [league_id, season])
    return {"candidates": len(out), "claims": sum(1 for r in out if r["recommendation"] == "CLAIM"), "coverage": coverage}


def current_week_reports(con, season: int, week: int) -> dict[str, str]:
    """gsis -> NFL report status, only if the newest report is for this scoring week."""
    out = {}
    for g, r in latest_injuries(con, season).items():
        if r["week"] == week and r["report_status"]:
            out[g] = r["report_status"]
    return out


def load_player_contexts(con, season: int) -> dict[str, dict]:
    if not table_exists(con, "player_context"):
        return {}
    out = {}
    for r in fetch_dicts(con, "SELECT gsis_id, labels, evidence FROM player_context WHERE season = ?", [season]):
        out[r["gsis_id"]] = {"labels": json.loads(r["labels"]) if isinstance(r["labels"], str) else r["labels"],
                             "evidence": json.loads(r["evidence"]) if isinstance(r["evidence"], str) else r["evidence"]}
    return out


def context_reasons(ctx: dict | None) -> list[str]:
    """Short, evidence-backed lines from the player context (see analytics/context.py)."""
    if not ctx:
        return []
    # The role-trend line is already stated (with its score adjustment) by evaluate_candidate.
    return [h for h in (ctx.get("evidence") or {}).get("headlines", []) if h not in ("Role rising", "Role shrinking")]


def _latest_depth(con, season: int) -> dict[str, dict]:
    if not table_exists(con, "nfl_depth_charts"):
        return {}
    rows = fetch_dicts(con, """
        SELECT gsis_id, team, pos_abb, pos_rank, dt FROM (
          SELECT *, max(dt) OVER (PARTITION BY team) AS latest FROM nfl_depth_charts WHERE season = ?)
        WHERE dt = latest AND gsis_id IS NOT NULL AND pos_abb IN ('QB','RB','WR','TE')
    """, [season])
    out: dict[str, dict] = {}
    for r in rows:
        cur = out.get(r["gsis_id"])
        if cur is None or (r["pos_rank"] or 99) < (cur["rank"] or 99):
            out[r["gsis_id"]] = {"team": r["team"], "position": r["pos_abb"], "rank": r["pos_rank"], "as_of": r["dt"]}
    return out

