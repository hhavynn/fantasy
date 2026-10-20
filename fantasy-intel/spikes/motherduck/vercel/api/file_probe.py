"""Phase 0b: read path from object storage. download = fetch the whole DuckDB file to /tmp and open it;
range = ATTACH the remote file over HTTP(S) and let DuckDB fetch only the pages the queries touch."""
import os, tempfile, time, urllib.request

import duckdb

QUERIES = [  # a home-page-like mix over league and global tables
    "SELECT team_id, name, wins, losses, points_for FROM fantasy_teams ORDER BY wins DESC, points_for DESC",
    "SELECT matchup_period, count(*) FROM matchups GROUP BY 1 ORDER BY 1",
    "SELECT p.full_name, p.position, p.injury_status FROM fantasy_players p WHERE p.on_team_id IS NOT NULL ORDER BY 1 LIMIT 200",
    "SELECT espn_player_id, waiver_score, recommendation FROM waiver_recommendations ORDER BY waiver_score DESC LIMIT 50",
    "SELECT espn_player_id, sum(actual_points) FROM espn_player_weekly_points GROUP BY 1 ORDER BY 2 DESC LIMIT 100",
    "SELECT espn_player_id, ros_projected_points FROM player_ros_projections ORDER BY 2 DESC LIMIT 100",
    "SELECT gsis_id, sum(targets), avg(offense_pct) FROM player_week_usage WHERE season = (SELECT max(season) FROM player_week_usage) GROUP BY 1 ORDER BY 2 DESC LIMIT 100",
    "SELECT week, count(*) FROM nfl_schedules WHERE season = (SELECT max(season) FROM nfl_schedules) GROUP BY 1",
]
TMP = tempfile.gettempdir()


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000, 1)


def _run(con, tables_ok: set | None = None) -> dict:
    t0 = time.perf_counter(); per = []
    for q in QUERIES:
        t1 = time.perf_counter()
        try:
            con.execute(q).fetchall(); per.append(_ms(t1))
        except duckdb.CatalogException:
            per.append(None)  # table lives in the other split file
    return {"queries_ms": _ms(t0), "per_query_ms": per}


def download(url: str, name: str, headers: dict) -> dict:
    path = os.path.join(TMP, name)
    out = {"cached": os.path.exists(path)}
    t0 = time.perf_counter()
    if not out["cached"]:
        for old in os.listdir(TMP):  # keep one cached file per instance (/tmp is 500 MB)
            if old.endswith(".duckdb") and old != name:
                os.remove(os.path.join(TMP, old))
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r, open(path + ".part", "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(path + ".part", path)
    out["fetch_ms"] = _ms(t0); out["bytes"] = os.path.getsize(path)
    t1 = time.perf_counter()
    con = duckdb.connect(path, read_only=True)
    out["open_ms"] = _ms(t1)
    out.update(_run(con)); con.close()
    out["total_ms"] = _ms(t0)
    return out


def range_read(url: str, headers: dict) -> dict:
    os.environ.setdefault("HOME", TMP)
    out = {}
    t0 = time.perf_counter()
    con = duckdb.connect(config={"extension_directory": os.path.join(TMP, "duckdb_ext")})
    con.execute("INSTALL httpfs"); con.execute("LOAD httpfs")
    out["httpfs_load_ms"] = _ms(t0)
    if headers:
        hdr = ", ".join(f"'{k}': '{v}'" for k, v in headers.items())
        con.execute(f"CREATE OR REPLACE SECRET probe_hdr (TYPE http, EXTRA_HTTP_HEADERS MAP {{{hdr}}})")
    try:
        con.execute("CALL enable_logging('HTTP')")
        logging = True
    except duckdb.Error:
        logging = False
    t1 = time.perf_counter()
    con.execute(f"ATTACH '{url}' AS r (READ_ONLY)"); con.execute("USE r")
    out["attach_ms"] = _ms(t1)
    out.update(_run(con))
    if logging:
        try:
            out["http_requests"], out["http_bytes"] = con.execute(
                "SELECT count(*), sum(TRY_CAST(response.headers['Content-Length'] AS BIGINT)) "
                "FROM duckdb_logs_parsed('HTTP')").fetchone()
        except duckdb.Error as e:
            out["http_log_error"] = str(e)[:200]
    out["total_ms"] = _ms(t0)
    con.close()
    return out


def pdownload(url: str, name: str, total: int, parts: int = 8) -> dict:
    """Parallel ranged GETs into one preallocated file (os.pwrite), then open + queries."""
    from concurrent.futures import ThreadPoolExecutor
    path = os.path.join(TMP, name)
    for old in os.listdir(TMP):
        if old.endswith(".duckdb") and old != name:
            os.remove(os.path.join(TMP, old))
    t0 = time.perf_counter()
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    os.ftruncate(fd, total)
    step = -(-total // parts)

    def fetch(i):
        start, end = i * step, min(total, (i + 1) * step) - 1
        req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
        off = start
        with urllib.request.urlopen(req, timeout=120) as r:
            while chunk := r.read(1 << 20):
                os.pwrite(fd, chunk, off)
                off += len(chunk)
        return off - start

    with ThreadPoolExecutor(parts) as ex:
        got = sum(ex.map(fetch, range(parts)))
    os.close(fd)
    out = {"fetch_ms": _ms(t0), "bytes": got, "parts": parts}
    t1 = time.perf_counter()
    con = duckdb.connect(path, read_only=True)
    out["open_ms"] = _ms(t1)
    out.update(_run(con)); con.close()
    out["total_ms"] = _ms(t0)
    return out
