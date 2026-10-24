"""Research: the stash planner as-of backtest (issue #218 plan item 5, research only; the decision rule is fixed in
docs/research/stash_backtest.md before the run, and app/analytics/stash_backtest.py implements it).

    cd backend
    uv run python ../scripts/stash_backtest_research.py --out OUT [--seasons 2023-2025] [--leagues 10]

Loads nflverse player_stats and schedules (free, public) for the evaluated seasons and the one before, builds
synthetic snake-draft leagues and runs the four arms on every team-week. Writes OUT/rows.jsonl and OUT/report.json
(keep OUT outside the repo) and prints the report. Never touches a database, an artifact or any production table.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

import _bootstrap  # noqa: F401

from app.analytics import stash_backtest as sb  # noqa: E402

POSITIONS = ("QB", "RB", "WR", "TE")


def load(seasons: list[int]) -> list[sb.Season]:
    import nflreadpy as nfl
    years = sorted({y for s in seasons for y in (s - 1, s)})
    stats = nfl.load_player_stats(years).to_dicts()
    sched = nfl.load_schedules(years).to_dicts()
    ids: dict[str, int] = {}

    def pid(gsis: str) -> int:
        return ids.setdefault(gsis, len(ids) + 1)

    by_year: dict[int, list[dict]] = {}
    for r in stats:
        if r.get("season_type") == "REG" and r.get("position") in POSITIONS and r.get("player_id"):
            by_year.setdefault(r["season"], []).append(r)
    out = []
    for s in seasons:
        games, position, name = {}, {}, {}
        for r in by_year.get(s, []):
            p = pid(r["player_id"])
            games.setdefault(p, []).append((int(r["week"]), r["team"], float(r.get("fantasy_points_ppr") or 0.0)))
            position[p], name[p] = r["position"], r.get("player_display_name") or r["player_id"]
        prior_games: dict[int, list[float]] = {}
        for r in by_year.get(s - 1, []):
            p = pid(r["player_id"])
            prior_games.setdefault(p, []).append(float(r.get("fantasy_points_ppr") or 0.0))
            if p not in position:                      # drafted on last season's value even if he never plays now
                position[p], name[p] = r["position"], r.get("player_display_name") or r["player_id"]
        prior_ppg = {p: sum(v) / len(v) for p, v in prior_games.items() if v}
        prior_total = {p: sum(v) for p, v in prior_games.items() if v}
        reg = [g for g in sched if g["season"] == s and g.get("game_type") == "REG"]
        teams = {g["home_team"] for g in reg} | {g["away_team"] for g in reg}
        weeks = sorted({int(g["week"]) for g in reg})
        played = {(g["home_team"], int(g["week"])) for g in reg} | {(g["away_team"], int(g["week"])) for g in reg}
        byes = {t: frozenset(w for w in weeks if (t, w) not in played) for t in teams}
        out.append(sb.Season(s, {p: sorted(v) for p, v in games.items()}, prior_ppg, prior_total, byes, position, name))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--seasons", default="2023-2025")
    ap.add_argument("--leagues", type=int, default=10)
    args = ap.parse_args()
    a, _, b = args.seasons.partition("-")
    seasons = list(range(int(a), int(b or a) + 1))
    args.out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    rows = []
    for s in load(seasons):
        season_rows = sb.run_season(s, args.leagues)
        rows += season_rows
        print(f"season {s.season}: {len(season_rows)} team-weeks, "
              f"{sum(r.get('plan') is not None for r in season_rows)} with a stash ({time.time() - t0:.0f} s)",
              file=sys.stderr)
    with open(args.out / "rows.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    report = sb.evaluate(rows)
    report["git_sha"] = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    report["leagues_per_season"] = args.leagues
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
