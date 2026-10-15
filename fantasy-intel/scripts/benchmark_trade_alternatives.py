"""Deterministic synthetic benchmark for trade vs. free agency (analytics/trade_alternatives).

Run from backend: uv run python ../scripts/benchmark_trade_alternatives.py
10/12/14 teams, 16 active + 1 IR per team, 12 remaining weeks, a 400-player free-agent pool. Measures:
  * one manual-analyzer panel (both managers) on a fresh pool, averaged over random 2-for-2 trades;
  * the panel for every row Trade Discovery shows (practical mode, default shapes), sharing one pool;
  * the discovery request itself, for scale.
In memory only: no database, no ESPN, no network.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path
from statistics import mean
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from app.analytics.trade_alternatives import FreeAgencyBoard, for_evaluation  # noqa: E402
from app.analytics.trade_discovery import TradeSearch  # noqa: E402
from app.analytics.trade_practical import discover  # noqa: E402
from app.analytics.trades import evaluate_trade  # noqa: E402
from synthetic_league import add_free_agents, synthetic_league  # noqa: E402

MANUAL_TRADES = 12


def run(teams: int) -> dict:
    st = synthetic_league(n_teams=teams, seed=7)
    add_free_agents(st, n=400, seed=13, start_id=70000, scale=0.75)
    st.free_agent_status = {p.espn_player_id: ("WAIVERS" if i % 10 == 0 else "FREEAGENT")
                            for i, p in enumerate(st.free_agents)}
    rng = random.Random(teams)
    manual = []
    for _ in range(MANUAL_TRADES):
        a, b = rng.sample(sorted(st.rosters), 2)
        sa = rng.sample([p.espn_player_id for p in st.rosters[a] if not p.on_ir], 2)
        sb = rng.sample([p.espn_player_id for p in st.rosters[b] if not p.on_ir], 2)
        ev = evaluate_trade(st, a, b, sa, sb, scenarios=False)
        t0 = perf_counter()
        out = for_evaluation(st, ev, a, b, sa, sb)          # a fresh pool each time: the analyzer's cost
        manual.append(perf_counter() - t0)
        assert out["consistent_with_canonical"]
    t0 = perf_counter()
    ts = TradeSearch(st, 1, ("1-for-1", "2-for-1", "1-for-2", "2-for-2", "3-for-1", "1-for-3", "3-for-2", "2-for-3"))
    res = discover(ts, "practical", limit=10)
    t_discover = perf_counter() - t0
    shown = [x for items in res["sections"].values() for x in items]
    t0 = perf_counter()
    board = FreeAgencyBoard(st, ts.window)
    t_board = perf_counter() - t0
    for row in shown:
        out = for_evaluation(st, {"team_a": row["mine_detail"], "team_b": row["theirs_detail"]}, 1,
                             row["opponent_team_id"], row["sends"], row["receives"], board=board, labels={1: "You"})
        assert out["consistent_with_canonical"]
    t_rows = perf_counter() - t0
    return {"teams": teams, "free_agents": len(st.free_agents), "scored": len(board.pool),
            "manual_mean_s": mean(manual), "manual_max_s": max(manual),
            "discovery_rows": len(shown), "discovery_panels_s": t_rows, "board_s": t_board,
            "discovery_request_s": t_discover, "lineup_solves": board.T.solves}


if __name__ == "__main__":
    print("| Teams | FA pool (scored) | Manual panel mean / max | Discovery rows | All discovery panels | Pool build | "
          "Discovery request (for scale) | Weekly lineup solves |")
    print("|---|---|---|---|---|---|---|---|")
    for n in (10, 12, 14):
        x = run(n)
        print(f"| {x['teams']} | {x['free_agents']} ({x['scored']}) | {x['manual_mean_s']:.3f} / {x['manual_max_s']:.3f} s | "
              f"{x['discovery_rows']} | {x['discovery_panels_s']:.2f} s | {x['board_s']:.3f} s | "
              f"{x['discovery_request_s']:.2f} s | {x['lineup_solves']:,} |")
