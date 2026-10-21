"""Write one Sleeper league into the league tables the analytics and every page read (multi-league Phase 4b).

The tables keep their ESPN-derived names and id spaces, so no engine changes:
  * league / team / member rows come from app/sleeper/normalize.py (Sleeper user ids are public; no hashing);
  * players are keyed by ESPN player id: Sleeper's own `espn_id`, else the DynastyProcess map in the global file
    (`nfl_ff_playerids`, then `nfl_players`); a team defense is ESPN's `-16000 - proTeamId`; a player with no ESPN
    id gets `-(10_000_000 + sleeper_id)`, which no ESPN id or D/ST id can collide with;
  * lineup slots use ESPN's numeric slot ids (IR = 21, bench = 20), positions ESPN's default position ids;
  * actual weekly points are Sleeper's own (`players_points`) for rostered player-weeks, else the player's
    nflverse box score scored with the league's rules (skill players only); there are no provider projections
    (`projected_points` stays NULL: consumers read Fantasy Intel's own projections, app/analytics/projection_source);
  * injury statuses are mapped to ESPN's vocabulary; ownership percentages stay NULL (Sleeper has none).

`scoring_items` hold the league's rules translated to ESPN-shaped items (app/sleeper/scoring.py); the original
settings, the rules no resolver implements and the translation notes are kept in `settings_raw`.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

from ..db.connection import SyncRun, fetch_dicts, replace_where, table_exists, upsert_rows, utcnow
from ..espn.constants import PRO_TEAM_MAP, nflverse_team as espn_nflverse_team
from ..espn.sync import write_roster_history
from .constants import BENCH, IR, TAXI
from .models import parse_league, parse_players, parse_rosters, parse_state, parse_users, sid
from .normalize import internal_position, league_row, member_rows, starting_slots, team_rows
from .scoring import to_scoring_items

PROVIDER = "sleeper"
SYNTHETIC_BASE = 10_000_000
SLOT_IDS = {"QB": 0, "RB": 2, "RB/WR": 3, "WR": 4, "WR/TE": 5, "TE": 6, "OP": 7, "D/ST": 16, "K": 17,
            BENCH: 20, IR: 21, TAXI: 20, "FLEX": 23}
ELIGIBLE_SLOTS = {"QB": [0, 7, 20, 21], "RB": [2, 3, 23, 7, 20, 21], "WR": [4, 3, 5, 23, 7, 20, 21],
                  "TE": [6, 5, 23, 7, 20, 21], "K": [17, 20, 21], "D/ST": [16, 20, 21]}
DEFAULT_POSITION_IDS = {"QB": 1, "RB": 2, "WR": 3, "TE": 4, "K": 5, "D/ST": 16}
PRO_TEAM_IDS = {espn_nflverse_team(i): i for i in PRO_TEAM_MAP if i}
# Sleeper injury_status -> ESPN's vocabulary (what every consumer and badge reads)
INJURY = {"questionable": "QUESTIONABLE", "doubtful": "DOUBTFUL", "out": "OUT", "ir": "INJURY_RESERVE",
          "pup": "INJURY_RESERVE", "nfi": "INJURY_RESERVE", "sus": "SUSPENSION", "na": "OUT", "cov": "OUT",
          "dnr": "OUT"}
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
TX_TYPES = {"free_agent": "FREEAGENT", "waiver": "WAIVER", "trade": "TRADE_ACCEPT", "commissioner": "ROSTER"}
TX_STATUS = {"complete": "EXECUTED", "failed": "FAILED", "pending": "PENDING"}


@dataclass
class SleeperPayloads:
    """Everything one Sleeper league sync fetched, as raw API JSON (stored for `recompute`, app/sync/raw_store)."""
    league: dict
    users: list
    rosters: list
    state: dict
    matchups: dict[int, list] = field(default_factory=dict)        # week -> /league/{id}/matchups/{week}
    transactions: dict[int, list] = field(default_factory=dict)    # week -> /league/{id}/transactions/{week}
    draft_picks: list = field(default_factory=list)                # /draft/{draft_id}/picks
    players: dict[str, dict] = field(default_factory=dict)         # rostered + the active fantasy pool

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "SleeperPayloads":
        return cls(league=d["league"], users=d["users"], rosters=d["rosters"], state=d["state"],
                   matchups={int(k): v for k, v in (d.get("matchups") or {}).items()},
                   transactions={int(k): v for k, v in (d.get("transactions") or {}).items()},
                   draft_picks=d.get("draft_picks") or [], players=d.get("players") or {})


def pool_players(all_players: dict[str, dict], rostered: set[str]) -> dict[str, dict]:
    """The players a league file carries: everyone rostered, plus every active fantasy-position player with an
    NFL team (team defenses always). Keeps the stored raw payload small (the full list is several MB)."""
    out = {}
    for pid, raw in all_players.items():
        if pid in rostered:
            out[pid] = raw
        elif raw.get("position") in FANTASY_POSITIONS and (raw.get("position") == "DEF"
                                                            or (raw.get("active") is True and sid(raw.get("team")))):
            out[pid] = raw
    return out


def _int_or_none(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def espn_ids(con, players: dict) -> dict[str, tuple[int, str | None, str]]:
    """sleeper id -> (ESPN player id, gsis id or None, how it was matched)."""
    by_ff, by_players = {}, {}
    for table, target in (("nfl_ff_playerids", by_ff), ("nfl_players", by_players)):
        if table_exists(con, table):
            for r in fetch_dicts(con, f"SELECT CAST(sleeper_id AS VARCHAR) AS s, espn_id, gsis_id FROM {table} "
                                      "WHERE sleeper_id IS NOT NULL"):
                target.setdefault(str(r["s"]).split(".")[0], (r["espn_id"], r["gsis_id"]))
    out = {}
    for pid, p in players.items():
        ff, nfl = by_ff.get(pid, (None, None)), by_players.get(pid, (None, None))
        gsis = p.gsis_id or ff[1] or nfl[1]
        if p.is_team_defense:
            team_id = PRO_TEAM_IDS.get(p.team and p.team.upper()) or PRO_TEAM_IDS.get(_nfl(p.team))
            if team_id is not None:
                out[pid] = (-16000 - team_id, None, "team_defense")
                continue
        for espn, how in ((p.espn_id, "sleeper"), (ff[0], "ff_playerids"), (nfl[0], "nfl_players")):
            e = _int_or_none(espn)
            if e:
                out[pid] = (e, gsis, how)
                break
        else:
            n = _int_or_none(pid)
            out[pid] = (-(SYNTHETIC_BASE + n), gsis, "synthetic") if n is not None else (None, gsis, "unmapped")
    return out


def _unique(ids: dict[str, tuple], rostered: set[str], stats: dict) -> dict[str, tuple]:
    """One Sleeper player per ESPN id: when the map gives two Sleeper ids the same ESPN id, the rostered one keeps it
    (then the lower Sleeper id) and the other gets his synthetic id, with a warning."""
    by_espn: dict[int, list[str]] = {}
    for s_id, (e, _, _) in ids.items():
        if e is not None:
            by_espn.setdefault(e, []).append(s_id)
    out = dict(ids)
    for e, s_ids in by_espn.items():
        if len(s_ids) < 2:
            continue
        keep = sorted(s_ids, key=lambda x: (x not in rostered, x))[0]
        for s_id in s_ids:
            if s_id != keep:
                n = _int_or_none(s_id)
                out[s_id] = ((-(SYNTHETIC_BASE + n), ids[s_id][1], "synthetic_duplicate") if n is not None
                             else (None, ids[s_id][1], "unmapped"))
        stats["warnings"].append(f"{len(s_ids)} Sleeper players map to one ESPN id; one keeps it")
    return out


def _nfl(code: str | None) -> str | None:
    from .constants import nflverse_team
    return nflverse_team(code)


def _ms(v) -> datetime | None:
    n = _int_or_none(v)
    return None if n is None else datetime.fromtimestamp(n / 1000, tz=timezone.utc).replace(tzinfo=None)


def write_sleeper(con, p: SleeperPayloads, *, synced_at: datetime | None = None) -> dict:
    """Write the league (one transaction), recorded as a "sleeper" sync run. Returns counts and warnings."""
    run = SyncRun(con, PROVIDER)
    try:
        stats = _write(con, p, synced_at or utcnow())
    except Exception as err:
        run.finish("error", f"{type(err).__name__}: {err}")
        raise
    run.finish("ok", "warnings: %d" % len(stats["warnings"]), stats)
    return stats


def _write(con, p: SleeperPayloads, now: datetime) -> dict:
    league = parse_league(p.league)
    users, rosters, state = parse_users(p.users), parse_rosters(p.rosters), parse_state(p.state)
    players = parse_players(p.players)
    stats: dict = {"warnings": []}
    lg = league_row(league, state, now)
    league_id, season, current = int(league.league_id), int(league.season), lg["current_week"]
    items, unsupported, notes = to_scoring_items(league.scoring_settings)
    rostered_now = {x for r in rosters for x in (*r.players, *r.reserve, *r.taxi)}
    ids = _unique(espn_ids(con, players), rostered_now, stats)
    pid = {s: v[0] for s, v in ids.items() if v[0] is not None}
    stats["players_mapped"] = Counter(v[2] for v in ids.values())
    rostered = {x for r in rosters for x in (*r.players, *r.reserve, *r.taxi)}
    on_team = {x: r.roster_id for r in rosters for x in (*r.players, *r.reserve, *r.taxi)}

    league_rec = {k: lg[k] for k in ("season", "name", "size", "scoring_type", "reception_points", "is_faab",
                                     "faab_budget", "current_week", "current_matchup_period", "final_scoring_period",
                                     "reg_season_matchups", "playoff_teams", "trade_deadline", "waiver_process_days",
                                     "roster_slots", "synced_at")}
    league_rec.update(league_id=league_id, scoring_items=items,
                      settings_raw={**league.settings, "provider": PROVIDER, "sleeper_league_id": league.league_id,
                                    "scoring_settings": league.scoring_settings, "scoring_unsupported": unsupported,
                                    "scoring_notes": notes, "trade_deadline_week": lg["trade_deadline_week"],
                                    "last_regular_season_week": lg["last_regular_season_week"]})
    if unsupported:
        stats["warnings"].append(f"scoring rules not implemented: {', '.join(unsupported)}")

    tx_rows, item_rows, counts = _transactions(p, league_id, season, pid, now)
    teams = []
    for t in team_rows(league, users, rosters, now):
        c = counts.get(t["team_id"], {})
        teams.append({**{k: t[k] for k in ("season", "team_id", "abbrev", "name", "owner_ids", "owner_names",
                                           "division_id", "wins", "losses", "ties", "points_for", "points_against",
                                           "playoff_seed", "waiver_rank", "faab_spent", "synced_at")},
                      "league_id": league_id, "is_mine": False, "acquisitions": c.get("adds", 0),
                      "drops": c.get("drops", 0), "trades": c.get("trades", 0), "move_to_ir": None,
                      "matchup_acquisitions": c.get("by_week", {})})
    members = [{**{k: m[k] for k in ("season", "member_id", "display_name", "first_name", "last_name")},
                "league_id": league_id} for m in member_rows(league, users)]

    matchups, lineups, points = _weeks(p, league, rosters, players, pid, league_id, season, current,
                                       lg["last_regular_season_week"], now)
    universe = _players(players, pid, on_team, rostered, points, now, stats)

    con.execute("BEGIN TRANSACTION")
    try:
        upsert_rows(con, "leagues", [league_rec], ["league_id", "season"])
        upsert_rows(con, "league_members", members, ["league_id", "season", "member_id"])
        stats["teams"] = upsert_rows(con, "fantasy_teams", teams, ["league_id", "season", "team_id"])
        stats["matchups"] = upsert_rows(con, "matchups", matchups, ["league_id", "season", "matchup_id"])
        for period in sorted({r["scoring_period"] for r in lineups}):
            replace_where(con, "lineup_snapshots", [r for r in lineups if r["scoring_period"] == period],
                          "league_id = ? AND season = ? AND scoring_period = ?", [league_id, season, period])
        stats["lineup_rows"] = len(lineups)
        stats["roster_history"] = write_roster_history(
            con, [r for r in lineups if r["scoring_period"] == current], league_id=league_id, season=season,
            day=date.today(), now=now)
        con.execute("DELETE FROM fantasy_players")
        stats["players"] = upsert_rows(con, "fantasy_players", universe, ["espn_player_id"])
        # Every sync fetches every week: the season's points are rewritten, so a stat correction reaches them
        con.execute("DELETE FROM espn_player_weekly_points WHERE season = ?", [season])
        weekly = [{"season": season, "espn_player_id": e, "scoring_period": w, "actual_points": v,
                   "projected_points": None, "synced_at": now} for (e, w), v in points.items()]
        stats["weekly_points"] = upsert_rows(con, "espn_player_weekly_points", weekly,
                                             ["season", "espn_player_id", "scoring_period"])
        stats["box_score_points"] = _box_score_points(con, items, season, now)
        stats["transactions"] = upsert_rows(con, "transactions", tx_rows, ["transaction_id"])
        if tx_rows:
            con.execute("DELETE FROM transaction_items WHERE transaction_id IN (SELECT unnest(?))",
                        [[r["transaction_id"] for r in tx_rows]])
            upsert_rows(con, "transaction_items", item_rows, ["transaction_id", "item_index"])
        stats["draft_picks"] = upsert_rows(con, "draft_picks", _draft(p, league_id, season, pid),
                                           ["league_id", "season", "overall_pick"])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    stats["league"], stats["current_week"] = league.name, current
    return stats


def _weeks(p, league, rosters, players, pid, league_id, season, current, regular_last, now):
    """matchups, lineup_snapshots (every played week and the current one) and Sleeper's per-player points."""
    slots = starting_slots(league)
    matchups, lineups, points = [], [], {}
    for week, raw in sorted(p.matchups.items()):
        played = current is not None and week < current
        live = current is not None and week == current
        groups: dict[str, list[dict]] = {}
        for row in raw:
            mid = sid(row.get("matchup_id"))
            if mid is not None:
                groups.setdefault(mid, []).append(row)
            if played or live:
                for s_id, v in (row.get("players_points") or {}).items():
                    if s_id in pid and v is not None:
                        points[(pid[s_id], week)] = round(float(v), 2)
            if played:
                starters = [sid(x) for x in (row.get("starters") or [])]
                for i, s_id in enumerate(starters):
                    if s_id and s_id in pid:
                        slot = slots[i] if i < len(slots) else BENCH
                        lineups.append(_lineup(league_id, season, week, row["roster_id"], pid[s_id], slot, True,
                                               players.get(s_id), (row.get("players_points") or {}).get(s_id), now))
                for s_id in row.get("players") or []:
                    if s_id not in starters and s_id in pid:
                        lineups.append(_lineup(league_id, season, week, row["roster_id"], pid[s_id], BENCH, False,
                                               players.get(s_id), (row.get("players_points") or {}).get(s_id), now))
        for mid, rows in sorted(groups.items()):
            rows = sorted(rows, key=lambda r: r["roster_id"])
            if len(rows) != 2:
                continue
            home, away = rows
            hp, ap = (_pts(home), _pts(away)) if played or live else (None, None)
            winner = ("UNDECIDED" if not played or hp is None or ap is None
                      else "HOME" if hp > ap else "AWAY" if ap > hp else "TIE")
            matchups.append({"league_id": league_id, "season": season, "matchup_id": week * 1000 + int(mid),
                             "matchup_period": week, "home_team_id": home["roster_id"], "away_team_id": away["roster_id"],
                             "home_points": hp, "away_points": ap, "winner": winner,
                             "playoff_tier": "NONE" if regular_last is None or week <= regular_last
                             else "WINNERS_BRACKET"})
    if current is not None:
        cur_points = {s_id: v for row in p.matchups.get(current, []) for s_id, v in (row.get("players_points") or {}).items()}
        for r in rosters:
            placed: set[str] = set()
            for i, s_id in enumerate(r.starters):
                if s_id and s_id in pid:
                    lineups.append(_lineup(league_id, season, current, r.roster_id, pid[s_id],
                                           slots[i] if i < len(slots) else BENCH, True, players.get(s_id),
                                           cur_points.get(s_id), now))
                    placed.add(s_id)
            for s_id, slot in ([(x, IR) for x in r.reserve] + [(x, TAXI) for x in r.taxi]
                               + [(x, BENCH) for x in r.players]):
                if s_id not in placed and s_id in pid:
                    lineups.append(_lineup(league_id, season, current, r.roster_id, pid[s_id], slot, False,
                                           players.get(s_id), cur_points.get(s_id), now))
                    placed.add(s_id)
    # one row per (period, team, player): Sleeper can list a player twice (starters + players)
    seen, out = set(), []
    for r in lineups:
        k = (r["scoring_period"], r["team_id"], r["espn_player_id"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return matchups, out, points


def _pts(row) -> float | None:
    v = row.get("points")
    return None if v is None or isinstance(v, bool) else round(float(v), 2)


def _lineup(league_id, season, week, team_id, espn_id, slot, starter, player, pts, now) -> dict:
    return {"league_id": league_id, "season": season, "scoring_period": week, "team_id": team_id,
            "espn_player_id": espn_id, "lineup_slot_id": SLOT_IDS.get(slot, 20), "lineup_slot": slot,
            "is_starter": starter, "acquisition_type": None, "acquisition_date": None,
            "injury_status": _injury(player.injury_status) if player else None,
            "points": None if pts is None else round(float(pts), 2), "projected_points": None, "synced_at": now}


def _injury(status: str | None) -> str | None:
    return INJURY.get(status.strip().lower()) if status else None


def _players(players, pid, on_team, rostered, points, now, stats) -> list[dict]:
    season_pts: dict[int, list[float]] = {}
    for (e, _w), v in points.items():
        season_pts.setdefault(e, []).append(v)
    unknown = Counter()
    out = []
    for s_id, pl in players.items():
        e = pid.get(s_id)
        if e is None:
            continue
        pos = internal_position(pl.position)
        if pos not in ELIGIBLE_SLOTS:
            continue
        team = _nfl(pl.team)
        injury = _injury(pl.injury_status)
        if pl.injury_status and injury is None:
            unknown[pl.injury_status] += 1
        pts = season_pts.get(e, [])
        out.append({"espn_player_id": e, "full_name": pl.full_name, "position": pos,
                    "default_position_id": DEFAULT_POSITION_IDS[pos], "pro_team_id": PRO_TEAM_IDS.get(team),
                    "pro_team": team, "eligible_slots": ELIGIBLE_SLOTS[pos], "injury_status": injury,
                    "injured": injury is not None,
                    "availability_status": "ONTEAM" if s_id in rostered else "FREEAGENT",
                    "on_team_id": on_team.get(s_id), "percent_owned": None, "percent_started": None,
                    "percent_change": None, "season_points": round(sum(pts), 2) if pts else None,
                    "season_avg_points": round(sum(pts) / len(pts), 2) if pts else None,
                    "detail_synced_at": now, "synced_at": now})
    if unknown:
        stats["warnings"].append(f"unknown Sleeper injury statuses left blank: {dict(unknown)}")
    return out


def _box_score_points(con, items, season, now) -> int:
    """Actual points for player-weeks Sleeper did not score (free agents): the nflverse box score scored with the
    league's skill rules. Skipped when those rules are not supported (unknown is never zero)."""
    from ..analytics.league_scoring import points_sql, resolve_skill_scoring

    scoring = resolve_skill_scoring(items)
    if not scoring.supported or not all(table_exists(con, t) for t in ("nfl_weekly_stats", "nfl_ff_playerids")):
        return 0
    expr = points_sql(scoring, "s", position_col="position")
    before = con.execute("SELECT count(*) FROM espn_player_weekly_points").fetchone()[0]
    con.execute(f"""
        INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points, projected_points,
                                               synced_at)
        SELECT s.season, f.espn_player_id, s.week, round({expr}, 2), NULL, ?
        FROM nfl_weekly_stats s
        JOIN (SELECT DISTINCT p.espn_player_id, m.gsis_id
              FROM fantasy_players p JOIN nfl_ff_playerids m ON m.espn_id = p.espn_player_id
              WHERE m.gsis_id IS NOT NULL AND p.position IN ('QB', 'RB', 'WR', 'TE')) f ON f.gsis_id = s.player_id
        WHERE s.season = ? AND s.season_type = 'REG'
        ON CONFLICT DO NOTHING""", [now, season])
    return con.execute("SELECT count(*) FROM espn_player_weekly_points").fetchone()[0] - before


def _transactions(p, league_id, season, pid, now):
    tx_rows, item_rows = [], []
    counts: dict[int, dict] = {}
    for week, raw in sorted(p.transactions.items()):
        for t in raw:
            tid = sid(t.get("transaction_id"))
            if tid is None:
                continue
            kind = TX_TYPES.get(str(t.get("type")), str(t.get("type") or "").upper() or None)
            status = TX_STATUS.get(str(t.get("status")), str(t.get("status") or "").upper() or None)
            rids = [r for r in (t.get("roster_ids") or []) if isinstance(r, int)]
            leg = t.get("leg") if isinstance(t.get("leg"), int) else week
            bid = (t.get("settings") or {}).get("waiver_bid")
            tx_rows.append({"league_id": league_id, "season": season, "transaction_id": f"sleeper:{tid}", "type": kind,
                            "status": status, "team_id": rids[0] if rids else None, "scoring_period": leg,
                            "proposed_date": _ms(t.get("created")), "process_date": _ms(t.get("status_updated")),
                            "bid_amount": float(bid) if bid is not None else None, "execution_type": None,
                            "related_transaction_id": None, "member_id": sid(t.get("creator")),
                            "is_pending": status == "PENDING", "raw": t, "synced_at": now})
            i = 0
            for field_, item_type in (("adds", "ADD"), ("drops", "DROP")):
                for s_id, rid in (t.get(field_) or {}).items():
                    e = pid.get(str(s_id))
                    if e is None:
                        continue
                    trade = kind == "TRADE_ACCEPT"
                    item_rows.append({"transaction_id": f"sleeper:{tid}", "item_index": i,
                                      "item_type": "TRADE" if trade else item_type, "espn_player_id": e,
                                      "from_team_id": rid if item_type == "DROP" else None,
                                      "to_team_id": rid if item_type == "ADD" else None,
                                      "from_lineup_slot_id": None, "to_lineup_slot_id": None})
                    i += 1
                    if status == "EXECUTED" and isinstance(rid, int):
                        c = counts.setdefault(rid, {"adds": 0, "drops": 0, "trades": 0, "by_week": {}})
                        if trade:
                            continue
                        c["adds" if item_type == "ADD" else "drops"] += 1
                        if item_type == "ADD":
                            c["by_week"][str(leg)] = c["by_week"].get(str(leg), 0) + 1
            if kind == "TRADE_ACCEPT" and status == "EXECUTED":
                for rid in rids:
                    counts.setdefault(rid, {"adds": 0, "drops": 0, "trades": 0, "by_week": {}})["trades"] += 1
    return tx_rows, item_rows, counts


def _draft(p, league_id, season, pid) -> list[dict]:
    out = []
    for d in p.draft_picks:
        n = _int_or_none(d.get("pick_no"))
        if n is None:
            continue
        amount = (d.get("metadata") or {}).get("amount")
        out.append({"league_id": league_id, "season": season, "overall_pick": n, "round": _int_or_none(d.get("round")),
                    "round_pick": _int_or_none(d.get("draft_slot")), "team_id": _int_or_none(d.get("roster_id")),
                    "espn_player_id": pid.get(str(d.get("player_id"))),
                    "bid_amount": float(amount) if amount not in (None, "") else None,
                    "keeper": bool(d.get("is_keeper"))})
    return out


def dumps(p: SleeperPayloads) -> str:
    return json.dumps(p.to_json(), sort_keys=True, separators=(",", ":"))
