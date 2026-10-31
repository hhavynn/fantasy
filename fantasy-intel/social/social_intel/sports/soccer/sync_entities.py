"""Build the soccer player list from free public sources.

* FPL ``bootstrap-static`` -- Premier League squads, with ownership as an audience proxy.
* American Soccer Analysis -- MLS players with 2026 minutes (please credit ASA).
* Wikidata (CC0) -- La Liga, Bundesliga, Serie A and Ligue 1. Only club spells with a recent start date
  count, because an open-ended old spell is usually just an unmaintained statement.

Each source is isolated: a failure is reported, the others still count, and if everything fails the
previous cache is left alone. ``popularity`` is a rough 0-1 audience proxy on one scale per source.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from social_intel.core.http import BOT_UA, Fetcher, FetchError
from social_intel.sports.soccer.entities import SoccerIndex

log = logging.getLogger(__name__)

FPL_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
ASA = "https://app.americansocceranalysis.com/api/v1/mls"
WIKIDATA = "https://query.wikidata.org/sparql"
WIKIDATA_LEAGUES = {"Q324867": "La Liga", "Q82595": "Bundesliga", "Q15804": "Serie A", "Q13394": "Ligue 1"}
FPL_POSITIONS = {1: "GK", 2: "DF", 3: "MF", 4: "FW"}

SQUAD_QUERY = """
SELECT ?p ?pLabel ?clubLabel ?links ?start WHERE {
  ?club wdt:P118 wd:%(league)s; wdt:P31 wd:Q476028 .
  ?p p:P54 ?st . ?st ps:P54 ?club; pq:P580 ?start .
  FILTER NOT EXISTS { ?st pq:P582 ?end }
  FILTER(?start >= "%(since)s-01-01T00:00:00Z"^^xsd:dateTime)
  ?p wdt:P106 wd:Q937857; wikibase:sitelinks ?links .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} ORDER BY DESC(?links) LIMIT 400
"""


def club_id(index: SoccerIndex, name: str) -> str | None:
    found = index.find_clubs(name, trusted=True)
    return found[0] if found else None


def parse_fpl(data: dict[str, Any], index: SoccerIndex) -> list[dict[str, Any]]:
    teams = {t["id"]: club_id(index, t["name"].replace("'m", "ingham").replace("Utd", "United")) for t in data["teams"]}
    rows = []
    for e in data["elements"]:
        rows.append({"id": f"fpl:{e['code']}", "name": f"{e['first_name']} {e['second_name']}", "team": teams.get(e["team"]),
                     "position": FPL_POSITIONS.get(e["element_type"]), "aliases": [e["web_name"]],
                     "popularity": round(0.3 + 0.7 * min(float(e["selected_by_percent"]) / 50, 1), 3)})
    return rows


def parse_asa(players: list[dict], stats: list[dict], teams: list[dict], index: SoccerIndex) -> list[dict[str, Any]]:
    names = {p["player_id"]: p["player_name"] for p in players}
    clubs = {t["team_id"]: club_id(index, t["team_name"].replace("D.C.", "DC").replace("St.", "St")) for t in teams}
    rows = []
    for s in stats:
        team = s["team_id"][-1] if isinstance(s["team_id"], list) else s["team_id"]
        if s["player_id"] in names:
            rows.append({"id": f"asa:{s['player_id']}", "name": names[s["player_id"]], "team": clubs.get(team),
                         "position": s.get("general_position"),
                         "popularity": round(0.2 + 0.3 * min((s.get("minutes_played") or 0) / 2500, 1), 3)})
    return rows


def parse_wikidata(bindings: list[dict], index: SoccerIndex) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for b in bindings:
        pid, start = b["p"]["value"].rsplit("/", 1)[-1], b["start"]["value"]
        if pid in latest and latest[pid]["_start"] >= start:
            continue
        latest[pid] = {"id": f"wd:{pid}", "name": b["pLabel"]["value"], "team": club_id(index, b["clubLabel"]["value"]),
                       "popularity": round(min(int(b["links"]["value"]) / 100, 1), 3), "_start": start}
    return [{k: v for k, v in r.items() if k != "_start"} for r in latest.values() if r["team"]]


def collect(fetcher: Fetcher, now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    index = SoccerIndex()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        rows += parse_fpl(fetcher.get(FPL_URL).json(), index)
    except (FetchError, KeyError, ValueError) as e:
        errors.append(f"fpl: {e}")
    try:
        rows += parse_asa(fetcher.get(f"{ASA}/players").json(),
                          fetcher.get(f"{ASA}/players/xgoals", params={"season_name": now.year}).json(),
                          fetcher.get(f"{ASA}/teams").json(), index)
    except (FetchError, KeyError, ValueError) as e:
        errors.append(f"asa: {e}")
    for qid, league in WIKIDATA_LEAGUES.items():
        query = SQUAD_QUERY % {"league": qid, "since": now.year - 5}
        try:
            r = fetcher.get(WIKIDATA, params={"format": "json", "query": query}, headers={"User-Agent": BOT_UA},
                            min_interval_s=5)
            rows += parse_wikidata(r.json()["results"]["bindings"], index)
        except (FetchError, KeyError, ValueError) as e:
            errors.append(f"wikidata {league}: {e}")
    return rows, errors


def sync(path: Path, fetcher: Fetcher, now: datetime) -> tuple[int, list[str]]:
    rows, errors = collect(fetcher, now)
    if rows:
        SoccerIndex(rows, now.date().isoformat()).dump(path)
    return len(rows), errors
