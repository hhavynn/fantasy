"""ESPN fantasy football id maps (from espn-api / ESPN's v3 API)."""

# lineupSlotId -> label
SLOT_MAP: dict[int, str] = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE",
    7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S",
    14: "DB", 15: "DP", 16: "D/ST", 17: "K", 18: "P", 19: "HC", 20: "BE",
    21: "IR", 22: "", 23: "FLEX", 24: "ER", 25: "Rookie",
}
BENCH_SLOT = 20
IR_SLOT = 21
NON_STARTER_SLOTS = {BENCH_SLOT, IR_SLOT, 22}

# player.defaultPositionId -> position (different id space from lineup slots!)
DEFAULT_POSITION_MAP: dict[int, str] = {
    1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 7: "P", 9: "DT", 10: "DE",
    11: "LB", 12: "CB", 13: "S", 14: "HC", 16: "D/ST",
}

# proTeamId -> ESPN abbreviation
PRO_TEAM_MAP: dict[int, str] = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR",
    15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI",
    22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WSH",
    29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}

# ESPN abbreviation -> nflverse abbreviation (only the ones that differ)
ESPN_TO_NFLVERSE_TEAM = {"WSH": "WAS", "LAR": "LA"}


def nflverse_team(pro_team_id: int | None) -> str | None:
    if pro_team_id is None:
        return None
    abbr = PRO_TEAM_MAP.get(pro_team_id)
    if abbr is None or abbr == "FA":
        return None
    return ESPN_TO_NFLVERSE_TEAM.get(abbr, abbr)


# Activity feed message types
ACTIVITY_MAP = {
    178: "FA ADDED",
    180: "WAIVER ADDED",
    179: "DROPPED",
    181: "DROPPED",
    239: "DROPPED",
    244: "TRADED",
}

# mTransactions2 types we request (DRAFT comes from mDraftDetail instead).
TRANSACTION_TYPES = [
    "FREEAGENT", "WAIVER", "WAIVER_ERROR", "TRADE_ACCEPT", "TRADE_UPHOLD",
    "TRADE_VETO", "TRADE_PROPOSAL", "TRADE_DECLINE", "TRADE_ERROR",
    "ROSTER", "FUTURE_ROSTER",
]

RECEPTION_STAT_ID = 53

# Forward ESPN projections: one kona_player_info request per remaining fantasy week.
PROJECTION_STATUSES = ("ONTEAM", "FREEAGENT", "WAIVERS")
PROJECTION_LIMIT = 3000      # real league universe is ~1,050 players; a full page means "maybe truncated"
# ESPN response header: total players matching the filter, independent of limit/offset
# (verified 2026-10: 1052 combined = 190 ONTEAM + 862 FREEAGENT/WAIVERS, same with limit 50).
PLAYER_COUNT_HEADER = "X-Fantasy-Filter-Player-Count"
RESPONSE_META_KEY = "_response_meta"   # where the client attaches response metadata to the payload

FANTASY_POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"]
