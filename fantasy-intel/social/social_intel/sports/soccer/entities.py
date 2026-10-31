"""Soccer club and player recognition in short social text.

Clubs come from ``clubs.py``. Players come from the cache written by ``sync_entities`` plus a short list
of stars who play outside the covered leagues. Matching follows the football index: a name resolves only
when it is unambiguous, and a common surname needs the player's club in the same post.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from social_intel.sports.base import Entity
from social_intel.sports.football.entities import normalize
from social_intel.sports.soccer.clubs import CLUB_BY_ID, CLUBS
from social_intel.sports.soccer.topics import SOCCER_CONTEXT

# Always known, whatever the sync finds: the names the audience argues about wherever they play.
STARS: tuple[dict[str, Any], ...] = (
    {"id": "star:cristiano-ronaldo", "name": "Cristiano Ronaldo", "popularity": 1.0},
    {"id": "star:lionel-messi", "name": "Lionel Messi", "team": "inter-miami", "popularity": 1.0},
    {"id": "star:neymar", "name": "Neymar", "popularity": 0.9},
)

NICKNAMES = {
    "cr7": "cristiano ronaldo", "ronaldo": "cristiano ronaldo", "messi": "lionel messi", "leo messi": "lionel messi",
    "mbappe": "kylian mbappe", "vini": "vinicius junior", "vini jr": "vinicius junior", "vinicius": "vinicius junior",
    "yamal": "lamine yamal", "lamine": "lamine yamal", "haaland": "erling haaland", "bellingham": "jude bellingham",
    "kdb": "kevin de bruyne", "trent": "trent alexander arnold", "taa": "trent alexander arnold",
    "salah": "mohamed salah", "mo salah": "mohamed salah", "kane": "harry kane",
}

# Everyday words that are also surnames: only ever matched as part of a full name.
ENGLISH_WORDS = set("""
rice white stones walker gray grey young ward bell cash king long day will may mark jack rose reed green hall
hill wood james ball cook fox hope silva jesus rodri son kim lee park
""".split())
# Common across squads: alone, they need the player's club in the post.
COMMON_NAMES = set("""
fernandes fernandez martinez garcia rodriguez lopez gonzalez sanchez perez gomez hernandez diaz alvarez ruiz
santos costa pereira oliveira souza mendes sousa muller meyer schmidt fischer weber wagner smith jones williams
johnson taylor brown davies wilson evans thomas roberts anderson alonso jackson robinson
""".split())
# Also first names, so a bare token usually means someone else ("Charles Leclerc", "coach George").
ENGLISH_WORDS |= set("charles lewis george john louis jose scott ferguson maxwell".split())
# Managers and pundits share surnames with players: their full names are consumed, never matched.
NOT_PLAYERS = (
    "xabi alonso", "jose mourinho", "pep guardiola", "mikel arteta", "hansi flick", "luis enrique", "arne slot",
    "michael carrick", "enzo maresca", "unai emery", "diego simeone", "vincent kompany", "thomas tuchel",
    "jamie carragher", "gary neville", "roy keane", "thierry henry", "alex ferguson",
)
SURNAME_MIN_POPULARITY = 0.35  # a fringe squad player's surname alone is more often someone else


class SoccerIndex:
    def __init__(self, players: list[dict[str, Any]] | None = None, as_of: str | None = None):
        self.as_of = as_of
        self.players: dict[str, Entity] = {}
        self.full: dict[str, list[str]] = {}
        self.single: dict[str, list[str]] = {}
        self.blocked = {normalize(n) for n in NOT_PLAYERS}
        self.first_names: set[str] = set()
        for p in [*(players or []), *STARS]:
            if p["id"] in self.players or (p["id"].startswith("star:") and normalize(p["name"]) in self.full):
                continue
            e = Entity(p["id"], "player", p["name"], p.get("team"), p.get("position"), float(p.get("popularity", 0)))
            self.players[e.entity_id] = e
            n = normalize(e.name)
            if not n:
                continue
            self.full.setdefault(n, []).append(e.entity_id)
            parts = n.split()
            self.first_names.add(parts[0])
            if len(parts) >= 2 and e.popularity >= SURNAME_MIN_POPULARITY:
                self.single.setdefault(parts[-1], []).append(e.entity_id)
            for short in p.get("aliases") or []:
                self.full.setdefault(normalize(short), []).append(e.entity_id)
        for nick, full in NICKNAMES.items():
            ids = self.full.get(full)
            if ids:
                self.full.setdefault(nick, list(ids))
        self._always = sorted(((a, c.id) for c in CLUBS for a in c.aliases), key=lambda x: -len(x[0]))
        self._context = sorted(((a, c.id) for c in CLUBS for a in c.context_aliases), key=lambda x: -len(x[0]))

    # ------------------------------------------------------------------ persist

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"id": e.entity_id, "name": e.name, "team": e.team, "position": e.position, "popularity": e.popularity}
                for e in self.players.values()]
        path.write_text(json.dumps({"as_of": self.as_of, "players": rows}))

    @classmethod
    def load(cls, path: Path) -> "SoccerIndex":
        d = json.loads(path.read_text())
        return cls(d["players"], d.get("as_of"))

    # ------------------------------------------------------------------ matching

    def find_clubs(self, text: str, trusted: bool = False) -> list[str]:
        """Longest alias first, each span consumed, so "Inter Miami" never also counts as "Inter"."""
        found: list[tuple[int, str]] = []
        padded = f" {normalize(text)} "

        def take(alias: str, cid: str) -> None:
            nonlocal padded
            needle = f" {alias} "
            at = padded.find(needle)
            if at < 0:
                return
            if cid not in (f[1] for f in found):
                found.append((at, cid))
            padded = padded.replace(needle, " # ")

        for alias, cid in self._always:
            take(alias, cid)
        if trusted or SOCCER_CONTEXT.search(text):
            caps = {normalize(w) for w in re.findall(r"\b[A-Z][\w'’.-]+", text)}
            for alias, cid in self._context:
                if alias in caps:
                    take(alias, cid)
        return [cid for _, cid in sorted(found)]

    def find_players(self, text: str, clubs: list[str] | None = None) -> list[str]:
        toks = normalize(text).split()
        clubs = clubs or []
        found: list[tuple[int, str]] = []
        used: set[int] = set()

        def add(pos: int, pid: str) -> None:
            if pid not in (f[1] for f in found):
                found.append((pos, pid))

        for n in (4, 3, 2):
            for i in range(len(toks) - n + 1):
                if any(j in used for j in range(i, i + n)):
                    continue
                gram = " ".join(toks[i:i + n])
                if gram in self.blocked:
                    used.update(range(i, i + n))
                    continue
                pick = self._pick(self.full.get(gram, []), clubs)
                if pick:
                    add(i, pick)
                    used.update(range(i, i + n))
        caps = {normalize(w) for w in re.findall(r"\b[A-Z][\w'’.-]+", text)}
        for i, t in enumerate(toks):
            if i in used or len(t) < 3 or t in ENGLISH_WORDS or t not in caps:
                continue
            pick = self._pick(self.full.get(t, []), clubs)
            if not pick:
                cands = self.single.get(t, [])
                if t in COMMON_NAMES or t in self.first_names:  # "Martin" is Raya Martin and Martin Odegaard
                    cands = [c for c in cands if self.players[c].team in clubs]
                pick = self._pick(cands, clubs, strict=True)
            if pick:
                add(i, pick)
        return [pid for _, pid in sorted(found)]

    def _pick(self, ids: list[str], clubs: list[str], strict: bool = False) -> str | None:
        ids = list(dict.fromkeys(ids))
        if len(ids) == 1:
            return ids[0]
        on_club = [i for i in ids if self.players[i].team in clubs]
        if len(on_club) == 1:
            return on_club[0]
        if strict or not ids:
            return None
        ranked = sorted(ids, key=lambda i: self.players[i].popularity, reverse=True)
        top, second = self.players[ranked[0]].popularity, self.players[ranked[1]].popularity
        return ranked[0] if top >= 0.2 and top >= 2 * second else None

    # ------------------------------------------------------------------ lookups

    def name_tokens(self, entity_id: str) -> set[str]:
        if e := self.players.get(entity_id):
            return set(normalize(e.name).split())
        if c := CLUB_BY_ID.get(entity_id):
            return set(" ".join((normalize(c.name), *c.aliases, *c.context_aliases)).split())
        return set()

    def entity(self, entity_id: str) -> Entity | None:
        if entity_id in self.players:
            return self.players[entity_id]
        if c := CLUB_BY_ID.get(entity_id):
            return Entity(c.id, "club", c.name, c.id, None, 0.5, meta={"league": c.league})
        return None
