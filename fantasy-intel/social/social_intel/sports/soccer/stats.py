"""Soccer statistics in their own database: fixtures, results, team form and player season lines.

* football-data.co.uk CSVs -- results, team xG and upcoming fixtures for the top five leagues, MLS results.
* FPL ``bootstrap-static`` -- Premier League player minutes, goals, assists, xG and xA.
* American Soccer Analysis -- MLS player minutes, goals, assists, xG and xA (please credit ASA).

``sync`` replaces one source's rows at a time, so a failed source keeps its previous data. ``fantasy.duckdb``
is never touched. Every stat ``SoccerEnricher`` returns has the contract shape: label, value, source, as_of.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from social_intel.core.http import Fetcher, FetchError
from social_intel.sports.soccer.clubs import CLUB_BY_ID
from social_intel.sports.soccer.entities import SoccerIndex
from social_intel.sports.soccer.sync_entities import ASA, FPL_URL

CSV_BASE = "https://www.football-data.co.uk"
CSV_LEAGUES = {"E0": "Premier League", "SP1": "La Liga", "D1": "Bundesliga", "I1": "Serie A", "F1": "Ligue 1"}
FPL_SOURCE = "FPL bootstrap-static"
ASA_SOURCE = "American Soccer Analysis (americansocceranalysis.com)"
# Names the CSVs spell differently from the club table.
CSV_NAMES = {
    "Ath Bilbao": "athletic-bilbao", "Ath Madrid": "atletico", "Sociedad": "real-sociedad", "Vallecano": "rayo",
    "Espanol": "espanyol", "Ein Frankfurt": "frankfurt", "M'gladbach": "gladbach", "Paris SG": "psg",
    "Nott'm Forest": "nottingham-forest", "Man United": "man-utd", "Man City": "man-city", "Inter": "inter",
    "Milan": "milan", "Roma": "roma", "Verona": "verona", "Como": "como", "Genoa": "genoa", "Parma": "parma",
    "Pisa": "pisa", "Bologna": "bologna", "Hull": "hull", "Wolves": "wolves", "Tottenham": "tottenham",
    "Nice": "nice", "Lens": "lens", "Brest": "brest", "Metz": "metz", "Monaco": "monaco", "Valencia": "valencia",
    "Forest": "nottingham-forest", "Villa": "aston-villa", "Crystal Palace": "crystal-palace",
    "Hamburg": "hamburg", "St Pauli": "st-pauli", "Union Berlin": "union-berlin", "Southampton": "southampton",
    "Sporting Kansas City": "sporting-kc", "St. Louis City": "st-louis-city", "D.C. United": "dc-united",
    "Atlanta Utd": "atlanta-united", "Charlotte": "charlotte-fc", "Los Angeles Galaxy": "la-galaxy",
    "New York City": "nycfc",
}
# A table missing a club (an unmapped promoted side) would rank everyone wrongly, so position needs them all.
LEAGUE_SIZE = {"Premier League": 20, "La Liga": 20, "Bundesliga": 18, "Serie A": 20, "Ligue 1": 18}

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (source VARCHAR, league VARCHAR, played DATE, home VARCHAR, away VARCHAR,
    home_goals INTEGER, away_goals INTEGER, home_xg DOUBLE, away_xg DOUBLE, as_of VARCHAR);
CREATE TABLE IF NOT EXISTS fixtures (source VARCHAR, league VARCHAR, kickoff TIMESTAMPTZ, home VARCHAR, away VARCHAR,
    as_of VARCHAR);
CREATE TABLE IF NOT EXISTS player_seasons (source VARCHAR, player_id VARCHAR, minutes INTEGER, goals INTEGER,
    assists INTEGER, xg DOUBLE, xa DOUBLE, as_of VARCHAR);
"""


def csv_season(now: datetime) -> str:
    start = now.year if now.month >= 7 else now.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def club_from_csv(index: SoccerIndex, name: str) -> str | None:
    if name in CSV_NAMES:
        return CSV_NAMES[name]
    found = index.find_clubs(name, trusted=True)
    return found[0] if found else None


def _rows(text: str, *required: str) -> list[dict[str, str]]:
    """A blank or HTML body still comes back 200, so a missing header must fail rather than erase a league."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    missing = set(required) - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"unexpected CSV, missing columns {sorted(missing)}")
    return list(reader)


def _day(value: str) -> date:
    fmt = "%d/%m/%y" if len(value.rsplit("/", 1)[-1]) == 2 else "%d/%m/%Y"
    return datetime.strptime(value, fmt).date()


def _num(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def parse_results(text: str, league: str, index: SoccerIndex, as_of: str) -> tuple[list[tuple], set[str]]:
    out, unknown = [], set()
    for r in _rows(text, "Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"):
        home, away = club_from_csv(index, r["HomeTeam"]), club_from_csv(index, r["AwayTeam"])
        unknown |= {n for n, c in ((r["HomeTeam"], home), (r["AwayTeam"], away)) if not c}
        if home and away and r.get("FTHG") not in (None, ""):
            out.append(("football-data.co.uk", league, _day(r["Date"]), home, away, int(r["FTHG"]), int(r["FTAG"]),
                        _num(r.get("HxG")), _num(r.get("AxG")), as_of))
    return out, unknown


def parse_mls(text: str, season: int, index: SoccerIndex, as_of: str) -> tuple[list[tuple], set[str]]:
    out, unknown = [], set()
    for r in _rows(text, "League", "Season", "Date", "Home", "Away", "HG", "AG"):
        if r.get("League") != "MLS" or r.get("Season") != str(season) or r.get("HG") in (None, ""):
            continue
        home, away = club_from_csv(index, r["Home"]), club_from_csv(index, r["Away"])
        unknown |= {n for n, c in ((r["Home"], home), (r["Away"], away)) if not c}
        if home and away:
            out.append(("football-data.co.uk", "MLS", _day(r["Date"]), home, away, int(float(r["HG"])), int(float(r["AG"])),
                        None, None, as_of))
    return out, unknown


def parse_fixtures(text: str, index: SoccerIndex, as_of: str) -> list[tuple]:
    out = []
    for r in _rows(text, "Div", "Date", "HomeTeam", "AwayTeam"):
        league = CSV_LEAGUES.get(r.get("Div", ""))
        home, away = club_from_csv(index, r.get("HomeTeam", "")), club_from_csv(index, r.get("AwayTeam", ""))
        if league and home and away:
            kickoff = datetime.strptime(f"{r['Date']} {r.get('Time') or '12:00'}", "%d/%m/%Y %H:%M")
            out.append(("football-data.co.uk", league, kickoff.replace(tzinfo=timezone.utc), home, away, as_of))
    return out


def parse_fpl_players(data: dict[str, Any], as_of: str) -> list[tuple]:
    return [(FPL_SOURCE, f"fpl:{e['code']}", e["minutes"], e["goals_scored"], e["assists"], _num(e["expected_goals"]),
             _num(e["expected_assists"]), as_of) for e in data["elements"] if e["minutes"]]


def parse_asa_players(stats: list[dict], as_of: str) -> list[tuple]:
    return [(ASA_SOURCE, f"asa:{s['player_id']}", s.get("minutes_played") or 0, s.get("goals") or 0,
             s.get("primary_assists") or 0, s.get("xgoals"), s.get("xassists"), as_of) for s in stats]


def _replace(con: duckdb.DuckDBPyConnection, table: str, where: str, params: list, rows: list[tuple]) -> None:
    con.execute("BEGIN")
    con.execute(f"DELETE FROM {table} WHERE {where}", params)
    if rows:
        con.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(rows[0]))})", rows)
    con.execute("COMMIT")


def sync(db_path: Path, fetcher: Fetcher, now: datetime, index: SoccerIndex | None = None) -> tuple[dict[str, int], list[str]]:
    index = index or SoccerIndex()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    as_of = now.date().isoformat()
    counts: dict[str, int] = {}
    errors: list[str] = []
    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA)

    def step(name: str, fn) -> None:
        try:
            fn()
        except (FetchError, KeyError, ValueError) as e:
            errors.append(f"{name}: {e}")

    def results(code: str, league: str) -> None:
        text = fetcher.get(f"{CSV_BASE}/mmz4281/{csv_season(now)}/{code}.csv").text
        rows, unknown = parse_results(text, league, index, as_of)
        if unknown:
            errors.append(f"{league}: unmapped clubs {sorted(unknown)}")
        _replace(con, "matches", "league = ?", [league], rows)
        counts[league] = len(rows)

    def mls() -> None:
        rows, unknown = parse_mls(fetcher.get(f"{CSV_BASE}/new/USA.csv").text, now.year, index, as_of)
        if unknown:
            errors.append(f"MLS: unmapped clubs {sorted(unknown)}")
        _replace(con, "matches", "league = ?", ["MLS"], rows)
        counts["MLS"] = len(rows)

    def fixtures() -> None:
        rows = parse_fixtures(fetcher.get(f"{CSV_BASE}/fixtures.csv").text, index, as_of)
        _replace(con, "fixtures", "source = ?", ["football-data.co.uk"], rows)
        counts["fixtures"] = len(rows)

    def fpl() -> None:
        rows = parse_fpl_players(fetcher.get(FPL_URL).json(), as_of)
        if not rows:
            raise ValueError("no player rows")
        _replace(con, "player_seasons", "source = ?", [FPL_SOURCE], rows)
        counts["fpl_players"] = len(rows)

    def asa() -> None:
        rows = parse_asa_players(fetcher.get(f"{ASA}/players/xgoals", params={"season_name": now.year}).json(), as_of)
        if not rows:
            raise ValueError("no player rows")
        _replace(con, "player_seasons", "source = ?", [ASA_SOURCE], rows)
        counts["asa_players"] = len(rows)

    for code, league in CSV_LEAGUES.items():
        step(league, lambda c=code, g=league: results(c, g))
    for name, fn in (("MLS", mls), ("fixtures", fixtures), ("FPL", fpl), ("ASA", asa)):
        step(name, fn)
    con.close()
    return counts, errors


def _stat(label: str, value: Any, source: str, as_of: str | None) -> dict[str, Any]:
    return {"label": label, "value": value, "source": source, "as_of": as_of}


class SoccerEnricher:
    """Reads the stats database read-only, one connection per call, so a sync can run between ranks."""

    def __init__(self, db_path: Path, index: SoccerIndex, now: datetime):
        self.db_path, self.index, self.now = db_path, index, now

    def _club_stats(self, con: duckdb.DuckDBPyConnection, cid: str) -> list[dict[str, Any]]:
        rows = con.execute(
            """SELECT league, played, home, home_goals, away_goals, home_xg, away_xg FROM matches
               WHERE (home = ? OR away = ?) AND played <= ? ORDER BY played DESC""",
            [cid, cid, self.now.date()]).fetchall()
        if not rows:
            return []
        league, latest = rows[0][0], rows[0][1].isoformat()
        rows = [r for r in rows if r[0] == league]
        name, source = CLUB_BY_ID[cid].name, f"football-data.co.uk {league} results"
        results, xg_for, xg_against, have_xg = [], 0.0, 0.0, False
        for _, _, home, hg, ag, hxg, axg in rows[:5]:
            mine, theirs = (hg, ag) if home == cid else (ag, hg)
            results.append("W" if mine > theirs else "L" if mine < theirs else "D")
            if hxg is not None and axg is not None:
                have_xg = True
                xg_for += hxg if home == cid else axg
                xg_against += axg if home == cid else hxg
        out = [_stat(f"{name} last {len(results)} league games", "-".join(results), source, latest)]
        pos = self._position(con, league, cid)
        if pos:
            out.append(_stat(f"{name} {league} position", pos, source, latest))
        if have_xg:
            out.append(_stat(f"{name} xG difference, last {len(results)} league games",
                             round(xg_for - xg_against, 2), source, latest))
        return out

    def _position(self, con: duckdb.DuckDBPyConnection, league: str, cid: str) -> int | None:
        table: dict[str, list[int]] = {}
        for home, away, hg, ag in con.execute("SELECT home, away, home_goals, away_goals FROM matches WHERE league = ? "
                                              "AND played <= ?", [league, self.now.date()]).fetchall():
            for club, gf, ga in ((home, hg, ag), (away, ag, hg)):
                t = table.setdefault(club, [0, 0, 0])
                t[0] += 3 if gf > ga else 1 if gf == ga else 0
                t[1] += gf - ga
                t[2] += gf
        order = sorted(table, key=lambda c: (table[c][0], table[c][1], table[c][2]), reverse=True)
        return order.index(cid) + 1 if cid in table and len(table) == LEAGUE_SIZE.get(league) else None

    def _player_stats(self, con: duckdb.DuckDBPyConnection, pid: str) -> list[dict[str, Any]]:
        row = con.execute("SELECT source, minutes, goals, assists, xg, xa, as_of FROM player_seasons "
                          "WHERE player_id = ? AND as_of <= ?", [pid, self.now.date().isoformat()]).fetchone()
        e = self.index.players.get(pid)
        if not row or not e:
            return []
        source, minutes, goals, assists, xg, xa, as_of = row
        out = [_stat(f"{e.name} minutes this season", minutes, source, as_of),
               _stat(f"{e.name} goals this season", goals, source, as_of),
               _stat(f"{e.name} assists this season", assists, source, as_of)]
        if xg is not None:
            out.append(_stat(f"{e.name} xG this season", round(xg, 2), source, as_of))
        if xa is not None:
            out.append(_stat(f"{e.name} xA this season", round(xa, 2), source, as_of))
        return out

    def enrich(self, players: list[str], clubs: list[str]) -> dict[str, Any]:
        empty = {"analytic_coverage": 0.0, "analytic_note": "no soccer stats for these entities", "players": [], "stats": []}
        if not self.db_path.exists():
            return empty
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            stats = [s for pid in players[:2] for s in self._player_stats(con, pid)]
            stats += [s for cid in clubs[:2] if cid in CLUB_BY_ID for s in self._club_stats(con, cid)]
        finally:
            con.close()
        if not stats:
            return empty
        return {"analytic_coverage": 1.0 if players and len(stats) > 3 else 0.6,
                "analytic_note": f"{len(stats)} sourced stats", "players": [], "stats": stats}

    def next_kickoff(self) -> tuple[str, datetime] | None:
        """The next Premier League kickoff, or the next in any covered league."""
        if not self.db_path.exists():
            return None
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            row = con.execute(
                """SELECT league, kickoff FROM fixtures WHERE kickoff > ?
                   ORDER BY (league = 'Premier League') DESC, kickoff LIMIT 1""", [self.now]).fetchone()
        finally:
            con.close()
        if not row:
            return None
        kickoff = row[1].astimezone(timezone.utc)
        return row[0], kickoff


