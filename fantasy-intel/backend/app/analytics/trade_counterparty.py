"""Trade counterparty scouting: what one team's recorded, executed trades look like (issue #215).

Read-only and descriptive. It reads the league's stored transaction rows and activity-feed trade entries through the
league activity feed's own grouping (analytics/transactions_feed.py: one event per trade however the provider stored
it), for the league in scope only. No provider calls, no writes, no trade offers, proposals, vetoes or messages.

Rules, in the order they apply:

  * Sample. Executed trades only. A season counts only when its transactions are stored at all; a season with no
    stored transaction rows and no activity-feed entries is "not recorded" (unknown), never zero trades.
  * Who. Trades are attributed to the team id, not to a person. The current season is always this team's. An earlier
    season stored for the same league counts only when that season's team with this id had exactly the same owners
    as it has now; a changed or unknown owner set leaves that season out, and the response says so. Partners from
    earlier seasons are matched to a current team the same way, or named as that season's team.
  * Patterns. Each one states its numerator, its denominator (what was counted) and the date window. Trade sizes and
    positions count players only (draft picks and FAAB are not stored as legs) over trades with at least one recorded
    player leg for this team; a trade whose legs the provider omitted is counted as a trade, with unknown size,
    positions and partner. Timing uses the scoring week the provider stored; a trade with no stored week is unknown.
  * Suppression. A pattern whose denominator is below MIN_TRADES shows its denominator but no breakdown.
  * Never. No acceptance likelihood, no willingness or personality label, and no reading of absent trades as
    unwillingness: a pattern says what happened in the trades that were recorded, nothing about trades that were not.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime

from ..db.connection import fetch_dicts, table_exists
from .legacy_league import latest_league
from .transactions_feed import build_events, load_season_rows, records_from_rows

MIN_TRADES = 3
POSITIONS = ("QB", "RB", "WR", "TE", "K", "D/ST")
# (label, first week, last week or None for open-ended). A stored week below 1 (preseason) is its own window.
WEEK_WINDOWS = (("Before week 1", None, 0), ("Weeks 1–4", 1, 4), ("Weeks 5–8", 5, 8), ("Weeks 9–12", 9, 12),
                ("Weeks 13+", 13, None))
MAX_TRADES_LISTED = 50

SUPPRESSION_RULE = (f"A breakdown needs at least {MIN_TRADES} trades in its denominator. Below that only the "
                    "denominator is shown: too few trades to describe a pattern.")
NOTES = (
    ("Recorded, executed trades only, from this league's stored transactions and its activity feed's trade entries. "
     "Trade offers, declined or vetoed trades and league messages are not read."),
    ("A pattern describes the trades that were recorded. It is not a likelihood that a trade would be accepted, and "
     "it says nothing about trades that did not happen: few or no trades is not a sign of unwillingness."),
    ("Trades are attributed to the team, not a person. If this team changed managers during a season, that season's "
     "earlier trades were made by the previous manager; the stored data does not record when ownership changed."),
    "Trade sizes and positions count players only. Draft picks and FAAB moved in a trade are not stored as legs.",
    "Positions are each player's current listed position.",
)


class NotSynced(LookupError):
    pass


def _owners(value) -> frozenset[str] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, list):
        return None
    out = frozenset(str(v) for v in value if v not in (None, ""))
    return out or None


def _iso(v: datetime | None) -> str | None:
    return None if v is None else v.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _share(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _recorded(con, league_id: int, season: int) -> bool:
    """Whether any transaction (any type or status) or activity-feed entry is stored for the season."""
    n = con.execute("SELECT count(*) FROM transactions WHERE league_id = ? AND season = ?",
                    [league_id, season]).fetchone()[0]
    if not n and table_exists(con, "activity_messages"):
        n = con.execute("SELECT count(*) FROM activity_messages WHERE league_id = ? AND season = ?",
                        [league_id, season]).fetchone()[0]
    return bool(n)


def _week_window(week: int | None) -> str | None:
    if week is None:
        return None
    for label, lo, hi in WEEK_WINDOWS:
        if (lo is None or week >= lo) and (hi is None or week <= hi):
            return label
    return None


def _pattern(key: str, title: str, denominator: int, denominator_label: str, rows: list[dict], window: dict, *,
             unknown: int = 0, unknown_label: str | None = None) -> dict:
    suppressed = denominator < MIN_TRADES
    return {
        "key": key, "title": title, "denominator": denominator, "denominator_label": denominator_label,
        "suppressed": suppressed,
        "suppressed_reason": (f"{denominator} of the required {MIN_TRADES} trades: too few to describe a pattern."
                              if suppressed else None),
        "rows": [] if suppressed else rows,
        "unknown": unknown, "unknown_label": unknown_label, "window": window,
    }


def build_trade_counterparty(con, team_id: int) -> dict | None:
    """The counterparty report for `team_id` in the league this connection holds; None for an unknown team.
    Raises NotSynced when no league is stored. Callers own the (read-only) connection."""
    lg = latest_league(con, ["league_id", "season", "name", "current_week", "synced_at"])
    if not lg:
        raise NotSynced("League not synced yet. Run the league sync first.")
    lid, season = lg["league_id"], lg["season"]
    team_rows = fetch_dicts(con, """SELECT season, team_id, name, owner_names, owner_ids, is_mine FROM fantasy_teams
        WHERE league_id = ? AND season <= ? ORDER BY season DESC, team_id""", [lid, season])
    by_season: dict[int, dict[int, dict]] = defaultdict(dict)
    for r in team_rows:
        by_season[r["season"]][r["team_id"]] = r
    current = by_season.get(season, {})
    if team_id not in current:
        return None
    me = current[team_id]

    def same_owners(s: int, tid: int) -> bool | None:
        """Whether team `tid` in season `s` had exactly the current owners of team `tid` (None: unknown)."""
        then, now = by_season[s].get(tid), current.get(tid)
        if then is None or now is None:
            return False
        a, b = _owners(then["owner_ids"]), _owners(now["owner_ids"])
        if a is None or b is None:
            return None
        return a == b

    seasons, events = [], []
    for s in sorted(by_season, reverse=True):
        status, reason = "included", None
        if s != season:
            same = same_owners(s, team_id)
            if same is None:
                status, reason = "excluded", ("Owner records are missing for this team in one of the seasons, so "
                                              "its trades cannot be attributed to the current manager.")
            elif not same:
                status, reason = "excluded", ("This team id had different owners that season (or did not exist): "
                                              "its trades belong to another manager.")
        if not _recorded(con, lid, s):
            seasons.append({"season": s, "status": "not_recorded", "team_trades": None, "league_trades": None,
                            "reason": "No transactions are stored for this season: trades are unknown, not zero."})
            continue
        rows = load_season_rows(con, lid, s)
        teams = {t["team_id"] for t in rows["teams"]}
        trades, _moves = records_from_rows(rows["tx_rows"], rows["activity"], teams)
        season_events = build_events(trades, [], is_faab=False)
        mine = [e for e in season_events if team_id in e["team_ids"]]
        seasons.append({"season": s, "status": status, "team_trades": len(mine),
                        "league_trades": len(season_events), "reason": reason})
        if status == "included":
            events += [(s, e) for e in mine]

    included = [x for x in seasons if x["status"] == "included"]
    recorded = bool(included)
    events.sort(key=lambda se: (se[1]["processed_at"] or se[1]["proposed_at"] or datetime.min, se[1]["event_id"]),
                reverse=True)

    player_ids = {leg.player_id for _, e in events for leg in e["legs"] if leg.player_id is not None}
    players = {r["espn_player_id"]: r for r in fetch_dicts(con, """
        SELECT espn_player_id, full_name, position FROM fantasy_players
        WHERE espn_player_id IN (SELECT unnest(?))""", [sorted(player_ids)])} if player_ids else {}

    def partner_key(s: int, tid: int) -> tuple:
        """A partner as a current team when the same owners held it, else as that season's team."""
        if s == season or same_owners(s, tid):
            return ("current", tid)
        return (s, tid)

    def partner_out(key: tuple) -> dict:
        s, tid = key
        if s == "current":
            t = current.get(tid) or {}
            return {"team_id": tid, "season": None, "name": t.get("name"), "matched_to_current_team": True}
        t = by_season.get(s, {}).get(tid) or {}
        return {"team_id": tid, "season": s, "name": t.get("name"), "matched_to_current_team": False}

    trades_out = []
    size, sent_pos, got_pos, sent_players, got_players = Counter(), Counter(), Counter(), Counter(), Counter()
    weeks, by_season_count, partners = Counter(), Counter(), Counter()
    with_legs = unknown_week = unknown_partner = multi_team = unlisted = 0
    sent_total = got_total = 0
    sent_unknown, got_unknown = [0], [0]     # players with no listed position
    dates = []
    for s, e in events:
        legs = [leg for leg in e["legs"] if leg.action == "trade"]
        sent = [leg for leg in legs if leg.from_team_id == team_id]
        got = [leg for leg in legs if leg.to_team_id == team_id]
        others = set(e["team_ids"]) - {team_id}
        when = e["processed_at"] or e["proposed_at"]
        if when is not None:
            dates.append(when)
        by_season_count[s] += 1
        if len(e["team_ids"]) > 2:
            multi_team += 1
        if e["unlisted_assets"]:
            unlisted += 1
        if others:
            for p in others:
                partners[partner_key(s, p)] += 1
        else:
            unknown_partner += 1
        wk = _week_window(e["week"])
        if wk is None:
            unknown_week += 1
        else:
            weeks[wk] += 1
        shape = None
        if sent or got:
            with_legs += 1
            shape = f"{len(sent)}-for-{len(got)}"
            size[shape] += 1
            for side, pos_c, player_c, unknown_c in ((sent, sent_pos, sent_players, sent_unknown),
                                                     (got, got_pos, got_players, got_unknown)):
                seen = set()
                for leg in side:
                    pos = (players.get(leg.player_id) or {}).get("position")
                    if pos is None:
                        unknown_c[0] += 1
                        continue
                    player_c[pos] += 1
                    seen.add(pos)
                for pos in seen:
                    pos_c[pos] += 1
            sent_total += len(sent)
            got_total += len(got)

        def leg_out(leg) -> dict:
            p = players.get(leg.player_id) or {}
            return {"espn_player_id": leg.player_id, "name": p.get("full_name"), "position": p.get("position"),
                    "from_team_id": leg.from_team_id, "to_team_id": leg.to_team_id}
        trades_out.append({
            "event_id": e["event_id"], "season": s, "week": e["week"], "processed_at": _iso(e["processed_at"]),
            "proposed_at": _iso(e["proposed_at"]), "shape": shape,
            "partners": [partner_out(partner_key(s, p)) for p in sorted(others)],
            "players_sent": [leg_out(leg) for leg in sent], "players_received": [leg_out(leg) for leg in got],
            "legs_recorded": bool(sent or got), "unlisted_assets": e["unlisted_assets"], "sources": e["sources"],
        })

    n = len(events)
    window = {
        "seasons": [x["season"] for x in included],
        "first_trade_at": _iso(min(dates)) if dates else None,
        "last_trade_at": _iso(max(dates)) if dates else None,
        "through": lg["synced_at"],
        "text": _window_text([x["season"] for x in included], dates),
    }

    def rows_from(counter: Counter, labels, denominator: int, extra=None) -> list[dict]:
        out = []
        for label in labels:
            if counter[label]:
                row = {"label": label, "numerator": counter[label], "denominator": denominator,
                       "share": _share(counter[label], denominator)}
                if extra:
                    row.update(extra(label))
                out.append(row)
        return out

    shapes = sorted(size, key=lambda k: (-size[k], k))
    position_labels = list(POSITIONS) + sorted((set(sent_pos) | set(got_pos)) - set(POSITIONS))
    partner_keys = sorted(partners, key=lambda k: (-partners[k], str(k[0]), k[1]))
    partner_rows = []
    for k in partner_keys:
        partner_rows.append({"label": partner_out(k)["name"] or f"Team {k[1]}", **partner_out(k),
                             "numerator": partners[k], "denominator": n - unknown_partner,
                             "share": _share(partners[k], n - unknown_partner), "recurring": partners[k] >= 2})
    patterns = [
        _pattern("trade_size", "Trade sizes (players sent-for-received)", with_legs,
                 "trades with a recorded player leg for this team", rows_from(size, shapes, with_legs), window,
                 unknown=n - with_legs, unknown_label="trades whose player legs were not recorded"),
        _pattern("positions_sent", "Positions sent", with_legs, "trades with a recorded player leg for this team",
                 rows_from(sent_pos, position_labels, with_legs,
                           lambda p: {"players": sent_players[p], "players_denominator": sent_total}), window,
                 unknown=sent_unknown[0], unknown_label="players sent with no listed position"),
        _pattern("positions_received", "Positions received", with_legs,
                 "trades with a recorded player leg for this team",
                 rows_from(got_pos, position_labels, with_legs,
                           lambda p: {"players": got_players[p], "players_denominator": got_total}), window,
                 unknown=got_unknown[0], unknown_label="players received with no listed position"),
        _pattern("timing_week", "Timing by scoring week", n - unknown_week, "trades with a stored scoring week",
                 rows_from(weeks, [w[0] for w in WEEK_WINDOWS], n - unknown_week), window,
                 unknown=unknown_week, unknown_label="trades with no stored scoring week"),
        _pattern("partners", "Trade partners", n - unknown_partner, "trades with a recorded partner team",
                 partner_rows, window, unknown=unknown_partner, unknown_label="trades with no recorded partner"),
    ]
    if len(included) > 1:
        patterns.append(_pattern(
            "timing_season", "Trades by season", n, "trades in the included seasons",
            [{"label": str(x["season"]), "numerator": by_season_count[x["season"]], "denominator": n,
              "share": _share(by_season_count[x["season"]], n), "league_trades": x["league_trades"]}
             for x in included if by_season_count[x["season"]]], window))
    league_trades = sum(x["league_trades"] for x in included) if recorded else None
    discovery = sorted({leg["position"] for t in trades_out for leg in t["players_sent"]
                        if leg["position"] in POSITIONS}, key=POSITIONS.index)
    return {
        "league": {k: lg[k] for k in ("league_id", "season", "name", "current_week", "synced_at")},
        "team": {"team_id": team_id, "name": me["name"], "owner_names": me["owner_names"],
                 "is_mine": bool(me["is_mine"])},
        "status": "recorded" if recorded else "not_recorded",
        "window": window,
        "min_trades": MIN_TRADES,
        "suppression_rule": SUPPRESSION_RULE,
        "summary": {
            "trades": n if recorded else None, "league_trades": league_trades,
            "trades_with_player_legs": with_legs if recorded else None,
            "trades_without_player_legs": n - with_legs if recorded else None,
            "multi_team_trades": multi_team if recorded else None,
            "trades_with_unlisted_assets": unlisted if recorded else None,
            "players_sent": sent_total if recorded else None, "players_received": got_total if recorded else None,
            "recurring_partners": sum(1 for r in partner_rows if r["recurring"]) if recorded else None,
        },
        "patterns": patterns if recorded else [],
        "seasons": seasons,
        "trades": trades_out[:MAX_TRADES_LISTED],
        "trades_listed": min(n, MAX_TRADES_LISTED),
        "discovery": {"opponent_team_id": team_id, "positions_sent": [] if with_legs < MIN_TRADES else discovery},
        "notes": list(NOTES),
    }


def _window_text(seasons: list[int], dates: list[datetime]) -> str:
    if not seasons:
        return "No season with stored transactions."
    span = f"season {seasons[0]}" if len(seasons) == 1 else f"seasons {min(seasons)}–{max(seasons)}"
    if not dates:
        return f"{span.capitalize()}; no dated trade."
    a, b = min(dates).date().isoformat(), max(dates).date().isoformat()
    return f"{span.capitalize()}; trades dated {a}" + ("" if a == b else f" to {b}") + "."
