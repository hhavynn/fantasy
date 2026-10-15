"""Sleeper player id -> nflverse gsis_id.

Evidence, first hit wins:
  1. DynastyProcess ID map (`nflreadpy.load_ff_playerids()`: sleeper_id + gsis_id) -> "high".
     If its rows for one sleeper_id disagree on gsis_id, the player is reported as a conflict,
     never resolved by guessing.
  2. The gsis_id Sleeper itself publishes on the player record -> "medium" (used only when the
     ID map has no gsis for that player; a disagreement with step 1 is noted, step 1 wins).
Team defenses (Sleeper ids like "DET") map to an NFL team, not a player. Everything else is
returned as unmatched with a reason; nothing is silently dropped. No name matching.

nflreadpy reads the ID map CSV with type inference, so `sleeper_id` arrives as an Int64
column; it is cast to a string column before use (Sleeper player ids are digit strings with no
leading zeros, so the round trip is exact).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .constants import nflverse_team
from .models import SleeperPlayer, sid

log = logging.getLogger("sleeper.id_map")

ID_MAP_FILE = "ff_playerids.json"
ID_MAP_MAX_AGE = 24 * 3600.0


@dataclass(frozen=True)
class IdMapEntry:
    gsis_id: str | None
    name: str | None
    position: str | None
    team: str | None          # nflverse abbreviation


@dataclass
class SleeperIdMap:
    by_sleeper: dict[str, list[IdMapEntry]] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> "SleeperIdMap":
        m = cls()
        for r in rows:
            key = sid(r.get("sleeper_id"))
            if key is None:
                continue
            m.by_sleeper.setdefault(key, []).append(IdMapEntry(
                gsis_id=sid(r.get("gsis_id")), name=r.get("name"), position=r.get("position"),
                team=nflverse_team(r.get("team"))))
        return m

    @classmethod
    def from_frame(cls, df) -> "SleeperIdMap":
        import polars as pl

        cols = [c for c in ("sleeper_id", "gsis_id", "name", "position", "team") if c in df.columns]
        slim = df.select(cols).with_columns(pl.col("sleeper_id").cast(pl.Utf8), pl.col("gsis_id").cast(pl.Utf8))
        return cls.from_rows(slim.to_dicts())

    def __len__(self) -> int:
        return len(self.by_sleeper)


@dataclass(frozen=True)
class PlayerMapping:
    sleeper_id: str
    name: str | None
    position: str | None
    team: str | None              # nflverse abbreviation
    gsis_id: str | None
    nfl_team: str | None          # team defenses only
    method: str                   # ff_playerids | sleeper_gsis | team_defense | conflict | unmatched | unknown_player
    confidence: str               # high | medium | n/a | none
    notes: str | None = None

    @property
    def matched(self) -> bool:
        return self.method in ("ff_playerids", "sleeper_gsis", "team_defense")


@dataclass
class MappingReport:
    mappings: dict[str, PlayerMapping]

    @property
    def unmatched(self) -> list[PlayerMapping]:
        return [m for m in self.mappings.values() if not m.matched]

    @property
    def by_method(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.mappings.values():
            out[m.method] = out.get(m.method, 0) + 1
        return out

    @property
    def match_rate(self) -> float | None:
        n = len(self.mappings)
        return None if n == 0 else sum(m.matched for m in self.mappings.values()) / n

    def gsis_ids(self) -> dict[str, str]:
        return {k: m.gsis_id for k, m in self.mappings.items() if m.gsis_id}


def map_player(sleeper_id: str, player: SleeperPlayer | None, idmap: SleeperIdMap) -> PlayerMapping:
    if player is None:
        return PlayerMapping(sleeper_id, None, None, None, None, None, "unknown_player", "none",
                             "Not in Sleeper's /players/nfl database")
    base = dict(sleeper_id=sleeper_id, name=player.full_name, position=player.position,
                team=nflverse_team(player.team))
    if player.is_team_defense:
        team = nflverse_team(player.team or sleeper_id)
        return PlayerMapping(**base, gsis_id=None, nfl_team=team, method="team_defense", confidence="n/a",
                             notes="Team defense: mapped to NFL team, no player id")

    entries = idmap.by_sleeper.get(sleeper_id, [])
    gsis = sorted({e.gsis_id for e in entries if e.gsis_id})
    if len(gsis) > 1:
        return PlayerMapping(**base, gsis_id=None, nfl_team=None, method="conflict", confidence="none",
                             notes="ID map rows disagree: " + ", ".join(gsis))
    if len(gsis) == 1:
        notes = None
        if player.gsis_id and player.gsis_id != gsis[0]:
            notes = f"Sleeper's own gsis_id {player.gsis_id} differs; ID map value used"
        return PlayerMapping(**base, gsis_id=gsis[0], nfl_team=None, method="ff_playerids", confidence="high",
                             notes=notes)
    if player.gsis_id:
        return PlayerMapping(**base, gsis_id=player.gsis_id, nfl_team=None, method="sleeper_gsis",
                             confidence="medium", notes="Not in the ID map with a gsis_id; Sleeper's own gsis_id used")
    reason = "In the ID map without a gsis_id" if entries else "Not in the DynastyProcess ID map"
    return PlayerMapping(**base, gsis_id=None, nfl_team=None, method="unmatched", confidence="none",
                         notes=reason + "; Sleeper has no gsis_id either")


def map_players(player_ids: Iterable[str], players: Mapping[str, SleeperPlayer], idmap: SleeperIdMap,
                *, log_unmatched: bool = True) -> MappingReport:
    report = MappingReport({pid: map_player(pid, players.get(pid), idmap) for pid in dict.fromkeys(player_ids)})
    if log_unmatched:
        for m in report.unmatched:
            log.warning("UNMATCHED sleeper %s: %s (%s %s): %s", m.sleeper_id, m.name, m.position, m.team, m.notes)
    return report


def load_id_map(cache_dir: Path | None = None, *, max_age: float = ID_MAP_MAX_AGE,
                loader: Callable[[], Any] | None = None, clock: Callable[[], float] = time.time) -> SleeperIdMap:
    """The DynastyProcess ID map, cached on disk (slim rows) for `max_age` seconds.
    `loader` returns a polars DataFrame; default nflreadpy.load_ff_playerids."""
    if cache_dir is None:
        from ..config import DATA_DIR
        cache_dir = DATA_DIR / "sleeper"
    path = Path(cache_dir) / ID_MAP_FILE
    now = clock()
    try:
        doc = json.loads(path.read_text())
        if 0 <= now - float(doc["fetched_at"]) < max_age:
            return SleeperIdMap.from_rows(doc["rows"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if loader is None:
        import nflreadpy

        loader = nflreadpy.load_ff_playerids
    idmap = SleeperIdMap.from_frame(loader())
    rows = [{"sleeper_id": k, "gsis_id": e.gsis_id, "name": e.name, "position": e.position, "team": e.team}
            for k, entries in idmap.by_sleeper.items() for e in entries]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"fetched_at": now, "rows": rows}))
    os.replace(tmp, path)
    return idmap
