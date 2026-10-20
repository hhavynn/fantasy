"""ESPN access for sync jobs (spec §2 "Credential security").

A private league's cookies are account-wide session cookies. While a job runs, `espn_client` swaps the `requests`
module that espn_api and app.espn.client use for a scoped stand-in: a request keeps its cookies only when its URL
is that league's own read endpoint (`…/seasons/<season>/segments/0/leagues/<id>…` or `…/leagueHistory/<id>…`);
every other request (pro players, cards on another path) goes out without them. Every request gets a timeout.

`control_request` and `probe` never carry cookies: the control request shows ESPN is reachable (spec §3.6) and the
probe reads a league's visibility as an anonymous visitor would (spec §2 "Dynamic visibility").
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import requests

FANTASY = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
TIMEOUT = 60


def _prefixes(league_id: int, season: int) -> tuple[str, ...]:
    return (f"{FANTASY}/seasons/{season}/segments/0/leagues/{league_id}", f"{FANTASY}/leagueHistory/{league_id}")


def cookie_allowed(url: str, league_id: int, season: int) -> bool:
    base = url.split("?", 1)[0]
    return any(base == p or base.startswith(p + "/") for p in _prefixes(league_id, season))


class _ScopedRequests:
    """The `requests` module, except that cookies reach only the league's endpoint and nothing waits forever."""

    def __init__(self, league_id: int, season: int):
        self.league_id, self.season = league_id, season

    def get(self, url, *args, cookies=None, timeout=None, **kwargs):
        if cookies and not cookie_allowed(url, self.league_id, self.season):
            cookies = None
        return requests.get(url, *args, cookies=cookies, timeout=timeout or TIMEOUT, **kwargs)

    def __getattr__(self, name):
        return getattr(requests, name)


@contextmanager
def espn_client(league_id: int, season: int, credential: dict | None) -> Iterator:
    """An EspnClient for one league; `credential` = {"espn_s2", "swid"} for a private league, None for a public one.
    The worker runs one job at a time per process, so the module swap is process-wide only for that job."""
    from espn_api.requests import espn_requests

    from ..espn import client as client_mod
    scoped = _ScopedRequests(int(league_id), int(season))
    saved = espn_requests.requests, client_mod.requests
    espn_requests.requests = client_mod.requests = scoped
    try:
        cred = credential or {}
        yield client_mod.EspnClient(int(league_id), int(season), cred.get("espn_s2"), cred.get("swid"))
    finally:
        espn_requests.requests, client_mod.requests = saved


def control_request(season: int) -> bool:
    """A cookie-less GET of ESPN's public player list: True when ESPN answered normally."""
    try:
        r = requests.get(f"{FANTASY}/seasons/{int(season)}/players", params={"view": "players_wl"},
                         headers={"x-fantasy-filter": '{"filterActive":{"value":true}}'}, timeout=30)
        return r.status_code == 200
    except requests.RequestException:
        return False


def probe(league_id: int, season: int) -> str:
    """The league as an anonymous visitor sees it: "public", "private" (401/403), "missing" (404) or "unknown"
    (anything else, including a network error: callers keep a league closed on "unknown")."""
    try:
        r = requests.get(_prefixes(int(league_id), int(season))[0], params={"view": "mSettings"}, timeout=30)
    except requests.RequestException:
        return "unknown"
    return {200: "public", 401: "private", 403: "private", 404: "missing"}.get(r.status_code, "unknown")
