import pytest

from app.db.connection import connect, init_db


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "test.duckdb")
    init_db(c)
    yield c
    c.close()
