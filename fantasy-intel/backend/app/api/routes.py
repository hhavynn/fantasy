"""Read-only JSON API consumed by the Next.js dashboard."""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, HTTPException, Query

from ..analytics.availability import availability, injury_order_sql, latest_injuries
from ..analytics.context import ftn_semantics
from ..db.connection import schema_version, table_exists
from .deps import db, one, rows

router = APIRouter(prefix="/api")

PLAYER_COLS = """
    fp.espn_player_id, fp.full_name, fp.position, fp.pro_team, fp.injury_status, fp.availability_status,
    fp.on_team_id, fp.percent_owned, fp.percent_change, fp.season_points, fp.season_avg_points,
    m.gsis_id, m.match_method, m.match_confidence,
    s.games, s.opportunity_basis, s.opportunity_tier, s.role_trend, s.market_signal,
    s.opportunity_score, s.production_score, s.usage_trend_score, s.trend_label,
    s.fp_per_game, s.xfp_per_game, s.fp_minus_xfp, s.value_gap_label, s.classification, s.value_score
"""
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


def _sync_status(con) -> dict:
    out = {}
    for r in rows(con, """
        SELECT source, max(finished_at) AS last_ok FROM sync_runs WHERE status = 'ok' GROUP BY 1"""):
        out[r["source"]] = r["last_ok"]
    last = rows(con, """
        SELECT source, status, message, finished_at FROM (
          SELECT *, row_number() OVER (PARTITION BY source ORDER BY started_at DESC) rn FROM sync_runs) WHERE rn = 1""")
    return {"last_success": out, "latest_runs": last}


def _nfl_latest_week(con, season: int) -> int | None:
    if not table_exists(con, "nfl_weekly_stats"):
        return None
    return con.execute("SELECT max(week) FROM nfl_weekly_stats WHERE season = ?", [season]).fetchone()[0]


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
               l.points AS week_points, l.projected_points AS week_projection, {PLAYER_COLS}
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


@router.get("/overview")
def overview():
    with db() as con:
        lg = _league(con)
        league_id, season, week = lg["league_id"], lg["season"], lg["current_week"]
        teams = rows(con, """SELECT * FROM fantasy_teams WHERE league_id=? AND season=?
                             ORDER BY wins DESC, ties DESC, points_for DESC""", [league_id, season])
        me = next((t for t in teams if t["is_mine"]), None)
        roster = _roster(con, league_id, season, week, me["team_id"]) if me else []
        injuries = [p for p in roster if (p["injury_status"] or "ACTIVE") not in ("ACTIVE", "NORMAL")
                    or (p.get("nfl_injury") or {}).get("report_status")]
        needs = rows(con, """SELECT * FROM team_position_needs WHERE league_id=? AND season=? AND team_id=?
                             ORDER BY need_score DESC""", [league_id, season, me["team_id"]]) if me else []
        return {
            "league": lg,
            "my_team": me,
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
            SELECT {PLAYER_COLS} FROM fantasy_players fp {_pj(lg["season"])}
            WHERE fp.position IN ('QB','RB','WR','TE','K','D/ST')
              AND (? = '' OR strip_accents(lower(fp.full_name)) LIKE '%' || strip_accents(lower(?)) || '%')
            ORDER BY fp.percent_owned DESC NULLS LAST LIMIT ?
        """, [q.strip(), q.strip(), limit])
        for r in out:
            r["fantasy_team"] = teams.get(r["on_team_id"]) if r["availability_status"] == "ONTEAM" else None
        return out


@router.get("/players/{espn_player_id}")
def player_detail(espn_player_id: int):
    with db() as con:
        lg = _league(con)
        season = lg["season"]
        p = one(con, f"SELECT {PLAYER_COLS}, m.notes AS mapping_notes, m.nfl_name, m.nfl_team "
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
        return {
            "player": p, "score": score, "weekly": weekly, "league_points": league_pts, "injuries": injuries,
            "depth_chart": depth[0] if depth else None, "ownership": ownership, "waiver": waiver,
            "context": context, "team_context": team_ctx, "opponent_defense": opp_def, "route_data": route_data,
            "unavailable_metrics": {
                "route_participation": "unavailable: nflverse participation data is not published for the current season",
                "routes_run": "unavailable",
                "man_zone_splits": "unavailable: not in free data",
            },
            "ftn_read_semantics": ftn_semantics(),
        }


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
def waivers(position: str | None = None, recommendation: str | None = None, limit: int = Query(150, le=300)):
    with db() as con:
        lg = _league(con)
        season = lg["season"]
        params: list = [lg["league_id"], season]
        where = ""
        if position:
            where += " AND w.position = ?"
            params.append(position.upper())
        if recommendation:
            where += " AND w.recommendation = ?"
            params.append(recommendation.upper())
        params.append(limit)
        out = rows(con, f"""
            SELECT w.*, fp.full_name, fp.pro_team, fp.injury_status, fp.percent_owned, fp.percent_change,
                   s.trend_label, s.classification, s.games, s.xfp_per_game, s.fp_per_game,
                   s.opportunity_basis, s.opportunity_tier, s.role_trend, s.market_signal, pc.labels AS context_labels,
                   d.full_name AS drop_name, d.position AS drop_position
            FROM waiver_recommendations w
            JOIN fantasy_players fp ON fp.espn_player_id = w.espn_player_id
            LEFT JOIN player_id_map m ON m.espn_player_id = w.espn_player_id
            LEFT JOIN player_scores s ON s.gsis_id = m.gsis_id AND s.season = w.season
            LEFT JOIN player_context pc ON pc.gsis_id = m.gsis_id AND pc.season = w.season
            LEFT JOIN fantasy_players d ON d.espn_player_id = w.suggested_drop_id
            WHERE w.league_id = ? AND w.season = ? {where}
            ORDER BY CASE w.recommendation WHEN 'CLAIM' THEN 0 WHEN 'WAIT' THEN 1 ELSE 2 END, w.waiver_score DESC
            LIMIT ?
        """, params)
        return {"league": lg, "players": out, "computed_at": out[0]["computed_at"] if out else None}


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
                              WHERE m.match_confidence = 'medium' ORDER BY fp.percent_owned DESC NULLS LAST LIMIT 100""")
        conflicts = rows(con, "SELECT * FROM player_id_map WHERE notes LIKE 'Conflicting%' LIMIT 100")
        runs = rows(con, "SELECT run_id, source, started_at, finished_at, status, message FROM sync_runs ORDER BY started_at DESC LIMIT 30")
        issues = rows(con, "SELECT * FROM validation_issues ORDER BY severity, check_name") \
            if table_exists(con, "validation_issues") else []
        external = _external_quality(con)
        return {"mapping_summary": by_method, "unmatched": unmatched, "medium_confidence": medium,
                "conflicts": conflicts, "sync_runs": runs, "sync": _sync_status(con),
                "validation_issues": issues, "schema_version": schema_version(con), "external": external}


def _external_quality(con) -> dict:
    from ..config import get_settings
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
