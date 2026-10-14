"""ESPN player id -> nflverse gsis_id mapping.

Order of evidence (first hit wins, conflicts are noted):
  1. espn_id crosswalk in nflverse weekly rosters (current season)
  2. espn_id crosswalk in nflverse players table
  3. espn_id crosswalk in ffverse ff_playerids
  4. normalised name + compatible position (+ team when needed), only if the
     candidate is unique -> confidence "medium"
D/ST entries map to a team (no gsis id). Everything else is recorded as
"unmatched" -- never silently dropped.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from ..db.connection import fetch_dicts, table_exists, utcnow

log = logging.getLogger("mapping")

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

POSITION_COMPAT = {
    "QB": {"QB"},
    "RB": {"RB", "FB", "HB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "K": {"K", "PK"},
    "P": {"P"},
}


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", "", s.lower().replace("-", " "))
    parts = [p for p in s.split() if p not in _SUFFIXES]
    return " ".join(parts)


@dataclass
class NflCandidate:
    gsis_id: str
    name: str
    position: str | None
    team: str | None


@dataclass
class NflIndex:
    by_espn: dict[int, list[tuple[str, str]]] = field(default_factory=dict)  # espn -> [(gsis, source)]
    by_name: dict[str, list[NflCandidate]] = field(default_factory=dict)
    info: dict[str, NflCandidate] = field(default_factory=dict)

    def add_crosswalk(self, espn_id, gsis_id, source: str) -> None:
        try:
            espn_id = int(espn_id)
        except (TypeError, ValueError):
            return
        if not gsis_id:
            return
        hits = self.by_espn.setdefault(espn_id, [])
        if all(g != gsis_id or s != source for g, s in hits):
            hits.append((gsis_id, source))

    def add_candidate(self, c: NflCandidate) -> None:
        if c.gsis_id in self.info:
            return
        self.info[c.gsis_id] = c
        self.by_name.setdefault(normalize_name(c.name), []).append(c)


def map_player(p: dict, idx: NflIndex) -> dict:
    """Map one ESPN player row (espn_player_id, full_name, position, pro_team)."""
    pid = p["espn_player_id"]
    base = {
        "espn_player_id": pid,
        "espn_name": p.get("full_name"),
        "espn_position": p.get("position"),
        "espn_team": p.get("pro_team"),
        "gsis_id": None,
        "nfl_name": None,
        "nfl_position": None,
        "nfl_team": None,
        "notes": None,
    }
    if p.get("position") == "D/ST" or (isinstance(pid, int) and pid < 0):
        return {**base, "match_method": "team_defense", "match_confidence": "n/a",
                "nfl_team": p.get("pro_team"), "notes": "Team defense: mapped to NFL team, no player id."}

    hits = idx.by_espn.get(pid, [])
    if hits:
        gsis, source = hits[0]
        distinct = {g for g, _ in hits}
        notes = None
        if len(distinct) > 1:
            notes = "Conflicting crosswalks: " + ", ".join(f"{g} ({s})" for g, s in hits)
        c = idx.info.get(gsis)
        return {**base, "gsis_id": gsis, "match_method": f"espn_id:{source}", "match_confidence": "high",
                "nfl_name": c.name if c else None, "nfl_position": c.position if c else None,
                "nfl_team": c.team if c else None, "notes": notes}

    key = normalize_name(p.get("full_name"))
    compat = POSITION_COMPAT.get(p.get("position") or "", set())
    cands = [c for c in idx.by_name.get(key, []) if c.position in compat] if key else []
    if len(cands) > 1 and p.get("pro_team"):
        cands = [c for c in cands if c.team == p.get("pro_team")]
    if len(cands) == 1:
        c = cands[0]
        team_note = "" if c.team == p.get("pro_team") else f" (team differs: ESPN {p.get('pro_team')} vs nflverse {c.team})"
        return {**base, "gsis_id": c.gsis_id, "match_method": "name_position_team", "match_confidence": "medium",
                "nfl_name": c.name, "nfl_position": c.position, "nfl_team": c.team,
                "notes": "Matched on normalised name + position" + team_note}
    note = "No espn_id crosswalk and no name/position match" if not cands else \
        f"Ambiguous name match ({len(cands)} candidates); refusing to guess"
    return {**base, "match_method": "unmatched", "match_confidence": "none", "notes": note}


def build_index(con, season: int) -> NflIndex:
    idx = NflIndex()
    if table_exists(con, "nfl_rosters_weekly"):
        rows = fetch_dicts(con, """
            SELECT gsis_id, espn_id, full_name, position, team FROM (
              SELECT *, row_number() OVER (PARTITION BY gsis_id ORDER BY week DESC) rn
              FROM nfl_rosters_weekly WHERE season = ? AND gsis_id IS NOT NULL) WHERE rn = 1
        """, [season])
        for r in rows:
            idx.add_candidate(NflCandidate(r["gsis_id"], r["full_name"], r["position"], r["team"]))
            idx.add_crosswalk(r["espn_id"], r["gsis_id"], "rosters_weekly")
    for r in fetch_dicts(con, "SELECT gsis_id, espn_id, display_name, position, latest_team FROM nfl_players"):
        idx.add_candidate(NflCandidate(r["gsis_id"], r["display_name"], r["position"], r["latest_team"]))
        idx.add_crosswalk(r["espn_id"], r["gsis_id"], "nfl_players")
    if table_exists(con, "nfl_ff_playerids"):
        for r in fetch_dicts(con, "SELECT gsis_id, espn_id FROM nfl_ff_playerids WHERE espn_id IS NOT NULL"):
            idx.add_crosswalk(r["espn_id"], r["gsis_id"], "ff_playerids")
    return idx


def rebuild_id_map(con, season: int) -> dict:
    from ..db.connection import replace_where

    players = fetch_dicts(con, """
        SELECT espn_player_id, full_name, position, pro_team, availability_status, percent_owned
        FROM fantasy_players WHERE position IN ('QB','RB','WR','TE','K','D/ST')
    """)
    idx = build_index(con, season)
    now = utcnow()
    rows = []
    for p in players:
        row = map_player(p, idx)
        row["mapped_at"] = now
        rows.append(row)
    replace_where(con, "player_id_map", rows, "true")

    by_method: dict[str, int] = {}
    for r in rows:
        by_method[r["match_method"]] = by_method.get(r["match_method"], 0) + 1
    relevant_unmatched = [
        (p, r) for p, r in zip(players, rows)
        if r["match_method"] == "unmatched"
        and (p.get("availability_status") == "ONTEAM" or (p.get("percent_owned") or 0) >= 1)
    ]
    for p, r in relevant_unmatched:
        log.warning("UNMATCHED: %s (%s %s, espn %s, %.1f%% owned): %s", p["full_name"], p["position"],
                    p["pro_team"], p["espn_player_id"], p.get("percent_owned") or 0, r["notes"])
    return {"mapped_by_method": by_method, "relevant_unmatched": len(relevant_unmatched)}
