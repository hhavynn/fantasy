"""ESPN league sync: fetch everything over the network, then write in one transaction."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..analytics import injury_freshness
from ..analytics.projections import build_ros_projections
from ..config import DATA_DIR
from ..db.connection import SyncRun, merge_columns, replace_where, upsert_rows, utcnow
from . import parse
from .constants import PROJECTION_LIMIT

log = logging.getLogger("espn.sync")

CARD_BATCH = 100


@dataclass
class EspnPayloads:
    league: dict
    pro_players: Any = None
    rosters: dict[int, dict] = field(default_factory=dict)       # period -> raw
    transactions: dict[int, dict] = field(default_factory=dict)  # period -> raw
    activity: list[dict] = field(default_factory=list)           # raw pages
    pool: dict | None = None
    draft: dict | None = None
    cards: list[dict] = field(default_factory=list)
    projection_weeks: list[int] = field(default_factory=list)       # weeks requested
    projections: dict[int, dict] = field(default_factory=dict)      # week -> raw kona_player_info
    projection_failures: dict[int, str] = field(default_factory=dict)  # week -> error
    warnings: list[str] = field(default_factory=list)
    # Our clock around the whole download (naive UTC); the injury retrieval log records these. The
    # player status rows reach `injury_status_rows` in write_all (in memory only).
    fetch_started_at: Any = None
    fetched_at: Any = None
    injury_status_rows: list[dict] | None = None
    injury_status_season: int | None = None


def _try(warnings: list[str], label: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as err:  # keep going; record what failed
        msg = f"{label} failed: {type(err).__name__}: {err}"
        log.warning(msg)
        warnings.append(msg)
        return None


def projection_weeks(current: int | None, final: int | None) -> list[int]:
    """Fantasy weeks to fetch ESPN projections for: current .. final scoring period, inclusive.
    League settings are authoritative (a league ending at 17 never fetches NFL week 18)."""
    if not current or not final:
        return []
    return list(range(min(current, final), final + 1))


def fetch_projections(client, p: EspnPayloads, weeks: list[int]) -> None:
    p.projection_weeks = list(weeks)
    for week in weeks:
        log.info("ESPN projections week %s", week)
        try:
            p.projections[week] = client.player_projections(week)
        except Exception as err:  # this week's stored projections are kept (see write_projections)
            msg = f"ESPN projections week {week} failed: {type(err).__name__}: {err}"
            log.warning(msg)
            p.warnings.append(msg)
            p.projection_failures[week] = msg


def fetch_all(client, *, max_activity_pages: int = 40) -> EspnPayloads:
    """Network phase. `client` is an EspnClient (or a fake with the same methods)."""
    started = utcnow()
    league = client.league()  # fatal on failure (bad credentials / league id)
    p = EspnPayloads(league=league)
    p.fetch_started_at = started
    current = min(league.get("scoringPeriodId") or 1, league.get("status", {}).get("finalScoringPeriod") or 99)

    p.pro_players = _try(p.warnings, "pro players", client.pro_players)
    for period in range(1, current + 1):
        log.info("period %s: rosters + transactions", period)
        roster = _try(p.warnings, f"roster week {period}", lambda: client.roster_for_period(period))
        if roster is not None:
            p.rosters[period] = roster
        tx = _try(p.warnings, f"transactions week {period}", lambda: client.transactions(period))
        if tx is not None:
            p.transactions[period] = tx

    for page in range(max_activity_pages):
        data = _try(p.warnings, f"activity page {page}", lambda: client.activity(offset=page * 50, size=50))
        if not data or not data.get("topics"):
            break
        p.activity.append(data)
        if len(data["topics"]) < 50:
            break

    p.pool = _try(p.warnings, "free-agent pool", lambda: client.player_pool(current))
    final = league.get("status", {}).get("finalScoringPeriod")
    if final:
        fetch_projections(client, p, projection_weeks(current, final))
    else:
        p.warnings.append("League final scoring period unknown; future ESPN projections not fetched.")
    p.draft = _try(p.warnings, "draft", client.draft)

    ids = set()
    for raw in [league, *p.rosters.values()]:
        for t in raw.get("teams", []):
            for e in (t.get("roster") or {}).get("entries", []):
                if e.get("playerId"):
                    ids.add(e["playerId"])
    for w in (p.pool or {}).get("players", []):
        if w.get("id"):
            ids.add(w["id"])
    ids_sorted = sorted(ids)
    final_period = league.get("status", {}).get("finalScoringPeriod") or 18
    for i in range(0, len(ids_sorted), CARD_BATCH):
        batch = ids_sorted[i:i + CARD_BATCH]
        card = _try(p.warnings, f"player cards {i}", lambda: client.player_cards(batch, final_period))
        if card:
            p.cards.append(card)
    p.fetched_at = utcnow()
    return p


def save_raw(p: EspnPayloads, run_id: str) -> Path:
    out = DATA_DIR / "raw" / "espn" / run_id
    out.mkdir(parents=True, exist_ok=True)

    def dump(name: str, obj: Any) -> None:
        if obj is not None:  # member SWIDs are hashed before anything touches disk
            (out / f"{name}.json").write_text(json.dumps(parse.scrub_member_ids(obj)))

    dump("league", p.league)
    dump("pro_players", p.pro_players)
    dump("pool", p.pool)
    dump("draft", p.draft)
    for k, v in p.rosters.items():
        dump(f"roster_{k:02d}", v)
    for k, v in p.transactions.items():
        dump(f"transactions_{k:02d}", v)
    for i, v in enumerate(p.activity):
        dump(f"activity_{i:02d}", v)
    for i, v in enumerate(p.cards):
        dump(f"cards_{i:02d}", v)
    for k, v in p.projections.items():
        dump(f"projections_{k:02d}", v)
    return out


def write_all(con, p: EspnPayloads, *, swid: str | None, my_team_id: int | None) -> dict:
    """Database phase. Idempotent: rerunning with the same payloads yields the same rows."""
    now = utcnow()
    lg = parse.parse_league(p.league, swid=swid, my_team_id=my_team_id, synced_at=now)
    league_id, season = lg["league"]["league_id"], lg["league"]["season"]
    current = lg["league"]["current_week"]
    team_ids = {t["team_id"] for t in lg["teams"]}
    stats: dict[str, Any] = {"warnings": list(p.warnings)}
    if not any(t["is_mine"] for t in lg["teams"]):
        stats["warnings"].append("Could not identify your team (set ESPN_MY_TEAM_ID in .env).")

    con.execute("BEGIN TRANSACTION")
    try:
        upsert_rows(con, "leagues", [lg["league"]], ["league_id", "season"])
        upsert_rows(con, "league_members", lg["members"], ["league_id", "season", "member_id"])
        stats["teams"] = upsert_rows(con, "fantasy_teams", lg["teams"], ["league_id", "season", "team_id"])
        stats["matchups"] = upsert_rows(con, "matchups", lg["matchups"], ["league_id", "season", "matchup_id"])

        # Rosters per period; the base league payload doubles as the current period.
        rosters = dict(p.rosters)
        rosters.setdefault(current, p.league)
        lineup_count, points_rows, roster_players, rostered_ids, current_lineups = 0, [], [], set(), []
        for period, raw in sorted(rosters.items()):
            r = parse.parse_rosters(raw, league_id=league_id, season=season, scoring_period=period, synced_at=now)
            lineup_count += replace_where(
                con, "lineup_snapshots", r["lineups"],
                "league_id = ? AND season = ? AND scoring_period = ?", [league_id, season, period])
            points_rows += r["points"]
            if period == current:
                roster_players = r["players"]
                rostered_ids = {row["espn_player_id"] for row in r["lineups"] if row["espn_player_id"]}
                current_lineups = r["lineups"]
        stats["lineup_rows"] = lineup_count
        stats["roster_periods"] = sorted(rosters)
        stats["roster_history"] = write_roster_history(con, current_lineups, league_id=league_id, season=season,
                                                       day=date.today(), now=now)

        # Player universe: pro players (base) <- FA pool <- current rosters.
        base = parse.parse_pro_players(p.pro_players, season=season, synced_at=now) if p.pro_players else []
        pool = parse.parse_player_pool(p.pool, season=season, synced_at=now) if p.pool else []
        for row in pool + roster_players:
            row["detail_synced_at"] = now
        players = parse.merge_player_rows(base, pool, roster_players)
        if base:
            # Complete universe fetched: replace so stale availability can't linger.
            con.execute("DELETE FROM fantasy_players")
        stats["players"] = upsert_rows(con, "fantasy_players", players, ["espn_player_id"])
        # In memory only (recorded after COMMIT by run_espn_sync): the status rows this fetch returned.
        # Only a complete universe (the pro-player base) proves a team's statuses were refreshed; without
        # it `players` holds just the FA pool and current rosters, so logging a per-team snapshot would
        # let injury_freshness call a team fresh although most of its statuses were never fetched.
        p.injury_status_season = season
        if base:
            p.injury_status_rows = [{"espn_player_id": r.get("espn_player_id"), "pro_team": r.get("pro_team"),
                                     "injury_status": r.get("injury_status")} for r in players]
        else:
            p.injury_status_rows = None
            stats["warnings"].append("injury status retrieval not logged: pro-player fetch returned nothing, "
                                     "so the ESPN snapshot is incomplete")
        stats["free_agent_pool"] = len(pool)
        today = date.today()
        upsert_rows(con, "player_ownership_history", [{
            "snapshot_date": today,
            "espn_player_id": r["espn_player_id"],
            "percent_owned": r.get("percent_owned"),
            "percent_started": r.get("percent_started"),
            "percent_change": r.get("percent_change"),
        } for r in pool + roster_players if r.get("percent_owned") is not None],
            ["snapshot_date", "espn_player_id"])

        # Forward ESPN projections, parsed per week. Each week has a reconcile scope: the whole week
        # when the response provably holds ESPN's full universe, otherwise only the players ESPN
        # explicitly returned. Stored projections in scope are cleared first (so a player ESPN
        # stopped projecting doesn't keep a stale value); this sync's roster/pool rows and the
        # projection rows are then written on top. Players outside the scope are never touched.
        expected_rostered = projection_eligible(rostered_ids, players)
        proj = {week: parse.parse_projection_week(raw, season=season, scoring_period=week, synced_at=now,
                                                  rostered_ids=rostered_ids, expected_rostered_ids=expected_rostered,
                                                  limit=PROJECTION_LIMIT)
                for week, raw in sorted(p.projections.items())}
        for week, x in proj.items():
            _in_scope(con, "UPDATE espn_player_weekly_points SET projected_points = NULL", season, week, x)

        # The free-agent pool payload (requested for the current scoring period) already carries each
        # free agent's single-period stats, including ESPN's projection for that period. Same
        # players[].player.stats shape as player cards, so the same parser applies.
        if p.pool:
            points_rows += parse.parse_player_cards(p.pool, season=season, synced_at=now)
        for card in p.cards:
            points_rows += parse.parse_player_cards(card, season=season, synced_at=now)
        # Cards are fetched last and cover every week; prefer them over roster stats
        # but never let a None overwrite a known value.
        merged_points: dict[tuple, dict] = {}
        for r in points_rows:
            key = (r["espn_player_id"], r["scoring_period"])
            cur = merged_points.setdefault(key, dict(r))
            for k in ("actual_points", "projected_points"):
                if r.get(k) is not None:
                    cur[k] = r[k]
        stats["weekly_points"] = upsert_rows(
            con, "espn_player_weekly_points", list(merged_points.values()),
            ["season", "espn_player_id", "scoring_period"])
        stats["projections"] = write_projections(con, p, proj, season=season, current=current,
                                                 final=lg["league"]["final_scoring_period"])
        for w, x in proj.items():
            if not x["complete"]:
                stats["warnings"].append(
                    f"ESPN projections week {w}: universe not verified ({x['coverage']['reason']}); only the "
                    f"{x['coverage']['returned']} returned players were updated, omitted players keep their previous values.")
        stats["ros_projections"] = build_ros_projections(con, season)

        tx_rows, item_rows = [], []
        for raw in p.transactions.values():
            t = parse.parse_transactions(raw, league_id=league_id, season=season, synced_at=now)
            tx_rows += t["transactions"]
            item_rows += t["items"]
        stats["transactions"] = upsert_rows(con, "transactions", tx_rows, ["transaction_id"])
        tx_ids = sorted({r["transaction_id"] for r in tx_rows})
        if tx_ids:
            con.execute("DELETE FROM transaction_items WHERE transaction_id IN (SELECT unnest(?))", [tx_ids])
            upsert_rows(con, "transaction_items", item_rows, ["transaction_id", "item_index"])
        stats["transaction_items"] = len(item_rows)
        stats["transaction_types"] = _count(tx_rows, "type")
        stats["transaction_statuses"] = _count(tx_rows, "status")

        act_rows = []
        for raw in p.activity:
            act_rows += parse.parse_activity(raw, league_id=league_id, season=season, team_ids=team_ids)
        stats["activity_messages"] = upsert_rows(con, "activity_messages", act_rows, ["message_id"])

        if p.draft:
            stats["draft_picks"] = upsert_rows(
                con, "draft_picks", parse.parse_draft(p.draft, league_id=league_id, season=season),
                ["league_id", "season", "overall_pick"])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    stats["league"] = lg["league"]["name"]
    stats["current_week"] = current
    return stats


def projection_eligible(rostered_ids: set[int], players: list[dict]) -> set[int]:
    """Rostered players ESPN should return with an NFL projection context: everyone rostered except
    players known to have no current NFL team (proTeamId missing or 0, e.g. a released player).
    A rostered player we know nothing about stays expected (stricter check = fewer deletions)."""
    team = {r["espn_player_id"]: r.get("pro_team_id") for r in players}
    return {pid for pid in rostered_ids if pid not in team or team[pid] not in (None, 0)}


def _in_scope(con, statement: str, season: int, week: int, x: dict, extra_where: str = "",
              extra_params: list | None = None) -> None:
    """Run `statement` (an UPDATE/DELETE on a table with season, scoring_period, espn_player_id)
    restricted to this week's reconcile scope."""
    where, params = "season = ? AND scoring_period = ?", [season, week]
    if not x["complete"]:
        if not x["returned_ids"]:
            return
        where += " AND espn_player_id IN (SELECT unnest(?))"
        params.append(x["returned_ids"])
    con.execute(f"{statement} WHERE {where}{extra_where}", params + (extra_params or []))


def pregame_history_locked(con, season: int, week: int, day: date, now) -> set[int]:
    """ESPN ids whose `day` history row for `week` was captured before their NFL team's kickoff
    while that kickoff has now passed. That row is the pregame benchmark; a later same-day sync
    must not replace it with a post-kickoff projection. Unknown team or kickoff: not locked."""
    from ..analytics.projection_backtest import _kickoff, _utc
    from ..db.connection import fetch_dicts, table_columns, table_exists

    if not table_exists(con, "nfl_schedules") or not {"gameday", "gametime"} <= set(table_columns(con, "nfl_schedules")):
        return set()
    kick = {}
    for g in fetch_dicts(con, "SELECT gameday, gametime, home_team, away_team FROM nfl_schedules "
                              "WHERE season = ? AND week = ? AND game_type = 'REG'", [season, week]):
        for t in (g["home_team"], g["away_team"]):
            kick[t] = _kickoff(g)
    now = _utc(now)
    out = set()
    for r in fetch_dicts(con, """SELECT h.espn_player_id, h.synced_at, fp.pro_team FROM espn_projection_history h
                                 JOIN fantasy_players fp USING (espn_player_id)
                                 WHERE h.snapshot_date = ? AND h.season = ? AND h.scoring_period = ?""",
                         [day, season, week]):
        ko, cap = kick.get(r["pro_team"]), _utc(r["synced_at"])
        if ko is not None and cap is not None and cap < ko <= now:
            out.add(int(r["espn_player_id"]))
    return out


def roster_history_locked(con, league_id: int, season: int, day, now) -> bool:
    """True when today's stored roster snapshot was synced before a week's first kickoff that has since
    passed: that snapshot is the kickoff roster power_rankings.load_previous needs, so a later sync the
    same day must not replace it. Unknown kickoffs: not locked."""
    from ..analytics.production_forecasts import first_kickoffs
    from ..analytics.projection_backtest import _utc
    from ..db.connection import fetch_dicts

    row = fetch_dicts(con, """SELECT max(synced_at) AS synced_at FROM roster_history
                              WHERE snapshot_date = ? AND league_id = ? AND season = ?""", [day, league_id, season])
    stored, now = (_utc(row[0]["synced_at"]) if row else None), _utc(now)
    if stored is None or now is None:
        return False
    return any(stored < ko <= now for ko in first_kickoffs(con, season).values())


def write_roster_history(con, lineups: list[dict], *, league_id: int, season: int, day, now) -> dict:
    """Append today's snapshot of every team's current-period roster to roster_history, the way
    espn_projection_history is kept: one snapshot per day, the latest sync of the day replacing an
    earlier one unless that one is locked (roster_history_locked). Earlier days are never touched.
    Called inside write_all's transaction."""
    rows = [{"snapshot_date": day, "league_id": league_id, "season": season, "team_id": r["team_id"],
             "espn_player_id": r["espn_player_id"], "lineup_slot_id": r["lineup_slot_id"], "synced_at": now}
            for r in lineups if r.get("espn_player_id")]
    out = {"snapshot_date": day.isoformat(), "rows": 0, "locked": False}
    if not rows:
        return out
    if roster_history_locked(con, league_id, season, day, now):
        return {**out, "locked": True}
    n = replace_where(con, "roster_history", rows, "snapshot_date = ? AND league_id = ? AND season = ?",
                      [day, league_id, season])
    return {**out, "rows": n}


def write_projections(con, p: EspnPayloads, proj: dict[int, dict], *, season: int, current: int,
                      final: int | None, now=None) -> dict:
    """Write parsed weekly projections (see docs/espn_ros_projections.md).

    Every requested week is reconciled within its scope (projected_points in scope were already
    cleared before this sync's roster/pool rows were written):
      complete week (ESPN's player count matched, nothing truncated, every expected rostered
        player returned): scope = the whole week. Players ESPN no longer returns lose their
        projection; today's history for the week is replaced.
      unverified week: scope = only the players ESPN explicitly returned. A returned player without
        a projection loses it; an omitted player keeps his previous value and today's history row
        (absence alone is not evidence). Warning recorded.
      failed week: nothing is touched; previous values remain; warning recorded.
    Rows left in scope with neither an actual nor a projection are deleted (unknown, not zero).
    Only projected_points is ever written here: actual_points is never touched."""
    rows = [r for x in proj.values() for r in x["rows"]]
    merge_columns(con, "espn_player_weekly_points", rows, ["season", "espn_player_id", "scoring_period"],
                  ["projected_points", "synced_at"])
    today = date.today()
    now = now or utcnow()
    locked: set[tuple[int, int]] = set()
    for week, x in proj.items():
        _in_scope(con, "DELETE FROM espn_player_weekly_points", season, week, x,
                  " AND actual_points IS NULL AND projected_points IS NULL")
        keep = sorted(pregame_history_locked(con, season, week, today, now))
        locked |= {(week, pid) for pid in keep}
        _in_scope(con, "DELETE FROM espn_projection_history", season, week, x,
                  " AND snapshot_date = ? AND espn_player_id NOT IN (SELECT unnest(?::BIGINT[]))", [today, keep])
    history = upsert_rows(con, "espn_projection_history", [{**r, "snapshot_date": today} for r in rows
                                                           if (r["scoring_period"], r["espn_player_id"]) not in locked],
                          ["snapshot_date", "season", "espn_player_id", "scoring_period"])
    return {
        "source": "ESPN kona_player_info (statSourceId 1, statSplitTypeId 1)",
        "completeness": "complete = returned == ESPN X-Fantasy-Filter-Player-Count, not truncated, "
                        "all rostered players with an NFL team returned",
        "current_week": current, "final_scoring_period": final,
        "weeks_requested": p.projection_weeks,
        "weeks_complete": [w for w, x in proj.items() if x["complete"]],
        "weeks_unverified": [w for w, x in proj.items() if not x["complete"]],
        "weeks_failed": sorted(p.projection_failures),
        "failures": {str(w): m for w, m in sorted(p.projection_failures.items())},
        "coverage": [x["coverage"] for x in proj.values()],
        "projection_rows": len(rows),
        "history_rows": history,
        "history_date": today.isoformat(),
    }


def _count(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[str(r.get(key))] = out.get(str(r.get(key)), 0) + 1
    return out


def record_injury_status_retrieval(con, p: EspnPayloads, sync_run_id: str | None = None) -> dict | None:
    """Append this fetch's per-team ESPN injury-status snapshot to the write-once retrieval log.
    ESPN supplies no publication time, so it is recorded as NULL. Nothing is logged unless the fetch
    finished and write_all captured its player rows, and write_all captures them only when the complete
    pro-player base was fetched (an incomplete snapshot never proves a team's statuses are fresh)."""
    if p.fetched_at is None or p.injury_status_rows is None or p.injury_status_season is None:
        return None
    obs = injury_freshness.espn_status_observations(
        p.injury_status_rows, p.injury_status_season, retrieved_at=p.fetched_at,
        fetch_started_at=p.fetch_started_at, sync_run_id=sync_run_id)
    return injury_freshness.record_observations(con, obs)


def run_espn_sync(con_factory, settings, client=None, save_raw_payloads: bool = True) -> dict:
    """Fetch then write. `con_factory` opens a read-write connection (context manager)."""
    from .client import EspnClient

    if client is None:
        if not settings.league_id:
            raise SystemExit("ESPN_LEAGUE_ID is not set. Copy .env.example to .env and fill it in.")
        if not settings.has_espn_credentials:
            log.warning("ESPN_S2/SWID not set; this only works for public leagues.")
        client = EspnClient(settings.league_id, settings.season, settings.espn_s2, settings.swid)

    payloads = fetch_all(client)
    with con_factory() as con:
        return write_synced(con, payloads, swid=settings.swid, my_team_id=settings.my_team_id,
                            before_write=(lambda run_id: save_raw(payloads, run_id)) if save_raw_payloads else None)


def write_synced(con, payloads: EspnPayloads, *, swid: str | None, my_team_id: int | None,
                 before_write: Callable[[str], Any] | None = None) -> dict:
    """The write half of a sync, recorded as an "espn" sync run: write_all, then the injury retrieval log. Sync jobs
    (app/sync/build.py) call it with payloads fetched earlier."""
    run = SyncRun(con, "espn")
    if before_write is not None:
        before_write(run.run_id)
    try:
        stats = write_all(con, payloads, swid=swid, my_team_id=my_team_id)
    except Exception as err:
        run.finish("error", f"{type(err).__name__}: {err}")
        raise
    try:   # audit log only; the synced data above is already committed
        stats["injury_retrievals"] = record_injury_status_retrieval(con, payloads, run.run_id)
    except Exception as err:
        msg = f"injury retrieval log failed: {type(err).__name__}: {err}"
        log.warning(msg)
        stats["warnings"].append(msg)
    run.finish("ok", "warnings: %d" % len(stats["warnings"]), stats)
    return stats
