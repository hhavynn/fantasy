"""Cold and warm performance baselines for the heavy pages under 1/10/25 simultaneous leagues (issue #201).

Everything is local and synthetic: a throwaway Postgres database with the control-plane migrations
(backend/tests/pg_harness.py, FI_TEST_PG_DSN or a local initdb), the E2E fixture league (scripts/build_e2e_db.py)
published as N public leagues to a local object directory exactly as production publishes them, and the real API app
called in-process (httpx ASGITransport: one event loop and its thread pool, like one API instance). The object store
is the local directory behind a simulated R2 link (per-object latency plus bandwidth), so "cold" includes a realistic
download. Production, R2, Supabase and Vercel are never touched.

Each page is modelled as the backend calls its server render makes, in the waves it makes them (the root layout's
/api/status and the cached league profile first, then the page's own calls in parallel). Page time = time until
every call of the last wave has answered.

    cd backend && FI_TEST_PG_DSN=postgresql://postgres@127.0.0.1:55432/postgres \
        uv run python ../scripts/perf_baseline.py [--leagues 1,10,25] [--repeat 3] [--r2-latency-ms 50] \
        [--r2-mbps 30] [--json OUT.json]

Output: Markdown tables on stdout (docs/perf/baseline-2026-10.md records one run).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

import _bootstrap  # noqa: F401

sys.path.insert(0, str(_bootstrap.BACKEND / "tests"))

TEAM = 1
PAGES: dict[str, list[list[str]]] = {
    "overview": [["/api/status", "/api/league-profile"],
                 [f"/api/overview?team_id={TEAM}", f"/api/matchup?team_id={TEAM}", f"/api/start-sit?team_id={TEAM}",
                  f"/api/waivers?view=BEST&limit=3&team_id={TEAM}"]],
    "waivers": [["/api/status", "/api/league-profile"],
                [f"/api/waivers?view=BEST&limit=360&team_id={TEAM}", f"/api/waivers?view=ALL&limit=12&team_id={TEAM}"]],
    "trades": [["/api/status", "/api/league-profile", "/api/trades/options"]],
    "trades-discover": [["/api/status", "/api/league-profile", f"/api/trades/discover/context?team_id={TEAM}&mode=mutual_upgrades"],
                        [f"/api/trades/discover/summary?team_id={TEAM}&mode=mutual_upgrades"]],
    "matchup": [["/api/status", "/api/league-profile"], [f"/api/matchup?team_id={TEAM}"]],
    "players": [["/api/status", "/api/league-profile"], ["/api/players?q=&limit=40"]],
}


class Link:
    """Counts what crosses the simulated R2 link."""

    def __init__(self):
        self.lock = threading.Lock()
        self.gets = 0
        self.bytes = 0

    def add(self, nbytes: int) -> None:
        with self.lock:
            self.gets += 1
            self.bytes += nbytes


def simulated_store(root: Path, link: Link, latency_s: float, bytes_per_s: float):
    from app.db.league_files import DirStore

    class SimulatedR2(DirStore):
        def fetch(self, key, dest, *, sha256_prefix, size):
            time.sleep(latency_s + size / bytes_per_s)
            link.add(size)
            super().fetch(key, dest, sha256_prefix=sha256_prefix, size=size)

    return SimulatedR2(root)


def fresh_instance(tmp: Path, objects: Path, link: Link, latency_s: float, bytes_per_s: float) -> None:
    """A cold API instance: empty file cache, response cache and DuckDB instances, a new /tmp."""
    from app.api import deps, league_scope
    from app.db.league_files import LeagueFileCache
    league_scope._CACHE.clear()
    drop_responses()
    deps._LAYERED.clear()
    cache_dir = tmp / f"inst-{time.monotonic_ns()}"
    league_scope._CACHE[("files", os.getpid())] = LeagueFileCache(
        simulated_store(objects, link, latency_s, bytes_per_s), cache_dir, on_evict=deps.layered_databases().forget)


def drop_responses() -> None:
    """Warm = files cached, every answer computed afresh: the response cache and the trade-search memo are emptied."""
    from app.api import league_scope, trade_discovery
    league_scope._RESPONSES.clear()
    trade_discovery._CACHE.clear()
    trade_discovery._VIEWS.clear()


async def load_page(client, league: str, page: str) -> tuple[float, list[int]]:
    t0 = time.perf_counter()
    statuses = []
    for wave in PAGES[page]:
        rs = await asyncio.gather(*(client.get(f"/v2/leagues/{league}{p}") for p in wave))
        statuses += [r.status_code for r in rs]
    return (time.perf_counter() - t0) * 1000, statuses


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


async def run(args, leagues: list[str], objects: Path, tmp: Path) -> dict:
    import httpx
    from app.api import league_scope
    from app.main import app
    latency, bw = args.r2_latency_ms / 1000, args.r2_mbps * 1_000_000
    out: dict = {"pages": {}, "concurrency": {}, "stampede": {}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://perf", timeout=120) as c:
        # 1. Per page, one league: cold / warm / cached, `repeat` fresh instances each.
        for page in PAGES:
            rows = {"cold": [], "warm": [], "cached": []}
            for _ in range(args.repeat):
                link = Link()
                fresh_instance(tmp, objects, link, latency, bw)
                ms, st = await load_page(c, leagues[0], page)
                assert all(s == 200 for s in st), (page, st)
                rows["cold"].append(ms)
                drop_responses()
                rows["warm"].append((await load_page(c, leagues[0], page))[0])
                rows["cached"].append((await load_page(c, leagues[0], page))[0])
            out["pages"][page] = {k: statistics.median(v) for k, v in rows.items()} | {"r2_gets_cold": link.gets}
        # 2. N leagues at once: Overview cold, then warm and cached; then the trade scan for every league at once.
        for n in args.leagues:
            link = Link()
            fresh_instance(tmp, objects, link, latency, bw)
            res = {}
            for kind in ("cold", "warm", "cached"):
                if kind == "warm":
                    drop_responses()
                t0 = time.perf_counter()
                timed = await asyncio.gather(*(load_page(c, lg, "overview") for lg in leagues[:n]))
                wall = (time.perf_counter() - t0) * 1000
                ms = [t for t, _ in timed]
                res[f"overview_{kind}"] = {"p50": pct(ms, .5), "p95": pct(ms, .95), "max": max(ms), "wall": wall,
                                           "errors": sum(s != 200 for _, st in timed for s in st)}
            res["r2_gets"], res["r2_bytes"] = link.gets, link.bytes
            drop_responses()
            t0 = time.perf_counter()
            timed = await asyncio.gather(*(load_page(c, lg, "trades-discover") for lg in leagues[:n]))
            ms = [t for t, _ in timed]
            res["trade_scan_warm"] = {"p50": pct(ms, .5), "p95": pct(ms, .95), "max": max(ms),
                                      "wall": (time.perf_counter() - t0) * 1000,
                                      "errors": sum(s != 200 for _, st in timed for s in st)}
            out["concurrency"][n] = res
        # 3. Stampede: 10 viewers of one league open Overview at once on a cold instance.
        link = Link()
        fresh_instance(tmp, objects, link, latency, bw)
        timed = await asyncio.gather(*(load_page(c, leagues[0], "overview") for _ in range(10)))
        cache = league_scope.response_cache()
        out["stampede"] = {"viewers": 10, "p50": pct([t for t, _ in timed], .5), "max": max(t for t, _ in timed),
                           "r2_gets": link.gets, "shared_answers": getattr(cache, "shared", 0), "cache_hits": cache.hits}
    return out


def setup(n: int, work: Path):
    """A throwaway control-plane database and N published copies of the E2E league."""
    import psycopg
    from cp_seed import seed_league
    from golden.golden import E2E_SEASON, build_e2e_db
    from pg_harness import PgCluster, PgDatabase, _find
    from app.db.league_publish import DirWriter, publish_league
    if not os.environ.get("FI_TEST_PG_DSN") and not (_find("initdb") and _find("pg_ctl")):
        sys.exit("perf_baseline: needs Postgres: set FI_TEST_PG_DSN or install postgresql (initdb, pg_ctl)")
    cluster = PgCluster()
    cluster.start()
    db = PgDatabase(cluster, f"fi_test_perf_{os.getpid()}")
    db.wait_ready()
    db.create()
    src = build_e2e_db(work / "e2e.duckdb")
    objects = work / "objects"
    leagues = []
    with db.connect() as su:
        for i in range(n):
            leagues.append(seed_league(su, visibility="public", name=f"Perf {i}", external_id=str(900_000 + i)))
        su.commit()
    with psycopg.connect(db.dsn_for("fi_sync"), prepare_threshold=None) as sync:
        for i, lg in enumerate(leagues):
            res = publish_league(src, DirWriter(objects), sync, lg, stamp=f"2026-10-21T12:{i // 60:02d}:{i % 60:02d}")
            assert res.applied
    os.environ.update({
        "MULTILEAGUE_ENABLED": "1", "SUPABASE_URL": "http://127.0.0.1:9", "SUPABASE_DB_URL": db.dsn_for("fi_api"),
        "LEAGUE_FILES_DIR": str(objects), "ESPN_SEASON": E2E_SEASON, "NFL_SEASON": E2E_SEASON,
        "FANTASY_DB_PATH": str(src), "FANTASY_API_KEY": "", "MOTHERDUCK_DATABASE": "", "ESPN_MY_TEAM_ID": "",
        "CLASSIC_SITE_ENABLED": "0", "FI_PERF_SAMPLE": "0", "FI_PERF_SLOW_MS": "0",
    })
    sizes = sorted(p.stat().st_size for p in objects.rglob("*.duckdb"))
    return cluster, db, leagues, objects, sizes


def teardown(cluster, db) -> None:
    import psycopg
    import psycopg.sql
    db.close_open_connections()
    db.terminate_other_backends()
    with psycopg.connect(cluster.superuser_dsn(), autocommit=True) as con:
        con.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(psycopg.sql.Identifier(db.name)))
    cluster.stop()


def report(args, result: dict, sizes: list[int]) -> str:
    from app.monitor.budgets import page_budget_ms, within_budget
    lines = [f"Machine: {platform.platform()}, Python {platform.python_version()}, {os.cpu_count()} CPUs; simulated R2 "
             f"{args.r2_latency_ms:g} ms + {args.r2_mbps:g} MB/s per object; files {min(sizes) / 1e6:.2f}-"
             f"{max(sizes) / 1e6:.2f} MB; median of {args.repeat} fresh instances.", "",
             "| Page | Cold ms | Warm ms | Cached ms | R2 GETs (cold) | Within budget (cold/warm/cached) |",
             "|---|---:|---:|---:|---:|---|"]
    for page, r in result["pages"].items():
        ok = "/".join("yes" if within_budget(page, k, r[k]) else f"NO (>{page_budget_ms(page, k):.0f})"
                      for k in ("cold", "warm", "cached"))
        lines.append(f"| {page} | {r['cold']:.0f} | {r['warm']:.0f} | {r['cached']:.0f} | {r['r2_gets_cold']} | {ok} |")
    lines += ["", "| Leagues at once | Overview cold p50/p95/max ms | warm p50/p95 | cached p50/p95 | R2 GETs | "
                  "Trade scan (warm) p50/p95/max ms | Failed calls |", "|---:|---|---|---|---:|---|---:|"]
    for n, r in result["concurrency"].items():
        c, w, h, t = r["overview_cold"], r["overview_warm"], r["overview_cached"], r["trade_scan_warm"]
        lines.append(f"| {n} | {c['p50']:.0f} / {c['p95']:.0f} / {c['max']:.0f} | {w['p50']:.0f} / {w['p95']:.0f} | "
                     f"{h['p50']:.0f} / {h['p95']:.0f} | {r['r2_gets']} | {t['p50']:.0f} / {t['p95']:.0f} / {t['max']:.0f} | "
                     f"{sum(r[k]['errors'] for k in ('overview_cold', 'overview_warm', 'overview_cached', 'trade_scan_warm'))} |")
    s = result["stampede"]
    lines += ["", f"Stampede: {s['viewers']} viewers open one league's Overview at once on a cold instance: p50 "
                  f"{s['p50']:.0f} ms, max {s['max']:.0f} ms, {s['r2_gets']} R2 GETs, {s['shared_answers']} answers "
                  f"shared from an in-flight identical request, {s['cache_hits']} response-cache hits."]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--leagues", default="1,10,25")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--r2-latency-ms", type=float, default=50.0)
    ap.add_argument("--r2-mbps", type=float, default=30.0)
    ap.add_argument("--json")
    args = ap.parse_args()
    args.leagues = sorted({int(x) for x in args.leagues.split(",") if x.strip()})
    work = Path(tempfile.mkdtemp(prefix="fi-perf-"))
    tempfile.tempdir = str(work / "tmp")
    (work / "tmp").mkdir()
    cluster = db = None
    try:
        cluster, db, leagues, objects, sizes = setup(max(args.leagues), work)
        result = asyncio.run(run(args, leagues, objects, work))
        print(report(args, result, sizes))
        if args.json:
            Path(args.json).write_text(json.dumps(result, indent=1))
    finally:
        if db is not None:
            teardown(cluster, db)
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
