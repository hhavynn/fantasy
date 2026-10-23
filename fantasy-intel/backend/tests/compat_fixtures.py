"""Synthetic league-format fixtures for compatibility certification (issue #253). Every name, id and number is invented.

FX-17 reproduces only the *structure* of an unusual six-team ESPN H2H-points league: 2 QB, 3 RB, 4 WR, 2 TE, 1 OP
(superflex), 3 FLEX, K, D/ST, 10 bench, 3 IR (17 starters). Its real scoring modifiers are unknown, so the scoring here
is an explicit, invented choice per matrix row, never a claim about any real league.

Matrix rows (`MATRIX`; docs: fantasy-intel/docs/compatibility/253-gap-inventory.md, section 3.1):

  M1  standard redraft (1 QB, 2 RB, 2 WR, TE, FLEX, K, D/ST), full PPR
  M2  FX-17 on ESPN, full PPR (M2b: plus an unmapped stat and a TE slot override, so Ours abstains)
  M3  FX-17 on Sleeper (reserve_slots 3), half PPR
  M4  superflex with RB/WR, WR/TE and two FLEX, tied projections and a sparse pool (no free agent projected)
  M8  individual defensive player (IDP) slots on ESPN (LB, DL) and Sleeper (DL, LB, IDP_FLEX)

Builders return raw provider payloads, shaped exactly like the responses the real sync code parses (the ESPN fixture
league `espn_fixture.league_payload()` is the template, as in tests/test_league_rules.py), so tests run the real import:
`espn_client(row)` feeds app/espn/sync.fetch_all + write_all, `sleeper_payloads(row)` feeds app/sleeper/write.write_sleeper.
`espn_fixture` itself is never mutated (its globals feed the goldens).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

import espn_fixture as ef
from app.espn.constants import PRO_TEAM_MAP, nflverse_team

SEASON, CURRENT, FINAL = ef.SEASON, ef.CURRENT, 17
SLEEPER_FIX = Path(__file__).parent / "fixtures" / "sleeper"

FX17 = {"QB": 2, "RB": 3, "WR": 4, "TE": 2, "OP": 1, "FLEX": 3, "K": 1, "D/ST": 1, "BE": 10, "IR": 3}
STANDARD = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "D/ST": 1, "BE": 6, "IR": 1}
SUPERFLEX_MULTI = {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "OP": 1, "RB/WR": 1, "WR/TE": 1, "FLEX": 2, "K": 1, "D/ST": 1,
                   "BE": 4, "IR": 1}
IDP_ESPN = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "LB": 1, "DL": 1, "K": 1, "D/ST": 1, "BE": 5, "IR": 1}
IDP_SLEEPER = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "DL": 1, "LB": 1, "DP": 1, "K": 1, "D/ST": 1, "BE": 5,
               "IR": 1}

# internal slot label -> ESPN lineupSlotId (app/espn/constants.SLOT_MAP) / Sleeper roster_positions entry
ESPN_SLOT_IDS = {"QB": 0, "RB": 2, "RB/WR": 3, "WR": 4, "WR/TE": 5, "TE": 6, "OP": 7, "LB": 10, "DL": 11, "D/ST": 16,
                 "K": 17, "BE": 20, "IR": 21, "FLEX": 23}
SLEEPER_SLOTS = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "D/ST": "DEF", "FLEX": "FLEX",
                 "OP": "SUPER_FLEX", "RB/WR": "WRRB_FLEX", "WR/TE": "REC_FLEX", "DL": "DL", "LB": "LB", "DB": "DB",
                 "DP": "IDP_FLEX", "BE": "BN"}
# who fills a slot in the synthetic rosters (a flex slot gets the position named here)
FILL = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "D/ST": "D/ST", "OP": "QB", "RB/WR": "RB",
        "WR/TE": "WR", "LB": "LB", "DL": "DT", "DP": "LB"}
FLEX_FILL = ("RB", "WR", "TE")
BENCH_FILL = ("QB", "RB", "WR", "TE", "RB", "WR", "QB", "RB", "WR", "TE")
ESPN_POSITION_IDS = {"QB": 1, "RB": 2, "WR": 3, "TE": 4, "K": 5, "DT": 9, "LB": 11, "D/ST": 16}
SLEEPER_POSITIONS = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "D/ST": "DEF", "LB": "LB", "DT": "DL"}
PRO_TEAMS = sorted(i for i in PRO_TEAM_MAP if i)          # ESPN proTeamIds, FA excluded

# ESPN-shaped scoring items (statId, points, D/ST rules as slot-16 overrides, as ESPN expresses them)
_DST = [(99, 1.0), (95, 2.0), (96, 2.0), (98, 2.0), (97, 2.0), (103, 6.0), (104, 6.0), (89, 5.0), (90, 4.0),
        (91, 3.0), (92, 1.0), (121, 0.0), (122, -1.0), (123, -1.0), (124, -3.0), (125, -5.0)]


def espn_scoring(ppr: float = 1.0, *, unmapped: bool = False) -> list[dict]:
    items = [{"statId": sid, "points": pts} for sid, pts in (
        (3, 0.04), (4, 4.0), (19, 2.0), (20, -2.0), (24, 0.1), (25, 6.0), (26, 2.0), (42, 0.1), (43, 6.0), (44, 2.0),
        (72, -2.0), (80, 3.0), (77, 4.0), (74, 5.0), (85, -1.0), (86, 1.0), (88, -1.0))]
    items.append({"statId": 53, "points": ppr, **({"pointsOverrides": {"6": ppr + 0.5}} if unmapped else {})})
    items += [{"statId": sid, "points": 0.0, "pointsOverrides": {"16": pts}} for sid, pts in _DST if pts]
    if unmapped:
        items.append({"statId": 8888, "points": 2.0})          # a rule with no readable label
    return items


SLEEPER_HALF_PPR = {
    "pass_yd": 0.04, "pass_td": 4, "pass_int": -1, "pass_2pt": 2, "rush_yd": 0.1, "rush_td": 6, "rush_2pt": 2,
    "rec": 0.5, "rec_yd": 0.1, "rec_td": 6, "rec_2pt": 2, "fum_lost": -2, "fum_rec_td": 6,
    "fgm_0_19": 3, "fgm_20_29": 3, "fgm_30_39": 3, "fgm_40_49": 4, "fgm_50p": 5, "fgmiss": -1, "xpm": 1, "xpmiss": -1,
    "sack": 1, "int": 2, "fum_rec": 2, "safe": 2, "blk_kick": 2, "def_td": 6,
    "pts_allow_0": 10, "pts_allow_1_6": 7, "pts_allow_7_13": 4, "pts_allow_14_20": 1, "pts_allow_21_27": 0,
    "pts_allow_28_34": -1, "pts_allow_35p": -4,
}
SLEEPER_IDP = {**SLEEPER_HALF_PPR, "idp_tkl": 1.0, "idp_sack": 3.0}


@dataclass(frozen=True)
class Row:
    """One matrix row: a provider, slot counts, scoring and roster/projection quirks."""
    id: str
    provider: str                     # espn | sleeper
    slots: dict
    teams: int = 4
    espn_items: list = field(default_factory=lambda: espn_scoring())
    sleeper_scoring: dict = field(default_factory=lambda: dict(SLEEPER_HALF_PPR))
    tied: bool = False                # every player projects the same: ties break by id
    free_agents: bool = True          # False: a sparse pool, nobody on waivers


MATRIX = {
    "M1": Row("M1", "espn", STANDARD),
    "M2": Row("M2", "espn", FX17, teams=6),
    "M2b": Row("M2b", "espn", FX17, teams=6, espn_items=espn_scoring(unmapped=True)),
    "M3": Row("M3", "sleeper", FX17, teams=6),
    "M4": Row("M4", "espn", SUPERFLEX_MULTI, tied=True, free_agents=False),
    "M8": Row("M8", "espn", IDP_ESPN),
    "M8s": Row("M8s", "sleeper", IDP_SLEEPER, sleeper_scoring=dict(SLEEPER_IDP)),
}


# ---- rosters -----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Player:
    pid: int                          # ESPN-style id (D/ST: -16000 - proTeamId)
    position: str                     # FILL vocabulary (QB, RB, WR, TE, K, D/ST, LB, DT)
    pro_team: int
    team: int | None                  # fantasy team, None = free agent
    slot: str | None                  # internal slot label on that team (starters, BE, IR)


def league_players(row: Row) -> list[Player]:
    """Every team's roster: one player per starting slot (a flex slot gets FILL / FLEX_FILL in turn), the bench from
    BENCH_FILL and injured players in IR; then free agents at each position. Deterministic."""
    out: list[Player] = []
    n = 0

    def next_team() -> int:
        nonlocal n
        n += 1
        return PRO_TEAMS[n % len(PRO_TEAMS)]

    for t in range(1, row.teams + 1):
        i = 0
        flex_i = 0
        for slot, count in row.slots.items():
            if slot in ("BE", "IR"):
                continue
            for _ in range(count):
                if slot == "FLEX":
                    pos = FLEX_FILL[flex_i % len(FLEX_FILL)]
                    flex_i += 1
                else:
                    pos = FILL[slot]
                i += 1
                if pos == "D/ST":
                    team = PRO_TEAMS[t - 1]
                    out.append(Player(-16000 - team, pos, team, t, slot))
                else:
                    out.append(Player(70_000 + t * 100 + i, pos, next_team(), t, slot))
        for b in range(row.slots.get("BE", 0)):
            i += 1
            out.append(Player(70_000 + t * 100 + i, BENCH_FILL[b % len(BENCH_FILL)], next_team(), t, "BE"))
        for _ in range(row.slots.get("IR", 0)):
            i += 1
            out.append(Player(70_000 + t * 100 + i, "RB", next_team(), t, "IR"))
    if row.free_agents:
        for j, pos in enumerate(("QB", "RB", "RB", "WR", "WR", "TE", "K", "K", "K")):
            out.append(Player(79_000 + j, pos, next_team(), None, None))
        used = {p.pro_team for p in out if p.position == "D/ST"}
        for team in [x for x in PRO_TEAMS if x not in used][:3]:
            out.append(Player(-16000 - team, "D/ST", team, None, None))
    return out


def projection(row: Row, pid: int, week: int) -> float:
    return 10.0 if row.tied else round(4.0 + abs(pid) % 13 * 0.9 + week / 10, 2)


def _name(p: Player) -> str:
    return f"FX {p.position} {abs(p.pid)}"


# ---- ESPN ----------------------------------------------------------------------------------------------------------------

def _espn_player(p: Player, stats: list | None = None) -> dict:
    injured = p.slot == "IR"
    return {"id": p.pid, "fullName": _name(p), "defaultPositionId": ESPN_POSITION_IDS[p.position],
            "proTeamId": p.pro_team, "eligibleSlots": [20, 21], "injured": injured,
            "injuryStatus": "INJURY_RESERVE" if injured else "ACTIVE",
            "ownership": {"percentOwned": 50.0, "percentStarted": 30.0, "percentChange": 0.0}, "stats": stats or []}


def espn_league_payload(row: Row) -> dict:
    """The mSettings + mTeam + mRoster league payload for the row (template: espn_fixture.league_payload)."""
    raw = ef.league_payload()
    template = raw["teams"][0]
    players = league_players(row)
    counts = {str(ESPN_SLOT_IDS[k]): v for k, v in row.slots.items()}
    counts.update({k: 0 for k in ("1", "22", "24") if k not in counts})   # zero counts, as ESPN sends them
    raw["settings"] = {**raw["settings"], "name": f"Compat {row.id}", "size": row.teams,
                       "rosterSettings": {"lineupSlotCounts": counts, "lineupLocktimeType": "INDIVIDUAL_GAME"},
                       "scoringSettings": {"scoringType": "H2H_POINTS", "scoringItems": copy.deepcopy(row.espn_items)}}
    raw["teams"] = [{**copy.deepcopy(template), "id": t, "abbrev": f"C{t}", "name": f"Compat Team {t}",
                     "owners": [f"{{COMPAT-{t}}}"], "playoffSeed": t, "waiverRank": t,
                     "roster": espn_roster(row, players, CURRENT)["teams"][t - 1]["roster"]}
                    for t in range(1, row.teams + 1)]
    raw["members"] = [{"id": f"{{COMPAT-{t}}}", "displayName": f"compat{t}"} for t in range(1, row.teams + 1)]
    raw["schedule"] = []
    return raw


def espn_roster(row: Row, players: list[Player], period: int) -> dict:
    teams = []
    for t in range(1, row.teams + 1):
        entries = []
        for p in (x for x in players if x.team == t):
            stats = ([{"seasonId": SEASON, "scoringPeriodId": period, "statSourceId": 0, "statSplitTypeId": 1,
                       "appliedTotal": 10.0, "stats": {}}] if period < CURRENT else [])
            entries.append({"playerId": p.pid, "lineupSlotId": ESPN_SLOT_IDS[p.slot], "acquisitionType": "DRAFT",
                            "acquisitionDate": ef.T0, "injuryStatus": "INJURY_RESERVE" if p.slot == "IR" else "ACTIVE",
                            "playerPoolEntry": {"id": p.pid, "onTeamId": t, "status": "ONTEAM",
                                                "player": _espn_player(p, stats)}})
        teams.append({"id": t, "roster": {"entries": entries}})
    return {"id": ef.LEAGUE_ID, "seasonId": SEASON, "scoringPeriodId": period, "teams": teams}


class EspnCompatClient:
    """The FakeEspnClient interface (tests/espn_fixture.py) over one matrix row. `missing`: player ids returned
    without a projection entry."""

    def __init__(self, row: Row, missing=()):
        self.row, self.players, self.missing = row, league_players(row), set(missing)

    def league(self):
        return espn_league_payload(self.row)

    def roster_for_period(self, period):
        return espn_roster(self.row, self.players, period)

    def transactions(self, period):
        return {"transactions": []}

    def activity(self, offset=0, size=50):
        return {"topics": []}

    def player_pool(self, period, limit=500):
        return {"players": [{"id": p.pid, "onTeamId": 0, "status": "FREEAGENT", "player": _espn_player(p)}
                            for p in self.players if p.team is None]}

    def player_projections(self, period, limit=3000):
        out = []
        for p in self.players:
            stats = [] if p.pid in self.missing else [
                {"seasonId": SEASON, "scoringPeriodId": period, "statSourceId": 1, "statSplitTypeId": 1,
                 "appliedTotal": projection(self.row, p.pid, period)}]
            out.append({"id": p.pid, "onTeamId": p.team or 0, "status": "ONTEAM" if p.team else "FREEAGENT",
                        "player": _espn_player(p, stats)})
        return {"players": out, "_response_meta": {"filter_player_count": len(out)}}

    def pro_players(self):
        return [{k: v for k, v in _espn_player(p).items() if k in ("id", "fullName", "defaultPositionId", "proTeamId",
                                                                   "eligibleSlots")} for p in self.players]

    def player_cards(self, ids, max_period):
        return {"players": []}

    def draft(self):
        return {"draftDetail": {"drafted": False, "picks": []}}


def write_espn(con, row: Row, missing=()) -> dict:
    """Import the row's ESPN league into `con` (init_db'd) with the real fetch + write path; team 1 is "mine"."""
    from app.espn.sync import fetch_all, write_all
    return write_all(con, fetch_all(EspnCompatClient(row, missing)), swid=None, my_team_id=1)


# ---- Sleeper -------------------------------------------------------------------------------------------------------------

def _sleeper_id(p: Player) -> str:
    return PRO_TEAM_MAP[p.pro_team] if p.position == "D/ST" else str(900_000 + p.pid)


def sleeper_payloads(row: Row):
    """SleeperPayloads for the row: roster_positions from the slot counts (IR from settings.reserve_slots), every
    player carrying his ESPN-style id as Sleeper's `espn_id`, no projections (Sleeper publishes none)."""
    from app.sleeper.write import SleeperPayloads

    players = league_players(row)
    positions = [SLEEPER_SLOTS[s] for s, n in row.slots.items() if s not in ("BE", "IR") for _ in range(n)]
    positions += ["BN"] * row.slots.get("BE", 0)
    league = json.loads((SLEEPER_FIX / "league.json").read_text())
    league.update(league_id="1180000000000025301", name=f"Compat {row.id}", total_rosters=row.teams,
                  roster_positions=positions, scoring_settings=dict(row.sleeper_scoring))
    league["settings"] = {**league["settings"], "num_teams": row.teams, "reserve_slots": row.slots.get("IR", 0),
                          "taxi_slots": row.slots.get("TAXI", 0)}
    users = [{"user_id": f"70000000000253{t:02d}", "display_name": f"compat{t}", "metadata": {"team_name": f"C{t}"},
              "is_owner": t == 1} for t in range(1, row.teams + 1)]
    rosters = []
    for t in range(1, row.teams + 1):
        mine = [p for p in players if p.team == t]
        starters = [_sleeper_id(p) for p in mine if p.slot not in ("BE", "IR")]
        rosters.append({"roster_id": t, "owner_id": users[t - 1]["user_id"], "co_owners": None,
                        "players": [_sleeper_id(p) for p in mine], "starters": starters,
                        "reserve": [_sleeper_id(p) for p in mine if p.slot == "IR"], "taxi": None,
                        "settings": {"wins": 0, "losses": 0, "ties": 0, "fpts": 0, "waiver_position": t,
                                     "waiver_budget_used": 0, "total_moves": 0, "division": 1}})
    universe = {}
    for p in players:
        s = _sleeper_id(p)
        pos = SLEEPER_POSITIONS[p.position]
        universe[s] = {"player_id": s, "full_name": _name(p), "position": pos, "fantasy_positions": [pos],
                       "team": nflverse_team(p.pro_team), "status": "Active", "active": True,
                       "espn_id": None if p.pid < 0 else p.pid,
                       "injury_status": "IR" if p.slot == "IR" else None}
    state = {"week": CURRENT, "season": str(SEASON), "season_type": "regular", "display_week": CURRENT, "leg": CURRENT}
    return SleeperPayloads(league=league, users=users, rosters=rosters, state=state, players=universe)


def write_sleeper_row(con, row: Row) -> dict:
    from app.sleeper.write import write_sleeper
    return write_sleeper(con, sleeper_payloads(row))


def import_row(con, row: Row, missing=()) -> dict:
    """Import any matrix row with its provider's real write path."""
    return write_espn(con, row, missing) if row.provider == "espn" else write_sleeper_row(con, row)
