"""Six reproducible synthetic decision examples, not real-player advice.

Run from backend: .venv/bin/python ../scripts/waiver_examples.py
"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.analytics.waivers import PlayerValue, evaluate_candidate, team_state


def player(pid, name, pos, value, **extra):
    return {"espn_player_id": pid, "full_name": name, "position": pos, "value": value,
            "injury_status": "ACTIVE", "lineup_slot_id": 20, **extra}


def example(name, roster, slots, candidate, need, expected, extra=None):
    vals = {p["espn_player_id"]: PlayerValue(p["espn_player_id"], p["position"], p["value"], p["value"],
            p["value"], None, "HOLD", "xfp") for p in roster}
    state = team_state(roster, vals, slots)
    for p, raw in zip(state["players"], roster):
        p["projections"] = raw.get("projections", {})
    cv = PlayerValue(candidate["espn_player_id"], candidate["position"], candidate["value"], candidate.get("opportunity", candidate["value"]),
                     candidate["value"], candidate.get("trend"), "HOLD", "xfp")
    ctx = {"rules": {"is_faab": False, "waiver_rule": "ROLLING"}, "team": {"waiver_rank": 1}, **(extra or {})}
    if candidate.get("projections"):
        state["weeks"] = sorted(candidate["projections"])
        ctx["projections"] = {candidate["espn_player_id"]: candidate["projections"]}
    row = evaluate_candidate(candidate, cv, my_team_id=1, needs={1: {candidate["position"]: {"need_score": need}}},
                             my_state=state, profiles={}, league_pos_share={}, team_names={}, scarcity={}, decision_context=ctx)
    decision = row["components"]["decision"]
    assert decision["action"] == expected, (name, decision)
    return {"scenario": name, "action": decision["action"], "roster_move": decision["roster_move"]["summary"],
            "drop": (decision["roster_move"]["drop"] or {}).get("name"), "benefit": decision["utility"]["summary"],
            "cost": decision["resources"]["summary"], "stream": decision["streaming"]}


def examples():
    return [
        example("Preserve a strong backup RB while acquiring upside", [player(1, "RB starter", "RB", 9),
                player(2, "Useful backup RB", "RB", 7), player(3, "WR starter", "WR", 9), player(4, "Surplus WR", "WR", 2)],
                {"RB": 1, "WR": 1, "BE": 2}, player(99, "Rising RB", "RB", 8, trend="UP", availability_status="WAIVERS"),
                .8, "WAIVER_CLAIM", {"team": {"waiver_rank": 6}}),
        example("Worthwhile free add with open capacity", [player(1, "RB starter", "RB", 5), player(2, "Bench WR", "WR", 2)], {"RB": 1, "BE": 2},
                player(99, "Upgrade RB", "RB", 8, availability_status="FREEAGENT"), .6, "ADD_NOW"),
        example("Elite rolling priority and a marginal upgrade", [player(1, "RB starter", "RB", 6), player(2, "Bench WR", "WR", 2)],
                {"RB": 1, "BE": 1}, player(99, "Marginal RB", "RB", 6.4, opportunity=9, availability_status="WAIVERS"), .2, "MONITOR"),
        example("Matchup-supported defense swap", [player(1, "Current defense", "D/ST", 5, projections={7: 6}),
                player(2, "Useful backup RB", "RB", 6, projections={7: 12})], {"D/ST": 1, "BE": 1},
                player(99, "Streaming defense", "D/ST", 7, projections={7: 10}, availability_status="FREEAGENT"), .2, "ADD_NOW",
                {"streaming": {"weeks": [{"week": 7, "points": 10, "opponent": "NYJ", "incumbent_points": 6}],
                               "incumbent_points": 6, "opponent_implied_total": 16.5, "alternative_points": 8}}),
        example("Highly owned player adds little to a deep roster", [player(1, "Elite WR", "WR", 9), player(2, "Strong backup WR", "WR", 8)],
                {"WR": 1, "BE": 2}, player(99, "Popular WR", "WR", 5, percent_owned=95, availability_status="FREEAGENT"), 0, "NOT_AN_UPGRADE"),
        example("Low-owned emerging RB remains actionable", [player(1, "Thin RB room", "RB", 5)], {"RB": 1, "BE": 2},
                player(99, "Emerging RB", "RB", 8, percent_owned=.1, trend="UP", availability_status="FREEAGENT"), .8, "ADD_NOW"),
    ]


if __name__ == "__main__":
    print(json.dumps(examples(), indent=2))
