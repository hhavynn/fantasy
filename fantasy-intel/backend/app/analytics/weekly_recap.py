"""Weekly League Recap (read-only, historical): one completed matchup period's scoreboard, awards,
standout player scores, league activity and the standings as they stood after that period.
See docs/weekly_recap.md.

Rules:
  * Only completed matchups count: ESPN winner HOME/AWAY/TIE (playoff_race.FINAL_WINNERS). An
    UNDECIDED matchup is never a result, even with live points.
  * A matchup period's scoring weeks come from playoff_race.period_weeks (multi-week periods take
    player points and transactions from every week they span).
  * Awards compare matchup-period totals. Ties keep every co-winner, sorted by team or matchup id.
    No Biggest Upset award: stored projections were synced after the games, so authentic pregame
    expectations do not exist for past weeks.
  * Player points are ESPN's league-scored actual_points. A missing value stays None (never 0); in a
    multi-week period a player's total needs every week, otherwise it is unknown.
  * Standings use completed regular-season matchups through the selected period only (no future
    leakage), ordered by win percentage, then points for, then team id (a display order, not ESPN's
    tiebreak).
  * Activity counts executed adds, drops and trades. A trade's period is its ESPN transaction's
    scoring period; a trade seen only in the activity feed is placed in the one period whose executed
    transactions span its date, otherwise it is left unplaced and counted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from ..db.connection import fetch_dicts, table_exists
from .manager_loader import load_trades
from .playoff_race import FINAL_WINNERS, is_regular, period_weeks

STANDOUT_LIMIT = 10
SIDE_LIMIT = 5                       # bench and unrostered standout lists
ADD_DROP_TYPES = ("FREEAGENT", "WAIVER", "ROSTER", "TRADE_ACCEPT")
SPAN_TYPES = ("FREEAGENT", "WAIVER", "ROSTER", "TRADE_ACCEPT", "TRADE_UPHOLD")

UPSET_OMITTED = ("Authentic pregame projected matchup scores are unavailable. Stored projection timestamps "
                 "indicate post-hoc syncs; to protect data authenticity, this award is omitted.")
STANDINGS_RULE = ("Standings count completed regular-season matchups through the selected period only. Order: win "
                  "percentage ((wins + half of ties) / games), then points for, then team id. A display order, not "
                  "ESPN's playoff tiebreak.")
AWARD_RULE = ("Awards compare matchup-period totals from completed matchups (ESPN winner home, away or tie). Margins "
              "are compared to the hundredth of a point; equal values share the award.")
PLAYER_RULE = ("Player points are ESPN's league-scored actual points for the period's scoring weeks. Lineup status "
               "comes from the stored roster snapshot of each week; a player on no snapshot is unrostered. A missing "
               "value is unknown, not zero.")
TRADE_RULE = ("A trade belongs to the scoring period of its ESPN transaction. Trades seen only in the league activity "
              "feed are placed in the single period whose executed transactions span the trade's date; otherwise they "
              "are left unplaced.")


class NotSynced(LookupError):
    """No ESPN league or teams."""


class NotFound(LookupError):
    """The requested week has no completed matchups."""


def _json(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def _r(x: float | None, n: int = 2) -> float | None:
    return None if x is None else round(x + 0.0, n) + 0.0


@dataclass
class RecapData:
    """Everything one recap needs, loaded by load_recap_data or built directly in tests."""
    league: dict                        # league_id season name reg_season_matchups final_scoring_period
    schedule_settings: dict
    teams: list[dict]                   # team_id name abbrev wins losses ties (ESPN's current record)
    matchups: list[dict]                # matchups rows
    week: int | None = None             # selected matchup period (None: no completed period)
    player_points: list[dict] = field(default_factory=list)   # espn_player_id scoring_period actual_points
    players: dict[int, dict] = field(default_factory=dict)    # id -> full_name position pro_team
    lineups: list[dict] = field(default_factory=list)         # scoring_period team_id espn_player_id is_starter lineup_slot
    moves: list[dict] = field(default_factory=list)           # executed ADD/DROP items in the period's weeks
    trades: list[dict] = field(default_factory=list)          # merged trades (manager_loader.load_trades) + scoring_period
    period_spans: dict[int, tuple] = field(default_factory=dict)  # scoring period -> (first, last) executed date


# ------------------------------------------------------------------ periods --

def is_final(m: dict) -> bool:
    return m.get("winner") in FINAL_WINNERS


def completed_periods(matchups: list[dict]) -> list[int]:
    """Matchup periods with at least one completed two-team matchup, ascending."""
    return sorted({m["matchup_period"] for m in matchups if m.get("matchup_period") is not None and is_final(m)
                   and m.get("home_team_id") is not None and m.get("away_team_id") is not None})


def resolve_week(matchups: list[dict], week: int | None) -> int | None:
    available = completed_periods(matchups)
    if week is None:
        return available[-1] if available else None
    if week not in available:
        raise NotFound(f"No completed matchups in week {week}. Completed weeks: "
                       f"{', '.join(map(str, available)) or 'none yet'}.")
    return week


def place_trade(when: datetime | None, spans: dict[int, tuple]) -> int | None:
    """The one scoring period whose executed transactions span `when`, else None."""
    if when is None:
        return None
    hits = [p for p, (lo, hi) in spans.items() if lo is not None and hi is not None and lo <= when <= hi]
    return hits[0] if len(hits) == 1 else None


# ------------------------------------------------------------------- loading --

def load_recap_data(con, week: int | None = None) -> RecapData:
    lgs = fetch_dicts(con, """SELECT league_id, season, name, reg_season_matchups, final_scoring_period, settings_raw
                              FROM leagues ORDER BY synced_at DESC LIMIT 1""")
    if not lgs:
        raise NotSynced("ESPN league not synced yet. Run scripts/sync_espn.py.")
    lg = lgs[0]
    sched = _json(lg.pop("settings_raw")).get("scheduleSettings") or {}
    lid, season = lg["league_id"], lg["season"]
    teams = fetch_dicts(con, """SELECT team_id, name, abbrev, wins, losses, ties FROM fantasy_teams
                                WHERE league_id = ? AND season = ? ORDER BY team_id""", [lid, season])
    if not teams:
        raise NotSynced("No fantasy teams synced for the league.")
    matchups = fetch_dicts(con, """SELECT matchup_id, matchup_period, home_team_id, away_team_id, home_points,
                                          away_points, winner, playoff_tier FROM matchups
                                   WHERE league_id = ? AND season = ? ORDER BY matchup_period, matchup_id""",
                           [lid, season]) if table_exists(con, "matchups") else []
    data = RecapData(league=lg, schedule_settings=sched, teams=teams, matchups=matchups,
                     week=resolve_week(matchups, week))
    weeks = period_weeks(sched, data.week) if data.week is not None else None
    if not weeks:
        return data
    marks = ", ".join("?" * len(weeks))
    data.player_points = fetch_dicts(con, f"""SELECT espn_player_id, scoring_period, actual_points
                                              FROM espn_player_weekly_points
                                              WHERE season = ? AND scoring_period IN ({marks})""", [season, *weeks])
    data.lineups = fetch_dicts(con, f"""SELECT scoring_period, team_id, espn_player_id, is_starter, lineup_slot
                                        FROM lineup_snapshots WHERE league_id = ? AND season = ?
                                          AND scoring_period IN ({marks})""", [lid, season, *weeks])
    types = ", ".join(f"'{t}'" for t in ADD_DROP_TYPES)
    data.moves = fetch_dicts(con, f"""
        SELECT t.transaction_id, t.type, t.scoring_period, t.bid_amount,
               coalesce(t.process_date, t.proposed_date) AS processed_at,
               ti.item_index, ti.item_type, ti.espn_player_id,
               CASE WHEN ti.item_type = 'DROP' THEN coalesce(ti.from_team_id, t.team_id)
                    ELSE coalesce(ti.to_team_id, t.team_id) END AS team_id
        FROM transactions t JOIN transaction_items ti USING (transaction_id)
        WHERE t.league_id = ? AND t.season = ? AND t.status = 'EXECUTED' AND t.type IN ({types})
          AND ti.item_type IN ('ADD', 'DROP') AND t.scoring_period IN ({marks})
        ORDER BY processed_at NULLS LAST, t.transaction_id, ti.item_index""", [lid, season, *weeks])
    span_types = ", ".join(f"'{t}'" for t in SPAN_TYPES)
    data.period_spans = {r["scoring_period"]: (r["lo"], r["hi"]) for r in fetch_dicts(con, f"""
        SELECT scoring_period, min(coalesce(process_date, proposed_date)) AS lo,
               max(coalesce(process_date, proposed_date)) AS hi
        FROM transactions WHERE league_id = ? AND season = ? AND status = 'EXECUTED' AND type IN ({span_types})
          AND scoring_period BETWEEN 1 AND ? GROUP BY 1""", [lid, season, lg["final_scoring_period"] or 99])}
    trade_periods = {r["transaction_id"]: r["scoring_period"] for r in fetch_dicts(
        con, """SELECT transaction_id, scoring_period FROM transactions
                WHERE league_id = ? AND season = ? AND type IN ('TRADE_ACCEPT', 'TRADE_UPHOLD')""", [lid, season])}
    for t in load_trades(con, lid, season):
        known = [trade_periods[i] for i in t["source_ids"].get("transactions", []) if trade_periods.get(i) is not None]
        t["scoring_period"] = known[0] if known else None
        data.trades.append(t)
    ids = {r["espn_player_id"] for r in data.player_points} | {m["espn_player_id"] for m in data.moves} \
        | {leg["espn_player_id"] for t in data.trades for leg in t["legs"]}
    if ids:
        data.players = {r["espn_player_id"]: r for r in fetch_dicts(
            con, f"""SELECT espn_player_id, full_name, position, pro_team FROM fantasy_players
                     WHERE espn_player_id IN ({", ".join("?" * len(ids))})""", sorted(ids))}
    return data


# ------------------------------------------------------------------ matchups --

def _team(names: dict[int, dict], tid: int, points: float | None) -> dict:
    t = names.get(tid) or {}
    return {"team_id": tid, "name": t.get("name") or f"Team {tid}", "abbrev": t.get("abbrev"), "points": _r(points)}


def scoreboard(data: RecapData) -> tuple[list[dict], dict]:
    """(completed two-team matchups of the selected period, counts of rows left out)."""
    names = {t["team_id"]: t for t in data.teams}
    rows, notes = [], {"pending_matchups": 0, "byes": 0, "missing_scores": 0}
    for m in data.matchups:
        if m["matchup_period"] != data.week:
            continue
        if m.get("home_team_id") is None or m.get("away_team_id") is None:
            notes["byes"] += 1
            continue
        if not is_final(m):
            notes["pending_matchups"] += 1
            continue
        hp, ap = m["home_points"], m["away_points"]
        if hp is None or ap is None:
            notes["missing_scores"] += 1
        rows.append({"matchup_id": m["matchup_id"], "matchup_period": m["matchup_period"],
                     "playoff_tier": None if m.get("playoff_tier") in (None, "NONE") else m["playoff_tier"],
                     "home_team": _team(names, m["home_team_id"], hp), "away_team": _team(names, m["away_team_id"], ap),
                     "winner": m["winner"], "is_tie": m["winner"] == "TIE",
                     "margin": None if hp is None or ap is None else _r(abs(hp - ap))})
    rows.sort(key=lambda r: r["matchup_id"])
    return rows, notes


def _sides(m: dict) -> tuple[dict, dict]:
    """(winner, loser) of a decided matchup; home first for a tie."""
    return (m["away_team"], m["home_team"]) if m["winner"] == "AWAY" else (m["home_team"], m["away_team"])


def _game(m: dict) -> dict:
    w, l_ = _sides(m)
    return {"matchup_id": m["matchup_id"], "winner_team_id": w["team_id"], "winner_name": w["name"],
            "loser_team_id": l_["team_id"], "loser_name": l_["name"], "margin": m["margin"], "is_tie": m["is_tie"],
            "score_summary": f"{w['points']:.2f} - {l_['points']:.2f}"}


def _award(name: str, description: str, winners: list[dict]) -> dict:
    return {"award_name": name, "description": description, "winners": winners, "is_tied": len(winners) > 1}


def _best(items: list, value, *, high: bool, key) -> list:
    if not items:
        return []
    target = (max if high else min)(value(x) for x in items)
    return sorted((x for x in items if value(x) == target), key=key)


def awards(games: list[dict]) -> dict:
    scored = [g for g in games if g["margin"] is not None]
    teams = [t for g in scored for t in (g["home_team"], g["away_team"])]
    decided = [g for g in scored if not g["is_tie"]]
    losers = [_sides(g)[1] for g in decided]
    by_team = lambda t: t["team_id"]  # noqa: E731
    by_game = lambda g: g["matchup_id"]  # noqa: E731
    return {
        "highest_score": _award("Highest scorer", "Top-scoring team of the week",
                                _best(teams, lambda t: t["points"], high=True, key=by_team)),
        "biggest_blowout": _award("Biggest blowout", "Largest margin of victory",
                                  [_game(g) for g in _best(decided, lambda g: g["margin"], high=True, key=by_game)]),
        "closest_matchup": _award("Closest matchup", "Narrowest margin, ties included",
                                  [_game(g) for g in _best(scored, lambda g: g["margin"], high=False, key=by_game)]),
        "highest_scoring_loser": _award("Tough luck", "Highest score in a losing effort",
                                        _best(losers, lambda t: t["points"], high=True, key=by_team)),
        "biggest_upset": {**_award("Biggest upset", "Underdog win against pregame projections", []),
                          "omitted_reason": UPSET_OMITTED},
    }


# ------------------------------------------------------------------ players --

def _status(rows: list[dict]) -> str:
    if not rows:
        return "UNROSTERED"
    if any(r["is_starter"] for r in rows):
        return "STARTER"
    return "IR" if all(r.get("lineup_slot") == "IR" for r in rows) else "BENCH"


def player_scores(data: RecapData, weeks: list[int]) -> tuple[list[dict], dict]:
    """(every player with a complete period total, coverage counts). A multi-week player whose team or
    lineup status differs between weeks is reported as MIXED with no single team."""
    names = {t["team_id"]: t["name"] for t in data.teams}
    pts: dict[int, dict[int, float | None]] = {}
    for r in data.player_points:
        pts.setdefault(r["espn_player_id"], {})[r["scoring_period"]] = r["actual_points"]
    roster: dict[int, dict[int, list[dict]]] = {}
    for r in data.lineups:
        roster.setdefault(r["espn_player_id"], {}).setdefault(r["scoring_period"], []).append(r)
    out, missing_rostered, partial = [], 0, 0
    for pid in sorted(set(pts) | set(roster)):
        by_week = pts.get(pid, {})
        vals = [by_week.get(w) for w in weeks]
        if any(v is None for v in vals):
            if pid in roster:
                missing_rostered += 1
            if any(v is not None for v in vals):
                partial += 1
            continue
        statuses = {w: _status(roster.get(pid, {}).get(w, [])) for w in weeks}
        teams = {r["team_id"] for w in weeks for r in roster.get(pid, {}).get(w, [])}
        status = next(iter(statuses.values())) if len(set(statuses.values())) == 1 else "MIXED"
        team = next(iter(teams)) if len(teams) == 1 and status != "MIXED" else None
        p = data.players.get(pid) or {}
        out.append({"espn_player_id": pid, "full_name": p.get("full_name") or f"Player {pid}",
                    "position": p.get("position"), "pro_team": p.get("pro_team"), "points": _r(sum(vals)),
                    "fantasy_team_id": team, "fantasy_team_name": names.get(team) if team is not None else None,
                    "lineup_status": status})
    out.sort(key=lambda r: (-r["points"], r["espn_player_id"]))
    return out, {"rostered_without_points": missing_rostered, "partial_period_players": partial,
                 "lineup_snapshot_weeks": sorted({r["scoring_period"] for r in data.lineups})}


# ------------------------------------------------------------------ activity --

def activity(data: RecapData, weeks: list[int]) -> dict:
    names = {t["team_id"]: t["name"] for t in data.teams}

    def player(pid):
        p = data.players.get(pid) or {}
        return {"player_id": pid, "player_name": p.get("full_name") or f"Player {pid}", "position": p.get("position")}

    adds, drops = [], []
    for m in data.moves:
        row = {**player(m["espn_player_id"]), "team_id": m["team_id"], "team_name": names.get(m["team_id"]),
               "type": m["type"], "processed_at": m["processed_at"], "scoring_period": m["scoring_period"]}
        if m["item_type"] == "ADD":
            adds.append({**row, "bid_amount": m["bid_amount"] if m["type"] == "WAIVER" else None})
        else:
            drops.append(row)
    trades, unplaced = [], 0
    for t in data.trades:
        period = t.get("scoring_period")
        placed_by = "transaction" if period is not None else None
        if period is None:
            period = place_trade(t.get("date"), data.period_spans)
            placed_by = "activity_date" if period is not None else None
        if period is None:
            unplaced += 1
            continue
        if period not in weeks:
            continue
        sides: dict[int, list] = {}
        for leg in sorted(t["legs"], key=lambda x: (x["to_team_id"] or 0, x["espn_player_id"])):
            sides.setdefault(leg["to_team_id"], []).append(player(leg["espn_player_id"]))
        trades.append({"trade_id": t["trade_id"], "processed_at": t.get("date"), "scoring_period": period,
                       "placed_by": placed_by,
                       "sides": [{"team_id": tid, "team_name": names.get(tid), "received": got}
                                 for tid, got in sorted(sides.items(), key=lambda kv: kv[0] or 0)]})
    trades.sort(key=lambda t: (t["processed_at"] is None, t["processed_at"] or datetime.min, t["trade_id"]))
    return {"total_moves": len(adds) + len(drops) + len(trades), "adds": adds, "drops": drops, "trades": trades,
            "unplaced_trades": unplaced}


# ----------------------------------------------------------------- standings --

def standings(data: RecapData, through: int) -> list[dict]:
    """Records from completed regular-season matchups with matchup_period <= `through`."""
    reg = data.league.get("reg_season_matchups")
    rec = {t["team_id"]: {"wins": 0, "losses": 0, "ties": 0, "pf": 0.0, "pa": 0.0, "scores_known": True}
           for t in data.teams}
    for m in data.matchups:
        p, h, a = m.get("matchup_period"), m.get("home_team_id"), m.get("away_team_id")
        if p is None or p > through or h is None or a is None or not is_final(m) or not is_regular(m, reg):
            continue
        res = FINAL_WINNERS[m["winner"]]
        for me, them, mine, theirs, side in ((h, a, m["home_points"], m["away_points"], "home"),
                                             (a, h, m["away_points"], m["home_points"], "away")):
            r = rec.get(me)
            if r is None:
                continue
            r["ties" if res == "tie" else "wins" if res == side else "losses"] += 1
            if mine is None or theirs is None:
                r["scores_known"] = False
            else:
                r["pf"] += mine
                r["pa"] += theirs
    names = {t["team_id"]: t for t in data.teams}
    out = []
    for tid, r in rec.items():
        games = r["wins"] + r["losses"] + r["ties"]
        out.append({"team_id": tid, "team_name": names[tid]["name"], "abbrev": names[tid].get("abbrev"),
                    "wins": r["wins"], "losses": r["losses"], "ties": r["ties"], "games": games,
                    "win_pct": _r((r["wins"] + 0.5 * r["ties"]) / games, 3) if games else None,
                    "points_for": _r(r["pf"]) if r["scores_known"] else None,
                    "points_against": _r(r["pa"]) if r["scores_known"] else None})
    out.sort(key=lambda s: (-(s["win_pct"] or 0.0), -(s["points_for"] or 0.0), s["team_id"]))
    for i, s in enumerate(out, 1):
        s["rank"] = i
    return out


def _weekly_results(games: list[dict]) -> dict[int, str]:
    res = {}
    for g in games:
        h, a = g["home_team"]["team_id"], g["away_team"]["team_id"]
        if g["is_tie"]:
            res[h] = res[a] = "T"
        else:
            w, l_ = _sides(g)
            res[w["team_id"]], res[l_["team_id"]] = "W", "L"
    return res


# --------------------------------------------------------------------- build --

def build_weekly_recap(data: RecapData) -> dict:
    lg = data.league
    available = completed_periods(data.matchups)
    base = {"season": lg["season"], "league_id": lg["league_id"], "league_name": lg.get("name"),
            "selected_week": data.week, "available_weeks": available,
            "available_periods": [{"matchup_period": p, "weeks": period_weeks(data.schedule_settings, p)}
                                  for p in available],
            "rules": {"awards": AWARD_RULE, "players": PLAYER_RULE, "standings": STANDINGS_RULE, "trades": TRADE_RULE}}
    if data.week is None:
        return {**base, "is_completed_week": False, "scoring_periods": [], "matchups": [], "awards": None,
                "standout_players": [], "bench_standouts": [], "unrostered_standouts": [], "league_activity": None,
                "standings": [], "standings_through": None, "notes": {}}
    weeks = period_weeks(data.schedule_settings, data.week)
    games, notes = scoreboard(data)
    reg = lg.get("reg_season_matchups")
    regular_done = [p for p in available
                    if any(m["matchup_period"] == p and is_final(m) and is_regular(m, reg) for m in data.matchups)]
    through_options = [p for p in regular_done if p <= data.week]
    through = through_options[-1] if through_options else None
    table = standings(data, through) if through is not None else []
    if through is not None:
        earlier = [p for p in regular_done if p < through]
        before = {s["team_id"]: s["rank"] for s in standings(data, earlier[-1])} if earlier else {}
        results = _weekly_results(games)
        for s in table:
            s["weekly_result"] = results.get(s["team_id"])
            s["rank_change"] = before[s["team_id"]] - s["rank"] if s["team_id"] in before else None
    check = None
    if through is not None and regular_done and through == regular_done[-1]:
        espn = {t["team_id"]: (t.get("wins"), t.get("losses"), t.get("ties")) for t in data.teams}
        check = {"compared": True, "mismatched_team_ids": sorted(
            s["team_id"] for s in table if espn.get(s["team_id"]) != (s["wins"], s["losses"], s["ties"]))}
    if weeks:
        players, coverage = player_scores(data, weeks)
        acts = activity(data, weeks)
    else:
        players, coverage, acts = [], {"rostered_without_points": 0, "partial_period_players": 0,
                                       "lineup_snapshot_weeks": []}, None
    return {**base,
            "is_completed_week": notes["pending_matchups"] == 0,
            "scoring_periods": weeks or [],
            "matchups": games,
            "awards": awards(games),
            "standout_players": players[:STANDOUT_LIMIT],
            "bench_standouts": [p for p in players if p["lineup_status"] == "BENCH"][:SIDE_LIMIT],
            "unrostered_standouts": [p for p in players if p["lineup_status"] == "UNROSTERED"][:SIDE_LIMIT],
            "league_activity": acts,
            "standings": table,
            "standings_through": through,
            "notes": {**notes, **coverage, "weeks_unmapped": not weeks, "espn_record_check": check}}
