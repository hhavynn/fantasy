"""Identity resolution for external player-week rows -> nflverse gsis_id.

Priority (first decisive rule wins):
  1. gsis_id supplied in the file    -> must exist in the nflverse player universe
                                        (an invalid id is NOT re-guessed by name)
  2. external_player_map (reviewed)  -> provider + source_player_key -> gsis_id
  3. exact normalized name + team (as of that week) + compatible position, only if
     exactly one nflverse player matches -> confidence "medium"
Anything else is kept with gsis_id NULL and mapping_method "unmatched" or
"ambiguous" plus a reason. Rows are never dropped.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..db.connection import fetch_dicts, table_exists
from ..nfl.mapping import POSITION_COMPAT, normalize_name
from .contract import normalize_exact_name

# Generic NFL team-code aliases (not provider-specific).
TEAM_ALIASES = {"LAR": "LA", "WSH": "WAS", "JAC": "JAX", "LVR": "LV", "OAK": "LV", "SD": "LAC", "STL": "LA",
                "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU", "SL": "LA", "KAN": "KC", "GNB": "GB",
                "NOR": "NO", "NWE": "NE", "SFO": "SF", "TAM": "TB"}


def canonical_team(team: str | None) -> str | None:
    if not team:
        return None
    t = team.upper()
    return TEAM_ALIASES.get(t, t)


def _pos_ok(source_pos: str | None, nfl_pos: str | None) -> bool:
    if not source_pos or not nfl_pos:
        return False
    allowed = POSITION_COMPAT.get(source_pos, {source_pos})
    return nfl_pos in allowed


@dataclass
class IdentityIndex:
    valid_gsis: set[str] = field(default_factory=set)
    # (season, week) -> exact name -> [(gsis, team, position)]
    by_week: dict[tuple, dict[str, list]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(list)))
    # season -> exact name -> [(gsis, team, position)]  (latest team; used when a week has no roster data)
    by_season: dict[int, dict[str, list]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(list)))
    # loose (suffix-stripped) names, only to explain why a strict match failed
    loose_names: set[str] = field(default_factory=set)
    manual: dict[tuple, str] = field(default_factory=dict)   # (provider, source_player_key) -> gsis


def build_index(con, seasons: set[int]) -> IdentityIndex:
    idx = IdentityIndex()
    for r in fetch_dicts(con, "SELECT gsis_id, display_name, position, latest_team FROM nfl_players"):
        if r["gsis_id"]:
            idx.valid_gsis.add(r["gsis_id"])
            idx.loose_names.add(normalize_name(r["display_name"]))
    if table_exists(con, "nfl_rosters_weekly") and seasons:
        rows = fetch_dicts(con, f"""
            SELECT season, week, gsis_id, full_name, position, team FROM nfl_rosters_weekly
            WHERE gsis_id IS NOT NULL AND season IN ({",".join(str(int(s)) for s in seasons)})""")
        latest: dict[tuple, dict] = {}
        for r in rows:
            entry = (r["gsis_id"], r["team"], r["position"])
            idx.by_week[(r["season"], r["week"])][normalize_exact_name(r["full_name"])].append(entry)
            k = (r["season"], r["gsis_id"])
            if k not in latest or r["week"] > latest[k]["week"]:
                latest[k] = r
            idx.loose_names.add(normalize_name(r["full_name"]))
        for (season, _), r in latest.items():
            idx.by_season[season][normalize_exact_name(r["full_name"])].append((r["gsis_id"], r["team"], r["position"]))
    if table_exists(con, "external_player_map"):
        for r in fetch_dicts(con, "SELECT provider, source_player_key, gsis_id FROM external_player_map "
                                  "WHERE gsis_id IS NOT NULL"):
            idx.manual[(r["provider"], r["source_player_key"])] = r["gsis_id"]
    return idx


def resolve(row: dict, idx: IdentityIndex) -> dict:
    """-> {gsis_id, mapping_method, mapping_confidence, mapping_reason}"""
    def out(gsis, method, conf, reason=None):
        return {"gsis_id": gsis, "mapping_method": method, "mapping_confidence": conf, "mapping_reason": reason}

    if row.get("gsis_id"):
        if row["gsis_id"] in idx.valid_gsis:
            return out(row["gsis_id"], "gsis_id", "high")
        return out(None, "unmatched", "none", f"invalid_gsis_id: {row['gsis_id']} is not in the nflverse player universe")

    manual = idx.manual.get((row.get("provider"), row.get("source_player_key")))
    if manual:
        if manual in idx.valid_gsis:
            return out(manual, "external_player_map", "reviewed")
        return out(None, "unmatched", "none", f"external_player_map points to unknown gsis_id {manual}")

    name, team, pos = normalize_exact_name(row.get("player_name")), canonical_team(row.get("team")), row.get("position")
    if not (name and team and pos):
        return out(None, "unmatched", "none", "insufficient identity: need gsis_id, a reviewed map entry, "
                                              "or player_name + team + position")
    week_index = idx.by_week.get((row.get("season"), row.get("week")))
    pool = (week_index or idx.by_season.get(row.get("season"), {})).get(name, [])
    same_name_pos = [c for c in pool if _pos_ok(pos, c[2])]
    cands = [c for c in same_name_pos if c[1] == team]
    if len(cands) == 1:
        return out(cands[0][0], "name_team_position", "medium")
    if len(cands) > 1:
        return out(None, "ambiguous", "none", f"{len(cands)} nflverse players named '{row.get('player_name')}' "
                                              f"at {pos} on {team}; refusing to guess")
    if same_name_pos:
        teams = sorted({c[1] for c in same_name_pos})
        return out(None, "unmatched", "none", f"team_mismatch: nflverse has this name/position on {teams}, "
                                              f"file says {team} (stale team or trade?)")
    if normalize_name(row.get("player_name")) in idx.loose_names:
        return out(None, "unmatched", "none", "suffix_or_punctuation_mismatch: only a loose name match exists "
                                              "(e.g. Jr./II); add a reviewed mapping")
    return out(None, "unmatched", "none", "no_exact_match: no nflverse player with this exact name/team/position")
