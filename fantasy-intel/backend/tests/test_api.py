"""End-to-end smoke test: fixture ESPN sync -> analytics -> every API endpoint."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.analytics.pipeline import run_analytics
from app.db.connection import connect, open_db
from app.espn.sync import run_espn_sync

from espn_fixture import MY_SWID, SEASON, FakeEspnClient


def test_api_endpoints(tmp_path, monkeypatch):
    db_path = tmp_path / "api.duckdb"
    settings = SimpleNamespace(swid=MY_SWID, my_team_id=None)
    run_espn_sync(lambda: open_db(db_path), settings, client=FakeEspnClient(), save_raw_payloads=False)
    con = connect(db_path)
    stats = run_analytics(con, SEASON)
    con.close()
    assert stats["managers"]["adds"] == 3 and stats["managers"]["trades"] == 1
    assert stats["waivers"]["candidates"] > 0

    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    client = TestClient(app)

    ov = client.get("/api/overview").json()
    assert ov["my_team"]["team_id"] == 2 and len(ov["roster"]) == 12
    assert any(m["item_type"] == "TRADE" for m in ov["recent_transactions"])
    assert ov["sync"]["last_success"]["espn"]

    managers = client.get("/api/managers").json()["managers"]
    assert len(managers) == 4 and "waiver_aggression" in managers[0]["classifications"]

    detail = client.get("/api/managers/1").json()
    trade_count = next(m for m in detail["metrics"] if m["metric"] == "trade_count")
    assert trade_count["value"] == 1
    assert next(m for m in detail["metrics"] if m["metric"] == "two_for_one_trade_count")["value"] == 1
    assert client.get("/api/managers/3").json()["failed_claims"]

    w = client.get("/api/waivers", params={"position": "RB"}).json()
    assert all(p["position"] == "RB" for p in w["players"])
    assert w["coverage"]["evaluated"] >= len(w["players"])
    assert all("decision" in p["components"] for p in w["players"])
    best = client.get("/api/waivers", params={"view": "BEST"}).json()
    assert all(p["components"]["decision"]["actionable"] for p in best["players"])
    free = client.get("/api/waivers", params={"view": "FREE_AGENTS"}).json()
    assert all(p["availability_status"] == "FREEAGENT" for p in free["players"])
    assert client.get("/api/waivers", params={"limit": -1}).status_code == 422
    first = client.get("/api/waivers", params={"limit": 1}).json()
    second = client.get("/api/waivers", params={"limit": 1, "offset": 1}).json()
    if second["players"]:
        assert first["players"][0]["espn_player_id"] != second["players"][0]["espn_player_id"]

    found = client.get("/api/players", params={"q": "echo"}).json()
    assert found and found[0]["full_name"] == "Echo RB"
    pd = client.get(f"/api/players/{found[0]['espn_player_id']}").json()
    assert pd["unavailable_metrics"]["route_participation"].startswith("unavailable")
    assert len(pd["league_points"]) >= 2

    assert client.get("/api/status").status_code == 200
    dq = client.get("/api/data-quality").json()
    from app.db.connection import SCHEMA_VERSION
    assert "validation_issues" in dq and dq["schema_version"] == SCHEMA_VERSION
    assert client.get("/api/players/123456789").status_code == 404
