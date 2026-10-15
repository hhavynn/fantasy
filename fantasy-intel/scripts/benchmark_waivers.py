"""Deterministic stored-data benchmark: 10/12/14 teams, full 16-player rosters, 900 free agents.

Run from backend: .venv/bin/python ../scripts/benchmark_waivers.py
Includes 11 future weeks, projection/usage lanes, add/drop planning and a read-only API query.
No external data or network calls. Timings exclude fixture construction.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from random import Random
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.analytics.waivers import build_waivers
from app.db.connection import connect, init_db, upsert_rows, utcnow
from app.espn.constants import PRO_TEAM_MAP, nflverse_team


def seed(con, size):
    rng, now, season = Random(42), utcnow(), 2026
    nfl_teams = [nflverse_team(t) for t in PRO_TEAM_MAP if t]
    playing, schedule, lines = {}, [], []
    for w in range(4, 18):
        bye = set(nfl_teams[(w - 7) * 4:(w - 6) * 4]) if 7 <= w <= 13 else set()
        playing[w] = [t for t in nfl_teams if t not in bye]
        for home, away in zip(playing[w][::2], playing[w][1::2]):
            schedule.append((season, w, "REG", home, away))
            for team, opp in ((home, away), (away, home)):
                lines.append({"season": season, "week": w, "team": team, "opponent": opp,
                              "implied_total": rng.uniform(16, 30)})
    con.execute("CREATE TABLE nfl_schedules (season INTEGER, week INTEGER, game_type VARCHAR, home_team VARCHAR, away_team VARCHAR)")
    con.executemany("INSERT INTO nfl_schedules VALUES (?, ?, ?, ?, ?)", schedule)
    upsert_rows(con, "team_game_lines", lines, ["season", "week", "team"])
    slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "D/ST": 1, "BE": 7, "IR": 1}
    upsert_rows(con, "leagues", [{"league_id": 1, "season": season, "name": "Benchmark", "size": size,
                "roster_slots": slots, "settings_raw": {"rosterSettings": {"lineupSlotCounts": slots},
                "acquisitionSettings": {"waiverOrderType": "ROLLING"}}, "current_week": 7, "final_scoring_period": 17,
                "reception_points": 1, "is_faab": False, "synced_at": now}], ["league_id", "season"])
    upsert_rows(con, "fantasy_teams", [{"league_id": 1, "season": season, "team_id": t, "name": f"Team {t}",
                "is_mine": t == 1, "waiver_rank": t} for t in range(1, size + 1)], ["league_id", "season", "team_id"])
    players, rosters, points = [], [], []
    positions = ["QB", "RB", "RB", "WR", "WR", "TE", "WR", "K", "D/ST", "RB", "RB", "WR", "WR", "TE", "QB", "D/ST"]
    for i in range(size * 16 + 900):
        pid = i + 1
        owned = i < size * 16
        pos = positions[i % 16] if owned else ["QB", "RB", "WR", "TE", "K", "D/ST"][i % 6]
        team = i // 16 + 1 if owned else 0
        pro_team = nfl_teams[pid % len(nfl_teams)]
        players.append({"espn_player_id": pid, "full_name": f"Player {pid}", "position": pos, "pro_team": pro_team,
                        "injury_status": "ACTIVE", "availability_status": "ONTEAM" if owned else "WAIVERS" if i % 3 == 0 else "FREEAGENT",
                        "on_team_id": team, "percent_owned": rng.uniform(0, 100), "detail_synced_at": now})
        if owned:
            rosters.append({"league_id": 1, "season": season, "scoring_period": 7, "team_id": team,
                            "espn_player_id": pid, "lineup_slot_id": 20, "injury_status": "ACTIVE", "synced_at": now})
        baseline = rng.uniform(4, 25 if pos not in ("K", "D/ST") else 12)
        for w in range(4, 18):
            points.append({"season": season, "scoring_period": w, "espn_player_id": pid,
                           "actual_points": baseline if w < 7 else None,
                           "projected_points": (baseline + rng.uniform(-2, 2) if pro_team in playing[w] else 0) if w >= 7 else None, "synced_at": now})
    upsert_rows(con, "fantasy_players", players, ["espn_player_id"])
    upsert_rows(con, "lineup_snapshots", rosters, ["league_id", "season", "scoring_period", "team_id", "espn_player_id"])
    upsert_rows(con, "espn_player_weekly_points", points, ["season", "scoring_period", "espn_player_id"])


def main():
    from fastapi.testclient import TestClient
    from app.main import app
    output = []
    for size in (10, 12, 14):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "benchmark.duckdb"
            con = connect(path)
            init_db(con)
            seed(con, size)
            start = perf_counter()
            result = build_waivers(con)
            elapsed = perf_counter() - start
            con.close()
            os.environ["FANTASY_DB_PATH"] = str(path)
            start = perf_counter()
            with TestClient(app) as client:
                response = client.get("/api/waivers", params={"view": "BEST", "limit": 30})
            api_ms = (perf_counter() - start) * 1000
            assert response.status_code == 200, response.text
            output.append({"teams": size, "rostered": size * 16, "eligible": result["coverage"]["eligible"],
                           "evaluated": result["candidates"], "build_seconds": round(elapsed, 3), "api_ms": round(api_ms, 2)})
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
