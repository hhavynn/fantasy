"""Optional live smoke test against the real Sleeper API. Skipped unless SLEEPER_LIVE_LEAGUE_ID is
set (CI never sets it). Uses the normal on-disk caches in data/sleeper/, so /players/nfl is still
fetched at most once a day.

    SLEEPER_LIVE_LEAGUE_ID=<public league id> uv run pytest tests/test_sleeper_live.py -s
"""

import os
from datetime import datetime, timezone

import pytest

LEAGUE_ID = os.getenv("SLEEPER_LIVE_LEAGUE_ID", "").strip()

pytestmark = pytest.mark.skipif(not LEAGUE_ID, reason="set SLEEPER_LIVE_LEAGUE_ID to run the live Sleeper smoke test")


def test_live_league_loads_and_maps():
    from app.sleeper import SleeperClient, build_rows, fetch_league, load_id_map, map_players, rostered_player_ids

    client = SleeperClient()
    data = fetch_league(client, LEAGUE_ID)
    rows = build_rows(data, datetime.now(timezone.utc).replace(tzinfo=None))
    assert rows["league"]["league_id"] == LEAGUE_ID
    assert all(isinstance(t["owner_ids"], list) and all(isinstance(o, str) for o in t["owner_ids"]) for t in rows["teams"])
    assert all(isinstance(r["player_id"], str) for r in rows["lineups"])

    ids = rostered_player_ids(data.rosters)
    report = map_players(ids, data.players, load_id_map(), log_unmatched=False)
    assert set(report.mappings) == set(ids)                   # nothing dropped

    print(f"\nSleeper league {LEAGUE_ID}: {rows['league']['name']!r}, {len(rows['teams'])} teams, "
          f"week {rows['league']['current_week']}, players source: {client.players_source}")
    print(f"Rostered players: {len(ids)}; match rate {report.match_rate:.1%}; by method {report.by_method}")
    for m in sorted(report.unmatched, key=lambda m: (m.method, m.position or "", m.name or "")):
        print(f"  UNMATCHED {m.sleeper_id:>6} {m.name} ({m.position} {m.team}) [{m.method}]: {m.notes}")
