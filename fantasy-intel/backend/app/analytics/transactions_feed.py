"""League activity feed: every executed add, drop and trade the league sync stored, one event per move.

Read-only over the tables the ESPN and Sleeper syncs already write (`transactions`, `transaction_items`,
`activity_messages`); no provider calls. The rules, in the order they apply:

  * Scope. Executed moves only: free-agent pickups, waiver claims, drops, trades, and Sleeper commissioner
    adds and drops. Failed, cancelled and pending claims, proposals, vetoes and lineup moves are left out
    (a manager's unsuccessful or pending claims are private to them in the provider).
  * One event per move. An add with its drop is one event. A trade is one event however the provider stored it:
    rows tied by `related_transaction_id` (accept and uphold), rows that move the same player between the same two
    teams within TRADE_WINDOW_DAYS, Sleeper's mirrored half legs (the drop side and the add side of each player),
    and the league activity feed's trade legs (ESPN omits other teams' trade items from mTransactions2) are
    merged, each leg once. A trade row with no items is matched to an activity-feed trade of the same team within
    ITEMLESS_WINDOW_HOURS; unmatched, it stays a trade with no recorded players rather than disappearing.
  * Times are UTC from the provider: `processed_at` (when it went through) and `proposed_at` (when it was made),
    the latter only where the provider stores both and they differ.
  * Bids: shown for waiver claims in FAAB leagues, as stored (a stored 0 is a measured 0). Nothing else is
    inferred: no draft picks or FAAB dollars moved in trades (a Sleeper trade that carried some is flagged).
  * After the move: for each incoming player, the completed weeks since the move in which the receiving team's
    lineup snapshot was stored, how many of those had the player on the roster and in the starting lineup, and his
    points as a starter. Omitted when no such week exists (no invented impact).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..db.connection import fetch_dicts, table_exists
from .legacy_league import latest_league

TRADE_TYPES = ("TRADE_ACCEPT", "TRADE_UPHOLD")
MOVE_TYPES = ("FREEAGENT", "WAIVER", "ROSTER")
TRADE_WINDOW_DAYS = 3
ITEMLESS_WINDOW_HOURS = 24
KINDS = ("trade", "waiver", "free_agent", "drop", "commissioner")
POSITION_ORDER = ("QB", "RB", "WR", "TE", "K", "D/ST")
SORTS = ("date", "week", "type")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

NOTES = {
    "scope": "Executed moves only: free-agent pickups, waiver claims, drops, trades and commissioner adds and drops. "
             "Failed, cancelled and pending claims, trade offers and lineup moves are not shown.",
    "trades": "Each trade is one entry even when the provider stored it more than once (one row per side, an accept "
              "and an uphold, or the league activity feed). Each player who moved is listed once.",
    "dates": "Times are stored in UTC and shown in your time zone. A proposed time is shown only where the provider "
             "stores it separately from the processed time.",
    "bids": "Bids are shown for waiver claims in FAAB leagues, as stored. Draft picks and FAAB dollars traded are "
            "not stored, so trades list players only.",
    "after": "After the move: completed weeks since the move in which the receiving team's lineup was stored, how "
             "many of them had the player on that roster and starting, and his points as a starter. Left out when "
             "no completed week has been stored since.",
}


class NotSynced(Exception):
    pass


class UnknownManager(Exception):
    pass


class StaleCursor(Exception):
    pass


# ------------------------------------------------------------------ records --

@dataclass
class Leg:
    action: str                       # add | drop | trade
    player_id: int | None
    from_team_id: int | None
    to_team_id: int | None

    def key(self) -> tuple:
        return (self.action, self.player_id, self.from_team_id, self.to_team_id)


@dataclass
class Record:
    """One stored source of a move: a transaction row with its items, or one activity-feed topic's trade legs."""
    source: str                       # transactions | activity
    source_id: str
    type: str | None
    team_id: int | None
    week: int | None
    processed_at: datetime | None
    proposed_at: datetime | None
    related_id: str | None = None
    bid_amount: float | None = None
    unlisted: list[str] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)

    @property
    def when(self) -> datetime | None:
        return self.processed_at or self.proposed_at


def _valid(team: int | None, teams: set[int]) -> int | None:
    return team if isinstance(team, int) and team in teams else None


def _trade_legs(items: list[dict], tx_team: int | None, teams: set[int]) -> list[Leg]:
    """A trade row's items as legs. TRADE items that carry only one side (Sleeper stores each traded player as a drop
    from one roster and an add to the other) are joined per player; a side left over is a plain drop or add (a
    player released or picked up as part of the trade). Explicit ADD/DROP items stay adds and drops."""
    full: list[Leg] = []
    halves: dict[int | None, dict[str, list[int]]] = defaultdict(lambda: {"from": [], "to": []})
    other: list[Leg] = []
    for it in items:
        pid, frm, to = it["espn_player_id"], _valid(it["from_team_id"], teams), _valid(it["to_team_id"], teams)
        if it["item_type"] == "DROP":
            other.append(Leg("drop", pid, frm or _valid(tx_team, teams), None))
        elif it["item_type"] == "ADD":
            other.append(Leg("add", pid, None, to or _valid(tx_team, teams)))
        elif frm is not None and to is not None:
            full.append(Leg("trade", pid, frm, to))
        elif frm is not None:
            halves[pid]["from"].append(frm)
        elif to is not None:
            halves[pid]["to"].append(to)
    for pid, h in halves.items():
        if len(h["from"]) == 1 and len(h["to"]) == 1:
            full.append(Leg("trade", pid, h["from"][0], h["to"][0]))
            continue
        other += [Leg("drop", pid, f, None) for f in h["from"]] + [Leg("add", pid, None, t) for t in h["to"]]
    return full + other


def _move_legs(items: list[dict], tx_team: int | None, teams: set[int]) -> list[Leg]:
    out = []
    for it in items:
        if it["item_type"] == "ADD":
            out.append(Leg("add", it["espn_player_id"], None,
                           _valid(it["to_team_id"], teams) or _valid(tx_team, teams)))
        elif it["item_type"] == "DROP":
            out.append(Leg("drop", it["espn_player_id"],
                           _valid(it["from_team_id"], teams) or _valid(tx_team, teams), None))
    return out


def _dedupe(legs: list[Leg]) -> list[Leg]:
    seen, out = set(), []
    for leg in legs:
        if leg.key() not in seen:
            seen.add(leg.key())
            out.append(leg)
    # A half leg (add or drop of a player) that a full trade leg of the same player already covers is the same move.
    traded = {(leg.player_id, leg.from_team_id) for leg in out if leg.action == "trade"} \
        | {(leg.player_id, leg.to_team_id) for leg in out if leg.action == "trade"}
    return [leg for leg in out if leg.action == "trade"
            or (leg.player_id, leg.from_team_id if leg.action == "drop" else leg.to_team_id) not in traded]


def records_from_rows(tx_rows: list[dict], activity_rows: list[dict],
                      teams: set[int]) -> tuple[list[Record], list[Record]]:
    """(trade records, other move records) from the stored rows. `tx_rows` is one row per transaction item (a
    transaction with no items has one row with NULL item columns); `activity_rows` one row per activity trade leg."""
    by_tx: dict[str, dict] = {}
    items: dict[str, list[dict]] = defaultdict(list)
    for r in tx_rows:
        by_tx.setdefault(r["transaction_id"], r)
        if r["item_type"] is not None:
            items[r["transaction_id"]].append(r)
    trades, moves = [], []
    for tid, r in by_tx.items():
        is_trade = r["type"] in TRADE_TYPES
        unlisted = [name for name, n in (("draft picks", r.get("draft_picks")), ("FAAB", r.get("faab_moved")))
                    if is_trade and n]
        rec = Record("transactions", tid, r["type"], _valid(r["team_id"], teams), r["scoring_period"],
                     r["process_date"], r["proposed_date"], r["related_transaction_id"], r["bid_amount"], unlisted)
        its = sorted(items.get(tid, []), key=lambda i: i["item_index"])
        if is_trade:
            rec.legs = _dedupe(_trade_legs(its, r["team_id"], teams))
            trades.append(rec)
        else:
            rec.legs = _dedupe(_move_legs(its, r["team_id"], teams))
            if rec.legs:
                moves.append(rec)
    topics: dict[str, Record] = {}
    for a in activity_rows:
        rec = topics.setdefault(str(a["topic_id"]), Record("activity", str(a["topic_id"]), "TRADE", None, None,
                                                           a["topic_date"], None))
        frm, to = _valid(a["from_team_id"], teams), _valid(a["to_team_id"], teams)
        if frm is not None and to is not None:
            rec.legs.append(Leg("trade", a["espn_player_id"], frm, to))
    for rec in topics.values():
        rec.legs = _dedupe(rec.legs)
        if rec.legs:
            trades.append(rec)
    return trades, moves


# -------------------------------------------------------------------- merge --

class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _within(a: datetime | None, b: datetime | None, window: timedelta) -> bool:
    return a is None or b is None or abs(a - b) <= window


def _teams_of(rec: Record) -> set[int]:
    out = {t for leg in rec.legs for t in (leg.from_team_id, leg.to_team_id) if t is not None}
    return out | ({rec.team_id} if rec.team_id is not None else set())


def merge_trades(records: list[Record]) -> list[list[Record]]:
    """Group trade records that describe the same trade (see the module docstring); each group is one event."""
    uf = _UnionFind(len(records))
    index = {(r.source, r.source_id): i for i, r in enumerate(records)}
    # Rows tied by related_transaction_id: to each other (an accept and an uphold both point at the proposal, which
    # is not an executed move and is not loaded) and to the related row when it is loaded.
    siblings: dict[str, int] = {}
    for i, r in enumerate(records):
        if r.source != "transactions" or not r.related_id:
            continue
        if ("transactions", r.related_id) in index:
            uf.union(i, index[("transactions", r.related_id)])
        uf.union(i, siblings.setdefault(r.related_id, i))
    window = timedelta(days=TRADE_WINDOW_DAYS)
    by_leg: dict[tuple, list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        for leg in r.legs:
            if leg.action == "trade":
                by_leg[(leg.player_id, leg.from_team_id, leg.to_team_id)].append(i)
    for idx in by_leg.values():
        # Clustered in time order around each cluster's first record, so a chain of rows a few days apart never
        # merges two trades of the same player weeks apart. An undated row joins the first cluster.
        dated = sorted((i for i in idx if records[i].when is not None), key=lambda i: (records[i].when, i))
        anchor = None
        for i in dated:
            if anchor is not None and _within(records[anchor].when, records[i].when, window):
                uf.union(anchor, i)
            else:
                anchor = i
        undated = [i for i in idx if records[i].when is None]
        for i in undated:
            uf.union(dated[0] if dated else undated[0], i)
    # A trade row without items: the closest trade of the same team within the window, preferring one with legs.
    hours = timedelta(hours=ITEMLESS_WINDOW_HOURS)
    for i, r in enumerate(records):
        if r.legs or r.team_id is None:
            continue
        near = [(j, o) for j, o in enumerate(records) if j != i and r.team_id in _teams_of(o)
                and r.when is not None and o.when is not None and abs(r.when - o.when) <= hours]
        if near:
            j, _ = min(near, key=lambda x: (not x[1].legs, abs(r.when - x[1].when), x[0]))
            uf.union(i, j)
    groups: dict[int, list[Record]] = defaultdict(list)
    for i, r in enumerate(records):
        groups[uf.find(i)].append(r)
    return list(groups.values())


# ------------------------------------------------------------------- events --

def _iso(v: datetime | None) -> str | None:
    if v is None:
        return None
    return (v.replace(tzinfo=None).isoformat(timespec="seconds")) + "Z"


def _kind(rec: Record) -> str:
    if rec.type == "WAIVER":
        return "waiver"
    if rec.type == "ROSTER":
        return "commissioner"
    return "free_agent" if any(leg.action == "add" for leg in rec.legs) else "drop"


def _event(kind: str, recs: list[Record], legs: list[Leg], *, is_faab: bool) -> dict:
    tx = [r for r in recs if r.source == "transactions"]
    processed = [r.processed_at for r in tx if r.processed_at] or [r.processed_at for r in recs if r.processed_at]
    proposed = [r.proposed_at for r in tx if r.proposed_at]
    processed_at = max(processed) if processed else None
    proposed_at = min(proposed) if proposed else None
    # Both times only where both are stored and differ; with no processed time the proposed one is the only time.
    if proposed_at is not None and processed_at is not None and abs(processed_at - proposed_at) < timedelta(minutes=1):
        proposed_at = None
    weeks = [r.week for r in tx if r.week is not None]
    teams = set().union(*(_teams_of(r) for r in recs)) if recs else set()
    tx_ids = sorted(r.source_id for r in tx)
    topic_ids = sorted(r.source_id for r in recs if r.source == "activity")
    bid = None
    if kind == "waiver" and is_faab and len(tx) == 1 and tx[0].bid_amount is not None:
        bid = tx[0].bid_amount
    unlisted = sorted({u for r in recs for u in r.unlisted})
    return {
        "event_id": f"tx:{tx_ids[0]}" if tx_ids else f"act:{topic_ids[0]}",
        "kind": kind,
        "week": max(weeks) if weeks else None,
        "processed_at": processed_at,
        "proposed_at": proposed_at,
        "team_ids": sorted(teams),
        "bid_amount": bid,
        "legs": legs,
        "unlisted_assets": unlisted,
        "sources": {"transactions": tx_ids, "activity": topic_ids},
    }


def build_events(trade_records: list[Record], move_records: list[Record], *, is_faab: bool) -> list[dict]:
    """Every event, newest first (undated last), ties by event id. Legs stay `Leg` objects until serialised."""
    events = []
    for group in merge_trades(trade_records):
        legs = _dedupe([leg for r in sorted(group, key=lambda r: (r.source != "transactions", r.source_id))
                        for leg in r.legs])
        events.append(_event("trade", group, legs, is_faab=is_faab))
    for rec in move_records:
        events.append(_event(_kind(rec), [rec], rec.legs, is_faab=is_faab))
    return sort_events(events, "date", "desc")


def _type_rank(kind: str) -> int:
    return KINDS.index(kind) if kind in KINDS else len(KINDS)


def sort_events(events: list[dict], key: str, direction: str) -> list[dict]:
    """Sorted copy: by `key` in `direction`, then newest first, then event id. Missing values sort last either way."""
    by_date = sorted(events, key=lambda e: e["event_id"])
    by_date.sort(key=lambda e: e["processed_at"] or e["proposed_at"] or datetime.min, reverse=True)
    by_date.sort(key=lambda e: (e["processed_at"] or e["proposed_at"]) is None)
    if key == "date":
        if direction == "asc":
            dated = [e for e in by_date if (e["processed_at"] or e["proposed_at"]) is not None]
            return dated[::-1] + [e for e in by_date if (e["processed_at"] or e["proposed_at"]) is None]
        return by_date
    value = (lambda e: e["week"]) if key == "week" else (lambda e: _type_rank(e["kind"]))
    present = [e for e in by_date if value(e) is not None]
    present.sort(key=value, reverse=direction == "desc")
    return present + [e for e in by_date if value(e) is None]


def filter_events(events: list[dict], *, manager: int | None, kind: str | None, position: str | None,
                  week: int | None, positions: dict[int, str | None]) -> list[dict]:
    out = []
    for e in events:
        if manager is not None and manager not in e["team_ids"]:
            continue
        if kind is not None and e["kind"] != kind:
            continue
        if week is not None and e["week"] != week:
            continue
        if position is not None and not any(positions.get(leg.player_id) == position for leg in e["legs"]):
            continue
        out.append(e)
    return out


def page(events: list[dict], cursor: str | None, limit: int) -> tuple[list[dict], dict]:
    """The page after the event named by `cursor` (None: the first page)."""
    start = 0
    if cursor:
        ids = [e["event_id"] for e in events]
        if cursor not in ids:
            raise StaleCursor("This page link is out of date. Start again from the newest moves.")
        start = ids.index(cursor) + 1
    rows = events[start:start + limit]
    prev_start = max(0, start - limit)
    return rows, {
        "limit": limit, "offset": start, "returned": len(rows), "total": len(events),
        "next_cursor": rows[-1]["event_id"] if rows and start + limit < len(events) else None,
        "has_prev": start > 0,
        "prev_cursor": events[prev_start - 1]["event_id"] if start > 0 and prev_start > 0 else None,
    }


# ------------------------------------------------------------- after a move --

def after_move(event_week: int | None, team_id: int | None, player_id: int | None, *, current_week: int | None,
               coverage: set[tuple[int, int]], snaps: dict[tuple[int, int, int], dict]) -> dict | None:
    """The receiving team's stored lineups in completed weeks from the move's week on (see NOTES['after'])."""
    if event_week is None or team_id is None or player_id is None or current_week is None:
        return None
    weeks = sorted(w for (t, w) in coverage if t == team_id and event_week <= w < current_week)
    if not weeks:
        return None
    rows = [snaps[(team_id, player_id, w)] for w in weeks if (team_id, player_id, w) in snaps]
    started = [r for r in rows if r["is_starter"]]
    # No start: no starter points to report (None, not 0); a stored NULL in a started week leaves the sum unknown.
    points = None if not started or any(r["points"] is None for r in started) \
        else round(sum(r["points"] for r in started), 2)
    return {"weeks": len(weeks), "first_week": weeks[0], "last_week": weeks[-1], "on_roster": len(rows),
            "started": len(started), "starter_points": points}


# ------------------------------------------------------------------ loading --

def _load(con) -> dict:
    lg = latest_league(con, ["league_id", "season", "name", "current_week", "is_faab"])
    if not lg:
        raise NotSynced("League not synced yet. Run the league sync first.")
    lid, season = lg["league_id"], lg["season"]
    teams = fetch_dicts(con, "SELECT team_id, name, abbrev FROM fantasy_teams WHERE league_id = ? AND season = ? "
                             "ORDER BY team_id", [lid, season])
    types = ", ".join(f"'{t}'" for t in (*TRADE_TYPES, *MOVE_TYPES))
    # One row per item (LEFT JOIN keeps item-less trade rows). Sleeper keeps traded draft picks and FAAB only in the
    # raw payload: counted for trades so the event can say they existed, never listed.
    tx_rows = fetch_dicts(con, f"""
        SELECT t.transaction_id, t.type, t.team_id, t.scoring_period, t.proposed_date, t.process_date, t.bid_amount,
               t.related_transaction_id,
               CASE WHEN t.type IN ('TRADE_ACCEPT', 'TRADE_UPHOLD') AND t.transaction_id LIKE 'sleeper:%'
                    THEN coalesce(json_array_length(json_extract(t.raw, '$.draft_picks')), 0) END AS draft_picks,
               CASE WHEN t.type IN ('TRADE_ACCEPT', 'TRADE_UPHOLD') AND t.transaction_id LIKE 'sleeper:%'
                    THEN coalesce(json_array_length(json_extract(t.raw, '$.waiver_budget')), 0) END AS faab_moved,
               ti.item_index, ti.item_type, ti.espn_player_id, ti.from_team_id, ti.to_team_id
        FROM transactions t
        LEFT JOIN transaction_items ti ON ti.transaction_id = t.transaction_id
                                      AND ti.item_type IN ('ADD', 'DROP', 'TRADE')
        WHERE t.league_id = ? AND t.season = ? AND t.status = 'EXECUTED' AND t.type IN ({types})
          AND (t.type <> 'ROSTER' OR t.transaction_id LIKE 'sleeper:%')
        ORDER BY t.transaction_id, ti.item_index""", [lid, season])
    activity = fetch_dicts(con, """
        SELECT topic_id, topic_date, espn_player_id, from_team_id, to_team_id FROM activity_messages
        WHERE league_id = ? AND season = ? AND message_type_id = 244
        ORDER BY topic_id, message_id""", [lid, season]) if table_exists(con, "activity_messages") else []
    return {"league": lg, "teams": teams, "tx_rows": tx_rows, "activity": activity}


def _players(con, ids: set[int]) -> dict[int, dict]:
    if not ids:
        return {}
    return {r["espn_player_id"]: r for r in fetch_dicts(con, """
        SELECT espn_player_id, full_name, position, pro_team FROM fantasy_players
        WHERE espn_player_id IN (SELECT unnest(?))""", [sorted(ids)])}


def _snapshots(con, lg: dict, player_ids: set[int]) -> tuple[set, dict]:
    cw = lg.get("current_week")
    if not player_ids or cw is None:
        return set(), {}
    args = [lg["league_id"], lg["season"], cw]
    coverage = {(r["team_id"], r["scoring_period"]) for r in fetch_dicts(con, """
        SELECT DISTINCT team_id, scoring_period FROM lineup_snapshots
        WHERE league_id = ? AND season = ? AND scoring_period < ?""", args)}
    snaps = {(r["team_id"], r["espn_player_id"], r["scoring_period"]): r for r in fetch_dicts(con, """
        SELECT team_id, espn_player_id, scoring_period, is_starter, points FROM lineup_snapshots
        WHERE league_id = ? AND season = ? AND scoring_period < ? AND espn_player_id IN (SELECT unnest(?))""",
        [*args, sorted(player_ids)])}
    return coverage, snaps


def build_transactions_feed(con, *, manager: int | None = None, kind: str | None = None,
                            position: str | None = None, week: int | None = None, sort: str = "date",
                            direction: str = "desc", cursor: str | None = None,
                            limit: int = DEFAULT_LIMIT) -> dict:
    data = _load(con)
    lg, teams = data["league"], data["teams"]
    team_ids = {t["team_id"] for t in teams}
    if manager is not None and manager not in team_ids:
        raise UnknownManager(f"No team {manager} in this league.")
    trades, moves = records_from_rows(data["tx_rows"], data["activity"], team_ids)
    events = build_events(trades, moves, is_faab=bool(lg.get("is_faab")))
    players = _players(con, {leg.player_id for e in events for leg in e["legs"] if leg.player_id is not None})
    positions = {pid: p["position"] for pid, p in players.items()}

    matching = filter_events(events, manager=manager, kind=kind, position=position, week=week, positions=positions)
    rows, paging = page(sort_events(matching, sort, direction), cursor, limit)

    incoming = {leg.player_id for e in rows for leg in e["legs"] if leg.to_team_id is not None}
    coverage, snaps = _snapshots(con, lg, incoming)
    names = {t["team_id"]: t for t in teams}

    def team(tid):
        t = names.get(tid)
        return {"team_id": tid, "name": t["name"], "abbrev": t["abbrev"]} if t else None

    def leg_out(e, leg: Leg) -> dict:
        p = players.get(leg.player_id) or {}
        return {"action": leg.action, "espn_player_id": leg.player_id, "full_name": p.get("full_name"),
                "position": p.get("position"), "pro_team": p.get("pro_team"),
                "from_team": team(leg.from_team_id), "to_team": team(leg.to_team_id),
                "after": after_move(e["week"], leg.to_team_id, leg.player_id, current_week=lg.get("current_week"),
                                    coverage=coverage, snaps=snaps) if leg.action in ("add", "trade") else None}

    out_events = [{**e, "processed_at": _iso(e["processed_at"]), "proposed_at": _iso(e["proposed_at"]),
                   "teams": [team(t) for t in e["team_ids"]],
                   "legs": [leg_out(e, leg) for leg in e["legs"]]} for e in rows]
    present_positions = {positions.get(leg.player_id) for e in events for leg in e["legs"]} - {None}
    return {
        "league": {k: lg.get(k) for k in ("league_id", "season", "name", "current_week", "is_faab")},
        "query": {"manager": manager, "type": kind, "position": position, "week": week, "sort": sort,
                  "dir": direction, "cursor": cursor, "limit": limit},
        "options": {
            "managers": [{"team_id": t["team_id"], "name": t["name"], "abbrev": t["abbrev"]} for t in teams],
            "types": [k for k in KINDS if any(e["kind"] == k for e in events)],
            "positions": [p for p in POSITION_ORDER if p in present_positions]
                         + sorted(p for p in present_positions if p not in POSITION_ORDER),
            "weeks": sorted({e["week"] for e in events if e["week"] is not None}),
        },
        "summary": {"total": len(events), "matching": len(matching),
                    "by_type": dict(sorted(Counter(e["kind"] for e in matching).items(),
                                           key=lambda kv: _type_rank(kv[0]))),
                    "undated": sum(1 for e in matching if e["processed_at"] is None and e["proposed_at"] is None),
                    "unplaced_week": sum(1 for e in matching if e["week"] is None)},
        "page": paging,
        "events": out_events,
        "notes": NOTES,
    }
