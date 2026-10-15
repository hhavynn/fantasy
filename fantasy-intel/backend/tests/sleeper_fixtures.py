"""Offline Sleeper test helpers: recorded JSON fixtures and a scripted HTTP transport."""

from __future__ import annotations

import json
from pathlib import Path

from app.sleeper.client import BASE_URL, Response, SleeperClient

FIXTURES = Path(__file__).parent / "fixtures" / "sleeper"
LEAGUE_ID = "1180000000000000001"


def raw(name: str):
    return json.loads((FIXTURES / name).read_text())


def body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def ok(payload: bytes | str, headers: dict | None = None) -> Response:
    return Response(200, headers or {}, payload.encode() if isinstance(payload, str) else payload)


def status(code: int, headers: dict | None = None, payload: bytes = b"") -> Response:
    return Response(code, headers or {}, payload)


class FakeTransport:
    """Serves scripted responses per path. A list is consumed in order (last one repeats);
    an Exception instance in the list is raised. Records every requested path."""

    def __init__(self, routes: dict[str, Response | list]):
        self.routes = {k: (v if isinstance(v, list) else [v]) for k, v in routes.items()}
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> Response:
        assert url.startswith(BASE_URL), url
        path = url[len(BASE_URL):]
        self.calls.append(path)
        if path not in self.routes:
            raise AssertionError(f"unexpected request {path}")
        seq = self.routes[path]
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return item

    def count(self, path: str) -> int:
        return self.calls.count(path)


def league_routes(league_id: str = LEAGUE_ID) -> dict:
    return {
        f"/league/{league_id}": ok(body("league.json")),
        f"/league/{league_id}/users": ok(body("users.json")),
        f"/league/{league_id}/rosters": ok(body("rosters.json")),
        "/state/nfl": ok(body("state.json")),
        "/players/nfl": ok(body("players.json")),
    }


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_client(tmp_path, routes: dict | None = None, **kw) -> tuple[SleeperClient, FakeTransport, list[float]]:
    transport = FakeTransport(routes if routes is not None else league_routes())
    sleeps: list[float] = []
    kw.setdefault("clock", Clock())
    client = SleeperClient(cache_dir=tmp_path / "sleeper", transport=transport, sleep=sleeps.append, **kw)
    return client, transport, sleeps
