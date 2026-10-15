"""Sleeper -> internal vocabulary (internal = the ESPN-derived names the analytics already use)."""

# Sleeper roster_positions entry -> internal lineup slot (app.analytics.lineup / espn SLOT_MAP labels).
SLOT_MAP: dict[str, str] = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "DEF": "D/ST",
    "FLEX": "FLEX",            # RB/WR/TE
    "SUPER_FLEX": "OP",        # QB/RB/WR/TE
    "WRRB_FLEX": "RB/WR",
    "REC_FLEX": "WR/TE",
    "BN": "BE",
    "DL": "DL", "LB": "LB", "DB": "DB", "IDP_FLEX": "DP",
}
BENCH = "BE"
IR = "IR"            # Sleeper `reserve`
TAXI = "TAXI"        # Sleeper dynasty taxi squad: rostered, never starts, no internal equivalent

# Sleeper player position -> internal position. Everything else is already the same label.
POSITION_MAP: dict[str, str] = {"DEF": "D/ST"}

# Team codes -> nflverse abbreviations (internal `pro_team`). Covers Sleeper's codes and the
# DynastyProcess ID map's own codes (KCC, GBP, LVR, ...), plus historical relocations.
NFLVERSE_TEAM: dict[str, str] = {
    "LAR": "LA", "RAM": "LA", "STL": "LA",
    "WSH": "WAS",
    "JAC": "JAX",
    "LVR": "LV", "OAK": "LV",
    "SD": "LAC", "SDC": "LAC",
    "KCC": "KC", "GBP": "GB", "NEP": "NE", "NOS": "NO", "SFO": "SF", "TBB": "TB",
}
FREE_AGENT_CODES = {"FA", "FA*", ""}

# league.settings.waiver_type: 0 rolling, 1 reverse standings, 2 FAAB.
WAIVER_TYPE_FAAB = 2


def nflverse_team(code: str | None) -> str | None:
    if code is None:
        return None
    c = code.strip().upper()
    if c in FREE_AGENT_CODES:
        return None
    return NFLVERSE_TEAM.get(c, c)
