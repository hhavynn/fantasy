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
