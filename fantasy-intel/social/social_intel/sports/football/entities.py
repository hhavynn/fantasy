"""NFL player and team recognition in short social text.

Built from the analytics database (current-season weekly rosters, ESPN roster %, season usage) and
cached as JSON so annotation needs no database at all. Matching is conservative, in the spirit of
the backend's ``mapping.py``: a name only resolves when it is unambiguous.

* Full names ("Jaxson Dart", "Amon-Ra St. Brown") always match, suffixes and accents ignored.
* A single token (a surname "Nabers", a first name "Saquon") matches only when exactly one
  fantasy-relevant player carries it and it is not an ordinary English word ("Love", "Chase").
* Curated fan nicknames ("CMC", "JSN", "ARSB") resolve through the same index.
* An ambiguous surname ("Allen") resolves only if the post also names that player's team.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import Any

from social_intel.sports.base import Entity

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "FB"}

# nflverse abbreviation -> (nickname, city/region words). "LA" is the Rams in nflverse.
TEAMS: dict[str, tuple[str, list[str]]] = {
    "ARI": ("Cardinals", ["arizona"]), "ATL": ("Falcons", ["atlanta"]), "BAL": ("Ravens", ["baltimore"]),
    "BUF": ("Bills", ["buffalo"]), "CAR": ("Panthers", ["carolina"]), "CHI": ("Bears", ["chicago"]),
    "CIN": ("Bengals", ["cincinnati"]), "CLE": ("Browns", ["cleveland"]), "DAL": ("Cowboys", ["dallas"]),
    "DEN": ("Broncos", ["denver"]), "DET": ("Lions", ["detroit"]), "GB": ("Packers", ["green bay"]),
    "HOU": ("Texans", ["houston"]), "IND": ("Colts", ["indianapolis", "indy"]),
    "JAX": ("Jaguars", ["jacksonville", "jags"]), "KC": ("Chiefs", ["kansas city"]),
    "LV": ("Raiders", ["las vegas"]), "LAC": ("Chargers", []), "LA": ("Rams", []),
    "MIA": ("Dolphins", ["miami", "fins"]), "MIN": ("Vikings", ["minnesota", "vikes"]),
    "NE": ("Patriots", ["new england", "pats"]), "NO": ("Saints", ["new orleans"]),
    "NYG": ("Giants", []), "NYJ": ("Jets", []), "PHI": ("Eagles", ["philadelphia", "philly"]),
    "PIT": ("Steelers", ["pittsburgh"]), "SF": ("49ers", ["niners", "san francisco"]),
    "SEA": ("Seahawks", ["seattle"]), "TB": ("Buccaneers", ["tampa bay", "bucs", "tampa"]),
    "TEN": ("Titans", ["tennessee"]), "WAS": ("Commanders", ["washington"]),
}
# Abbreviations that are also everyday words/initialisms are never matched on their own.
_AMBIGUOUS_ABBR = {"NO", "LA", "NE", "SF", "GB", "KC", "LV", "TB", "DAL", "MIN", "DEN", "CAR", "ARI", "SEA",
                   "CHI", "IND", "TEN", "WAS", "PIT", "MIA", "HOU", "DET", "BAL", "ATL", "CIN", "CLE", "BUF",
                   "PHI", "JAX"}
_ALT_ABBR = {"LAR": "LA", "WSH": "WAS", "JAC": "JAX", "LVR": "LV", "NYJ": "NYJ", "NYG": "NYG", "LAC": "LAC"}

NICKNAMES = {
    "cmc": "christian mccaffrey", "arsb": "amon ra st brown", "amon ra": "amon ra st brown",
    "jsn": "jaxon smith njigba", "mhj": "marvin harrison", "btj": "brian thomas", "kwiii": "kenneth walker",
    "jjm": "j j mccarthy", "jj mccarthy": "j j mccarthy", "tmac": "terry mclaurin", "dk": "dk metcalf",
    "aj brown": "a j brown", "cj stroud": "c j stroud", "tj hockenson": "t j hockenson",
    "jk dobbins": "j k dobbins", "dj moore": "d j moore", "the hamburglar": "mike gesicki",
    "bijan": "bijan robinson", "saquon": "saquon barkley", "puka": "puka nacua", "jamarr": "jamarr chase",
    "lamar": "lamar jackson", "mahomes": "patrick mahomes", "jefferson": "justin jefferson",
}

# Ordinary English words that are also player names: never matched alone, only as a full name.
ENGLISH_WORDS = set("""
love chase swift hunt cook white rice ward young bell hall hill ford gay brown green black gray grey king price
long little day will may mike pierce hope worthy god london fields golden flowers cross stone reed
all likely burden strange lock loop case justice lemon mark jack drew woody boston drake kraft noel
""".split())
# Very common surnames / first names: matched alone only when the post also names that player's team.
COMMON_NAMES = set("""
moore wilson jones smith williams johnson davis miller allen walker jackson harris robinson thomas taylor lewis
lee martin scott adams baker carter cooper hunter mason parker rogers ross russell thompson wright evans collins
kelly kirk bates sanders warren jennings washington george brooks bryant bush wells tracy mitchell murray
henry tucker hurst palmer
josh justin jalen james chris david michael tyler kyle jordan cam aj dj tj jj cj
""".split())
COMMON_WORDS = ENGLISH_WORDS | COMMON_NAMES


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    s = s.replace("’", "'").replace("'s ", " ").replace("'", "").replace(".", "").replace("-", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in _SUFFIXES)


class EntityIndex:
    def __init__(self, players: list[dict[str, Any]], season: int | None = None, as_of_week: int | None = None):
        self.season = season
        self.as_of_week = as_of_week
        self.players: dict[str, Entity] = {}
        self.full: dict[str, list[str]] = {}
        self.single: dict[str, list[str]] = {}
        for p in players:
            e = Entity(p["id"], "player", p["name"], p.get("team"), p.get("position"), float(p.get("popularity", 0)),
                       meta={k: v for k, v in p.items() if k not in {"id", "name", "team", "position", "popularity"}})
            self.players[e.entity_id] = e
            n = normalize(e.name)
            if not n:
                continue
            self.full.setdefault(n, []).append(e.entity_id)
            parts = n.split()
            relevant = e.popularity >= 0.05 and e.position in FANTASY_POSITIONS
            if relevant and len(parts) >= 2:
                self.single.setdefault(parts[-1], []).append(e.entity_id)
                self.single.setdefault("first:" + parts[0], []).append(e.entity_id)
        for nick, full in NICKNAMES.items():
            ids = self.full.get(full) or self.full.get(normalize(full))
            if ids:
                self.full.setdefault(nick, list(ids))
        self.teams: dict[str, str] = {}
        for abbr, (nick, cities) in TEAMS.items():
            self.teams[nick.lower()] = abbr
            for c in cities:
                self.teams[c] = abbr
        self.max_ngram = max((len(k.split()) for k in self.full), default=3)

    # ------------------------------------------------------------------ build / persist

    @classmethod
    def from_db(cls, con, season: int) -> "EntityIndex":
        week = con.execute("SELECT max(week) FROM nfl_rosters_weekly WHERE season = ?", [season]).fetchone()[0]
        rows = con.execute(
            """
            WITH r AS (
                SELECT gsis_id, arg_max(full_name, week) AS full_name, arg_max(team, week) AS team,
                       arg_max(position, week) AS position, arg_max(status, week) AS status, arg_max(espn_id, week) AS espn_id,
                       arg_max(sleeper_id, week) AS sleeper_id
                FROM nfl_rosters_weekly WHERE season = ? AND gsis_id IS NOT NULL GROUP BY gsis_id
            ),
            u AS (
                SELECT gsis_id, avg(fantasy_points_ppr) AS ppg, avg(xfp) AS xfp, count(*) AS games
                FROM player_week_usage WHERE season = ? GROUP BY gsis_id
            ),
            own AS (
                SELECT m.gsis_id, max(f.percent_owned) AS owned
                FROM fantasy_players f JOIN player_id_map m ON m.espn_player_id = f.espn_player_id GROUP BY 1
            )
            SELECT r.gsis_id, r.full_name, r.team, r.position, r.status, r.espn_id, u.ppg, u.xfp, u.games, own.owned, r.sleeper_id
            FROM r LEFT JOIN u USING (gsis_id) LEFT JOIN own USING (gsis_id)
            WHERE r.status IN ('ACT', 'RES', 'INA', 'DEV') AND r.position IN ('QB','RB','WR','TE','K','FB')
            """, [season, season]).fetchall()
        ppgs = sorted(r[6] for r in rows if r[6] is not None)

        def pct(v):
            if v is None or not ppgs:
                return 0.0
            import bisect
            return bisect.bisect_left(ppgs, v) / len(ppgs)

        players = []
        for gid, name, team, pos, status, espn_id, ppg, _xfp, games, owned, sleeper_id in rows:
            pop = max((owned or 0) / 100.0, pct(ppg) * (0.9 if (games or 0) >= 2 else 0.5))
            players.append({"id": gid, "name": name, "team": team, "position": pos, "popularity": round(pop, 3),
                            "status": status, "espn_id": espn_id, "ppg": ppg, "owned": owned,
                            "sleeper_id": str(sleeper_id) if sleeper_id else None})
        return cls(players, season, week)

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"id": e.entity_id, "name": e.name, "team": e.team, "position": e.position,
                 "popularity": e.popularity, **e.meta} for e in self.players.values()]
        path.write_text(json.dumps({"season": self.season, "as_of_week": self.as_of_week, "players": rows}))

    @classmethod
    def load(cls, path: Path) -> "EntityIndex":
        d = json.loads(path.read_text())
        return cls(d["players"], d.get("season"), d.get("as_of_week"))

    # ------------------------------------------------------------------ matching

    def find_teams(self, text: str, norm: str | None = None) -> list[str]:
        norm = norm if norm is not None else normalize(text)
        found: list[str] = []
        padded = f" {norm} "
        for key, abbr in self.teams.items():
            if f" {key} " in padded and abbr not in found:
                found.append(abbr)
        for tok in re.findall(r"\b[A-Z]{2,3}\b", text):
            abbr = _ALT_ABBR.get(tok, tok)
            if abbr in TEAMS and abbr not in _AMBIGUOUS_ABBR and abbr not in found:
                found.append(abbr)
        return found

    def find_players(self, text: str, teams: list[str] | None = None) -> list[str]:
        norm = normalize(text)
        toks = norm.split()
        teams = teams if teams is not None else self.find_teams(text, norm)
        found: list[tuple[int, str]] = []
        used: set[int] = set()

        def add(pos: int, pid: str) -> None:
            if pid not in (f[1] for f in found):
                found.append((pos, pid))

        # 1) full names / nicknames, longest n-gram first
        for n in range(min(self.max_ngram, 4), 1, -1):
            for i in range(len(toks) - n + 1):
                if any(j in used for j in range(i, i + n)):
                    continue
                ids = self.full.get(" ".join(toks[i:i + n]))
                if ids:
                    pick = self._disambiguate(ids, teams)
                    if pick:
                        add(i, pick)
                        used.update(range(i, i + n))
        # 2) single distinctive tokens (surname, first name, short nickname)
        orig_caps = {normalize(w) for w in re.findall(r"\b[A-Z][\w'’.-]+", text)}
        for i, t in enumerate(toks):
            if i in used or len(t) < 2:
                continue
            ids = self.full.get(t)  # one-word nicknames ("cmc", "puka")
            if ids:
                pick = self._disambiguate(ids, teams)
                if pick:
                    add(i, pick)
                continue
            if t in ENGLISH_WORDS or len(t) < 3 or (t not in orig_caps and t.upper() not in text):
                continue
            cands = self.single.get(t, []) or self.single.get("first:" + t, [])
            if not cands:
                continue
            if t in COMMON_NAMES:  # "Allen" means nothing without "Jets"/"Bills" nearby
                on_team = list(dict.fromkeys(c for c in cands if self.players[c].team in teams))
                pick = on_team[0] if len(on_team) == 1 else None
            else:
                pick = self._disambiguate(cands, teams, strict=True)
            if pick:
                add(i, pick)
        return [pid for _, pid in sorted(found)]

    def _disambiguate(self, ids: list[str], teams: list[str], strict: bool = False) -> str | None:
        ids = list(dict.fromkeys(ids))
        if len(ids) == 1:
            return ids[0]
        if teams:
            on_team = [i for i in ids if self.players[i].team in teams]
            if len(on_team) == 1:
                return on_team[0]
        if strict:
            return None
        # same full name twice (e.g. two "Josh Allen"s): take the clearly more relevant one
        ranked = sorted(ids, key=lambda i: self.players[i].popularity, reverse=True)
        top, second = self.players[ranked[0]].popularity, self.players[ranked[1]].popularity
        return ranked[0] if top >= 0.2 and top >= 2 * second else None

    def name_tokens(self, entity_id: str) -> set[str]:
        e = self.players.get(entity_id)
        if e:
            return set(normalize(e.name).split())
        if entity_id in TEAMS:
            nick, cities = TEAMS[entity_id]
            return {nick.lower(), entity_id.lower(), *" ".join(cities).split()}
        return set()

    def entity(self, entity_id: str) -> Entity | None:
        if entity_id in self.players:
            return self.players[entity_id]
        if entity_id in TEAMS:
            return Entity(entity_id, "team", TEAMS[entity_id][0], entity_id, None, 0.5)
        return None

    def to_rows(self) -> list[dict[str, Any]]:
        return [asdict(e) for e in self.players.values()]
