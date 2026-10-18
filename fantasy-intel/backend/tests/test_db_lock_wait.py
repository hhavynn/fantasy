"""app.db.connection.connect: waiting out another process's DuckDB lock (FANTASY_DB_LOCK_WAIT_S) for the scheduled
batch jobs, while callers that pass `retries` (the API) keep their short budget."""

import subprocess
import sys
import time
from types import SimpleNamespace

import duckdb
import pytest

from app.db import connection as dbc

LOCK_MSG = 'IO Error: Could not set lock on file "x.duckdb": Conflicting lock is held in python3.13 (PID 54372)'


class FakeClock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def fake(monkeypatch):
    """connect() with a fake clock and a duckdb.connect that fails `plan` times (then returns "con")."""
    clock = FakeClock()
    monkeypatch.setattr(dbc, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    state = {"fail": 0, "msg": LOCK_MSG, "calls": 0}

    def fake_connect(path, read_only=False):
        state["calls"] += 1
        if state["calls"] <= state["fail"]:
            raise duckdb.IOException(state["msg"])
        return "con"

    monkeypatch.setattr(dbc.duckdb, "connect", fake_connect)
    monkeypatch.delenv(dbc.LOCK_WAIT_ENV, raising=False)
    return clock, state


def test_without_the_wait_setting_the_old_short_budget_applies(fake, tmp_path):
    clock, state = fake
    state["fail"] = 10**6
    with pytest.raises(dbc.DatabaseBusy, match="Could not set lock"):
        dbc.connect(tmp_path / "x.duckdb")
    assert state["calls"] == dbc.DEFAULT_RETRIES and clock.t == pytest.approx(0.25 * (dbc.DEFAULT_RETRIES - 1))


def test_the_wait_setting_backs_off_until_the_lock_is_released(fake, tmp_path, monkeypatch):
    clock, state = fake
    monkeypatch.setenv(dbc.LOCK_WAIT_ENV, "120")
    state["fail"] = 8
    assert dbc.connect(tmp_path / "x.duckdb") == "con"
    assert clock.sleeps == [0.25, 0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0]      # doubling, capped at 5 s


def test_the_wait_is_bounded_and_then_raises(fake, tmp_path, monkeypatch):
    clock, state = fake
    monkeypatch.setenv(dbc.LOCK_WAIT_ENV, "30")
    state["fail"] = 10**6
    with pytest.raises(dbc.DatabaseBusy):
        dbc.connect(tmp_path / "x.duckdb")
    assert clock.t == pytest.approx(30.0) and max(clock.sleeps) == 5.0


def test_an_explicit_retries_budget_ignores_the_wait_setting(fake, tmp_path, monkeypatch):
    """The API passes retries=8: a request never hangs for the batch jobs' two minutes."""
    clock, state = fake
    monkeypatch.setenv(dbc.LOCK_WAIT_ENV, "120")
    state["fail"] = 10**6
    with pytest.raises(dbc.DatabaseBusy):
        dbc.connect(tmp_path / "x.duckdb", read_only=True, retries=8)
    assert state["calls"] == 8 and clock.t == pytest.approx(0.25 * 7)


def test_an_error_that_is_not_a_lock_never_waits_past_the_attempt_count(fake, tmp_path, monkeypatch):
    clock, state = fake
    monkeypatch.setenv(dbc.LOCK_WAIT_ENV, "600")
    state.update(fail=10**6, msg="IO Error: Cannot open database: No such file or directory")
    with pytest.raises(dbc.DatabaseBusy, match="No such file"):
        dbc.connect(tmp_path / "x.duckdb", read_only=True)
    assert state["calls"] == dbc.DEFAULT_RETRIES and clock.t < 10


@pytest.mark.parametrize("raw,expected", [("", None), ("  ", None), ("abc", None), ("-5", 0.0), ("90", 90.0)])
def test_lock_wait_setting_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(dbc.LOCK_WAIT_ENV, raw)
    assert dbc.lock_wait_s() == expected


HOLD = """
import sys, time, duckdb
con = duckdb.connect(sys.argv[1], read_only=True)   # a read-only reader, like the social-trends job
print("held", flush=True)
time.sleep(float(sys.argv[2]))
con.close()
"""


def test_a_writer_waits_out_another_processs_read_only_lock(tmp_path, monkeypatch):
    """The 2026-10-18 failure for real: another process holds the file read-only, so a writer cannot open it.
    With the short budget the writer fails; with FANTASY_DB_LOCK_WAIT_S it opens once the reader is gone."""
    db = tmp_path / "live.duckdb"
    duckdb.connect(str(db)).close()

    def hold(seconds: float) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", HOLD, str(db), str(seconds)], stdout=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == "held"
        return proc

    monkeypatch.delenv(dbc.LOCK_WAIT_ENV, raising=False)
    proc = hold(30)
    try:
        with pytest.raises(dbc.DatabaseBusy, match="lock"):
            dbc.connect(db, retries=2)
    finally:
        proc.kill()
        proc.wait()

    monkeypatch.setenv(dbc.LOCK_WAIT_ENV, "60")
    proc = hold(2)
    try:
        started = time.monotonic()
        con = dbc.connect(db)
        con.execute("CREATE TABLE t AS SELECT 1 AS x")      # a real write connection
        con.close()
        assert 1.0 < time.monotonic() - started < 30
    finally:
        proc.wait()


HOLD_WRITE = """
import sys, time, duckdb
con = duckdb.connect(sys.argv[1])                    # a writer, like sync_nfl.py or the production `project` run
con.execute("CREATE TABLE IF NOT EXISTS held (x INT)")
print("held", flush=True)
time.sleep(float(sys.argv[2]))
con.close()
"""


def _load_script(name: str):
    import importlib.util
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(name, scripts / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("script,opener", [
    # the freeze path: the schedule read before predict (also injury_snapshot / check_missed / _target_week)
    ("ctx_shadow", lambda mod, db: mod._last_week(SimpleNamespace(live_db=db, season=2026))),
    # the project path: backtest_independent_projections.py's scoring/bootstrap connection, opened before open_db
    ("backtest_independent_projections", lambda mod, db: mod._ro(db).close()),
])
def test_the_research_and_project_readers_wait_out_a_writers_lock(tmp_path, monkeypatch, script, opener):
    """The children the scheduled jobs launch open the live database read-only through app.db.connection, so
    FANTASY_DB_LOCK_WAIT_S reaches them: a writer holding the file no longer fails them at once."""
    db = tmp_path / "live.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR)")
    con.execute("INSERT INTO nfl_schedules VALUES (2026, 18, 'REG')")
    con.close()
    mod = _load_script(script)
    monkeypatch.setenv(dbc.LOCK_WAIT_ENV, "60")
    proc = subprocess.Popen([sys.executable, "-c", HOLD_WRITE, str(db), "2"], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        started = time.monotonic()
        out = opener(mod, db)
        assert 1.0 < time.monotonic() - started < 30
        if script == "ctx_shadow":
            assert out == 18
    finally:
        proc.wait()
