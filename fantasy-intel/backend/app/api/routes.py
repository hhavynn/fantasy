"""Read-only JSON API consumed by the Next.js dashboard."""

from __future__ import annotations

import json
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..analytics.availability import availability, injury_order_sql, latest_injuries
from ..analytics import forward_ours
from ..analytics.context import ftn_semantics
from ..analytics.projections import BASELINE_NOTE, league_window, player_forward
from ..analytics.team_lineups import project_team, public_weeks, replacement_market, roster_marginal_value, source_label
from ..analytics.trade_data import (load_league_state, manager_context, named_weeks, player_intelligence,
                                    player_names)
from ..analytics.player_availability import NOTE as AVAILABILITY_NOTE
from ..analytics import trade_dual_value
from ..analytics.trade_alternatives import FreeAgencyBoard, for_evaluation as trade_vs_free_agency
from ..analytics.trade_discovery import _side_detail
from ..analytics.trades import NoRemainingWeeksError, TradeError, evaluate_trade, with_streaming
from ..config import get_settings
from ..db.connection import schema_version, table_columns, table_exists
from .deps import db, one, rows
from .perspective import league_teams, resolve_team, waiver_table

router = APIRouter(prefix="/api")

_PLAYER_COLS = """
    fp.espn_player_id, fp.full_name, fp.position, fp.pro_team, fp.injury_status, fp.availability_status,
    fp.on_team_id, fp.percent_owned, fp.percent_change, fp.season_points, fp.season_avg_points,
    m.gsis_id, m.match_method, m.match_confidence,
    s.games, s.opportunity_basis, s.opportunity_tier, s.role_trend, s.market_signal,
    s.opportunity_score, s.production_score, s.usage_trend_score, s.trend_label,
    s.fp_per_game, s.xfp_per_game, s.fp_minus_xfp, s.value_gap_label, s.classification, s.value_score,
    {ratings},
    s.components->>'production_sustainability' AS production_sustainability
"""
# 0-100 ratings (ratings-100.v1): ALTERed into player_scores, so a hosted/older copy may not have them yet.
_RATING_COLS = ("opportunity_score_100", "production_score_100", "value_score_100", "rating_scale_version")


def _player_cols(con) -> str:
    have = set(table_columns(con, "player_scores"))
    return _PLAYER_COLS.format(ratings=",\n    ".join(
        f"s.{c}" if c in have else f"NULL AS {c}" for c in _RATING_COLS))


PLAYER_JOINS = """
    LEFT JOIN player_id_map m ON m.espn_player_id = fp.espn_player_id
    LEFT JOIN player_scores s ON s.gsis_id = m.gsis_id AND s.season = $season
"""


def _league(con) -> dict:
    lg = one(con, "SELECT * EXCLUDE (settings_raw, scoring_items) FROM leagues ORDER BY synced_at DESC LIMIT 1")
    if not lg:
        raise HTTPException(503, "ESPN league not synced yet. Run scripts/sync_espn.py.")
    return lg


def _pj(season: int) -> str:
    return PLAYER_JOINS.replace("$season", str(int(season)))


# A 'running' row with no finish time is left behind by a sync that died (SyncRun.start writes it, finish()
# replaces it); a live sync holds the DuckDB lock, so the API cannot even read during one. Report it as
# 'interrupted', and keep it out of the "latest run" per source so it neither shows as running nor as failed.
_INTERRUPTED = "(status = 'running' AND finished_at IS NULL)"
_RUN_STATUS = f"CASE WHEN {_INTERRUPTED} THEN 'interrupted' ELSE status END"


def _sync_status(con) -> dict:
    out = {}
    for r in rows(con, """
        SELECT source, max(finished_at) AS last_ok FROM sync_runs WHERE status = 'ok' GROUP BY 1"""):
        out[r["source"]] = r["last_ok"]
    last = rows(con, f"""
        SELECT source, status, message, finished_at FROM (
          SELECT *, row_number() OVER (PARTITION BY source ORDER BY started_at DESC) rn FROM sync_runs
          WHERE NOT {_INTERRUPTED}) WHERE rn = 1""")
    return {"last_success": out, "latest_runs": last}


def _nfl_latest_week(con, season: int) -> int | None:
    if not table_exists(con, "nfl_weekly_stats"):
        return None
    return con.execute("SELECT max(week) FROM nfl_weekly_stats WHERE season = ?", [season]).fetchone()[0]


@router.get("/league-profile")
def league_profile():
    """Which provider this league comes from and which projections its pages show by default (multi-league Phase
    4b): ESPN's for an ESPN league, Fantasy Intel's own (Ours) for a Sleeper league, whose provider publishes none."""
    from ..analytics.projection_source import default_source
    with db() as con:
        from ..analytics.legacy_league import latest_league
        lg = latest_league(con, ["settings_raw"])
        raw = (lg or {}).get("settings_raw")
        settings = json.loads(raw) if isinstance(raw, str) else (raw or {})
        provider = settings.get("provider") if isinstance(settings, dict) and settings.get("provider") else "espn"
        source = default_source(con)
        return {"provider": provider, "default_source": source, "provider_projections": source == "espn"}


@router.get("/status")
def status():
    with db() as con:
        lg = one(con, "SELECT league_id, season, name, current_week, synced_at FROM leagues ORDER BY synced_at DESC LIMIT 1")
        season = lg["season"] if lg else None
        warnings = rows(con, """
            SELECT source, stats->'$.warnings' AS warnings FROM (
              SELECT *, row_number() OVER (PARTITION BY source ORDER BY started_at DESC) rn FROM sync_runs
              WHERE status = 'ok') WHERE rn = 1""")
        return {
            "league": lg,
            "sync": _sync_status(con),
            "nfl_latest_week": _nfl_latest_week(con, season) if season else None,
            "warnings": {w["source"]: w["warnings"] for w in warnings},
        }


def _roster(con, league_id: int, season: int, week: int, team_id: int) -> list[dict]:
    roster = rows(con, f"""
        SELECT l.lineup_slot, l.lineup_slot_id, l.is_starter, l.acquisition_type, l.acquisition_date,
               l.points AS week_points, l.projected_points AS week_projection, {_player_cols(con)}
        FROM lineup_snapshots l JOIN fantasy_players fp ON fp.espn_player_id = l.espn_player_id
        {_pj(season)}
        WHERE l.league_id = ? AND l.season = ? AND l.scoring_period = ? AND l.team_id = ?
        ORDER BY l.is_starter DESC, l.lineup_slot_id, fp.full_name
    """, [league_id, season, week, team_id])
    inj = latest_injuries(con, season)  # newest report: week, then date_modified when available
    playing: set[str] = set()
    if table_exists(con, "nfl_schedules"):
        playing = {r["t"] for r in rows(con, """
            SELECT home_team t FROM nfl_schedules WHERE season=? AND week=? AND game_type='REG'
            UNION SELECT away_team FROM nfl_schedules WHERE season=? AND week=? AND game_type='REG'""",
                                         [season, week, season, week])}
    for p in roster:
        p["nfl_injury"] = inj.get(p["gsis_id"]) if p["gsis_id"] else None
        report = p["nfl_injury"]["report_status"] if p["nfl_injury"] and p["nfl_injury"]["week"] == week else None
        p["availability"] = availability(p["injury_status"], p["lineup_slot_id"], report)
        # None = schedule unknown; otherwise whether his NFL team has no game this week
        p["on_bye"] = (bool(p["pro_team"]) and p["pro_team"] not in playing) if playing else None
    return roster


@router.get("/teams")
def teams():
    """The league's teams for the team-perspective selector: names, the owner's default and roster freshness."""
    with db() as con:
        lg = _league(con)
        ts = league_teams(con, lg["league_id"], lg["season"])
        return {"league": {k: lg[k] for k in ("league_id", "season", "name", "current_week", "synced_at")},
                "teams": ts, "default_team_id": next((t["team_id"] for t in ts if t["is_owner_default"]), None)}


@router.get("/overview")
def overview(team_id: int | None = Query(None, description="Fantasy team to analyze (defaults to the owner's team)")):
    with db() as con:
        lg = _league(con)
        league_id, season, week = lg["league_id"], lg["season"], lg["current_week"]
        perspective = resolve_team(con, lg, team_id)
        teams = rows(con, """SELECT * FROM fantasy_teams WHERE league_id=? AND season=?
                             ORDER BY wins DESC, ties DESC, points_for DESC""", [league_id, season])
        me = next((t for t in teams if t["is_mine"]), None)
        team = next((t for t in teams if t["team_id"] == perspective["team_id"]), None) if perspective else None
        roster = _roster(con, league_id, season, week, team["team_id"]) if team else []
        injuries = [p for p in roster if (p["injury_status"] or "ACTIVE") not in ("ACTIVE", "NORMAL")
                    or (p.get("nfl_injury") or {}).get("report_status")]
        needs = rows(con, """SELECT * FROM team_position_needs WHERE league_id=? AND season=? AND team_id=?
                             ORDER BY need_score DESC""", [league_id, season, team["team_id"]]) if team else []
        return {
            "league": lg,
            "my_team": me,          # the owner's team (is_mine), unchanged by the selection
            "team": team,           # the team this overview is for
            "perspective": perspective,
            "standings": teams,
            "roster": roster,
            "injuries": injuries,
            "needs": needs,
            "recent_transactions": _recent_moves(con, league_id, season, limit=25),
            "sync": _sync_status(con),
            "nfl_latest_week": _nfl_latest_week(con, season),
        }


def _recent_moves(con, league_id: int, season: int, limit: int = 25, team_id: int | None = None) -> list[dict]:
    team_filter = "AND (coalesce(ti.to_team_id, t.team_id) = $team OR coalesce(ti.from_team_id, t.team_id) = $team)" \
        if team_id is not None else ""
    sql = f"""
        SELECT t.transaction_id, t.type, t.status, coalesce(t.process_date, t.proposed_date) AS date,
               t.scoring_period, t.bid_amount, ti.item_type, ti.espn_player_id, fp.full_name, fp.position,
               CASE WHEN ti.item_type = 'DROP' THEN coalesce(ti.from_team_id, t.team_id)
                    ELSE coalesce(ti.to_team_id, t.team_id) END AS team_id,
               ti.from_team_id, ti.to_team_id
        FROM transactions t JOIN transaction_items ti USING (transaction_id)
        LEFT JOIN fantasy_players fp ON fp.espn_player_id = ti.espn_player_id
        WHERE t.league_id = ? AND t.season = ? AND t.status = 'EXECUTED'
          AND t.type IN ('FREEAGENT', 'WAIVER', 'TRADE_ACCEPT', 'TRADE_UPHOLD')
          AND ti.item_type IN ('ADD', 'DROP', 'TRADE') {team_filter}
        ORDER BY date DESC NULLS LAST, t.transaction_id, ti.item_index
    """.replace("$team", str(int(team_id)) if team_id is not None else "0")
    out = rows(con, sql, [league_id, season])
    trades = rows(con, f"""
        SELECT a.topic_id AS transaction_id, 'TRADE' AS type, 'EXECUTED' AS status, a.topic_date AS date,
               NULL AS scoring_period, NULL AS bid_amount, 'TRADE' AS item_type, a.espn_player_id, fp.full_name,
               fp.position, a.to_team_id AS team_id, a.from_team_id, a.to_team_id
        FROM activity_messages a LEFT JOIN fantasy_players fp ON fp.espn_player_id = a.espn_player_id
        WHERE a.league_id = ? AND a.season = ? AND a.message_type_id = 244
        {"AND (a.from_team_id = ? OR a.to_team_id = ?)" if team_id is not None else ""}
    """, [league_id, season] + ([team_id, team_id] if team_id is not None else []))
    known = {(r["espn_player_id"], r["item_type"]) for r in out if r["item_type"] == "TRADE"}
    out += [t for t in trades if (t["espn_player_id"], "TRADE") not in known]
    names = {r["team_id"]: r["name"] for r in rows(con, "SELECT team_id, name FROM fantasy_teams WHERE league_id=? AND season=?",
                                                   [league_id, season])}
    for r in out:
        r["team_name"] = names.get(r["team_id"])
        r["from_team_name"] = names.get(r["from_team_id"])
        r["to_team_name"] = names.get(r["to_team_id"])
    out.sort(key=lambda r: (r["date"] is not None, r["date"]), reverse=True)
    return out[:limit]


@router.get("/players")
def search_players(q: str = "", limit: int = Query(30, le=100)):
    with db() as con:
        lg = _league(con)
        teams = {r["team_id"]: r["name"] for r in rows(con, "SELECT team_id, name FROM fantasy_teams WHERE league_id=? AND season=?",
                                                       [lg["league_id"], lg["season"]])}
        out = rows(con, f"""
            SELECT {_player_cols(con)} FROM fantasy_players fp {_pj(lg["season"])}
            WHERE fp.position IN ('QB','RB','WR','TE','K','D/ST')
              AND (? = '' OR strip_accents(lower(fp.full_name)) LIKE '%' || strip_accents(lower(?)) || '%')
            ORDER BY fp.percent_owned DESC NULLS LAST, fp.espn_player_id LIMIT ?
        """, [q.strip(), q.strip(), limit])
        for r in out:
            r["fantasy_team"] = teams.get(r["on_team_id"]) if r["availability_status"] == "ONTEAM" else None
        return out


@router.get("/players/{espn_player_id}")
def player_detail(espn_player_id: int):
    with db() as con:
        lg = _league(con)
        season = lg["season"]
        p = one(con, f"SELECT {_player_cols(con)}, m.notes AS mapping_notes, m.nfl_name, m.nfl_team "
                     f"FROM fantasy_players fp {_pj(season)} WHERE fp.espn_player_id = ?", [espn_player_id])
        if not p:
            raise HTTPException(404, "Unknown ESPN player id")
        if p["availability_status"] == "ONTEAM":
            t = one(con, "SELECT name FROM fantasy_teams WHERE league_id=? AND season=? AND team_id=?",
                    [lg["league_id"], season, p["on_team_id"]])
            p["fantasy_team"] = t["name"] if t else None
        score = one(con, "SELECT components, reasons, as_of_week FROM player_scores WHERE season=? AND gsis_id=?",
                    [season, p["gsis_id"]]) if p["gsis_id"] else None
        weekly = rows(con, "SELECT * FROM player_week_usage WHERE season=? AND gsis_id=? ORDER BY week",
                      [season, p["gsis_id"]]) if p["gsis_id"] else []
        league_pts = rows(con, """SELECT scoring_period, actual_points, projected_points FROM espn_player_weekly_points
                                  WHERE season=? AND espn_player_id=? ORDER BY scoring_period""", [season, espn_player_id])
        injuries = rows(con, f"""SELECT * FROM nfl_injuries WHERE season=? AND gsis_id=?
                                 ORDER BY {injury_order_sql(con)}""", [season, p["gsis_id"]]) \
            if p["gsis_id"] and table_exists(con, "nfl_injuries") else []
        depth = rows(con, """SELECT dt, team, pos_abb, pos_rank FROM nfl_depth_charts
                             WHERE season=? AND gsis_id=? ORDER BY dt DESC LIMIT 1""", [season, p["gsis_id"]]) \
            if p["gsis_id"] and table_exists(con, "nfl_depth_charts") else []
        ownership = rows(con, """SELECT snapshot_date, percent_owned, percent_change FROM player_ownership_history
                                 WHERE espn_player_id=? ORDER BY snapshot_date""", [espn_player_id])
        waiver = one(con, "SELECT * FROM waiver_recommendations WHERE espn_player_id=? AND season=?",
                     [espn_player_id, season])
        route_data = _route_summary(weekly)
        context = one(con, "SELECT labels, evidence FROM player_context WHERE season=? AND gsis_id=?",
                      [season, p["gsis_id"]]) if p["gsis_id"] else None
        team_ctx = one(con, "SELECT * FROM team_context WHERE season=? AND team=?", [season, p["pro_team"]]) \
            if p["pro_team"] else None
        opp_def = one(con, "SELECT * FROM defense_context WHERE season=? AND defteam=?",
                      [season, team_ctx["next_opponent"]]) if team_ctx and team_ctx.get("next_opponent") else None
        forward = player_forward(con, season, espn_player_id, p["pro_team"])
        return {
            "player": p, "score": score, "weekly": weekly, "league_points": league_pts, "injuries": injuries,
            "forward_projection": forward,
            "depth_chart": depth[0] if depth else None, "ownership": ownership, "waiver": waiver,
            "context": context, "team_context": team_ctx, "opponent_defense": opp_def, "route_data": route_data,
            "unavailable_metrics": {
                "route_participation": "unavailable: nflverse participation data is not published for the current season",
                "routes_run": "unavailable",
                "man_zone_splits": "unavailable: not in free data",
            },
            "ftn_read_semantics": ftn_semantics(),
        }


@router.get("/players/{espn_player_id}/forward")
def player_forward_view(espn_player_id: int,
                        source: str | None = Query(None, description="Projection source: espn | independent (default: the league's own, espn when it has ESPN projections, else independent)")):
    """ESPN's projection for every remaining fantasy week (unmodified) + the ROS summary.

    With source=independent the response also carries `ours`: Fantasy Intel's production model (ifp-2.2.0) for
    each remaining regular-season week {available, reason, label, model_version, weeks [{week, value, origin
    (ours | ours_zero | espn | bye), on_bye, run_id, model_version, run_created_at, horizon, projected_from_week,
    experimental}], ros_projected_points (NULL unless every non-bye week is ours / ours_zero), ros_weeks_expected,
    ros_weeks_available, ros_complete, fallback_weeks, missing_weeks, run_created_at}. available=false (with a
    reason) means no rest-of-season run is stored yet. Without it, or with source=espn, the response is ESPN's only.

    404 for an unknown player, 422 for a bad source, 503 when the league window is unknown."""
    try:
        forward_ours.normalize(source)
    except ValueError as err:
        raise HTTPException(422, str(err))
    with db() as con:
        lg = _league(con)
        p = one(con, "SELECT pro_team FROM fantasy_players WHERE espn_player_id = ?", [espn_player_id])
        if not p:
            raise HTTPException(404, "Unknown ESPN player id")
        out = player_forward(con, lg["season"], espn_player_id, p["pro_team"], source=source)
        if out is None:
            raise HTTPException(503, "League current/final week unknown. Run scripts/sync_espn.py.")
        return out


ROUTE_FIELDS = ["routes", "route_participation", "tprr", "yprr", "pass_snaps", "pass_block_snaps",
                "pass_block_rate", "slot_rate", "wide_rate", "inline_rate", "backfield_rate",
                "two_minute_route_share", "long_down_route_share", "short_down_route_share", "man_tprr", "zone_tprr"]


def _route_summary(weekly: list[dict]) -> dict | None:
    """Season summary of external route data (None when no week has any). Route totals
    are summed; rates are averaged over weeks that report them; TPRR/YPRR are recomputed
    from season nflverse targets/yards over season routes."""
    from ..external.contract import PROVIDER_DEFINED, derived_ratio
    weeks = [w for w in weekly if any(w.get(f) is not None for f in ROUTE_FIELDS)]
    if not weeks:
        return None
    def mean(f):
        vals = [w[f] for w in weeks if w.get(f) is not None]
        return sum(vals) / len(vals) if vals else None
    def total(f):
        vals = [w[f] for w in weeks if w.get(f) is not None]
        return sum(vals) if vals else None
    routed = [w for w in weeks if w.get("routes") is not None]
    routes = total("routes")
    summary = {f: mean(f) for f in ROUTE_FIELDS if f not in ("routes", "pass_snaps", "pass_block_snaps",
                                                               "tprr", "yprr")}
    summary.update(routes=routes, pass_snaps=total("pass_snaps"), pass_block_snaps=total("pass_block_snaps"),
                   tprr=derived_ratio(sum(w["targets"] or 0 for w in routed) if routed else None, routes),
                   yprr=derived_ratio(sum(w["receiving_yards"] or 0 for w in routed) if routed else None, routes))
    return {
        "season": {k: v for k, v in summary.items() if v is not None},
        "weeks": [w["week"] for w in weeks],
        "providers": sorted({w["route_data_provider"] for w in weeks if w.get("route_data_provider")}),
        "datasets": sorted({w["route_data_dataset"] for w in weeks if w.get("route_data_dataset")}),
        "source_updated_at": max((w["route_data_updated_at"] for w in weeks if w.get("route_data_updated_at")),
                                 default=None),
        "provider_defined": PROVIDER_DEFINED,
        "note": "External provider data (not nflverse). TPRR/YPRR = nflverse targets/yards per provider route.",
    }


@router.get("/waivers")
def waivers(position: str | None = None, recommendation: str | None = None,
            limit: int = Query(150, ge=1, le=360), offset: int = Query(0, ge=0), view: str = "ALL",
            team_id: int | None = Query(None, description="Fantasy team to evaluate pickups for (defaults to the owner's team)")):
    with db() as con:
        lg = _league(con)
        season = lg["season"]
        perspective = resolve_team(con, lg, team_id)
        table, team_params = waiver_table(con, lg, perspective)
        params: list = [lg["league_id"], season, *team_params]
        where = " AND w.team_id = ?" if team_params else ""
        if position:
            where += " AND w.position = ?"
            params.append(position.upper())
        if recommendation:
            where += " AND w.recommendation = ?"
            params.append(recommendation.upper())
        views = {
            "BEST": " AND coalesce(try_cast(w.components->'decision'->>'actionable' AS BOOLEAN), false)",
            "FREE_AGENTS": " AND w.availability_status = 'FREEAGENT'",
            "CLAIMS": " AND w.availability_status = 'WAIVERS'",
            "STREAMING": " AND w.position IN ('K', 'D/ST')",
            "WATCHLIST": " AND (w.components->'decision'->>'action' IN ('MONITOR', 'PRESERVE_PRIORITY', 'VERIFY_LEGALITY', 'VERIFY_COST', 'NO_LEGAL_MOVE') OR coalesce(try_cast(w.components->'decision'->'utility'->>'stash' AS BOOLEAN), false))",
            "ALL": "",
        }
        if view.upper() not in views:
            raise HTTPException(422, "Unknown waiver view")
        where += views[view.upper()]
        total = one(con, f"SELECT count(*) n FROM {table} WHERE w.league_id=? AND w.season=? {where}", params)["n"]
        meta = one(con, f"SELECT w.components, w.computed_at FROM {table} WHERE w.league_id=? AND w.season=? "
                        f"{' AND w.team_id = ?' if team_params else ''} LIMIT 1", [lg["league_id"], season, *team_params])
        params.extend([limit, offset])
        out = rows(con, f"""
            SELECT w.*, fp.full_name, fp.pro_team, fp.injury_status, fp.percent_owned, fp.percent_change,
                   s.trend_label, s.classification, s.games, s.xfp_per_game, s.fp_per_game,
                   s.opportunity_basis, s.opportunity_tier, s.role_trend, s.market_signal, pc.labels AS context_labels,
                   d.full_name AS drop_name, d.position AS drop_position
            FROM {table}
            JOIN fantasy_players fp ON fp.espn_player_id = w.espn_player_id
            LEFT JOIN player_id_map m ON m.espn_player_id = w.espn_player_id
            LEFT JOIN player_scores s ON s.gsis_id = m.gsis_id AND s.season = w.season
            LEFT JOIN player_context pc ON pc.gsis_id = m.gsis_id AND pc.season = w.season
            LEFT JOIN fantasy_players d ON d.espn_player_id = w.suggested_drop_id
            WHERE w.league_id = ? AND w.season = ? {where}
            ORDER BY coalesce(try_cast(w.components->'decision'->>'actionable' AS BOOLEAN), false) DESC,
                     coalesce(try_cast(w.components->'decision'->'utility'->>'ranking_utility' AS DOUBLE), 0) DESC,
                     CASE w.recommendation WHEN 'CLAIM' THEN 0 WHEN 'WAIT' THEN 1 ELSE 2 END,
                     w.waiver_score DESC, w.espn_player_id
            LIMIT ? OFFSET ?
        """, params)
        computed_at = meta["computed_at"] if meta else None
        if perspective:
            # The roster may have been re-synced after the analysis ran: say so, and mark suggested drops that are no
            # longer on the team instead of presenting them as current.
            on_roster = {r["espn_player_id"] for r in rows(con, """SELECT espn_player_id FROM lineup_snapshots
                         WHERE league_id=? AND season=? AND scoring_period=? AND team_id=?""",
                                                                [lg["league_id"], season, lg["current_week"],
                                                                 perspective["team_id"]])}
            for r in out:
                r["drop_on_roster"] = None if r["suggested_drop_id"] is None else r["suggested_drop_id"] in on_roster
        roster_synced = perspective["roster_synced_at"] if perspective else None
        return {"league": lg, "perspective": perspective, "players": out, "computed_at": computed_at,
                "roster_newer_than_analysis": bool(roster_synced and computed_at and roster_synced > computed_at),
                "coverage": (meta["components"].get("decision") or {}).get("coverage") if meta else None,
                "total": total, "offset": offset, "limit": limit}


def _team_composition(con, league_id, season, week) -> dict[int, dict]:
    comp: dict[int, dict] = defaultdict(dict)
    for r in rows(con, """
        SELECT l.team_id, fp.position, count(*) n FROM lineup_snapshots l JOIN fantasy_players fp USING (espn_player_id)
        WHERE l.league_id=? AND l.season=? AND l.scoring_period=? GROUP BY 1, 2""", [league_id, season, week]):
        comp[r["team_id"]][r["position"]] = r["n"]
    return comp


@router.get("/managers")
def managers():
    with db() as con:
        lg = _league(con)
        league_id, season = lg["league_id"], lg["season"]
        teams = rows(con, "SELECT * FROM fantasy_teams WHERE league_id=? AND season=? ORDER BY team_id", [league_id, season])
        classes = defaultdict(dict)
        for c in rows(con, "SELECT * FROM manager_classifications WHERE league_id=? AND season=?", [league_id, season]):
            classes[c["team_id"]][c["dimension"]] = c
        metrics = defaultdict(dict)
        for m in rows(con, "SELECT team_id, metric, value, numerator, denominator, sample_size FROM manager_metrics "
                           "WHERE league_id=? AND season=?", [league_id, season]):
            metrics[m["team_id"]][m["metric"]] = m
        comp = _team_composition(con, league_id, season, lg["current_week"])
        return {"league": lg, "managers": [
            {**t, "classifications": classes.get(t["team_id"], {}), "metrics": metrics.get(t["team_id"], {}),
             "composition": comp.get(t["team_id"], {})} for t in teams]}


@router.get("/managers/{team_id}")
def manager_detail(team_id: int):
    with db() as con:
        lg = _league(con)
        league_id, season = lg["league_id"], lg["season"]
        team = one(con, "SELECT * FROM fantasy_teams WHERE league_id=? AND season=? AND team_id=?", [league_id, season, team_id])
        if not team:
            raise HTTPException(404, "Unknown team")
        metrics = rows(con, "SELECT * FROM manager_metrics WHERE league_id=? AND season=? AND team_id=? ORDER BY metric",
                       [league_id, season, team_id])
        classes = rows(con, "SELECT * FROM manager_classifications WHERE league_id=? AND season=? AND team_id=? ORDER BY dimension",
                       [league_id, season, team_id])
        weekly_comp = rows(con, """
            SELECT l.scoring_period, fp.position, count(*) n, sum(CASE WHEN l.is_starter THEN 1 ELSE 0 END) starters
            FROM lineup_snapshots l JOIN fantasy_players fp USING (espn_player_id)
            WHERE l.league_id=? AND l.season=? AND l.team_id=? GROUP BY 1, 2 ORDER BY 1, 2""", [league_id, season, team_id])
        needs = rows(con, "SELECT * FROM team_position_needs WHERE league_id=? AND season=? AND team_id=? ORDER BY need_score DESC",
                     [league_id, season, team_id])
        failed = rows(con, """
            SELECT t.transaction_id, t.type, t.status, t.scoring_period, t.bid_amount,
                   coalesce(t.process_date, t.proposed_date) AS date, fp.full_name, fp.position
            FROM transactions t LEFT JOIN transaction_items ti ON ti.transaction_id = t.transaction_id AND ti.item_type = 'ADD'
            LEFT JOIN fantasy_players fp ON fp.espn_player_id = ti.espn_player_id
            WHERE t.league_id=? AND t.season=? AND t.team_id=? AND t.type IN ('WAIVER','WAIVER_ERROR')
              AND t.status LIKE 'FAILED%' ORDER BY date DESC""", [league_id, season, team_id])
        return {
            "league": lg, "team": team, "metrics": metrics, "classifications": classes,
            "roster": _roster(con, league_id, season, lg["current_week"], team_id),
            "weekly_composition": weekly_comp, "needs": needs,
            "moves": _recent_moves(con, league_id, season, limit=200, team_id=team_id),
            "failed_claims": failed,
        }


@router.get("/data-quality")
def data_quality():
    with db() as con:
        by_method = rows(con, "SELECT match_method, match_confidence, count(*) n FROM player_id_map GROUP BY 1, 2 ORDER BY n DESC")
        unmatched = rows(con, """
            SELECT m.*, fp.availability_status, fp.percent_owned FROM player_id_map m
            JOIN fantasy_players fp USING (espn_player_id)
            WHERE m.match_method = 'unmatched' ORDER BY (fp.availability_status = 'ONTEAM') DESC,
                  fp.percent_owned DESC NULLS LAST LIMIT 200""")
        medium = rows(con, """SELECT m.*, fp.percent_owned FROM player_id_map m JOIN fantasy_players fp USING (espn_player_id)
                              WHERE m.match_confidence = 'medium' ORDER BY fp.percent_owned DESC NULLS LAST, fp.espn_player_id LIMIT 100""")
        conflicts = rows(con, "SELECT * FROM player_id_map WHERE notes LIKE 'Conflicting%' LIMIT 100")
        runs = rows(con, f"SELECT run_id, source, started_at, finished_at, {_RUN_STATUS} AS status, message FROM sync_runs "
                    "ORDER BY started_at DESC LIMIT 30")
        issues = rows(con, "SELECT * FROM validation_issues ORDER BY severity, check_name") \
            if table_exists(con, "validation_issues") else []
        external = _external_quality(con)
        return {"mapping_summary": by_method, "unmatched": unmatched, "medium_confidence": medium,
                "conflicts": conflicts, "sync_runs": runs, "sync": _sync_status(con),
                "validation_issues": issues, "schema_version": schema_version(con), "external": external,
                "espn_projections": _projection_quality(con)}


def _projection_quality(con) -> dict | None:
    """ESPN future projections: what the latest sync fetched (per week, from its stats) and what
    is stored now (failed weeks keep older values, so the two can differ)."""
    lg = one(con, "SELECT league_id, season, current_week FROM leagues ORDER BY synced_at DESC LIMIT 1")
    if not lg:
        return None
    season = lg["season"]
    run = one(con, """SELECT run_id, finished_at, json_extract(stats, '$.projections') AS fetch,
                             json_extract(stats, '$.ros_projections') AS ros
                      FROM sync_runs WHERE source = 'espn' AND status = 'ok'
                        AND json_extract(stats, '$.projections') IS NOT NULL
                      ORDER BY started_at DESC LIMIT 1""")
    if run:
        run["fetch"] = json.loads(run["fetch"]) if isinstance(run["fetch"], str) else run["fetch"]
        run["ros"] = json.loads(run["ros"]) if isinstance(run["ros"], str) else run["ros"]
    window = league_window(con, season)
    stored, incomplete, summary = [], [], None
    if window:
        stored = rows(con, """
            WITH r AS (SELECT DISTINCT espn_player_id FROM lineup_snapshots
                       WHERE league_id = ? AND season = ? AND scoring_period = ?)
            SELECT w.week, count(p.espn_player_id) FILTER (WHERE p.projected_points IS NOT NULL) AS projected,
                   count(*) FILTER (WHERE p.projected_points = 0) AS zero,
                   count(r.espn_player_id) FILTER (WHERE p.projected_points IS NOT NULL) AS rostered_projected,
                   (SELECT count(*) FROM r) AS rostered,
                   max(p.synced_at) FILTER (WHERE p.projected_points IS NOT NULL) AS latest_synced_at
            FROM range(?, ? + 1) w(week)
            LEFT JOIN espn_player_weekly_points p ON p.season = ? AND p.scoring_period = w.week
            LEFT JOIN r ON r.espn_player_id = p.espn_player_id
            GROUP BY 1 ORDER BY 1""", [lg["league_id"], season, window.current, window.current, window.final, season])
        summary = one(con, """SELECT count(*) AS players, count(ros_projected_points) AS full_coverage,
                                     count(*) - count(ros_projected_points) AS incomplete, max(computed_at) AS computed_at
                              FROM player_ros_projections WHERE season = ?""", [season])
        incomplete = rows(con, """
            SELECT r.espn_player_id, fp.full_name, fp.position, fp.pro_team, fp.availability_status,
                   r.ros_weeks_expected, r.ros_weeks_available, r.missing_weeks
            FROM player_ros_projections r LEFT JOIN fantasy_players fp USING (espn_player_id)
            WHERE r.season = ? AND r.ros_projected_points IS NULL
            ORDER BY (fp.availability_status = 'ONTEAM') DESC NULLS LAST, fp.percent_owned DESC NULLS LAST, fp.espn_player_id LIMIT 60""",
                          [season])
    history = one(con, """SELECT count(*) AS rows, count(DISTINCT snapshot_date) AS days, max(snapshot_date) AS latest
                          FROM espn_projection_history WHERE season = ?""", [season])
    return {"window": {"current": window.current, "final": window.final, "last_regular": window.last_regular}
            if window else None,
            "latest_fetch": run, "stored": stored, "ros_summary": summary, "ros_incomplete": incomplete,
            "history": history, "note": BASELINE_NOTE}


def _external_quality(con) -> dict:
    from ..external.selection import select_provider
    if not table_exists(con, "external_usage_imports"):
        return {"imports": [], "coverage": [], "unmatched": [], "issues": [], "selection": None}
    imports = rows(con, """SELECT import_id, provider, dataset, source_filename, season, weeks, imported_at,
                                  source_updated_at, row_count, matched_rows, unmatched_rows, ambiguous_rows,
                                  warning_count, error_count, status, duplicate_of, message
                           FROM external_usage_imports ORDER BY imported_at DESC LIMIT 50""")
    coverage = rows(con, """SELECT provider, dataset, season, list(DISTINCT week ORDER BY week) AS weeks,
                                   count(*) AS rows, count(gsis_id) AS matched,
                                   count(*) FILTER (WHERE mapping_method = 'unmatched') AS unmatched,
                                   count(*) FILTER (WHERE mapping_method = 'ambiguous') AS ambiguous,
                                   max(coalesce(source_updated_at, imported_at)) AS updated_at
                            FROM external_player_week_usage GROUP BY 1, 2, 3 ORDER BY 3 DESC, 1, 2""")
    unmatched = rows(con, """SELECT provider, dataset, season, week, player_name, team, position, provider_player_id,
                                    source_player_key, mapping_method, mapping_reason
                             FROM external_player_week_usage WHERE gsis_id IS NULL
                             ORDER BY season DESC, week, player_name LIMIT 300""")
    issues = rows(con, """SELECT i.import_id, i.severity, i.check_name, i.detail, i.week, i.row_number, i.player_name
                          FROM external_usage_issues i JOIN external_usage_imports m USING (import_id)
                          ORDER BY m.imported_at DESC, i.severity, i.row_number LIMIT 200""")
    seasons = sorted({c["season"] for c in coverage}, reverse=True)
    pref = get_settings().external_preferred_provider
    return {"imports": imports, "coverage": coverage, "unmatched": unmatched, "issues": issues,
            "selection": {s: select_provider(con, s, pref) for s in seasons},
            "duplicate_noops": sum(1 for i in imports if i["status"] == "duplicate_noop"),
            "rejected": sum(1 for i in imports if i["status"] == "rejected")}


# ------------------------------------------------------------- V2 lineups + trades --
# Hypothetical and read-only: nothing below writes to the database or to ESPN.

def _state(con):
    state = load_league_state(con)
    if state is None:
        raise HTTPException(503, "League current/final week unknown. Run scripts/sync_espn.py.")
    return state


def _window_info(state) -> dict:
    w, reason = state.window(), state.start_week_reason or {}
    return {"start_week": w.start, "final_week": state.final_week, "last_regular_season_week": w.last_regular,
            "weeks": list(w.weeks), "remaining_weeks": bool(w.weeks), "uncertain": bool(reason.get("uncertain")),
            "warning": reason.get("warning"), "start_week_reason": reason}


@router.get("/lineup-projections")
def lineup_projections():
    """Every team's stored optimal-lineup projection summary + the live replacement market."""
    with db() as con:
        lg = _league(con)
        state = _state(con)
        teams = rows(con, """SELECT r.*, t.name, t.abbrev, t.is_mine FROM team_ros_lineup_projections r
                             JOIN fantasy_teams t ON t.league_id = r.league_id AND t.season = r.season
                                                 AND t.team_id = r.team_id
                             WHERE r.league_id = ? AND r.season = ?
                             ORDER BY r.ros_known_starting_points DESC NULLS LAST""", [lg["league_id"], lg["season"]]) \
            if table_exists(con, "team_ros_lineup_projections") else []
        window = state.window()
        return {"league": lg, "evaluation": _window_info(state), "teams": teams,
                "teams_note": None if window.weeks else
                "No remaining fantasy scoring periods: these are the stored rows from the last actionable window.",
                "replacement_market": replacement_market(state.free_agents, state.slots, window, state.projections,
                                                         projection_source=source_label(state.projection_source))
                if window.weeks else None,
                "projection_source": "ESPN", "note": BASELINE_NOTE}


@router.get("/teams/{team_id}/lineup-projection")
def team_lineup_projection(team_id: int):
    """Live weekly optimal lineups for one team + every rostered player's marginal lineup value."""
    with db() as con:
        lg = _league(con)
        state = _state(con)
        if team_id not in state.rosters:
            raise HTTPException(404, "Unknown team")
        roster, window = state.rosters[team_id], state.window()
        if not window.weeks:
            return {"league": lg, "team_id": team_id, "team_name": state.team_names.get(team_id),
                    "evaluation": _window_info(state), "summary": None, "weeks": [], "marginal_values": []}
        team = project_team(roster, state.slots, window, state.projections)
        names = player_names(con, [p.espn_player_id for p in roster])
        open_spots = max(0, state.active_capacity - sum(1 for p in roster if not p.on_ir))
        marginal = [{"name": p.name, "position": p.position, "pro_team": p.pro_team,
                     **roster_marginal_value(roster, p.espn_player_id, state.slots, window, state.projections, team,
                                             open_spots)}
                    for p in roster]
        marginal.sort(key=lambda m: -(m["known_marginal_lineup_points"] or 0))
        return {"league": lg, "team_id": team_id, "team_name": state.team_names.get(team_id),
                "evaluation": _window_info(state), "summary": team["summary"],
                "weeks": named_weeks(public_weeks(team), names), "marginal_values": marginal}


@router.get("/trades/options")
def trade_options():
    """Teams and current rosters for the Trade Evaluator."""
    with db() as con:
        lg = _league(con)
        state = _state(con)
        teams = rows(con, "SELECT team_id, name, abbrev, is_mine FROM fantasy_teams WHERE league_id=? AND season=? "
                          "ORDER BY team_id", [lg["league_id"], lg["season"]])
        ros = {r["espn_player_id"]: r for r in rows(con, """SELECT espn_player_id, ros_projected_points,
                   regular_season_projected_points, playoff_projected_points FROM player_ros_projections
                   WHERE season = ?""", [lg["season"]])} if table_exists(con, "player_ros_projections") else {}
        order = {p: i for i, p in enumerate(["QB", "RB", "WR", "TE", "K", "D/ST"])}
        for t in teams:
            t["roster"] = sorted(
                [{"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position, "pro_team": p.pro_team,
                  "on_ir": p.on_ir, "injury_status": p.injury_status,
                  "ros_projected_points": (ros.get(p.espn_player_id) or {}).get("ros_projected_points"),
                  "nfl_ineligible_weeks": sorted(p.unavailable_weeks),
                  "availability": _availability_brief(state.availability.get(p.espn_player_id))}
                 for p in state.rosters.get(t["team_id"], [])],
                key=lambda p: (order.get(p["position"], 9), -(p["ros_projected_points"] or 0), p["name"] or ""))
        return {"league": lg, "evaluation": _window_info(state), "teams": teams,
                "active_capacity": state.active_capacity, "position_limits": state.position_limits,
                "max_players_per_side": get_settings().trade_max_players_per_side,
                "availability_note": AVAILABILITY_NOTE}


def _availability_brief(rec: dict | None) -> dict | None:
    if not rec or (rec["status"] == "available" and not rec["warnings"]):
        return None
    return {k: rec[k] for k in ("status", "nfl_designation_label", "earliest_eligible_week", "return_confirmed",
                                "fantasy_ir_slot", "espn_injury_status", "warnings")}


class TradeRequest(BaseModel):
    team_a_id: int
    team_b_id: int
    team_a_sends: list[int]
    team_b_sends: list[int]
    start_week: int | None = None
    kdst_streaming: bool = True       # value K and D/ST against weekly free-agent streaming (team_lineups)


def _dual_value(con, lg: dict, state, res: dict, body: "TradeRequest") -> dict:
    """The dual-value assessment (docs/trade_discovery_dual_value.md) of an evaluated trade: the same
    trade_dual_value.for_evaluation call, over the same window and side views, that Trade Discovery makes,
    so the analyzer and discovery assess a trade identically. Read-only; adds a field, changes none."""
    window = state.window(body.start_week)
    mine = {t["team_id"] for t in rows(con, "SELECT team_id FROM fantasy_teams WHERE league_id=? AND season=? "
                                            "AND is_mine", [lg["league_id"], lg["season"]])}
    name = lambda t: state.team_names.get(t) or f"Team {t}"   # noqa: E731
    labels = ("You", name(body.team_b_id)) if body.team_a_id in mine else (name(body.team_a_id), name(body.team_b_id))
    return trade_dual_value.for_evaluation(
        state, window, body.team_a_id, list(res["trade"]["team_a_sends"]), list(res["trade"]["team_b_sends"]),
        _side_detail(res["team_a"]), _side_detail(res["team_b"]), phase="ros",
        fa_board=FreeAgencyBoard(state, window) if state.free_agents else None, labels=labels)


@router.post("/trades/evaluate")
def trades_evaluate(body: TradeRequest):
    """Hypothetical exact before/after lineup simulation for both teams. Read-only."""
    with db() as con:
        lg = _league(con)
        state = _state(con)
        if body.kdst_streaming:
            state = with_streaming(state)
        try:
            res = evaluate_trade(state, body.team_a_id, body.team_b_id, body.team_a_sends, body.team_b_sends,
                                 body.start_week, max_per_side=get_settings().trade_max_players_per_side)
        except NoRemainingWeeksError as err:
            raise HTTPException(409, str(err))
        except TradeError as err:
            raise HTTPException(422, str(err))
        res["dual_value"] = _dual_value(con, lg, state, res, body)
        ids = set(body.team_a_sends) | set(body.team_b_sends)
        for side in (res["team_a"], res["team_b"]):
            for key in ("before", "after"):
                for w in side["lineups"][key]:
                    ids |= {a["player_id"] for a in w["assignment"] if a["player_id"]}
                    ids |= {b["player_id"] for b in w["bench"]}
                    ids |= {x["player_id"] for x in w["ir"]}
                    ids |= {x["player_id"] for x in w.get("unavailable", [])}
        names = player_names(con, ids)
        for side in (res["team_a"], res["team_b"]):
            for key in ("before", "after"):
                side["lineups"][key] = named_weeks(side["lineups"][key], names)
        intel = player_intelligence(con, lg["season"], sorted(set(body.team_a_sends) | set(body.team_b_sends)))
        for p in res["players"]:
            p["intelligence"] = intel.get(p["espn_player_id"])
        res["manager_context"] = manager_context(con, lg["league_id"], lg["season"], [body.team_a_id, body.team_b_id])
        res["names"] = {pid: {"name": n["full_name"], "position": n["position"], "pro_team": n["pro_team"]}
                        for pid, n in names.items()}
        res["league"] = {k: lg[k] for k in ("league_id", "season", "name", "current_week", "final_scoring_period")}
        if state.undroppable_list_in_use:
            res["limitations"] = res["limitations"] + [
                "This league uses ESPN's undroppable list; forced drops may name a player ESPN would not let you drop."]
        res["trade_vs_free_agency"] = trade_vs_free_agency(state, res, body.team_a_id, body.team_b_id,
                                                           body.team_a_sends, body.team_b_sends, body.start_week)
        return res

