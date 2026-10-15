"""scripts/refresh_all.py: step order, MotherDuck gating, stop-on-failure and the pregame warning."""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture
def refresh(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("refresh_all", SCRIPTS / "refresh_all.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_publish_only_with_a_motherduck_token(refresh, monkeypatch):
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    assert refresh.plan(publish=True) == ["sync_espn.py", "sync_nfl.py", "compute_analytics.py"]
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "  ")
    assert "publish_motherduck.py" not in refresh.plan(publish=True)
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "tok")
    assert refresh.plan(publish=True)[-1] == "publish_motherduck.py"
    assert "publish_motherduck.py" not in refresh.plan(publish=False)


def test_first_failure_stops_the_chain(refresh, monkeypatch, tmp_path):
    for name, body in (("sync_espn.py", "print('espn ok')"), ("sync_nfl.py", "raise SystemExit(3)"),
                       ("compute_analytics.py", "open(__file__ + '.ran', 'w').close()")):
        (tmp_path / name).write_text(body)
    monkeypatch.setattr(refresh, "SCRIPTS", tmp_path)
    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(refresh, "check_pregame", lambda log: pytest.fail("check must not run after a failure"))
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--no-publish"])
    assert refresh.main() == 1
    assert not (tmp_path / "compute_analytics.py.ran").exists()
    log = next((tmp_path / "logs").glob("refresh-*.log")).read_text()
    assert "espn ok" in log and "<== exit 3" in log and "FAILED: sync_nfl.py exited 3" in log


def _snap(**kw):
    return {"week": 6, "first_kickoff": "2026-10-16T00:15:00+00:00", "last_kickoff": "2026-10-20T00:15:00+00:00",
            "latest_pregame_snapshot_at": None, "latest_snapshot_at": None, "captured": False, "players": 0, **kw}


def test_pregame_warning(refresh):
    warn = refresh.pregame_warning
    before = datetime(2026, 10, 15, 22, 0, tzinfo=timezone.utc)
    after = datetime(2026, 10, 16, 1, 0, tzinfo=timezone.utc)
    assert warn(_snap(captured=True, latest_pregame_snapshot_at="2026-10-15T22:00:00+00:00"), after) is None
    assert "must land before the first kickoff" in warn(_snap(), before)
    late = warn(_snap(latest_snapshot_at="2026-10-16T01:00:00+00:00"), after)
    assert late.startswith("missed the week 6 pregame ESPN snapshot") and "2026-10-16T01:00" in late
    assert "can't be checked" in warn(_snap(first_kickoff=None), after)
