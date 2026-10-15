"""Build the synthetic DuckDB the browser smoke test (frontend/e2e) runs against. No network and no
ESPN cookies: it syncs the ESPN fixture league from backend/tests/espn_fixture.py, gives it a full
round-robin schedule and a synthetic NFL schedule (backend/tests/synthetic_nfl.py), then runs the
analytics pipeline. Every number is an invented test input.

    cd backend && uv run python ../scripts/build_e2e_db.py [path]   # default: ../data/e2e.duckdb

Serve it with FANTASY_DB_PATH=<path> ESPN_SEASON=2026 (the fixture season).
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401

sys.path.insert(0, str(_bootstrap.BACKEND / "tests"))

import synthetic_nfl as nfl  # noqa: E402
from espn_fixture import CURRENT, MY_SWID, SEASON, FakeEspnClient  # noqa: E402

from app.analytics.pipeline import run_analytics  # noqa: E402
from app.db.connection import connect, fetch_dicts, open_db  # noqa: E402
from app.espn.sync import run_espn_sync  # noqa: E402

REGULAR_PERIODS = 14
TEAMS = [1, 2, 3, 4]


def round_robin(team_ids: list[int], periods: int) -> dict[int, list[tuple[int, int]]]:
    t = list(team_ids)
    out = {}
    for p in range(1, periods + 1):
        out[p] = [(t[i], t[-1 - i]) if (p + i) % 2 else (t[-1 - i], t[i]) for i in range(len(t) // 2)]
        t = [t[0]] + [t[-1]] + t[1:-1]
    return out


def build(db_path: Path) -> dict:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for p in (db_path, db_path.with_suffix(db_path.suffix + ".wal")):
        p.unlink(missing_ok=True)
    run_espn_sync(lambda: open_db(db_path), SimpleNamespace(swid=MY_SWID, my_team_id=None),
                  client=FakeEspnClient(), save_raw_payloads=False)
    con = connect(db_path)
    try:
        # A full regular season: weeks before CURRENT final, the rest scheduled and unplayed.
        lid = fetch_dicts(con, "SELECT league_id FROM leagues")[0]["league_id"]
        con.execute("DELETE FROM matchups")
        mid = 0
        for period, pairs in round_robin(TEAMS, REGULAR_PERIODS).items():
            for h, a in pairs:
                mid += 1
                if period < CURRENT:
                    con.execute("INSERT INTO matchups VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'NONE')",
                                [lid, SEASON, mid, period, h, a, 110.0 + h + period, 95.0 + a, "HOME"])
                else:
                    con.execute("INSERT INTO matchups VALUES (?, ?, ?, ?, ?, ?, 0.0, 0.0, 'UNDECIDED', 'NONE')",
                                [lid, SEASON, mid, period, h, a])
        settings = json.loads(con.execute("SELECT settings_raw FROM leagues").fetchone()[0] or "{}")
        settings["scheduleSettings"] = {**settings.get("scheduleSettings", {}), "matchupPeriodLength": 1,
                                        "matchupPeriods": {str(w): [w] for w in range(1, 18)}}
        con.execute("UPDATE leagues SET settings_raw = ?", [json.dumps(settings)])
        # The fixture snapshots lineups for period 1 only: reuse it for the other played weeks and the current one.
        con.execute("DELETE FROM lineup_snapshots WHERE scoring_period BETWEEN 2 AND ?", [CURRENT])
        for period in range(2, CURRENT + 1):
            con.execute("""INSERT INTO lineup_snapshots
                           SELECT league_id, season, ?, team_id, espn_player_id, lineup_slot_id, lineup_slot, is_starter,
                                  acquisition_type, acquisition_date, injury_status, ?, projected_points, synced_at
                           FROM lineup_snapshots WHERE scoring_period = 1""",
                        [period, 12.5 if period < CURRENT else 0.0])
        # NFL schedule and defensive samples: weeks before CURRENT final, later weeks in the future.
        nfl.write(con, SEASON, nfl.schedule(18), set(range(1, CURRENT)))
        nfl.record_fetch(con)
        return run_analytics(con, SEASON)
    finally:
        con.close()


def main() -> int:
    db_path = Path(sys.argv[1] if len(sys.argv) > 1 else _bootstrap.BACKEND.parent / "data" / "e2e.duckdb").resolve()
    stats = build(db_path)
    print(json.dumps({"db_path": str(db_path), "analytics": stats}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
