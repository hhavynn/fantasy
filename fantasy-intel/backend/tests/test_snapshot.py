"""Hosted snapshot (free tiers): publish to R2, the API's per-instance cache, and serving from it."""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.analytics.pipeline import run_analytics
from app.db import r2
from app.db.connection import connect, open_db
from app.db.snapshot import MANIFEST, Manifest, SnapshotCache
from app.db.snapshot_publish import publish_snapshot
from app.espn.sync import run_espn_sync
from espn_fixture import MY_SWID, SEASON, FakeEspnClient
from fake_r2 import FakeR2


@pytest.fixture
def fake():
    f = FakeR2()
    yield f
    f.close()


@pytest.fixture
def synced_db(tmp_path):
    db_path = tmp_path / "local.duckdb"
    run_espn_sync(lambda: open_db(db_path), SimpleNamespace(swid=MY_SWID, my_team_id=None),
                  client=FakeEspnClient(), save_raw_payloads=False)
    con = connect(db_path)
    run_analytics(con, SEASON)
    con.close()
    return db_path


def _put_snapshot(fake, key, payload: bytes, published_at="2026-10-20T00:00:00"):
    import hashlib
    fake.objects[key] = payload
    m = Manifest(key=key, published_at=published_at, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
    fake.objects[f"snapshots/{MANIFEST}"] = m.to_json()
    return m


def _gets(fake, key):
    return sum(1 for method, k, _ in fake.requests if method == "GET" and k == key)


# ----------------------------------------------------------------------------- publish


def test_publish_uploads_a_compact_stamped_copy_before_the_manifest(fake, synced_db, tmp_path):
    before = synced_db.read_bytes()
    m = publish_snapshot(synced_db, fake.config)
    puts = [k for method, k, _ in fake.requests if method == "PUT"]
    assert puts == [m.key, f"snapshots/{MANIFEST}"]           # the file is complete before anything points at it
    assert Manifest.parse(fake.objects[f"snapshots/{MANIFEST}"]) == m
    assert m.schema_version is not None and m.bytes == len(fake.objects[m.key])
    copy = tmp_path / "copy.duckdb"
    copy.write_bytes(fake.objects[m.key])
    con = duckdb.connect(str(copy), read_only=True)
    assert con.execute("SELECT value FROM schema_meta WHERE key = 'published_at'").fetchone()[0] == m.published_at
    assert con.execute("SELECT count(*) FROM fantasy_teams").fetchone()[0] > 0
    con.close()
    assert synced_db.read_bytes() == before                     # the local database is only read


def test_publish_keeps_the_last_three_snapshots(fake, synced_db, monkeypatch):
    import app.db.snapshot_publish as sp
    stamps = iter(f"2026-10-20T0{i}:00:00" for i in range(5))
    monkeypatch.setattr(sp, "utcnow", lambda: SimpleNamespace(isoformat=lambda timespec: next(stamps)))
    keys = [publish_snapshot(synced_db, fake.config).key for _ in range(5)]
    assert sorted(k for k in fake.objects if k.endswith(".duckdb")) == keys[2:]


def test_publish_refuses_missing_or_unsynced_local_db(fake, tmp_path):
    with pytest.raises(FileNotFoundError):
        publish_snapshot(tmp_path / "nope.duckdb", fake.config)
    empty = tmp_path / "empty.duckdb"
    connect(empty).close()
    with pytest.raises(ValueError, match="no fantasy-intel schema"):
        publish_snapshot(empty, fake.config)
    assert fake.objects == {}


# ----------------------------------------------------------------------------- the API's cache


def _settle(cache):
    """Wait for the cache's background download, if one is running."""
    if cache._background is not None:
        cache._background.join(10)


def test_cache_downloads_once_for_concurrent_requests(fake, tmp_path):
    m = _put_snapshot(fake, "snapshots/a.duckdb", b"A" * 500_000)
    cache = SnapshotCache(fake.config, cache_dir=tmp_path / "cache")
    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.local_path())) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len({p for p, _ in results}) == 1 and results[0][1] == m
    assert results[0][0].name.endswith("a.duckdb") and results[0][0].read_bytes() == b"A" * 500_000
    assert _gets(fake, "snapshots/a.duckdb") == 1


def test_a_new_publish_is_fetched_in_the_background_while_the_old_file_is_served(fake, tmp_path):
    now = [0.0]
    cache = SnapshotCache(fake.config, cache_dir=tmp_path / "cache", ttl_s=30, clock=lambda: now[0])
    _put_snapshot(fake, "snapshots/a.duckdb", b"A")
    first = cache.local_path()
    _put_snapshot(fake, "snapshots/b.duckdb", b"B", published_at="2026-10-21T00:00:00")
    now[0] = 10
    assert cache.local_path() == first                          # within the TTL: no manifest request
    assert _gets(fake, f"snapshots/{MANIFEST}") == 1
    now[0] = 31
    assert cache.local_path() == first                          # never blocks on the new download
    _settle(cache)
    path, m = cache.local_path()
    assert path.read_bytes() == b"B" and m.published_at == "2026-10-21T00:00:00"
    assert [p.read_bytes() for p in (tmp_path / "cache").iterdir()] == [b"B"]


def test_a_key_republished_with_other_content_is_downloaded_again(fake, tmp_path):
    now = [0.0]
    cache = SnapshotCache(fake.config, cache_dir=tmp_path / "cache", ttl_s=30, clock=lambda: now[0])
    _put_snapshot(fake, "snapshots/a.duckdb", b"first")
    cache.local_path()
    _put_snapshot(fake, "snapshots/a.duckdb", b"second")      # same key, same second, different bytes
    now[0] = 31
    cache.local_path()
    _settle(cache)
    assert cache.local_path()[0].read_bytes() == b"second"


def test_cache_keeps_serving_when_r2_fails_after_a_first_success(fake, tmp_path):
    now = [0.0]
    cache = SnapshotCache(fake.config, cache_dir=tmp_path / "cache", ttl_s=30, clock=lambda: now[0])
    _put_snapshot(fake, "snapshots/a.duckdb", b"A")
    first = cache.local_path()
    now[0] = 100
    fake.fail_next["GET"] = 500                                 # the manifest read fails
    assert cache.local_path() == first
    _put_snapshot(fake, "snapshots/b.duckdb", b"B")
    fake.objects["snapshots/b.duckdb"] = b"corrupt"             # the new file fails its checksum
    now[0] = 200
    assert cache.local_path() == first
    _settle(cache)
    now[0] = 210
    assert cache.local_path() == first                          # a failed fetch is not retried within the TTL
    _settle(cache)
    assert _gets(fake, "snapshots/b.duckdb") == 1
    now[0] = 241
    cache.local_path()
    _settle(cache)
    assert _gets(fake, "snapshots/b.duckdb") == 2               # ... and is retried after it


def test_a_body_cut_off_mid_stream_falls_back_to_the_cached_file(fake, tmp_path):
    now = [0.0]
    cache = SnapshotCache(fake.config, cache_dir=tmp_path / "cache", ttl_s=30, clock=lambda: now[0])
    _put_snapshot(fake, "snapshots/a.duckdb", b"A")
    first = cache.local_path()
    _put_snapshot(fake, "snapshots/b.duckdb", b"B" * 100_000)
    fake.truncate.add("snapshots/b.duckdb")
    now[0] = 31
    assert cache.local_path() == first
    _settle(cache)
    assert cache.local_path() == first and cache._failed[0] == "snapshots/b.duckdb"
    assert not [p for p in (tmp_path / "cache").iterdir() if p.name.endswith(".part")]


def test_a_truncated_body_is_an_r2_error(fake, tmp_path):
    fake.objects["k"] = b"x" * 10
    fake.truncate.add("k")
    with pytest.raises(r2.R2Error) as err:
        r2.get_bytes(fake.config, "k")
    assert err.value.code == "IncompleteRead"


def test_cache_raises_when_nothing_was_ever_served(fake, tmp_path):
    cache = SnapshotCache(fake.config, cache_dir=tmp_path / "cache")
    with pytest.raises(r2.R2Error):
        cache.local_path()


# ----------------------------------------------------------------------------- the API in snapshot mode


def test_api_serves_the_published_snapshot(fake, synced_db, monkeypatch, tmp_path):
    from app.api import deps

    m = publish_snapshot(synced_db, fake.config)
    for key, value in {"SERVE_SNAPSHOT": "1", "SNAPSHOT_BUCKET": fake.bucket, "R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "AKIDTEST",
                       "R2_SECRET_ACCESS_KEY": "secret-test-key", "R2_ENDPOINT": fake.config.endpoint,
                       "MOTHERDUCK_DATABASE": "fantasy_intel"}.items():   # snapshot wins over MotherDuck
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FANTASY_DB_PATH", "/nonexistent/fantasy.duckdb")
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)
    monkeypatch.setattr(deps, "_SNAP", {})
    monkeypatch.setattr(deps, "connect_motherduck", lambda db: pytest.fail("MotherDuck must not be used"))
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    from app.main import app
    client = TestClient(app)
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/managers").status_code == 200
    assert _gets(fake, m.key) == 1
    with deps.db() as cur:
        assert deps.data_version(cur) == f"snapshot:{m.published_at}"
        with pytest.raises(duckdb.Error):                        # the API's connection is read-only
            cur.execute("DELETE FROM fantasy_teams")


def test_r2_settings_alone_do_not_switch_the_local_api(fake, synced_db, monkeypatch):
    """A laptop .env holds the publisher's R2 settings; its local API must keep serving the local file."""
    from app.api import deps
    for key, value in {"SNAPSHOT_BUCKET": fake.bucket, "R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "AKIDTEST",
                       "R2_SECRET_ACCESS_KEY": "secret-test-key", "R2_ENDPOINT": fake.config.endpoint}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FANTASY_DB_PATH", str(synced_db))
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)
    monkeypatch.setattr(deps, "_SNAP", {})
    from app.main import app
    assert TestClient(app).get("/api/overview").status_code == 200
    assert fake.requests == []


def test_api_keeps_its_open_snapshot_when_a_new_one_cannot_be_opened(fake, synced_db, monkeypatch, tmp_path):
    """A file that passes its checksum but DuckDB cannot open (e.g. a newer storage version)."""
    import tempfile
    from app.api import deps
    from app.db import snapshot as snapshot_mod
    publish_snapshot(synced_db, fake.config)
    for key, value in {"SERVE_SNAPSHOT": "1", "SNAPSHOT_BUCKET": fake.bucket, "R2_ACCOUNT_ID": "acct",
                       "R2_ACCESS_KEY_ID": "AKIDTEST", "R2_SECRET_ACCESS_KEY": "secret-test-key",
                       "R2_ENDPOINT": fake.config.endpoint}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)
    monkeypatch.setattr(deps, "_SNAP", {})
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    from app.main import app
    client = TestClient(app)
    assert client.get("/api/overview").status_code == 200
    cache = deps._SNAP["cache"]
    _put_snapshot(fake, "snapshots/garbage.duckdb", b"not a duckdb file")
    cache._checked_at = -1e9                                     # expire the manifest TTL
    client.get("/api/overview")
    _settle(cache)
    assert cache.local_path()[0].name.endswith("garbage.duckdb")
    assert client.get("/api/overview").status_code == 200      # still served from the open connection
    monkeypatch.setattr(deps, "_SNAP", {})                      # a fresh instance has nothing to fall back to
    r = client.get("/api/overview")
    assert r.status_code == 503 and "Cannot open the published snapshot" in r.json()["detail"]


def test_api_without_a_reachable_snapshot_answers_503(fake, monkeypatch, tmp_path):
    from app.api import deps
    for key, value in {"SERVE_SNAPSHOT": "1", "SNAPSHOT_BUCKET": fake.bucket, "R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "AKIDTEST",
                       "R2_SECRET_ACCESS_KEY": "secret-test-key", "R2_ENDPOINT": fake.config.endpoint}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)
    monkeypatch.setattr(deps, "_SNAP", {})
    from app.main import app
    r = TestClient(app).get("/api/overview")
    assert r.status_code == 503 and "Cannot load the published snapshot" in r.json()["detail"]
    assert "secret-test-key" not in json.dumps(r.json())
