"""The request path being measured: open a league DB, attach fi_global read-only, search_path, run an
engine-like query that joins league and global tables. Shared by latency_local.py and the Vercel probe."""
import os, tempfile, time
import duckdb

GLOBAL_DB = "spike_fi_global"
QUERY = """
SELECT t.team_id, t.name, count(DISTINCT m.matchup_period) AS periods, count(DISTINCT s.game_id) AS nfl_games
FROM fantasy_teams t
LEFT JOIN matchups m ON m.home_team_id = t.team_id OR m.away_team_id = t.team_id
CROSS JOIN (SELECT game_id FROM nfl_schedules LIMIT 50) s
GROUP BY ALL ORDER BY 1"""

_BASE = None


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000, 1)


def open_league(db: str, token: str):
    if not os.getenv("HOME"):
        os.environ["HOME"] = tempfile.gettempdir()
    t = {}
    t0 = time.perf_counter()
    con = duckdb.connect(f"md:{db}?attach_mode=single&saas_mode=true", config={"motherduck_token": token})
    t["connect_ms"] = _ms(t0)
    t1 = time.perf_counter()
    con.execute(f"ATTACH IF NOT EXISTS 'md:{GLOBAL_DB}' (READ_ONLY)")
    con.execute(f"SET search_path = '{db}.main,{GLOBAL_DB}.main'")
    t["attach_global_ms"] = _ms(t1)
    return con, t


def cold(db: str, token: str) -> dict:
    t0 = time.perf_counter()
    con, t = open_league(db, token)
    t1 = time.perf_counter()
    rows = con.execute(QUERY).fetchall()
    t["query_ms"] = _ms(t1)
    t["total_ms"] = _ms(t0)
    t["rows"] = len(rows)
    con.close()
    return t


def warm_switch(db: str, token: str) -> dict:
    """One cached base connection per process; each request ATTACHes (if needed) its league and queries
    through a fresh cursor with that league first on the search_path."""
    global _BASE
    t = {"reused_base": _BASE is not None}
    t0 = time.perf_counter()
    if _BASE is None:
        _BASE, _ = open_league("spike_lg_001", token)
    cur = _BASE.cursor()
    cur.execute(f"ATTACH IF NOT EXISTS 'md:{db}'")
    cur.execute(f"SET search_path = '{db}.main,{GLOBAL_DB}.main'")
    t["attach_ms"] = _ms(t0)
    t1 = time.perf_counter()
    rows = cur.execute(QUERY).fetchall()
    t["query_ms"] = _ms(t1)
    t["total_ms"] = _ms(t0)
    t["rows"] = len(rows)
    cur.close()
    return t
