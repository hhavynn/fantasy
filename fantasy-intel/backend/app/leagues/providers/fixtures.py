"""Offline Sleeper data for `SLEEPER_SOURCE=fixture`: a transport that serves recorded-shape JSON by URL.

Where the data lives: `app/leagues/providers/fixture_data/sleeper/`, inside the application package, so
fixture mode works from a deployed build (Vercel excludes `tests/**`). The files are invented
(names, ids) but shaped like real `api.sleeper.app/v1` responses; see the README next to them.
Fixture mode is a demo / integration-test source, never a stand-in for live data: results carry
provenance `fixture:...` and `data_origin` is "fixture".

The transport never opens a socket. It plugs into `SleeperClient(transport=...)` unchanged, so the
real client code (id checks, caching, retry) runs against it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ...sleeper.client import BASE_URL, Response

FIXTURE_DIR = Path(__file__).resolve().parent / "fixture_data" / "sleeper"

_SAFE = r"[A-Za-z0-9_.-]{1,64}"
_ROUTES: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"^/state/nfl$"), "state.json"),
    (re.compile(rf"^/players/nfl$"), "players.json"),
    (re.compile(rf"^/user/(?P<user>{_SAFE})$"), "user_{user}.json"),
    (re.compile(rf"^/user/(?P<user>{_SAFE})/leagues/nfl/(?P<season>\d{{4}})$"), "user_leagues_{user}_{season}.json"),
    (re.compile(r"^/league/(?P<lid>\d{1,32})$"), "leagues/{lid}/league.json"),
    (re.compile(r"^/league/(?P<lid>\d{1,32})/users$"), "leagues/{lid}/users.json"),
    (re.compile(r"^/league/(?P<lid>\d{1,32})/rosters$"), "leagues/{lid}/rosters.json"),
    (re.compile(r"^/league/(?P<lid>\d{1,32})/matchups/(?P<week>\d{1,2})$"), "leagues/{lid}/matchups_{week}.json"),
    (re.compile(r"^/league/(?P<lid>\d{1,32})/transactions/(?P<week>\d{1,2})$"), "leagues/{lid}/transactions_{week}.json"),
]
# What Sleeper answers (200 + "null") for an id it does not know; a known league with no data that
# week answers an empty list.
_UNKNOWN = b"null"
_EMPTY_LIST = b"[]"


class FixtureTransport:
    """`SleeperClient` transport backed by files. `is_fixture` lets tests and the provider assert that a
    client is offline; `calls` records every path requested."""

    is_fixture = True

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else FIXTURE_DIR
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> Response:
        if not url.startswith(BASE_URL):
            raise AssertionError(f"fixture transport only serves {BASE_URL}, got a different host")
        path = unquote(urlsplit(url).path)[len(urlsplit(BASE_URL).path):]
        self.calls.append(path)
        for pattern, template in _ROUTES:
            m = pattern.match(path)
            if m is None:
                continue
            file = self.root / template.format(**m.groupdict())
            # The patterns only admit [A-Za-z0-9_.-] segments; refuse anything that could climb out.
            if not file.resolve().is_relative_to(self.root.resolve()):
                return Response(404, {}, b"")
            if file.is_file():
                return Response(200, {}, file.read_bytes())
            if "lid" in m.groupdict() and not (self.root / "leagues" / m["lid"] / "league.json").is_file():
                return Response(200, {}, _UNKNOWN)
            if "week" in m.groupdict():
                return Response(200, {}, _EMPTY_LIST)
            return Response(200, {}, _UNKNOWN)
        return Response(404, {}, b"")

    def league_ids(self) -> list[str]:
        return sorted(p.name for p in (self.root / "leagues").iterdir() if p.is_dir())


def fixture_transport(root: Path | None = None) -> FixtureTransport:
    return FixtureTransport(root)


def load_fixture_id_map():
    """The recorded DynastyProcess-shaped ID map, read like `nflreadpy.load_ff_playerids()` (polars type
    inference, so sleeper_id is an integer column that the id map casts back to a string)."""
    import polars as pl

    from ...sleeper.id_map import SleeperIdMap

    return SleeperIdMap.from_frame(pl.read_csv(FIXTURE_DIR / "ff_playerids.csv"))


def read_json(name: str):
    return json.loads((FIXTURE_DIR / name).read_text())
