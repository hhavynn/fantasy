"""Sleeper access for sync jobs (multi-league Phase 4b). Sleeper's API is public and read-only: no credential is
ever involved, and a Sleeper league is always public (there is no private Sleeper league to restrict).

`fetch` reads one league: league, users, rosters, NFL state, every week's matchups (pairings for the weeks still
to play), the transactions of every week played, the draft's picks, and the players list restricted to what the
league file carries (app/sleeper/write.pool_players). Sleeper asks that the full players list (several MB) be read
at most once a day, so the drain keeps one copy in the object store (`cache/sleeper/players.json.gz`) and refreshes
it when it is a day old. `probe` checks that a league still exists; `control_request` that Sleeper answers.
"""

from __future__ import annotations

import gzip
import json
import tempfile
import time
from pathlib import Path

from ..sleeper.client import SleeperClient, check_league_id
from ..sleeper.models import parse_league, parse_rosters
from ..sleeper.normalize import schedule_weeks
from ..sleeper.write import SleeperPayloads, pool_players

PLAYERS_KEY = "cache/sleeper/players.json.gz"
PLAYERS_MAX_AGE_S = 24 * 3600
LAST_WEEK = 18


def client(work_root: Path | None = None) -> SleeperClient:
    return SleeperClient(cache_dir=Path(tempfile.mkdtemp(prefix="fi-sleeper-", dir=work_root)), timeout=30.0)


def players(sleeper: SleeperClient, objects, now: float | None = None) -> dict[str, dict]:
    """The full players list, from the object store's copy when it is less than a day old."""
    now = time.time() if now is None else now
    try:
        doc = json.loads(gzip.decompress(objects.get_bytes(PLAYERS_KEY)))
        if 0 <= now - float(doc["fetched_at"]) < PLAYERS_MAX_AGE_S and doc.get("players"):
            return doc["players"]
    except Exception:          # noqa: BLE001 - no copy yet, or an unreadable one: fetch a fresh list
        pass
    data = sleeper.players(force_refresh=True)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "players.json.gz"
        path.write_bytes(gzip.compress(json.dumps({"fetched_at": now, "players": data}).encode(), mtime=0))
        objects.put(PLAYERS_KEY, path)
    return data


def fetch(sleeper: SleeperClient, league_id: str, *, objects) -> SleeperPayloads:
    lid = check_league_id(str(league_id))
    league_raw = sleeper.league(lid)
    league = parse_league(league_raw)
    users, rosters_raw, state = sleeper.league_users(lid), sleeper.league_rosters(lid), sleeper.nfl_state()
    sched = schedule_weeks(league)
    first = int(sched["start_week"] or 1)
    last = int(sched["final_scoring_period"] or LAST_WEEK)
    week = state.get("week") if str(state.get("season")) == str(league.season) else None
    played_through = min(int(week), last) if isinstance(week, int) else last
    matchups = {w: sleeper.get_json(f"/league/{lid}/matchups/{w}") or [] for w in range(first, last + 1)}
    transactions = {w: sleeper.get_json(f"/league/{lid}/transactions/{w}") or [] for w in range(first, played_through + 1)}
    picks = (sleeper.get_json(f"/draft/{league.draft_id}/picks") or []) if league.draft_id else []
    rosters = parse_rosters(rosters_raw)
    rostered = {x for r in rosters for x in (*r.players, *r.reserve, *r.taxi, *(s for s in r.starters if s))}
    for raw in matchups.values():
        for row in raw:
            rostered.update(str(x) for x in (row.get("players") or []))
    return SleeperPayloads(league=league_raw, users=users, rosters=rosters_raw, state=state, matchups=matchups,
                           transactions=transactions, draft_picks=picks,
                           players=pool_players(players(sleeper, objects), rostered))


def probe(league_id) -> str:
    """"public" (the league exists), "missing" (Sleeper has no such league) or "unknown" (no answer)."""
    from ..sleeper.client import InvalidLeagueId

    try:
        client().league(str(league_id))
        return "public"
    except InvalidLeagueId:
        return "missing"
    except Exception:          # noqa: BLE001 - unreachable or an error status: keep the current state
        return "unknown"


def control_request(_season: int | None = None) -> bool:
    try:
        return bool(client().nfl_state())
    except Exception:          # noqa: BLE001
        return False
