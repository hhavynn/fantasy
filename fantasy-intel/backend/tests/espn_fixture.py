"""Synthetic payloads shaped exactly like ESPN's v3 fantasy API responses."""

from __future__ import annotations

import copy

LEAGUE_ID = 999
SEASON = 2026
MY_SWID = "{ABCD-1234}"
CURRENT = 3
DAY = 86_400_000
T0 = 1_757_000_000_000  # ~ Sept 2026, epoch ms

# espn id -> (name, defaultPositionId, proTeamId)
PLAYERS = {
    101: ("Alpha QB", 1, 12), 102: ("Bravo QB", 1, 2), 103: ("Charlie QB", 1, 8), 104: ("Delta QB", 1, 21),
    201: ("Echo RB", 2, 12), 202: ("Foxtrot RB", 2, 2), 203: ("Golf RB", 2, 8), 204: ("Hotel RB", 2, 21),
    205: ("India RB", 2, 25), 206: ("Juliet RB", 2, 26), 207: ("Kilo RB", 2, 27), 208: ("Lima RB", 2, 28),
    301: ("Mike WR", 3, 12), 302: ("November WR", 3, 2), 303: ("Oscar WR", 3, 8), 304: ("Papa WR", 3, 21),
    305: ("Quebec WR", 3, 25), 306: ("Romeo WR", 3, 26), 307: ("Sierra WR", 3, 27), 308: ("Tango WR", 3, 28),
    401: ("Uniform TE", 4, 12), 402: ("Victor TE", 4, 2), 403: ("Whiskey TE", 4, 8), 404: ("Xray TE", 4, 21),
    -16012: ("Chiefs D/ST", 16, 12), -16002: ("Bills D/ST", 16, 2),
    501: ("Yankee K", 5, 12), 502: ("Zulu K", 5, 2),
    # free agents
    209: ("Waiver RB", 2, 12), 309: ("Waiver WR", 3, 2), 405: ("Waiver TE", 4, 8), 105: ("Waiver QB", 1, 21),
}

# team -> [(player, slotId)]
ROSTERS = {
    1: [(101, 0), (201, 2), (202, 2), (301, 4), (302, 4), (401, 6), (203, 23), (-16012, 16), (501, 17), (303, 20)],
    2: [(102, 0), (204, 2), (205, 2), (304, 4), (305, 4), (402, 6), (306, 23), (-16002, 16), (502, 17),
        (206, 20), (307, 20), (207, 21)],
    3: [(103, 0), (208, 2), (308, 4), (403, 6)],
    4: [(104, 0), (404, 6)],
}


def player_obj(pid: int, period: int | None = None, points: float | None = None, injury: str = "ACTIVE") -> dict:
    name, pos, team = PLAYERS[pid]
    stats = []
    if period is not None and points is not None:
        stats.append({"seasonId": SEASON, "scoringPeriodId": period, "statSourceId": 0, "statSplitTypeId": 1,
                      "appliedTotal": points, "stats": {}})
        stats.append({"seasonId": SEASON, "scoringPeriodId": period, "statSourceId": 1, "statSplitTypeId": 1,
                      "appliedTotal": points + 1, "stats": {}})
    return {"id": pid, "fullName": name, "defaultPositionId": pos, "proTeamId": team, "eligibleSlots": [20, 21],
            "injured": injury != "ACTIVE", "injuryStatus": injury,
            "ownership": {"percentOwned": 50.0, "percentStarted": 30.0, "percentChange": 1.0}, "stats": stats}


def roster_payload(period: int, rosters: dict | None = None) -> dict:
    rosters = rosters or ROSTERS
    teams = []
    for tid, entries in rosters.items():
        teams.append({"id": tid, "roster": {"entries": [{
            "playerId": pid, "lineupSlotId": slot, "acquisitionType": "DRAFT", "acquisitionDate": T0,
            "injuryStatus": "ACTIVE",
            "playerPoolEntry": {"id": pid, "onTeamId": tid, "status": "ONTEAM",
                                "player": player_obj(pid, period, 10.0 if period < CURRENT else None)},
        } for pid, slot in entries]}})
    return {"id": LEAGUE_ID, "seasonId": SEASON, "scoringPeriodId": period, "teams": teams}


def league_payload() -> dict:
    base = roster_payload(CURRENT)
    teams = []
    for t in base["teams"]:
        tid = t["id"]
        teams.append({**t, "abbrev": f"T{tid}", "name": f"Team {tid}", "owners": [f"{{OWNER-{tid}}}"] if tid != 2 else ["{abcd-1234}"],
                      "divisionId": 0, "playoffSeed": tid, "waiverRank": tid,
                      "record": {"overall": {"wins": 4 - tid, "losses": tid - 1, "ties": 0,
                                             "pointsFor": 100.0 * (5 - tid), "pointsAgainst": 90.0}},
                      "transactionCounter": {"acquisitions": 2, "drops": 2, "trades": 0, "moveToIR": 1 if tid == 2 else 0,
                                             "acquisitionBudgetSpent": 10 * tid, "matchupAcquisitionTotals": {"1": 1}}})
    return {
        "id": LEAGUE_ID, "seasonId": SEASON, "scoringPeriodId": CURRENT,
        "status": {"currentMatchupPeriod": CURRENT, "firstScoringPeriod": 1, "finalScoringPeriod": 17,
                   "latestScoringPeriod": CURRENT, "previousSeasons": []},
        "settings": {
            "name": "Fixture League", "size": 4,
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "16": 1, "17": 1,
                                                    "20": 3, "21": 1, "7": 0}},
            "scoringSettings": {"scoringType": "H2H_POINTS", "scoringItems": [{"statId": 53, "points": 1.0}]},
            "acquisitionSettings": {"isUsingAcquisitionBudget": True, "acquisitionBudget": 100},
            "scheduleSettings": {"matchupPeriodCount": 14, "playoffTeamCount": 2},
            "tradeSettings": {"deadlineDate": T0 + 60 * DAY},
        },
        "members": [{"id": "{abcd-1234}", "displayName": "me"}, {"id": "{OWNER-1}", "displayName": "rival"}],
        "teams": teams,
        "schedule": [{"id": 1, "matchupPeriodId": 1, "home": {"teamId": 1, "totalPoints": 110.5},
                      "away": {"teamId": 2, "totalPoints": 99.0}, "winner": "HOME", "playoffTierType": "NONE"}],
    }


def tx(tid: str, team: int, period: int, day: int, items: list[dict], ttype: str = "FREEAGENT",
       status: str = "EXECUTED", bid: int = 0) -> dict:
    return {"id": tid, "teamId": team, "type": ttype, "status": status, "scoringPeriodId": period,
            "proposedDate": T0 + day * DAY, "processDate": T0 + day * DAY, "bidAmount": bid,
            "executionType": "EXECUTE", "isPending": False, "items": items}


def transactions_payload(period: int) -> dict:
    by_period = {
        1: [tx("tx-1", 1, 1, 1, [{"type": "ADD", "playerId": 203, "toTeamId": 1, "fromTeamId": -1},
                                 {"type": "DROP", "playerId": 209, "fromTeamId": 1, "toTeamId": -1}], "WAIVER", bid=12),
            tx("tx-2", 2, 1, 2, [{"type": "ADD", "playerId": 206, "toTeamId": 2, "fromTeamId": -1}])],
        2: [tx("tx-3", 2, 2, 8, [{"type": "ADD", "playerId": 307, "toTeamId": 2, "fromTeamId": -1},
                                 {"type": "DROP", "playerId": 309, "fromTeamId": 2, "toTeamId": -1}]),
            tx("tx-4", 3, 2, 9, [{"type": "ADD", "playerId": 209, "toTeamId": 3, "fromTeamId": -1}], "WAIVER",
               status="FAILED_INVALIDPLAYERSOURCE", bid=5),
            tx("tx-5", 2, 2, 9, [{"type": "LINEUP", "playerId": 207, "fromLineupSlotId": 20, "toLineupSlotId": 21}],
               "ROSTER")],
        3: [tx("tx-6", 1, 3, 15, [], "TRADE_ACCEPT")],
    }
    return {"transactions": copy.deepcopy(by_period.get(period, []))}


def activity_payload() -> dict:
    return {"topics": [
        {"id": "topic-trade", "date": T0 + 15 * DAY, "type": "ACTIVITY_TRANSACTIONS", "messages": [
            {"id": "m1", "messageTypeId": 244, "targetId": 302, "from": 1, "to": 4},
            {"id": "m2", "messageTypeId": 244, "targetId": 303, "from": 1, "to": 4},
            {"id": "m3", "messageTypeId": 244, "targetId": 404, "from": 4, "to": 1},
        ]},
        {"id": "topic-add", "date": T0 + 2 * DAY, "messages": [
            {"id": "m4", "messageTypeId": 178, "targetId": 206, "to": 2, "from": -1}]},
    ]}


def pool_payload() -> dict:
    out = []
    for pid, status in [(209, "FREEAGENT"), (309, "WAIVERS"), (405, "FREEAGENT"), (105, "FREEAGENT")]:
        out.append({"id": pid, "onTeamId": 0, "status": status, "player": player_obj(pid)})
    return {"players": out}


def pro_players_payload() -> list:
    return [{k: v for k, v in player_obj(pid).items() if k in ("id", "fullName", "defaultPositionId", "proTeamId",
                                                                 "eligibleSlots")} for pid in PLAYERS]


def cards_payload(ids: list[int]) -> dict:
    players = []
    for pid in ids:
        p = player_obj(pid)
        p["stats"] = [{"seasonId": SEASON, "scoringPeriodId": w, "statSourceId": 0, "statSplitTypeId": 1,
                       "appliedTotal": 8.0 + w} for w in range(1, CURRENT)]
        players.append({"id": pid, "player": p})
    return {"players": players}


def draft_payload() -> dict:
    return {"draftDetail": {"drafted": True, "picks": [
        {"overallPickNumber": 1, "roundId": 1, "roundPickNumber": 1, "teamId": 1, "playerId": 201, "bidAmount": 0,
         "keeper": False}]}}


class FakeEspnClient:
    def __init__(self, transactions_override: dict | None = None):
        self.transactions_override = transactions_override or {}

    def league(self):
        return league_payload()

    def roster_for_period(self, period):
        return roster_payload(period)

    def transactions(self, period):
        return self.transactions_override.get(period) or transactions_payload(period)

    def activity(self, offset=0, size=50):
        return activity_payload() if offset == 0 else {"topics": []}

    def player_pool(self, period, limit=500):
        return pool_payload()

    def pro_players(self):
        return pro_players_payload()

    def player_cards(self, ids, max_period):
        return cards_payload(ids)

    def draft(self):
        return draft_payload()
