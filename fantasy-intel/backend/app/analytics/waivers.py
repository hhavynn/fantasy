"""Waiver intelligence: player side x my-roster side x league side.

All scores are explainable weighted sums; every component is stored.
League interest is LOW / MEDIUM / HIGH (no fake probabilities).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean

from ..db.connection import fetch_dicts, replace_where, table_exists, utcnow
from ..espn.constants import FANTASY_POSITIONS, IR_SLOT
from .player_scores import percentile_scores

UNAVAILABLE_ESPN = {"OUT", "INJURY_RESERVE", "SUSPENSION"}
DEPTH_EXTRA = {"RB": 1, "WR": 1}          # bench depth wanted beyond starters
# How useful a non-starting player at the position is (flex/bye/injury cover).
DEPTH_UTILITY = {"RB": 1.0, "WR": 1.0, "TE": 0.3, "QB": 0.3, "K": 0.15, "D/ST": 0.15}
FLEX_SPLIT = {
    "FLEX": {"RB": 0.45, "WR": 0.45, "TE": 0.10},
    "RB/WR": {"RB": 0.5, "WR": 0.5},
    "WR/TE": {"WR": 0.6, "TE": 0.4},
    "OP": {"QB": 1.0},
}

W_OPP, W_FIT = 0.55, 0.45
CLAIM_SCORE, WAIT_SCORE = 6.0, 4.5
CLAIM_MARGIN = 0.5                         # value points the add must beat the drop by
CLAIM_MIN_FIT = 3.0                        # a CLAIM must actually help *my* roster
INTERESTED = 0.55
INTEREST_HIGH, INTEREST_MED = 0.45, 0.25


# --------------------------------------------------------------- helpers --

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def starters_by_position(slots: dict[str, int]) -> dict[str, float]:
    req: dict[str, float] = defaultdict(float)
    for slot, n in (slots or {}).items():
        if slot in FANTASY_POSITIONS:
            req[slot] += n
        elif slot in FLEX_SPLIT:
            for pos, share in FLEX_SPLIT[slot].items():
                req[pos] += n * share
    return dict(req)


def starter_slots(required: float) -> int:
    """Whole starting spots at a position; a flex share only counts if >= 0.4 (RB/WR, not TE)."""
    whole = math.floor(required + 1e-9)
    return whole + (1 if required - whole >= 0.4 else 0)


def is_unavailable(p: dict) -> bool:
    return p.get("lineup_slot_id") == IR_SLOT or (p.get("injury_status") or "").upper() in UNAVAILABLE_ESPN


@dataclass
class PlayerValue:
    espn_player_id: int
    position: str | None
    value: float | None
    opportunity: float | None
    production: float | None
    trend_label: str | None
    classification: str | None
    basis: str                      # usage_model | espn_points | none
    gsis_id: str | None = None
    games: int | None = None
    extras: dict = field(default_factory=dict)


# ------------------------------------------------------------ team needs --

def team_needs(roster: list[dict], values: dict[int, PlayerValue], required: dict[str, float],
               league_starter_avg: dict[str, float]) -> dict[str, dict]:
    """Need per position (0..1) for one team's roster rows (espn_player_id, position, lineup_slot_id, injury_status)."""
    out = {}
    for pos in FANTASY_POSITIONS:
        need_starters = required.get(pos, 0.0)
        players = [p for p in roster if p.get("position") == pos]
        healthy = [p for p in players if not is_unavailable(p)]
        n_start = starter_slots(need_starters)
        target = n_start + DEPTH_EXTRA.get(pos, 0)
        hv = sorted((values[p["espn_player_id"]].value or 0.0 for p in healthy
                     if p["espn_player_id"] in values and values[p["espn_player_id"]].value is not None), reverse=True)
        starter_vals = (hv + [0.0] * n_start)[:n_start] if n_start and hv else []
        starter_value = mean(starter_vals) if starter_vals else None   # None = no value data at all
        shortage = clamp((target - len(healthy)) / target) if target else 0.0
        gap = 0.0
        if starter_value is not None and league_starter_avg.get(pos) is not None:
            gap = clamp((league_starter_avg[pos] - starter_value) / 4.0)
        need = round(clamp(0.55 * clamp(shortage * 2) + 0.45 * gap), 3) if n_start else 0.0
        out[pos] = {
            "position": pos, "starters_required": need_starters, "rostered": len(players),
            "healthy": len(healthy), "unavailable": len(players) - len(healthy),
            "starter_value": round(starter_value, 2) if starter_value is not None else None,
            "need_score": need,
            "details": {"depth_target": target,
                        "weakest_starter_value": round(min(starter_vals), 2) if starter_vals else None, "shortage": round(shortage, 3), "quality_gap": round(gap, 3),
                        "league_avg_starter_value": league_starter_avg.get(pos),
                        "formula": "need = 0.55*min(1, 2*shortage) + 0.45*quality_gap"},
        }
    return out


def league_starter_averages(rosters: dict[int, list[dict]], values: dict[int, PlayerValue],
                            required: dict[str, float]) -> dict[str, float]:
    out = {}
    for pos in FANTASY_POSITIONS:
        n = starter_slots(required.get(pos, 0))
        if not n:
            continue
        per_team = []
        for roster in rosters.values():
            vals = sorted((values[p["espn_player_id"]].value for p in roster
                           if p.get("position") == pos and not is_unavailable(p)
                           and p["espn_player_id"] in values and values[p["espn_player_id"]].value is not None),
                          reverse=True)
            if vals:
                per_team.append(mean((vals + [0.0] * n)[:n]))
        if per_team:
            out[pos] = round(mean(per_team), 2)
    return out


# --------------------------------------------------------- suggested drop --

def suggest_drop(my_roster: list[dict], values: dict[int, PlayerValue], candidate_pos: str,
                 required: dict[str, float]) -> dict | None:
    """Lowest-value bench player whose removal keeps every position startable."""
    counts = defaultdict(int)
    for p in my_roster:
        if not is_unavailable(p):
            counts[p.get("position")] += 1
    options = []
    for p in my_roster:
        if p.get("lineup_slot_id") == IR_SLOT or p.get("is_starter"):
            continue
        pos = p.get("position")
        min_needed = starter_slots(required.get(pos, 0))
        if pos != candidate_pos and not is_unavailable(p) and counts[pos] - 1 < min_needed:
            continue  # would leave the position short
        v = values.get(p["espn_player_id"])
        options.append((v.value if v and v.value is not None else 0.0, p))
    if not options:
        return None
    value, p = min(options, key=lambda t: (t[0], t[1]["espn_player_id"]))
    return {"espn_player_id": p["espn_player_id"], "name": p.get("full_name"), "position": p.get("position"),
            "value": round(value, 2), "injury_status": p.get("injury_status")}


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

def evaluate_candidate(c: dict, cv: PlayerValue, *, my_team_id: int, needs: dict[int, dict],
                       my_roster: list[dict], values: dict[int, PlayerValue], required: dict[str, float],
                       profiles: dict[int, dict], league_pos_share: dict[str, float], team_names: dict[int, str],
                       scarcity: dict[str, int]) -> dict:
    pos = c["position"]
    reasons: list[str] = []
    injured = (c.get("injury_status") or "").upper() in UNAVAILABLE_ESPN

    # Player side
    base = cv.opportunity if cv.opportunity is not None else cv.value
    trend_adj = {"UP": 1.0, "DOWN": -1.0}.get(cv.trend_label or "", 0.0)
    injury_adj = -3.0 if injured else 0.0
    opportunity = round(clamp((base or 0.0) + trend_adj + injury_adj, 0, 10), 1) if base is not None else None
    if cv.basis == "usage_model":
        reasons.append(f"Opportunity {cv.opportunity:.1f}/10 at {pos} (recent expected-points percentile)"
                       if cv.opportunity is not None else "No recent opportunity percentile")
    elif cv.basis == "espn_points":
        reasons.append("Limited model: based on recent ESPN league points only (no NFL usage data for this player)")
    else:
        reasons.append("No usage or scoring data available")
    if trend_adj:
        reasons.append(f"Role trend {cv.trend_label} ({trend_adj:+.0f})")
    if injured:
        reasons.append(f"Currently {c.get('injury_status')} ({injury_adj:+.0f})")

    # My roster side
    my_need = needs.get(my_team_id, {}).get(pos, {})
    need = my_need.get("need_score", 0.0)
    starter_value = my_need.get("starter_value")
    weakest = (my_need.get("details") or {}).get("weakest_starter_value")
    cand_value = cv.value or 0.0
    upgrade = clamp((cand_value - (weakest or 0.0)) / 3.0)        # would he start over my weakest starter?
    drop = suggest_drop(my_roster, values, pos, required)
    margin = cand_value - drop["value"] if drop else None
    bench_upgrade = clamp(margin / 4.0) if margin is not None else 1.0
    depth_utility = DEPTH_UTILITY.get(pos, 0.5)
    fit = 10.0 * (0.35 * need + 0.35 * upgrade + 0.30 * bench_upgrade * depth_utility)
    if drop is not None and margin is not None and margin <= 0:
        fit *= 0.5
        reasons.append(f"Not clearly better than your weakest bench option ({drop['name']}, value {drop['value']:.1f})")
    fit = round(clamp(fit, 0, 10), 1)
    if need >= 0.5:
        reasons.append(f"Fills a real need at {pos} (need {need:.2f})")
    if upgrade >= 0.5:
        reasons.append(f"Would start over your weakest {pos} starter (value {cand_value:.1f} vs {weakest or 0:.1f})")
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

    waiver_score = round(W_OPP * (opportunity or 0) + W_FIT * fit + scarcity_bonus, 2)
    if injured:
        rec = "WAIT" if (base or 0) >= 6 else "IGNORE"
        reasons.append("Injured: stash only if you have the roster room" if rec == "WAIT" else "Injured and low role")
    elif waiver_score >= CLAIM_SCORE and fit >= CLAIM_MIN_FIT and (margin is None or margin >= CLAIM_MARGIN):
        rec = "CLAIM"
    elif waiver_score >= WAIT_SCORE:
        rec = "WAIT"
    else:
        rec = "IGNORE"
    if rec == "CLAIM" and league_interest == "HIGH":
        reasons.append("Likely contested: bid/prioritise accordingly")
    elif rec == "WAIT" and c.get("availability_status") == "FREEAGENT" and league_interest == "LOW":
        reasons.append("Free agent with low expected competition: can wait a week for more usage data")

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
            "player": {"basis": cv.basis, "base_opportunity": base, "trend_label": cv.trend_label,
                       "trend_adj": trend_adj, "injury_adj": injury_adj, "value": cv.value,
                       "classification": cv.classification, "percent_owned": c.get("percent_owned"),
                       "percent_change": c.get("percent_change"), **cv.extras},
            "roster": {"need": need, "my_starter_value": starter_value, "my_weakest_starter_value": weakest,
                       "upgrade": round(upgrade, 3), "bench_upgrade": round(bench_upgrade, 3),
                       "depth_utility": depth_utility,
                       "fit_formula": "10*(0.35*need + 0.35*upgrade_over_weakest_starter + 0.30*upgrade_over_drop*depth_utility)",
                       "suggested_drop": drop, "value_margin_vs_drop": round(margin, 2) if margin is not None else None},
            "league": {"attractiveness": round(attractiveness, 3), "max_manager_interest": max_interest,
                       "managers_interested": len(interested), "scarcity_available_startable": scarcity_count,
                       "scarcity_bonus": scarcity_bonus},
            "formula": "waiver_score = 0.55*opportunity + 0.45*fit + scarcity_bonus; "
                       "CLAIM: score >= 6.0, fit >= 3.0, beats drop by 0.5 value; WAIT: score >= 4.5",
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
            r["trend_label"], r["classification"], "usage_model", r["gsis_id"], r["games"])
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
            remaining = max(0.0, faab_budget - (t.get("faab_spent") or 0))
            resource = remaining / faab_budget
            note = f"${remaining:.0f} FAAB left" if resource >= 0.6 else None
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


def build_waivers(con) -> dict:
    lg = fetch_dicts(con, "SELECT * FROM leagues ORDER BY synced_at DESC LIMIT 1")
    if not lg:
        return {"skipped": "no ESPN league synced"}
    lg = lg[0]
    league_id, season, week = lg["league_id"], lg["season"], lg["current_week"] or 1
    slots = json.loads(lg["roster_slots"]) if isinstance(lg["roster_slots"], str) else (lg["roster_slots"] or {})
    required = starters_by_position(slots)
    teams = fetch_dicts(con, "SELECT * FROM fantasy_teams WHERE league_id=? AND season=?", [league_id, season])
    mine = [t["team_id"] for t in teams if t["is_mine"]]
    if not mine:
        return {"skipped": "your team is not identified (set ESPN_MY_TEAM_ID)"}
    my_team_id = mine[0]
    team_names = {t["team_id"]: t["name"] for t in teams}

    values = load_values(con, season, week)
    roster_rows = fetch_dicts(con, """
        SELECT l.team_id, l.espn_player_id, l.lineup_slot_id, l.is_starter, fp.position, fp.full_name,
               coalesce(fp.injury_status, l.injury_status) AS injury_status
        FROM lineup_snapshots l JOIN fantasy_players fp USING (espn_player_id)
        WHERE l.league_id=? AND l.season=? AND l.scoring_period = ?
    """, [league_id, season, week])
    rosters: dict[int, list[dict]] = defaultdict(list)
    for r in roster_rows:
        rosters[r["team_id"]].append(r)
    lsa = league_starter_averages(rosters, values, required)
    needs = {tid: team_needs(rosters.get(tid, []), values, required, lsa) for tid in team_names}
    now = utcnow()
    need_rows = [{"league_id": league_id, "season": season, "team_id": tid, **n, "computed_at": now}
                 for tid, per in needs.items() for n in per.values()]
    replace_where(con, "team_position_needs", need_rows, "league_id=? AND season=?", [league_id, season])

    profiles, league_share = load_profiles(con, league_id, season, teams, bool(lg["is_faab"]), lg["faab_budget"])
    cands = fetch_dicts(con, """
        SELECT * FROM fantasy_players
        WHERE availability_status IN ('FREEAGENT', 'WAIVERS') AND position IN ('QB','RB','WR','TE','K','D/ST')
    """)
    cands = [c for c in cands if c["espn_player_id"] in values or (c.get("percent_owned") or 0) >= 0.5]
    cands.sort(key=lambda c: (-(values[c["espn_player_id"]].value or 0) if c["espn_player_id"] in values else 0,
                              -(c.get("percent_owned") or 0)))
    cands = cands[:300]
    scarcity = {pos: sum(1 for c in cands if c["position"] == pos and c["espn_player_id"] in values
                         and (values[c["espn_player_id"]].value or 0) >= (lsa.get(pos) or 99))
                for pos in FANTASY_POSITIONS}
    depth = _latest_depth(con, season)
    out = []
    for c in cands:
        cv = values.get(c["espn_player_id"]) or PlayerValue(c["espn_player_id"], c["position"], None, None, None,
                                                            None, None, "none")
        if cv.gsis_id and cv.gsis_id in depth:
            cv.extras = {**cv.extras, "depth_chart": depth[cv.gsis_id]}
        row = evaluate_candidate(c, cv, my_team_id=my_team_id, needs=needs, my_roster=rosters.get(my_team_id, []),
                                 values=values, required=required, profiles=profiles, league_pos_share=league_share,
                                 team_names=team_names, scarcity=scarcity)
        row.update(league_id=league_id, season=season, computed_at=now)
        out.append(row)
    replace_where(con, "waiver_recommendations", out, "league_id=? AND season=?", [league_id, season])
    return {"candidates": len(out), "claims": sum(1 for r in out if r["recommendation"] == "CLAIM")}


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

