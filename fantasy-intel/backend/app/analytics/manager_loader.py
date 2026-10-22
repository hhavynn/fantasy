"""Loads manager-engine inputs from DuckDB (keeps the engine itself pure)."""

from __future__ import annotations

import json
from collections import defaultdict

from ..db.connection import fetch_dicts, replace_where, utcnow
from .availability import latest_injuries
from ..espn.constants import IR_SLOT
from .manager_metrics import ManagerInputs, classify_metrics, compute_metrics
from .reserve_slots import active_capacity

UNAVAILABLE_NFL = {"Out", "Doubtful"}


def load_league(con) -> dict | None:
    rows = fetch_dicts(con, "SELECT * FROM leagues ORDER BY synced_at DESC LIMIT 1")
    if not rows:
        return None
    lg = rows[0]
    lg["roster_slots"] = json.loads(lg["roster_slots"]) if isinstance(lg["roster_slots"], str) else lg["roster_slots"]
    return lg


def _adds_drops(con, league_id: int, season: int) -> tuple[list[dict], list[dict], str]:
    items = fetch_dicts(con, """
        SELECT ti.item_type, ti.espn_player_id, coalesce(ti.to_team_id, t.team_id) AS to_team,
               coalesce(ti.from_team_id, t.team_id) AS from_team, coalesce(t.process_date, t.proposed_date) AS date,
               t.scoring_period AS period, t.type AS via, t.bid_amount, t.transaction_id, fp.position
        FROM transaction_items ti JOIN transactions t USING (transaction_id)
        LEFT JOIN fantasy_players fp ON fp.espn_player_id = ti.espn_player_id
        WHERE t.league_id = ? AND t.season = ? AND t.status = 'EXECUTED'
          AND t.type IN ('FREEAGENT', 'WAIVER') AND ti.item_type IN ('ADD', 'DROP')
    """, [league_id, season])
    if items:
        adds = [{"team_id": r["to_team"], "espn_player_id": r["espn_player_id"], "position": r["position"],
                 "date": r["date"], "period": r["period"], "via": r["via"], "bid": r["bid_amount"],
                 "transaction_id": r["transaction_id"]} for r in items if r["item_type"] == "ADD"]
        drops = [{"team_id": r["from_team"], "espn_player_id": r["espn_player_id"], "position": r["position"],
                  "date": r["date"], "period": r["period"], "transaction_id": r["transaction_id"]}
                 for r in items if r["item_type"] == "DROP"]
        return adds, drops, "transactions (mTransactions2)"
    msgs = fetch_dicts(con, """
        SELECT a.*, fp.position FROM activity_messages a
        LEFT JOIN fantasy_players fp ON fp.espn_player_id = a.espn_player_id
        WHERE a.league_id = ? AND a.season = ? AND a.team_id IS NOT NULL
    """, [league_id, season])
    adds = [{"team_id": m["team_id"], "espn_player_id": m["espn_player_id"], "position": m["position"],
             "date": m["topic_date"], "period": None, "via": m["action"], "bid": m["bid_amount"],
             "transaction_id": m["topic_id"]} for m in msgs if m["message_type_id"] in (178, 180)]
    drops = [{"team_id": m["team_id"], "espn_player_id": m["espn_player_id"], "position": m["position"],
              "date": m["topic_date"], "period": None, "transaction_id": m["topic_id"]}
             for m in msgs if m["message_type_id"] in (179, 181, 239)]
    return adds, drops, "activity feed (fallback)"


TRADE_DEDUP_DAYS = 3


def merge_trades(activity_legs: list[dict], transaction_legs: list[dict]) -> list[dict]:
    """Union trade evidence from the activity feed and mTransactions2.

    Each source is grouped into trades (activity: topic_id, transactions:
    transaction_id). Two trades are the same if they move the identical set of
    (player, from_team, to_team) legs within TRADE_DEDUP_DAYS; the merged trade
    keeps every source id. Trades whose transaction rows carry no legs (ESPN
    often omits other teams' trade items) contribute nothing and are skipped.
    """
    def group(legs, key, source):
        out: dict[str, dict] = {}
        for leg in legs:
            t = out.setdefault(str(leg[key]), {"date": leg.get("date"), "legs": [], "sources": [source],
                                                "source_ids": {source: [str(leg[key])]}})
            t["legs"].append({k: leg[k] for k in ("espn_player_id", "from_team_id", "to_team_id")})
        return list(out.values())

    def legset(t):
        return frozenset((leg["espn_player_id"], leg["from_team_id"], leg["to_team_id"]) for leg in t["legs"])

    def close(a, b):
        if a is None or b is None:
            return True
        return abs((a - b).total_seconds()) <= TRADE_DEDUP_DAYS * 86400

    merged = group(activity_legs, "topic_id", "activity")
    for t in group(transaction_legs, "transaction_id", "transactions"):
        match = next((m for m in merged if legset(m) == legset(t) and close(m["date"], t["date"])), None)
        if match is None:
            merged.append(t)
            continue
        for src, ids in t["source_ids"].items():
            match["source_ids"].setdefault(src, [])
            match["source_ids"][src] += [i for i in ids if i not in match["source_ids"][src]]
            if src not in match["sources"]:
                match["sources"].append(src)
    for t in merged:
        t["trade_id"] = "+".join(f"{s}:{','.join(v)}" for s, v in sorted(t["source_ids"].items()))
    return merged


def load_trades(con, league_id: int, season: int) -> list[dict]:
    activity = fetch_dicts(con, """
        SELECT topic_id, topic_date AS date, espn_player_id, from_team_id, to_team_id FROM activity_messages
        WHERE league_id = ? AND season = ? AND message_type_id = 244
    """, [league_id, season])
    transactions = fetch_dicts(con, """
        SELECT t.transaction_id, coalesce(t.process_date, t.proposed_date) AS date, ti.espn_player_id,
               ti.from_team_id, ti.to_team_id
        FROM transactions t JOIN transaction_items ti USING (transaction_id)
        WHERE t.league_id = ? AND t.season = ? AND t.status = 'EXECUTED'
          AND t.type IN ('TRADE_ACCEPT', 'TRADE_UPHOLD') AND ti.item_type = 'TRADE'
    """, [league_id, season])
    return merge_trades(activity, transactions)


def load_inputs(con) -> tuple[ManagerInputs, str] | None:
    lg = load_league(con)
    if lg is None:
        return None
    league_id, season = lg["league_id"], lg["season"]
    teams = fetch_dicts(con, "SELECT * FROM fantasy_teams WHERE league_id = ? AND season = ?", [league_id, season])
    adds, drops, source = _adds_drops(con, league_id, season)
    lineups = fetch_dicts(con, """
        SELECT l.scoring_period AS period, l.team_id, l.espn_player_id, fp.position, l.lineup_slot_id, l.is_starter
        FROM lineup_snapshots l LEFT JOIN fantasy_players fp USING (espn_player_id)
        WHERE l.league_id = ? AND l.season = ?
    """, [league_id, season])

    gsis = {r["espn_player_id"]: r["gsis_id"] for r in fetch_dicts(
        con, "SELECT espn_player_id, gsis_id FROM player_id_map WHERE gsis_id IS NOT NULL")}
    rookie_season = {r["gsis_id"]: r["rookie_season"] for r in fetch_dicts(
        con, "SELECT gsis_id, rookie_season FROM nfl_players")}
    # Newest report per player-week (date_modified when available; see availability.py).
    injuries: dict[tuple, str] = {k: r["report_status"] for k, r in latest_injuries(con, season, per_week=True).items()
                                  if r["report_status"]}

    roster_by: dict[tuple, list[dict]] = defaultdict(list)
    for r in lineups:
        roster_by[(r["team_id"], r["period"])].append(r)

    for a in adds:
        g = gsis.get(a["espn_player_id"])
        if a.get("position") == "D/ST":
            a["is_rookie"] = None
        elif g and rookie_season.get(g) is not None:
            a["is_rookie"] = rookie_season[g] == season
        else:
            a["is_rookie"] = None
        a["injury_context"] = injury_context(a, roster_by, gsis, injuries)

    failed = fetch_dicts(con, """
        SELECT team_id, scoring_period AS period FROM transactions
        WHERE league_id = ? AND season = ? AND type IN ('WAIVER', 'WAIVER_ERROR') AND status LIKE 'FAILED%'
    """, [league_id, season])
    slots = lg["roster_slots"] or {}
    return ManagerInputs(
        league_id=league_id, season=season, current_week=lg["current_week"] or 1,
        roster_spots=active_capacity(slots) or 16,           # IR and taxi slots are not active roster spots
        is_faab=bool(lg["is_faab"]), faab_budget=lg["faab_budget"],
        teams=teams, adds=adds, drops=drops, trades=load_trades(con, league_id, season),
        lineups=lineups, failed_claims=failed, failed_claims_visible=bool(failed), now=utcnow(),
    ), source


def injury_context(add: dict, roster_by: dict, gsis: dict, injuries: dict) -> bool | None:
    """Was a same-position teammate Out/Doubtful (NFL report) or in an IR slot that week?"""
    period, team, pos = add.get("period"), add["team_id"], add.get("position")
    if period is None or pos is None:
        return None
    roster = roster_by.get((team, period)) or roster_by.get((team, period - 1))
    if not roster:
        return None
    for r in roster:
        if r["espn_player_id"] == add["espn_player_id"] or r.get("position") != pos:
            continue
        if r["lineup_slot_id"] == IR_SLOT:
            return True
        g = gsis.get(r["espn_player_id"])
        if g and injuries.get((g, period)) in UNAVAILABLE_NFL:
            return True
    return False


def build_manager_metrics(con) -> dict:
    loaded = load_inputs(con)
    if loaded is None:
        return {"skipped": "no ESPN league synced"}
    inp, source = loaded
    now = utcnow()
    metrics = compute_metrics(inp)
    for r in metrics:
        r["computed_at"] = now
    classes = classify_metrics(metrics)
    for r in classes:
        r["computed_at"] = now
    where, params = "league_id = ? AND season = ?", [inp.league_id, inp.season]
    replace_where(con, "manager_metrics", metrics, where, params)
    replace_where(con, "manager_classifications", classes, where, params)
    return {"metrics": len(metrics), "classifications": len(classes), "adds": len(inp.adds),
            "drops": len(inp.drops), "trades": len(inp.trades), "transaction_source": source,
            "failed_claims_visible": inp.failed_claims_visible}
