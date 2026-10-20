"""A sync job end to end on the fixture league: fetch (fake ESPN) -> build -> fenced publish, the private-cookie path,
the §3.6 outcomes, lease loss, probes, and the public-log audit (spec §3.8)."""
import io
import uuid
from contextlib import contextmanager

import psycopg
import pytest
from espn_api.requests.espn_requests import ESPNAccessDenied

from app.control_plane.credential_crypto import PrivateKeyring, PublicKeyring, generate_keypair
from app.db.league_files import global_key
from app.db.league_publish import DirWriter
from app.db.league_split import content_hash, split_database
from app.sync import queue
from app.sync.redact import Printer
from app.sync.worker import Context, drain
from cp_seed import U1, as_role, seed_league, seed_membership, seed_user
from espn_fixture import MY_SWID, PLAYERS, FakeEspnClient
from golden.golden import build_e2e_db

COOKIES = {"espn_s2": "S2-SECRET-COOKIE-VALUE", "swid": MY_SWID}


def sync(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("worker")
    db = build_e2e_db(d / "e2e.duckdb")
    out = split_database(db, d / "league.duckdb", d / "global.duckdb", stamp="2026-10-21T10:00:00",
                         global_stamp="content-addressed")
    return out


@pytest.fixture
def world(pg, files, tmp_path):
    objects = DirWriter(tmp_path / "objects")
    objects.put(global_key(files.global_version), files.global_path)
    with sync(pg) as con:
        con.execute("INSERT INTO fi.global_snapshot (global_version, content_hash) VALUES (%s, %s)",
                    [files.global_version, content_hash(files.global_path)])
        con.commit()
    out = io.StringIO()
    calls = []

    @contextmanager
    def client(external_id, season, credential, *, fake=None):
        calls.append({"external_id": external_id, "season": season, "credential": dict(credential or {})})
        yield fake or FakeEspnClient()

    ctx = Context(sync_dsn=pg.dsn_for("fi_sync"), objects=objects, printer=Printer(out, github=False), owner="run1-w1",
                  client=client, control=lambda season: True, probe=lambda ext, season: "public", heartbeat_s=0.05,
                  work_root=tmp_path)
    return {"ctx": ctx, "out": out, "calls": calls, "objects": tmp_path / "objects"}


def pointer(pg, lid):
    with pg.connect() as su:
        return su.execute("SELECT data_version, global_version FROM fi.league_snapshots WHERE league_id = %s",
                          [lid]).fetchone()


def job(pg, lid):
    with pg.connect() as su:
        return su.execute("SELECT status, error_code, attempt FROM fi.sync_jobs WHERE league_id = %s "
                          "ORDER BY created_at DESC LIMIT 1", [lid]).fetchone()


def league_row(pg, lid):
    with pg.connect() as su:
        return su.execute("SELECT visibility, visibility_epoch, status, name FROM fi.leagues WHERE id = %s", [lid]).fetchone()


def enqueue(pg, lid, kind="initial"):
    with sync(pg) as con:
        queue.enqueue(con, lid, kind, key=f"{kind}:{lid}")


def test_a_public_league_is_fetched_built_and_published(pg, world, files):
    with pg.connect() as su:
        lid = seed_league(su, external_id="999", name=None)
        su.commit()
    enqueue(pg, lid)
    done = drain(world["ctx"], budget_s=120)
    assert [d[2] for d in done] == ["published"]
    dv, gv = pointer(pg, lid)
    assert gv == files.global_version
    assert (world["objects"] / "leagues" / lid / f"{dv}.duckdb").exists()
    assert (world["objects"] / "raw" / lid / f"{dv}.json.gz").exists()
    assert job(pg, lid)[0] == "succeeded"
    assert league_row(pg, lid)[3] == "Fixture League", "the name is refreshed from the provider"
    assert world["calls"][0] == {"external_id": 999, "season": 2026, "credential": {}}


def test_the_drain_log_names_no_league_data(pg, world, capfd):
    """Spec §3.8 audit: only opaque ids, durations and codes reach the public log."""
    with pg.connect() as su:
        lid = seed_league(su, external_id="999")
        su.commit()
    enqueue(pg, lid)
    drain(world["ctx"], budget_s=120)
    captured = world["out"].getvalue() + "".join(capfd.readouterr())
    assert f"league {lid}" in world["out"].getvalue()
    forbidden = ["Fixture League", "999", *(f"Team {t}" for t in range(1, 5)), *(n for n, _, _ in PLAYERS.values()),
                 COOKIES["espn_s2"]]
    leaked = [f for f in forbidden if f in world["out"].getvalue()]
    assert leaked == [], leaked
    assert "S2-SECRET" not in captured


@pytest.fixture
def private(pg, world):
    """A private league whose admin U1 stored their cookies (encrypted to a fresh keypair)."""
    priv_pem, pub_pem = generate_keypair()
    world["ctx"].keyring = PrivateKeyring({"v1": priv_pem})
    cid = str(uuid.uuid4())
    with pg.connect() as su:
        seed_user(su, U1)
        lid = seed_league(su, external_id="999", visibility="private")
        seed_membership(su, U1, lid, "admin")
        su.commit()
    enc = PublicKeyring({"v1": pub_pem}).encrypt(COOKIES, league_id=lid, credential_id=cid, owner_user_id=U1)
    with pg.connect(user="fi_api") as con, as_role(con, "authenticated", U1) as c:
        c.execute("SELECT fi.store_credential(%s, %s, %s, %s, %s, %s)",
                  [cid, lid, enc.ciphertext, enc.nonce, enc.wrapped_data_key, enc.wrap_key_version])
    return {"league": lid, "cid": cid}


def test_a_private_league_syncs_with_its_owners_cookies(pg, world, private):
    import duckdb
    enqueue(pg, private["league"])
    assert [d[2] for d in drain(world["ctx"], budget_s=120)] == ["published"]
    assert world["calls"][0]["credential"] == COOKIES
    dv, _ = pointer(pg, private["league"])
    con = duckdb.connect(str(world["objects"] / "leagues" / private["league"] / f"{dv}.duckdb"), read_only=True)
    try:
        assert con.execute("SELECT team_id FROM fantasy_teams WHERE is_mine").fetchall() == [(2,)], "the owner's team"
    finally:
        con.close()
    assert "S2-SECRET" not in world["out"].getvalue()


def test_an_authenticated_401_makes_the_credential_suspect_and_retries(pg, world, private):
    class Denied(FakeEspnClient):
        def league(self):
            raise ESPNAccessDenied("League 999 cannot be accessed with the provided credentials")

    @contextmanager
    def client(external_id, season, credential):
        yield Denied()

    world["ctx"].client = client
    enqueue(pg, private["league"])
    assert [d[2] for d in drain(world["ctx"], budget_s=120)] == ["provider_auth:suspect"]
    assert job(pg, private["league"])[:2] == ("queued", "provider_auth")
    with pg.connect() as su:
        assert su.execute("SELECT status FROM fi.league_credentials").fetchone() == ("suspect",)
        assert su.execute("SELECT status FROM fi.leagues WHERE id = %s", [private["league"]]).fetchone() == ("active",)
    assert pointer(pg, private["league"]) is None


def test_an_anonymous_401_restricts_a_public_league_at_once(pg, world):
    with pg.connect() as su:
        lid = seed_league(su, external_id="999", epoch=3)
        su.commit()

    class Denied(FakeEspnClient):
        def league(self):
            raise ESPNAccessDenied("espn_s2 and swid are required")

    @contextmanager
    def client(external_id, season, credential):
        yield Denied()

    world["ctx"].client = client
    enqueue(pg, lid)
    assert [d[2] for d in drain(world["ctx"], budget_s=120)] == ["provider_private"]
    assert league_row(pg, lid)[:3] == ("private", 4, "unavailable")


def test_a_lost_lease_publishes_nothing(pg, world):
    with pg.connect() as su:
        lid = seed_league(su, external_id="999")
        su.commit()

    @contextmanager
    def client(external_id, season, credential):
        with pg.connect() as su:      # another worker reclaimed the job meanwhile
            su.execute("UPDATE fi.sync_jobs SET lease_token = lease_token + 100 WHERE league_id = %s", [lid])
            su.commit()
        yield FakeEspnClient()

    world["ctx"].client = client
    enqueue(pg, lid)
    assert [d[2] for d in drain(world["ctx"], budget_s=120)] == ["lease_lost"]
    assert pointer(pg, lid) is None
    assert not (world["objects"] / "leagues" / lid).exists()


@pytest.mark.parametrize("verdict, expected", [
    ("public", ("public", 0, "active")),
    ("private", ("private", 1, "unavailable")),
])
def test_a_probe_confirms_or_restricts(pg, world, verdict, expected):
    with pg.connect() as su:
        lid = seed_league(su, external_id="999", checked_at="7 hours")
        su.commit()
    world["ctx"].probe = lambda ext, season: verdict
    enqueue(pg, lid, "probe")
    drain(world["ctx"], budget_s=60)
    assert league_row(pg, lid)[:3] == expected
    with pg.connect() as su:
        fresh = su.execute("SELECT visibility_checked_at > now() - interval '1 minute' FROM fi.leagues WHERE id = %s",
                           [lid]).fetchone()[0]
    assert fresh
    assert job(pg, lid)[0] == "succeeded"


def test_a_probe_that_cannot_tell_keeps_the_league_as_it_was_and_retries(pg, world):
    with pg.connect() as su:
        lid = seed_league(su, external_id="999", checked_at="30 hours")
        su.commit()
    world["ctx"].probe = lambda ext, season: "unknown"
    enqueue(pg, lid, "probe")
    drain(world["ctx"], budget_s=60)
    assert job(pg, lid)[:2] == ("queued", "probe_unknown")
    with pg.connect() as su:
        assert su.execute("SELECT visibility_checked_at < now() - interval '29 hours' FROM fi.leagues WHERE id = %s",
                          [lid]).fetchone() == (True,), "an expired check stays expired: non-members keep getting 404"


def test_recompute_rebuilds_from_the_stored_raw_payloads_without_fetching(pg, world):
    with pg.connect() as su:
        lid = seed_league(su, external_id="999")
        su.commit()
    enqueue(pg, lid)
    drain(world["ctx"], budget_s=120)
    first = pointer(pg, lid)[0]
    world["calls"].clear()
    enqueue(pg, lid, "recompute")
    assert [d[2] for d in drain(world["ctx"], budget_s=120)] == ["published"]
    assert world["calls"] == [], "no provider request"
    assert pointer(pg, lid)[0] != first


def test_a_private_first_sync_keeps_retrying_while_the_credential_is_only_suspect(pg, world, private):
    """The 6-hour confirmation rule must get to decide: the job is not dead after the usual five attempts."""
    class Denied(FakeEspnClient):
        def league(self):
            raise ESPNAccessDenied("denied")

    @contextmanager
    def client(external_id, season, credential):
        yield Denied()

    world["ctx"].client = client
    enqueue(pg, private["league"])
    with pg.connect() as su:
        su.execute("UPDATE fi.sync_jobs SET attempt = 5")
        su.commit()
    drain(world["ctx"], budget_s=60)
    assert job(pg, private["league"])[:2] == ("queued", "provider_auth")


def test_a_429_pauses_the_provider_for_every_worker(pg, world):
    from espn_api.requests.espn_requests import ESPNUnknownError
    with pg.connect() as su:
        a, b = seed_league(su, external_id="999"), seed_league(su, external_id="998")
        su.commit()

    class Limited(FakeEspnClient):
        def league(self):
            raise ESPNUnknownError("ESPN returned an HTTP 429")

    @contextmanager
    def client(external_id, season, credential):
        yield Limited()

    world["ctx"].client = client
    enqueue(pg, a)
    enqueue(pg, b)
    assert [d[2] for d in drain(world["ctx"], budget_s=60)] == ["provider_429"], "the second league waits"
    with pg.connect() as su:
        assert su.execute("SELECT bool_and(paused_until > now()) FROM fi.provider_slots WHERE provider = 'espn'").fetchone() == (True,)
