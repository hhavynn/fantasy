"""Smoke every manager from the local database, without syncs or writes.

Run from backend: uv run python ../scripts/check_manager_scouting.py --db /path/to/fantasy.duckdb
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics.manager_scouting import build_manager_scouting
from app.analytics.manager_metrics import DIMENSIONS
from app.config import get_settings
from app.db.connection import connect, fetch_dicts


def audit(con):
    league = fetch_dicts(con, "SELECT league_id,season,name,current_week,synced_at FROM leagues ORDER BY synced_at DESC LIMIT 1")[0]
    teams = fetch_dicts(con, "SELECT team_id FROM fantasy_teams WHERE league_id=? AND season=? ORDER BY team_id",
                        [league["league_id"], league["season"]])
    results = []
    for team in teams:
        d = build_manager_scouting(con, team["team_id"])
        assert d is not None
        assert all(h["evidence"] and h["confidence"] for h in d["headlines"])
        assert "acceptance" not in json.dumps(d, default=str).lower()
        assert d["current_roster"]["available"], f"No current roster for team {team['team_id']}"
        # An audit ordering, not a new manager score: supported extremes first,
        # then sample confidence and stored absolute z. Streaming has no z.
        signals = fetch_dicts(con, """
            SELECT c.* FROM manager_classifications c JOIN manager_metrics m
              ON c.league_id=m.league_id AND c.season=m.season AND c.team_id=m.team_id AND c.metric=m.metric
            WHERE c.league_id=? AND c.season=? AND c.team_id=? AND c.dimension!='trade_activity'
              AND c.label!='INSUFFICIENT' AND m.value IS NOT NULL AND m.sample_size>=c.min_sample
              AND c.value IS NOT DISTINCT FROM m.value AND c.sample_size IS NOT DISTINCT FROM m.sample_size
              AND (c.computed_at IS NULL OR m.computed_at IS NULL OR c.computed_at=m.computed_at)
            ORDER BY CASE WHEN c.label IN ('HIGH','LOW') THEN 1 ELSE 0 END DESC,
              CASE c.confidence WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END DESC,
              abs(coalesce(c.z_score,0)) DESC, c.dimension
        """, [league["league_id"], league["season"], team["team_id"]])
        s = signals[0] if signals else None
        tendency = {"dimension": s["dimension"], "title": "Historical RB roster size" if s["dimension"] == "rb_hoarding" else DIMENSIONS[s["dimension"]]["title"],
                    "classification": s["label"], "value": s["value"], "league_mean": s["league_mean"],
                    "sample_size": s["sample_size"], "confidence": s["confidence"], "evidence": [s["rule"]]} if s else None
        needs = [n for n in d["current_needs"] if (n["starters_required"] or 0) > 0 and n["need_score"] is not None]
        need = max(needs, key=lambda n: n["need_score"]) if needs else None
        results.append({"team_id": team["team_id"], "team": d["identity"]["name"], "is_mine": d["identity"]["is_mine"],
            "strongest_supported_tendency": tendency,
            "biggest_current_need": {k: need[k] for k in ("position", "need_score", "rostered", "healthy", "effective_available", "starter_value", "league_starter_average")} if need else None,
            "observed_adds": d["identity"]["observed_adds"], "observed_drops": d["identity"]["observed_drops"],
            "completed_trades": d["trade_history"]["completed_trades"],
            "trade_behavior_confidence": d["trade_history"]["trade_behavior_confidence"],
            "current_roster_count": sum(p["count"] for p in d["current_roster"]["positions"] if p["position"] != "IR"),
            "uneven_trades": d["trade_history"]["uneven_player_count_trades"]})
    most_active = max(results, key=lambda r: r["observed_adds"] or 0)
    fewest = min(results, key=lambda r: (r["observed_adds"] or 0) + (r["observed_drops"] or 0))
    most_trades = max(results, key=lambda r: r["completed_trades"])
    return {"league": league, "teams_checked": len(results), "teams": results,
            "spot_checks": {"user": [r["team_id"] for r in results if r["is_mine"]],
                            "most_active_waivers": most_active["team_id"], "fewest_moves": fewest["team_id"],
                            "most_trades": most_trades["team_id"]}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=get_settings().db_path)
    args = parser.parse_args()
    con = connect(args.db, read_only=True)
    try:
        print(json.dumps(audit(con), default=str, indent=2))
    finally:
        con.close()
