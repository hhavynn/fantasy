"""Print the league-agnostic trade market (buy-low / sell-high candidates and offers) as Markdown.

Read-only: opens the DuckDB file read-only and touches nflverse-derived tables only.

    cd backend && uv run python ../scripts/trade_market_report.py [--db PATH] [--season 2026] [--limit 8]
"""

import argparse
import os
import sys

import _bootstrap  # noqa: F401
import duckdb

from app.analytics.trade_market import build_trade_market, candidates, generate_offer
from app.config import get_settings


def _who(m, ids):
    return " + ".join(f"{m[i].name} ({m[i].position}, {m[i].team})" for i in ids)


def _ranks(m, ids):
    return "; ".join(f"{m[i].name}: usage {m[i].position}{m[i].usage_rank} / market {m[i].position}{m[i].market_rank}"
                     for i in ids)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("STUDIO_DB") or str(get_settings().db_path))
    ap.add_argument("--season", type=int, default=get_settings().nfl_season)
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--offers", type=int, default=3, help="candidates per side that get full offers")
    a = ap.parse_args()
    con = duckdb.connect(a.db, read_only=True)
    m = build_trade_market(con, a.season)
    through = max((w for p in m.values() for w in p.weeks), default=None)
    print(f"# Trade market, {a.season} through week {through}\n")
    print("Values are 0-100 on a cross-position scale (100 = top role at the position). `usage` is our "
          "role-based view; `market` is the box-score proxy for what managers pay. Not fantasy points.\n")
    for kind, title in (("buy", "Buy-low"), ("sell", "Sell-high")):
        rows = candidates(m, kind, a.limit)
        print(f"## {title} candidates\n")
        print("| # | Player | Pos | Team | Usage (rank) | Market (rank) | Trend | Flags |")
        print("|---|---|---|---|---|---|---|---|")
        for i, c in enumerate(rows, 1):
            print(f"| {i} | {c['name']} | {c['position']} | {c['team']} | {c['usage_value']:.0f} "
                  f"({c['position']}{c['usage_rank']}) | {c['market_value']:.0f} ({c['position']}{c['market_rank']}) "
                  f"| {c['trend']} | {', '.join(c['flags']) or '-'} |")
        print()
        for c in rows[:a.offers]:
            o = generate_offer(m, c["gsis_id"], kind)
            print(f"### {title}: {c['name']}\n")
            print("Evidence: " + "; ".join(e["text"] for e in c["evidence"][:7]) + "\n")
            for label, off in [("Opening 1-for-1", o["opening"])] + [("Alternative", x) for x in o["alternatives"]]:
                if not off:
                    continue
                print(f"- **{label}** ({off['shape']}, {off['fairness']}): you give {_who(m, off['you_give'])}; "
                      f"you get {_who(m, off['you_get'])}")
                print(f"  - ranks: {_ranks(m, off['you_give'] + off['you_get'])}")
                print(f"  - position: {off['position_check']}; roster: {off['balance_check']}")
                print(f"  - their incentive: {' '.join(off['incentive'])}")
                print(f"  - risks: {' '.join(off['risks']) or 'none flagged'}")
            for n in o["notes"]:
                print(f"- note: {n}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
