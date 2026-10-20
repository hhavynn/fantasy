import pytest

from app.db.connection import connect, init_db


_HOSTED_ENV = ("SERVE_SNAPSHOT", "SNAPSHOT_BUCKET", "SNAPSHOT_PREFIX", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
               "R2_ENDPOINT")


@pytest.fixture(autouse=True)
def _no_hosted_snapshot_env(monkeypatch):
    """The developer's .env may hold real R2 settings (load_dotenv runs at import); no test may see them."""
    for key in _HOSTED_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "test.duckdb")
    init_db(c)
    yield c
    c.close()


# ---- control-plane Postgres (tests/pg_harness.py) ----------------------------------------------------

from pg_harness import PgCluster, PgDatabase  # noqa: E402


@pytest.fixture(scope="session")
def pg_cluster():
    cluster = PgCluster()
    cluster.start()
    try:
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(scope="session")
def pg_db(pg_cluster):
    db = PgDatabase(pg_cluster)
    db.wait_ready()
    db.create()
    return db


@pytest.fixture
def pg(pg_db):
    pg_db.reset()
    return pg_db


# ---- CI sharding: `pytest --shard=K/N` runs shard K of N ------------------------------------------------

import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

SHARD_WEIGHTS = Path(__file__).with_name("shard_weights.json")   # seconds per test file (scripts/shard_weights.py)


def pytest_addoption(parser):
    parser.addoption("--shard", default=None, metavar="K/N",
                     help="run only shard K of N (balanced by tests/shard_weights.json)")


def shard_of(tests: dict[str, list[str]], n: int, weights: dict[str, float]) -> dict[str, int]:
    """Test id -> shard. `tests`: file -> its test ids in collection order. Whole files go, heaviest first, to the
    lightest shard; then a file heavier than an even share is dealt out test by test into the lightest shards (its
    module fixtures are then built once per shard it spans). Files missing from the weights count as the median
    weight. Deterministic, so N runs with K = 1..N run every test exactly once."""
    known = sorted(weights.values())
    default = known[len(known) // 2] if known else 1.0
    w = {f: weights.get(f, default) for f in tests}
    share = sum(w.values()) / n
    loads, owner = [0.0] * n, {}

    def lightest() -> int:
        return min(range(n), key=lambda i: (loads[i], i))

    order = sorted(tests, key=lambda f: (-w[f], f))
    split = [f for f in order if w[f] > share and len(tests[f]) > 1]
    for f in (f for f in order if f not in split):     # whole files first, heaviest first ...
        k = lightest()
        owner.update((t, k) for t in tests[f])
        loads[k] += w[f]
    for f in split:                                      # ... then the heavy files' tests fill the gaps
        for t in tests[f]:
            k = lightest()
            owner[t] = k
            loads[k] += w[f] / len(tests[f])
    return owner


def pytest_collection_modifyitems(config, items):
    spec = config.getoption("--shard")
    if not spec:
        return
    m = re.fullmatch(r"(\d+)/(\d+)", spec)
    if not m or not 1 <= int(m[1]) <= int(m[2]):
        raise pytest.UsageError(f"--shard must be K/N with 1 <= K <= N, got {spec!r}")
    k, n = int(m[1]) - 1, int(m[2])
    weights = json.loads(SHARD_WEIGHTS.read_text()) if SHARD_WEIGHTS.exists() else {}
    tests: dict[str, list[str]] = {}
    for item in items:
        tests.setdefault(item.nodeid.split("::")[0], []).append(item.nodeid)
    owner = shard_of(tests, n, weights)
    config.hook.pytest_deselected(items=[item for item in items if owner[item.nodeid] != k])
    items[:] = [item for item in items if owner[item.nodeid] == k]
