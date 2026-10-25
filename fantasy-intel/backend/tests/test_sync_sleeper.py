"""Sleeper leagues through the sync jobs (Phase 4b): fetch -> build (write_sleeper) -> fenced publish, recompute from
the stored raw payloads, the §3.6 outcomes, the provider switch and the public-log audit."""
import io

import psycopg
import pytest

from app.db.league_files import global_key
from app.db.league_publish import DirWriter
from app.db.league_split import content_hash, split_database
from app.sleeper.client import InvalidLeagueId, SleeperRateLimited
from app.sync import queue
from app.sync.raw_store import load_payloads
from app.sync.redact import Printer
from app.sync.worker import Context, drain
from cp_seed import seed_league
from golden.golden import build_e2e_db
from test_sleeper_write import LEAGUE, _j, payloads

SLEEPER_ID = "1180000000000000001"
BOTH = ("espn", "sleeper")


def sync(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("sleeper-worker")
    db = build_e2e_db(d / "e2e.duckdb")
    return split_database(db, d / "league.duckdb", d / "global.duckdb", stamp="2026-10-21T10:00:00",
                          global_stamp="content-addressed")


@pytest.fixture
def world(pg, files, tmp_path):
    objects = DirWriter(tmp_path / "objects")
    objects.put(global_key(files.global_version), files.global_path)
    with sync(pg) as con:
        con.execute("INSERT INTO fi.global_snapshot (global_version, content_hash) VALUES (%s, %s)",
                    [files.global_version, content_hash(files.global_path)])
        con.commit()
    out, calls = io.StringIO(), []
    state = {"fetch": lambda ext, ctx: payloads(), "probe": "public"}

    def fetch(ext, ctx):
        calls.append(ext)
        return state["fetch"](ext, ctx)

    ctx = Context(sync_dsn=pg.dsn_for("fi_sync"), objects=objects, printer=Printer(out, github=False), owner="run1-w1",
                  client=None, control=lambda season: True, probe=lambda ext, season: "public", heartbeat_s=0.05,
                  work_root=tmp_path, sleeper_fetch=fetch, sleeper_probe=lambda ext: state["probe"],
                  sleeper_control=lambda season: True)
    return {"ctx": ctx, "out": out, "calls": calls, "objects": tmp_path / "objects", "state": state}


def league(pg):
    with pg.connect() as su:
        lid = seed_league(su, provider="sleeper", external_id=SLEEPER_ID, name=None)
        su.commit()
    return lid


def enqueue(pg, lid, kind="initial"):
    with sync(pg) as con:
        queue.enqueue(con, lid, kind, key=f"{kind}:{lid}")


def pointer(pg, lid):
    with pg.connect() as su:
        return su.execute("SELECT data_version FROM fi.league_snapshots WHERE league_id = %s", [lid]).fetchone()


def last_job(pg, lid):
    with pg.connect() as su:
        return su.execute("SELECT kind, status, error_code FROM fi.sync_jobs WHERE league_id = %s "
                          "ORDER BY created_at DESC LIMIT 1", [lid]).fetchone()


def test_a_sleeper_league_is_fetched_built_and_published(pg, world):
    lid = league(pg)
    enqueue(pg, lid)
    done = drain(world["ctx"], budget_s=120, providers=BOTH)
    assert [d[2] for d in done] == ["published"] and world["calls"] == [SLEEPER_ID]
    (dv,) = pointer(pg, lid)
    path = world["objects"] / "leagues" / lid / f"{dv}.duckdb"
    import duckdb
    con = duckdb.connect(str(path), read_only=True)
    try:
        assert con.execute("SELECT league_id FROM leagues").fetchone() == (int(SLEEPER_ID),)
        assert con.execute("SELECT count(*) FROM fantasy_teams").fetchone() == (4,)
    finally:
        con.close()
    raw = load_payloads((world["objects"] / "raw" / lid / f"{dv}.json.gz").read_bytes())
    assert raw.league["league_id"] == SLEEPER_ID
    with pg.connect() as su:
        assert su.execute("SELECT name FROM fi.leagues WHERE id = %s", [lid]).fetchone()[0] == \
            _j(LEAGUE / "league.json")["name"]


def test_a_recompute_rebuilds_from_the_stored_payloads_without_fetching(pg, world):
    lid = league(pg)
    enqueue(pg, lid)
    drain(world["ctx"], budget_s=120, providers=BOTH)
    first = pointer(pg, lid)
    enqueue(pg, lid, "recompute")
    done = drain(world["ctx"], budget_s=120, providers=BOTH)
    assert [d[2] for d in done] == ["published"] and world["calls"] == [SLEEPER_ID]
    assert pointer(pg, lid) != first


def test_without_the_switch_the_drain_leaves_sleeper_jobs_queued(pg, world):
    lid = league(pg)
    enqueue(pg, lid)
    assert drain(world["ctx"], budget_s=60) == []
    assert last_job(pg, lid)[1] == "queued"


def test_a_429_pauses_sleeper_and_an_unknown_league_counts_as_missing(pg, world):
    lid = league(pg)
    enqueue(pg, lid)

    def limited(ext, ctx):
        raise SleeperRateLimited("429")
    world["state"]["fetch"] = limited
    drain(world["ctx"], budget_s=60, providers=BOTH)
    assert last_job(pg, lid)[1:] == ("queued", "provider_429")
    with pg.connect() as su:
        assert su.execute("SELECT count(*) FROM fi.provider_slots WHERE provider = 'sleeper' "
                          "AND paused_until > now()").fetchone()[0] > 0
        su.execute("UPDATE fi.provider_slots SET paused_until = NULL")
        su.execute("UPDATE fi.sync_jobs SET run_after = now() WHERE league_id = %s", [lid])
        su.commit()

    def gone(ext, ctx):
        raise InvalidLeagueId(ext, "Sleeper has no league with this id")
    world["state"]["fetch"] = gone
    drain(world["ctx"], budget_s=60, providers=BOTH)
    assert last_job(pg, lid)[2] == "provider_not_found"
    with pg.connect() as su:
        assert su.execute("SELECT missing_count FROM fi.leagues WHERE id = %s", [lid]).fetchone()[0] == 1


def test_a_probe_checks_existence_only(pg, world):
    lid = league(pg)
    enqueue(pg, lid, "probe")
    assert [d[2] for d in drain(world["ctx"], budget_s=60, providers=BOTH)] == ["probe_public"]
    world["state"]["probe"] = "missing"
    enqueue(pg, lid, "probe")
    assert drain(world["ctx"], budget_s=60, providers=BOTH)[0][2].startswith("probe_missing")
    with pg.connect() as su:
        assert su.execute("SELECT visibility FROM fi.leagues WHERE id = %s", [lid]).fetchone()[0] == "public"


def test_the_drain_log_names_no_league_data(pg, world):
    lid = league(pg)
    enqueue(pg, lid)
    drain(world["ctx"], budget_s=120, providers=BOTH)
    log = world["out"].getvalue()
    assert f"league {lid}" in log
    names = [_j(LEAGUE / "league.json")["name"], SLEEPER_ID,
             *(u.get("display_name") for u in _j(LEAGUE / "users.json") if u.get("display_name"))]
    assert [n for n in names if n and n in log] == []
