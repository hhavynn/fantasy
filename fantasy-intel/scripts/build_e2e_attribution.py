"""Build the synthetic DuckDB the "Why it changed" browser test (frontend/e2e/movers-attribution.spec.ts) runs against.

The Sleeper fixture league (scripts/build_e2e_sleeper.py, unchanged: its own goldens and specs never see this file) plus
two more sealed Fantasy Intel rest-of-season cycles with invented input terms, so /movers has two forecasts to compare:

  * cutoff week CURRENT - 1, made before that week's kickoff (data through the week before);
  * cutoff week CURRENT, made after the fixture's own run of that cycle (so it is the newest), data through CURRENT - 1.

Both use the fixture artifact, so the change splits into inputs. Every number is an invented test input; no network.

    cd backend && uv run python ../scripts/build_e2e_attribution.py [path]   # default: ../data/e2e-attribution.duckdb
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import _bootstrap  # noqa: F401

sys.path.insert(0, str(_bootstrap.BACKEND / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_e2e_sleeper as sleeper  # noqa: E402
from espn_fixture import CURRENT, PLAYERS, SEASON  # noqa: E402

from app.analytics import independent_projection_store as ips  # noqa: E402
from app.analytics.league_scoring import resolve_skill_scoring  # noqa: E402
from app.db.connection import connect  # noqa: E402
from app.espn.constants import nflverse_team  # noqa: E402
from app.sleeper.scoring import to_scoring_items  # noqa: E402

BASE = {1: 18.0, 2: 12.0, 3: 11.0, 4: 8.0}
WITHHELD = 202          # Foxtrot RB: withheld (ESPN status OUT) in the earlier forecast, projected in the latest
EARLIER = datetime(SEASON, 9, 1, 12, 0)     # before week CURRENT - 1's kickoff (synthetic_nfl: Sep 2)
LATEST = datetime(SEASON, 9, 21, 12, 0)     # after the fixture's own cycle run (Sep 20), before week CURRENT


def _term(feature, contribution, value, mean):
    return {"feature": feature, "value": value, "training_mean": mean, "contribution": round(contribution, 2),
            "text": f"{feature}: {value} (training average {mean})"}


def _row(pid, pos, team, week, k, later: bool):
    """One stored row. The latest forecast moves each player by `shift` points if he plays; most of it through two
    stored input terms, the rest through an input only the latest forecast stored (the residual)."""
    shift = 1.2 if pid % 2 == 0 else -0.9
    x = round(BASE[pos] + (pid % 5) * 0.7 - k * 0.1 + (shift if later else 0.0), 2)
    expl = [_term("targets_pg", 1.0 + (0.6 * shift if later else 0.0), 6.0 + (1.0 if later else 0.0), 5.0),
            _term("snap_pct", 0.6 + (0.2 * shift if later else 0.0), 0.72 if later else 0.68, 0.6),
            _term("pts_l3", 0.3, 12.0, 10.5)]
    if later:
        expl.append(_term("opp_pos_allowed", 0.2 * shift, 1.5, 0.0))
    row = {"season": SEASON, "week": week, "espn_player_id": pid, "gsis_id": f"00-E2E{pid:04d}",
           "position": sleeper.POSITIONS[pos], "nfl_team": nflverse_team(team), "opponent": None, "is_home": None,
           "projected_points": x, "interval_low": round(x * 0.4, 2), "interval_high": round(x * 1.7, 2),
           "model": "e2e", "coverage_status": "projected", "games_used": CURRENT - 1,
           "latest_usage_season": SEASON, "latest_usage_week": CURRENT - (1 if later else 2), "missing_inputs": [],
           "flags": [], "explanation": expl, "p_play": 0.95, "expected_points": round(x * 0.95, 2), "participation": None}
    if pid == WITHHELD and not later:
        row.update(coverage_status="withheld_unavailable", projected_points=None, interval_low=None,
                   interval_high=None, p_play=None, expected_points=None, explanation=[],
                   missing_inputs=["ESPN status OUT"])
    return row


def cycles(con) -> int:
    items, _, _ = to_scoring_items(sleeper.scoring(False))
    fp = resolve_skill_scoring(items).fingerprint
    n = 0
    for cutoff, created, later in ((CURRENT - 1, EARLIER, False), (CURRENT, LATEST, True)):
        for week in range(cutoff, sleeper.FINAL + 1):
            k = week - cutoff
            rows = [_row(pid, pos, team, week, k, later) for pid, (_, pos, team) in sorted(PLAYERS.items())
                    if pos in BASE]
            meta = {"season": SEASON, "week": week, "forecast_horizon": k, "projected_from_week": cutoff,
                    "data_through_season": SEASON, "data_through_week": cutoff - 1, "created_at": created,
                    "artifact_id": "e2e-fixture", "artifact_created_at": created.isoformat(),
                    "run_id": f"e2e-attr-c{cutoff}-w{week:02d}", "model_version": "ifp-2.2.0", "scoring_fingerprint": fp,
                    "model_status": {}, "evaluation": {}, "participation_version": None, "participation_status": None,
                    "participation_evaluation": None, "nfl_synced_at": created}
            ips.write_run(con, meta, rows)
            n += 1
    return n


def build(db_path: Path) -> dict:
    stats = sleeper.build(db_path)
    con = connect(db_path)
    try:
        stats["attribution_runs"] = cycles(con)
    finally:
        con.close()
    return stats


def main() -> int:
    db_path = Path(sys.argv[1] if len(sys.argv) > 1 else _bootstrap.BACKEND.parent / "data" / "e2e-attribution.duckdb")
    stats = build(db_path.resolve())
    print(json.dumps({"db_path": str(db_path.resolve()), "stats": stats}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
