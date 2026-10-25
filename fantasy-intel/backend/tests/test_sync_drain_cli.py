"""scripts/sync_drain.py end to end on the fixture league (local object directory, test Postgres), the public-log
audit (scripts/audit_drain_log.py) and the workflow's guard rails (.github/workflows/sync-drain.yml)."""
import importlib.util
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from app.db.league_files import global_key
from app.db.league_publish import DirWriter
from app.db.league_split import content_hash, split_database
from app.sync import espn_source, queue
from cp_seed import seed_league
from espn_fixture import FakeEspnClient
from golden.golden import build_e2e_db

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "fantasy-intel" / "scripts"


def load(name):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(SCRIPTS))


@pytest.fixture(autouse=True)
def _restore_logging():
    """The drain switches library logging off for its whole process; give it back to the rest of the suite."""
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("drain")
    return split_database(build_e2e_db(d / "e2e.duckdb"), d / "league.duckdb", d / "global.duckdb",
                          stamp="2026-10-21T10:00:00", global_stamp="content-addressed")


def test_the_drain_publishes_and_its_whole_output_passes_the_audit(pg, files, tmp_path, monkeypatch, capsys):
    objects = DirWriter(tmp_path / "objects")
    objects.put(global_key(files.global_version), files.global_path)
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as con:
        con.execute("INSERT INTO fi.global_snapshot (global_version, content_hash) VALUES (%s, %s)",
                    [files.global_version, content_hash(files.global_path)])
        con.commit()
    with pg.connect() as su:
        lid = seed_league(su, external_id="999")
        su.commit()
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as con:
        queue.enqueue(con, lid, "initial", key=f"initial:{lid}")

    @contextmanager
    def fake_client(external_id, season, credential):
        yield FakeEspnClient()

    monkeypatch.setattr(espn_source, "espn_client", fake_client)
    monkeypatch.setenv("FI_SYNC_DB_URL", pg.dsn_for("fi_sync"))
    monkeypatch.setenv("LEAGUE_FILES_DIR", str(tmp_path / "objects"))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    drain = load("sync_drain")
    assert drain.main(["--workers", "1", "--budget-min", "2"]) == 0
    out = capsys.readouterr()
    lines = (out.out + out.err).splitlines()
    assert any(line.startswith("maintenance: ") for line in lines)
    assert any(f"league {lid} kind initial attempt 1 -> published" in line for line in lines)
    assert load("audit_drain_log").audit(lines) == []
    with pg.connect() as su:
        assert su.execute("SELECT count(*) FROM fi.league_snapshots WHERE league_id = %s", [lid]).fetchone() == (1,)


def test_an_unconfigured_drain_does_nothing(monkeypatch, capsys):
    monkeypatch.delenv("FI_SYNC_DB_URL", raising=False)
    assert load("sync_drain").main(["--workers", "1"]) == 0
    assert load("audit_drain_log").audit(capsys.readouterr().out.splitlines()) == []


@pytest.mark.parametrize("line", [
    "{11111111-2222-3333-4444-555555555555}",
    "espn_s2=AEabc",
    "AE" + "x" * 60,
    "GET https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/1",
    "Traceback (most recent call last):",
    "Team Tacos beat Team Burritos",
])
def test_the_audit_fails_on_anything_but_known_drain_lines(line):
    assert load("audit_drain_log").audit([line]) != []


def test_the_workflow_never_runs_for_pull_requests_and_is_off_by_default():
    wf = (REPO / ".github" / "workflows" / "sync-drain.yml").read_text()
    on = wf[wf.index("\non:"):wf.index("\npermissions:")]
    assert "pull_request" not in on and "push" not in on
    assert "schedule:" in on and "workflow_dispatch:" in on
    assert "vars.SYNC_DRAIN_ENABLED == 'true'" in wf
    assert "persist-credentials: false" in wf
    assert "contents: read" in wf and "write" not in wf.split("permissions:")[1].split("concurrency:")[0]
    assert "SERVICE_ROLE" not in wf.upper().replace("-", "_")
    assert "audit_drain_log.py" in wf and "if: always()" in wf
