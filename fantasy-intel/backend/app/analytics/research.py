"""Fantasy Research Lab: opportunity vs production for the whole NFL WR / RB / TE pool.

Read-only and deterministic. Built on `player_week_usage` (nflverse) and the stored
`player_scores`; nothing here is written back and no ESPN league data feeds a metric.
Definitions (docs/research_lab.md):

  qualifying game   a game the player played (offensive snaps or a target/carry, the same test as
                    player_scores.played) with BOTH nflverse xFP and nflverse PPR points. A played
                    game without xFP (ffopportunity not yet published) is excluded from every
                    average and reported, never filled with 0.
  window            season = every qualifying game; last3 / last5 = the player's 3 / 5 most recent
                    qualifying games (the player's games, not calendar weeks: byes and missed games are
                    skipped, and the weeks used are returned).
  xFP/G, PPR/G      sum over the window's qualifying games / number of those games.
  FPOE/G            mean of (PPR - xFP) over the SAME games, which equals PPR/G - xFP/G only
                    because both averages use one game set.
  shares            aggregate over the window: sum(player numerator) / sum(team denominator), over
                    games where both are known (target share uses nflverse team targets, air-yard
                    share nflverse team air yards, backfield share team RB designed rushes). Snap
                    share is the mean of weekly offense_pct (team snap totals can be rejected).
  per-game counts   sum over games where the count is known / those games; coverage is reported
                    when a source (play-by-play) missed some of the window's games.
  momentum          the existing trends.compute_trend windows (recent 2-3 games vs the up-to-5
                    before) on the season's qualifying games; confidence reflects sample coverage.

No routes, TPRR, YPRR or alignment are estimated: they appear only when an authorized external
import provides them. FTN read codes are not used.
"""

from __future__ import annotations

import re
from collections import defaultdict
from statistics import mean
from typing import Iterable

from ..db.connection import fetch_dicts, table_exists
from .availability import latest_injuries
from .player_scores import GAP_ABS, GAP_REL, MIN_GAMES_CLASSIFY, TIER_THRESHOLDS, percentile_scores, played, value_gap
from .trends import TREND_METRICS, compute_trend

POSITIONS = ("WR", "RB", "TE")
WINDOWS = {"season": None, "last3": 3, "last5": 5}
WINDOW_LABEL = {"season": "Season", "last3": "Last 3 games", "last5": "Last 5 games"}

# Default player-pool filters, per position. The volume measure is a per-game average over the window.
#   WR 4+ targets/G: wide enough to keep emerging receivers in view (the reference video used 7+
#      targets; that stays one click away). TE 3+ targets/G: tight ends see fewer targets, and a
#      blocking TE with 1 target/G has no fantasy question to answer. RB 8+ carries+targets/G: a
#      committee back clears it, a pure change-of-pace back does not.
DEFAULTS = {
    "WR": {"volume_metric": "targets_pg", "volume_label": "targets per game", "min_volume": 4.0, "min_games": 2},
    "TE": {"volume_metric": "targets_pg", "volume_label": "targets per game", "min_volume": 3.0, "min_games": 2},
    "RB": {"volume_metric": "opportunities_pg", "volume_label": "carries + targets per game",
           "min_volume": 8.0, "min_games": 2},
}

MOMENTUM_PPR_THRESHOLD = 2.0   # same size as the xFP trend threshold so the two deltas compare directly
TEAM_RE = re.compile(r"^[A-Z]{2,3}$")
GSIS_RE = re.compile(r"^[0-9A-Za-z-]{4,20}$")

# Leaderboard metrics that get a position-relative percentile (0-100) within the shown pool.
PERCENTILE_METRICS = {
    "WR": ["xfp_pg", "ppr_pg", "fpoe_pg", "target_share", "air_yards_share", "wopr", "targets_pg", "adot",
           "snap_share"],
    "TE": ["xfp_pg", "ppr_pg", "fpoe_pg", "target_share", "targets_pg", "rz_targets_pg", "snap_share", "wopr"],
    "RB": ["xfp_pg", "ppr_pg", "fpoe_pg", "designed_rushes_pg", "backfield_rush_share", "targets_pg",
           "rz_carries_pg", "gl_carries_pg", "snap_share"],
}

COLUMNS = """u.week, u.gsis_id, u.team, u.opponent, u.position, u.offense_snaps, u.offense_pct, u.targets,
    u.receptions, u.air_yards, u.target_share, u.air_yards_share, u.wopr, u.carries, u.designed_rushes,
    u.rush_attempts, u.team_rush_attempts, u.team_rb_carries, u.backfield_rush_share, u.team_rush_share,
    u.rz_targets, u.rz_carries, u.gl_carries, u.opportunities, u.fantasy_points_ppr, u.xfp, u.routes"""


class ResearchError(ValueError):
    """Invalid research filter (HTTP 422)."""


# ------------------------------------------------------------------ loading --

def team_totals_source(con) -> str:
    return "nfl_weekly_stats" if table_exists(con, "nfl_weekly_stats") else "player_week_usage"


def _team_totals_sql(con, season: int) -> str:
    """Team targets and air yards per team-week (the target / air-yard share denominators): nflverse
    weekly stats summed over every player, or, when that raw table is absent, the stored skill-player
    usage rows (which miss the rare target to a lineman; the source is reported in `data`)."""
    if team_totals_source(con) == "nfl_weekly_stats":
        return f"""SELECT team, week, sum(targets) AS team_targets, sum(receiving_air_yards) AS team_air_yards
                   FROM nfl_weekly_stats WHERE season = {int(season)} AND season_type = 'REG' GROUP BY 1, 2"""
    return f"""SELECT team, week, sum(targets) AS team_targets, sum(air_yards) AS team_air_yards
               FROM player_week_usage WHERE season = {int(season)} GROUP BY 1, 2"""


def resolve_season(con, season: int | None) -> int | None:
    if season is not None:
        return season
    if table_exists(con, "leagues"):
        row = con.execute("SELECT season FROM leagues ORDER BY synced_at DESC LIMIT 1").fetchone()
        if row and row[0]:
            return int(row[0])
    if table_exists(con, "player_week_usage"):
        row = con.execute("SELECT max(season) FROM player_week_usage").fetchone()
        if row and row[0]:
            return int(row[0])
    return None


def load_usage(con, season: int, gsis_id: str | None = None) -> list[dict]:
    """Every WR/RB/TE usage row of the season (one query), with team target / air-yard totals."""
    where = "u.season = ? AND u.position IN ('WR', 'RB', 'TE')"
    params: list = [season]
    if gsis_id is not None:
        where += " AND u.gsis_id = ?"
        params.append(gsis_id)
    return fetch_dicts(con, f"""
        SELECT {COLUMNS}, tm.team_targets, tm.team_air_yards
        FROM player_week_usage u
        LEFT JOIN ({_team_totals_sql(con, season)}) tm ON tm.team = u.team AND tm.week = u.week
        WHERE {where} ORDER BY u.gsis_id, u.week""", params)


def freshness(con, season: int) -> dict:
    """Latest NFL week, the latest week with published xFP, per-week coverage and sync times."""
    weeks = fetch_dicts(con, """
        SELECT week, count(*) AS rows, count(xfp) AS rows_with_xfp FROM player_week_usage
        WHERE season = ? AND position IN ('WR', 'RB', 'TE') GROUP BY week ORDER BY week""", [season])
    latest = max((w["week"] for w in weeks), default=None)
    xfp_weeks = [w["week"] for w in weeks if w["rows_with_xfp"]]
    partial = [w["week"] for w in weeks if w["rows"] and w["rows_with_xfp"] < 0.5 * w["rows"]]
    nfl_sync = analytics = None
    if table_exists(con, "sync_runs"):
        for r in fetch_dicts(con, """SELECT source, max(finished_at) AS at FROM sync_runs
                                     WHERE status = 'ok' AND source IN ('nfl', 'analytics') GROUP BY 1"""):
            if r["source"] == "nfl":
                nfl_sync = r["at"]
            else:
                analytics = r["at"]
    routes = con.execute("SELECT count(*) FROM player_week_usage WHERE season = ? AND routes IS NOT NULL",
                         [season]).fetchone()[0]
    return {
        "season": season,
        "latest_week": latest,
        "xfp_through_week": max(xfp_weeks, default=None),
        "weeks_with_data": [w["week"] for w in weeks],
        "weeks_missing_xfp": partial,
        "nfl_synced_at": nfl_sync,
        "analytics_computed_at": analytics,
        "route_rows": routes,
        "team_totals_source": team_totals_source(con),
        "scoring_basis": "nflverse full PPR (1 per reception); xFP is nflverse ffopportunity expected PPR points",
    }


def espn_mapping(con, gsis_ids: Iterable[str]) -> dict[str, dict]:
    """gsis -> {espn_player_id, status}. Only a high-confidence ID match links to ESPN; two different
    ESPN ids for one gsis id is ambiguous and links to neither."""
    ids = sorted(set(gsis_ids))
    if not ids or not table_exists(con, "player_id_map"):
        return {}
    rows = fetch_dicts(con, """SELECT gsis_id, espn_player_id, match_confidence FROM player_id_map
                               WHERE gsis_id IN (SELECT unnest(?::VARCHAR[]))""", [ids])
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["gsis_id"]].append(r)
    out = {}
    for g, rs in by.items():
        high = sorted({r["espn_player_id"] for r in rs if r["match_confidence"] == "high"})
        if len(high) == 1:
            out[g] = {"espn_player_id": high[0], "status": "verified"}
        elif len(high) > 1:
            out[g] = {"espn_player_id": None, "status": "ambiguous", "candidates": high}
        else:
            out[g] = {"espn_player_id": None, "status": "name_match_only"}
    return out


def _names(con, gsis_ids: Iterable[str]) -> dict[str, str]:
    ids = sorted(set(gsis_ids))
    if not ids or not table_exists(con, "nfl_players"):
        return {}
    return {r["gsis_id"]: r["display_name"] for r in fetch_dicts(
        con, "SELECT gsis_id, display_name FROM nfl_players WHERE gsis_id IN (SELECT unnest(?::VARCHAR[]))", [ids])}


def _scores(con, season: int, gsis_id: str | None = None) -> dict[str, dict]:
    if not table_exists(con, "player_scores"):
        return {}
    sql = """SELECT gsis_id, games, as_of_week, opportunity_tier, role_trend, market_signal, value_gap_label,
                    fp_per_game, xfp_per_game, fp_minus_xfp,
                    components->>'production_sustainability' AS production_sustainability
             FROM player_scores WHERE season = ?"""
    params: list = [season]
    if gsis_id is not None:
        sql += " AND gsis_id = ?"
        params.append(gsis_id)
    return {r["gsis_id"]: r for r in fetch_dicts(con, sql, params)}


def _espn_status(con, espn_ids: Iterable[int]) -> dict[int, dict]:
    ids = sorted({i for i in espn_ids if i is not None})
    if not ids or not table_exists(con, "fantasy_players"):
        return {}
    return {r["espn_player_id"]: r for r in fetch_dicts(con, """
        SELECT espn_player_id, injury_status, availability_status, on_team_id FROM fantasy_players
        WHERE espn_player_id IN (SELECT unnest(?::BIGINT[]))""", [ids])}


# ----------------------------------------------------------------- compute --

def group_players(rows: Iterable[dict]) -> dict[str, list[dict]]:
    """gsis -> that player's rows, sorted by week. The position is the latest row's (as in player_scores),
    so a player is in exactly one positional pool."""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["gsis_id"]].append(r)
    for rs in by.values():
        rs.sort(key=lambda r: r["week"])
    return by


def is_qualifying(r: dict) -> bool:
    return played(r) and r.get("xfp") is not None and r.get("fantasy_points_ppr") is not None


def split_games(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(qualifying games, played games excluded because xFP or PPR is missing)."""
    q = [r for r in rows if is_qualifying(r)]
    missing = [r for r in rows if played(r) and not is_qualifying(r)]
    return q, missing


def window_games(qualifying: list[dict], window: str) -> list[dict]:
    n = WINDOWS[window]
    return qualifying if n is None else qualifying[-n:]


def _per_game(games: list[dict], field: str) -> tuple[float | None, int]:
    vals = [g[field] for g in games if g.get(field) is not None]
    return (sum(vals) / len(vals) if vals else None), len(vals)


def _total(games: list[dict], field: str) -> int | float | None:
    vals = [g[field] for g in games if g.get(field) is not None]
    return sum(vals) if vals else None


def _share(games: list[dict], num: str, den: str) -> tuple[float | None, int]:
    pairs = [(g[num], g[den]) for g in games if g.get(num) is not None and g.get(den) is not None]
    d = sum(b for _, b in pairs)
    return (sum(a for a, _ in pairs) / d if d else None), len(pairs)


def _round(v, digits=4):
    return round(v, digits) if isinstance(v, float) else v


def summarize_window(games: list[dict], position: str) -> dict:
    """Per-game rates and aggregate shares over one set of qualifying games."""
    n = len(games)
    out: dict = {"games": n, "weeks": [g["week"] for g in games], "coverage": {}}
    if n == 0:
        return out
    xfp = sum(g["xfp"] for g in games)
    ppr = sum(g["fantasy_points_ppr"] for g in games)
    out.update(xfp_total=xfp, ppr_total=ppr, xfp_pg=xfp / n, ppr_pg=ppr / n,
               fpoe_pg=mean(g["fantasy_points_ppr"] - g["xfp"] for g in games))
    coverage: dict[str, int] = {}

    def rate(key: str, field: str):
        v, k = _per_game(games, field)
        out[key] = v
        if k < n:
            coverage[key] = k

    def share(key: str, num: str, den: str):
        v, k = _share(games, num, den)
        out[key] = v
        if k < n:
            coverage[key] = k

    rate("snap_share", "offense_pct")
    rate("targets_pg", "targets")
    rate("receptions_pg", "receptions")
    rate("opportunities_pg", "opportunities")
    rate("rz_targets_pg", "rz_targets")
    out["rz_targets_total"] = _total(games, "rz_targets")
    share("target_share", "targets", "team_targets")
    if position in ("WR", "TE"):
        rate("air_yards_pg", "air_yards")
        share("air_yards_share", "air_yards", "team_air_yards")
        # aDOT pairs numerator and denominator: only games that report both air yards and targets.
        paired = [g for g in games if g.get("air_yards") is not None and g.get("targets") is not None]
        tgt = sum(g["targets"] for g in paired)
        out["adot"] = sum(g["air_yards"] for g in paired) / tgt if tgt else None
        if len(paired) < n:
            coverage["adot"] = len(paired)
        ts, ays = out["target_share"], out["air_yards_share"]
        out["wopr"] = 1.5 * ts + 0.7 * ays if ts is not None and ays is not None else None
    if position == "RB":
        rate("carries_pg", "carries")
        rate("designed_rushes_pg", "designed_rushes")
        rate("rz_carries_pg", "rz_carries")
        rate("gl_carries_pg", "gl_carries")
        share("backfield_rush_share", "designed_rushes", "team_rb_carries")
        share("team_rush_share", "rush_attempts", "team_rush_attempts")
        out["rz_carries_total"] = _total(games, "rz_carries")
        out["gl_carries_total"] = _total(games, "gl_carries")
    routes, k = _per_game(games, "routes")
    if k:
        out["routes_pg"] = routes
        if k < n:
            coverage["routes_pg"] = k
    out["coverage"] = coverage
    return {key: _round(v) for key, v in out.items()}


def high_threshold(position: str) -> float:
    return TIER_THRESHOLDS["xfp"][position][0]


def production_group(position: str, s: dict) -> dict:
    """Descriptive opportunity/production group. Not a BUY or SELL call."""
    if not s.get("games"):
        return {"key": "insufficient", "label": "No qualifying games"}
    gap = value_gap(s["ppr_pg"], s["xfp_pg"], s["games"])
    high = s["xfp_pg"] >= high_threshold(position)
    if gap == "INSUFFICIENT":
        return {"key": "insufficient", "label": f"Fewer than {MIN_GAMES_CLASSIFY} games: not grouped", "gap": gap}
    side = {"USAGE_EXCEEDS_PRODUCTION": "below", "PRODUCTION_EXCEEDS_USAGE": "above", "ALIGNED": "inline"}[gap]
    labels = {
        (True, "below"): "High opportunity, production lagging",
        (True, "above"): "High opportunity, production above it",
        (True, "inline"): "High opportunity, production in line",
        (False, "below"): "Limited opportunity, production lagging",
        (False, "above"): "Production ahead of opportunity",
        (False, "inline"): "Limited opportunity, production in line",
    }
    return {"key": f"{'high' if high else 'limited'}_{side}", "label": labels[(high, side)], "gap": gap,
            "high_opportunity": high, "production": side}


def _confidence(n_recent: int, n_baseline: int) -> dict:
    if n_recent == 0:
        return {"level": "INSUFFICIENT", "label": "Not enough games", "n_recent": 0, "n_baseline": 0}
    level = "HIGH" if n_recent >= 3 and n_baseline >= 4 else "MEDIUM" if n_recent >= 3 else "LOW"
    return {"level": level, "label": f"{n_recent} recent vs {n_baseline} earlier games",
            "n_recent": n_recent, "n_baseline": n_baseline}


def momentum(qualifying: list[dict], position: str) -> dict:
    """Recent vs baseline on the season's qualifying games (existing trend windows). xFP and PPR use the
    same games, so the two deltas compare like with like."""
    thresholds = dict(TREND_METRICS.get(position, []))
    volume = "targets" if position in ("WR", "TE") else "opportunities"
    share = "target_share" if position in ("WR", "TE") else "backfield_rush_share"
    specs = [("xfp", thresholds.get("xfp", 2.0)), ("fantasy_points_ppr", MOMENTUM_PPR_THRESHOLD),
             (volume, thresholds.get(volume, 1.5)), ("offense_pct", thresholds.get("offense_pct", 0.08)),
             (share, thresholds.get(share, 0.04))]
    metrics = {}
    for field, thr in specs:
        t = compute_trend(field, [(r["week"], r.get(field)) for r in qualifying], thr)
        metrics[field] = {k: _round(v) for k, v in t.as_dict().items()}
    x, p = metrics["xfp"], metrics["fantasy_points_ppr"]
    if x["label"] == "INSUFFICIENT":
        reading = None
    elif x["label"] == "UP" and (p["delta"] or 0) < x["delta"] - MOMENTUM_PPR_THRESHOLD:
        reading = "Workload rising faster than production"
    elif x["label"] == "UP":
        reading = "Workload and production rising together"
    elif p["label"] == "UP" and x["label"] != "UP":
        reading = "Production rising without matching workload growth"
    elif x["label"] == "DOWN":
        reading = "Workload falling"
    else:
        reading = "No clear workload change"
    return {
        "xfp_delta": x["delta"], "ppr_delta": p["delta"],
        "xfp_recent": x["recent_avg"], "xfp_baseline": x["baseline_avg"],
        "ppr_recent": p["recent_avg"], "ppr_baseline": p["baseline_avg"],
        "recent_weeks": x["recent_weeks"], "baseline_weeks": x["baseline_weeks"],
        "label": x["label"], "reading": reading,
        "confidence": _confidence(0, 0) if x["label"] == "INSUFFICIENT" else _confidence(x["n_recent"], x["n_baseline"]),
        "metrics": metrics, "volume_metric": volume, "share_metric": share,
    }


def _market(score: dict | None) -> dict | None:
    if not score:
        return None
    return {k: score.get(k) for k in ("market_signal", "opportunity_tier", "role_trend", "value_gap_label",
                                      "production_sustainability", "as_of_week", "games", "fp_minus_xfp")}


def _injury(gsis: str, espn_id: int | None, injuries: dict, espn: dict) -> dict | None:
    rep = injuries.get(gsis)
    status = espn.get(espn_id, {}).get("injury_status") if espn_id is not None else None
    if status in ("ACTIVE", "NORMAL"):
        status = None
    if not rep and not status:
        return None
    return {"espn_status": status,
            "nfl_report": ({"week": rep["week"], "status": rep.get("report_status"),
                            "injury": rep.get("report_primary_injury"), "practice": rep.get("practice_status")}
                           if rep else None)}


def research_groups(position: str, player: dict) -> list[str]:
    """Exploratory groups (not recommendations) a player falls in for the window."""
    s, m, mk = player["window"], player["momentum"], player.get("market") or {}
    out = []
    if not s.get("games"):
        return out
    gap = player["group"].get("gap")
    role = mk.get("role_trend")
    if gap == "USAGE_EXCEEDS_PRODUCTION" and s["xfp_pg"] >= high_threshold(position) and role != "DOWN":
        out.append("underperforming_opportunity")
    if m["label"] == "UP" or (role == "UP" and m["confidence"]["level"] != "INSUFFICIENT"):
        out.append("emerging_role")
    if gap == "PRODUCTION_EXCEEDS_USAGE" and s["xfp_pg"] < high_threshold(position):
        out.append("production_ahead")
    return out


GROUP_INFO = {
    "underperforming_opportunity": {
        "title": "Underperforming opportunity leaders",
        "rule": "xFP/G at or above the high-opportunity line, PPR/G below xFP/G by the value-gap margin, "
                "role trend not falling.",
        "caveat": "A negative FPOE says the box score lags the role, not that it will catch up. Check the "
                  "role is stable and whether the player is below average at converting it.",
    },
    "emerging_role": {
        "title": "Emerging role increases",
        "rule": "xFP/G over the recent 2-3 games up by the trend threshold or more against the games before, "
                "or the existing composite role trend is up.",
        "caveat": "Recent windows are short. A one-week spike from an injury elsewhere on the roster can "
                  "reverse when that player returns.",
    },
    "production_ahead": {
        "title": "Production ahead of measured opportunity",
        "rule": "PPR/G above xFP/G by the value-gap margin while xFP/G is below the high-opportunity line.",
        "caveat": "Often touchdown or efficiency driven. Elite players can beat an average-player model "
                  "for a long time; this is not a prediction of regression.",
    },
}


def build_player(gsis: str, rows: list[dict], window: str, ctx: dict) -> dict | None:
    position = rows[-1]["position"]
    if position not in POSITIONS:
        return None
    qualifying, missing = split_games(rows)
    games = window_games(qualifying, window)
    s = summarize_window(games, position)
    mapping = ctx["mapping"].get(gsis, {"espn_player_id": None, "status": "unmapped"})
    espn_id = mapping.get("espn_player_id")
    # Current team from the latest played game, even one excluded for missing xFP (a trade shows at once).
    last = ([r for r in rows if played(r)] or rows)[-1]
    player = {
        "gsis_id": gsis,
        "name": ctx["names"].get(gsis) or gsis,
        "position": position,
        "team": last.get("team"),
        "espn_player_id": espn_id,
        "mapping_status": mapping["status"],
        "games_played": sum(1 for r in rows if played(r)),
        "excluded_games": ({"weeks": [r["week"] for r in missing],
                            "reason": "played, but nflverse xFP or PPR is missing for the game"} if missing else None),
        "last_game_week": qualifying[-1]["week"] if qualifying else None,
        "window": s,
        "momentum": momentum(qualifying, position),
        "market": _market(ctx["scores"].get(gsis)),
        "injury": _injury(gsis, espn_id, ctx["injuries"], ctx["espn"]),
    }
    player["group"] = production_group(position, s)
    player["research_groups"] = research_groups(position, player)
    return player


def _context(con, season: int, gsis_ids: list[str], gsis_filter: str | None = None) -> dict:
    mapping = espn_mapping(con, gsis_ids)
    return {
        "mapping": mapping,
        "names": _names(con, gsis_ids),
        "scores": _scores(con, season, gsis_filter),
        "injuries": latest_injuries(con, season),
        "espn": _espn_status(con, (m.get("espn_player_id") for m in mapping.values())),
    }


def validate_filters(position: str, window: str, min_games: int | None, min_volume: float | None,
                     team: str | None) -> dict:
    if position not in POSITIONS:
        raise ResearchError(f"position must be one of {', '.join(POSITIONS)}")
    if window not in WINDOWS:
        raise ResearchError(f"window must be one of {', '.join(WINDOWS)}")
    d = DEFAULTS[position]
    games = d["min_games"] if min_games is None else min_games
    cap = WINDOWS[window]
    if cap is not None and games > cap:
        raise ResearchError(f"min_games ({games}) cannot exceed the {WINDOW_LABEL[window].lower()} window ({cap})")
    if team is not None and not TEAM_RE.match(team):
        raise ResearchError("team must be an NFL team abbreviation such as KC or LA")
    return {"position": position, "window": window, "min_games": games,
            "min_volume": d["min_volume"] if min_volume is None else min_volume, "team": team,
            "volume_metric": d["volume_metric"], "volume_label": d["volume_label"]}


def _add_percentiles(position: str, players: list[dict]) -> None:
    for metric in PERCENTILE_METRICS[position]:
        values = {p["gsis_id"]: p["window"][metric] for p in players if p["window"].get(metric) is not None}
        pct = percentile_scores(values)
        for p in players:
            p.setdefault("percentiles", {})
            if p["gsis_id"] in pct:
                p["percentiles"][metric] = round(pct[p["gsis_id"]] * 10)


def build_opportunity(con, season: int | None, position: str, window: str = "season", min_games: int | None = None,
                      min_volume: float | None = None, team: str | None = None) -> dict:
    f = validate_filters(position, window, min_games, min_volume, team)
    season = resolve_season(con, season)
    base = {"filters": f, "defaults": DEFAULTS[position], "windows": WINDOW_LABEL,
            "high_opportunity_xfp": high_threshold(position),
            "value_gap_margin": {"abs": GAP_ABS, "rel": GAP_REL, "min_games": MIN_GAMES_CLASSIFY},
            "groups_info": GROUP_INFO}
    if season is None or not table_exists(con, "player_week_usage"):
        return {**base, "season": season, "status": "no_data", "data": None, "players": [], "teams": [],
                "pool": {"at_position": 0, "shown": 0, "excluded": {}},
                "message": "No NFL usage data is stored yet. Run scripts/sync_nfl.py."}
    by = {g: rs for g, rs in group_players(load_usage(con, season)).items() if rs[-1]["position"] == position}
    data = freshness(con, season)
    ctx = _context(con, season, list(by))
    players, excluded = [], {"no_qualifying_games": 0, "min_games": 0, "min_volume": 0, "team": 0}
    teams = set()
    for g, rs in by.items():
        p = build_player(g, rs, window, ctx)
        if p is None or not any(played(r) for r in rs):
            continue
        if p["team"]:
            teams.add(p["team"])
        s = p["window"]
        if not s["games"]:
            excluded["no_qualifying_games"] += 1
        elif s["games"] < f["min_games"]:
            excluded["min_games"] += 1
        elif (s.get(f["volume_metric"]) or 0) < f["min_volume"]:
            excluded["min_volume"] += 1
        elif team and p["team"] != team:
            excluded["team"] += 1
        else:
            players.append(p)
    _add_percentiles(position, players)
    players.sort(key=lambda p: (-p["window"]["xfp_pg"], p["name"]))
    at_position = len(players) + sum(excluded.values())
    return {**base, "season": season, "status": "ok" if players else "empty", "data": data,
            "players": players, "teams": sorted(teams),
            "pool": {"at_position": at_position, "shown": len(players), "excluded": excluded}}


# ------------------------------------------------------------ player view --

def _schedule(con, season: int) -> dict[int, set[str]] | None:
    if not table_exists(con, "nfl_schedules"):
        return None
    out: dict[int, set[str]] = defaultdict(set)
    for r in fetch_dicts(con, """SELECT week, home_team, away_team FROM nfl_schedules
                                 WHERE season = ? AND game_type = 'REG'""", [season]):
        out[r["week"]].update((r["home_team"], r["away_team"]))
    return out or None


def week_states(rows: list[dict], through_week: int | None, schedule: dict[int, set[str]] | None,
                reports: dict[int, dict] | None = None) -> list[dict]:
    """One entry per week 1..through_week saying what happened. A week without a usage row is a bye, a
    missed game or unknown: never a zero."""
    if not through_week:
        return []
    by_week = {r["week"]: r for r in rows}
    reports = reports or {}
    out = []
    for w in range(1, through_week + 1):
        r = by_week.get(w)
        prior = [x for x in rows if x["week"] < w]
        later = [x for x in rows if x["week"] > w]
        team = r["team"] if r else (prior[-1]["team"] if prior else later[0]["team"] if later else None)
        rep = reports.get(w)
        if r and is_qualifying(r):
            state, label = "played", "Played"
        elif r and played(r):
            state, label = "missing_xfp", "Played; xFP not published"
        elif r:
            state, label = "no_offensive_snaps", "No offensive snaps or touches"
        elif schedule is None or team is None:
            state, label = "unknown", "No record (schedule unknown)"
        elif team not in schedule.get(w, set()):
            state, label = "bye", "Bye"
        else:
            state = "did_not_play"
            label = f"Did not play (listed {rep['report_status']})" if rep and rep.get("report_status") \
                else "Did not play"
        entry = {"week": w, "state": state, "label": label, "team": team,
                 "opponent": r.get("opponent") if r else None}
        if r:
            entry.update({k: r.get(k) for k in (
                "xfp", "fantasy_points_ppr", "offense_pct", "targets", "target_share", "air_yards_share",
                "air_yards", "carries", "designed_rushes", "backfield_rush_share", "rz_targets", "rz_carries",
                "gl_carries", "opportunities", "routes")})
            if is_qualifying(r):
                entry["fpoe"] = round(r["fantasy_points_ppr"] - r["xfp"], 3)
        out.append(entry)
    return out


def _league_context(con, espn_id: int | None) -> dict | None:
    """Where the player sits in the synced ESPN league, only to offer trade / waiver links."""
    if espn_id is None or not table_exists(con, "fantasy_players") or not table_exists(con, "fantasy_teams"):
        return None
    fp = fetch_dicts(con, "SELECT availability_status, on_team_id FROM fantasy_players WHERE espn_player_id = ?",
                     [espn_id])
    if not fp:
        return None
    mine = fetch_dicts(con, """SELECT t.team_id FROM fantasy_teams t JOIN (
                                   SELECT league_id, season FROM leagues ORDER BY synced_at DESC LIMIT 1) l
                                 ON l.league_id = t.league_id AND l.season = t.season WHERE t.is_mine""") \
        if table_exists(con, "leagues") else []
    my_team = mine[0]["team_id"] if mine else None
    status, on_team = fp[0]["availability_status"], fp[0]["on_team_id"]
    return {"availability_status": status, "on_team_id": on_team if status == "ONTEAM" else None,
            "my_team_id": my_team, "is_mine": status == "ONTEAM" and my_team is not None and on_team == my_team}


def build_player_research(con, gsis_id: str, season: int | None = None) -> dict | None:
    if not GSIS_RE.match(gsis_id):
        raise ResearchError("gsis_id is not a valid nflverse player id")
    season = resolve_season(con, season)
    if season is None or not table_exists(con, "player_week_usage"):
        return None
    rows = load_usage(con, season, gsis_id)
    if not rows or rows[-1]["position"] not in POSITIONS:
        return None
    ctx = _context(con, season, [gsis_id], gsis_id)
    windows = {w: build_player(gsis_id, rows, w, ctx) for w in WINDOWS}
    season_view = windows["season"]
    data = freshness(con, season)
    reports = {w: r for (g, w), r in latest_injuries(con, season, per_week=True).items() if g == gsis_id} \
        if table_exists(con, "nfl_injuries") else {}
    return {
        "season": season,
        "data": data,
        "player": {k: season_view[k] for k in ("gsis_id", "name", "position", "team", "espn_player_id",
                                               "mapping_status", "games_played", "last_game_week",
                                               "excluded_games", "market", "injury")},
        "windows": {w: {"window": p["window"], "group": p["group"], "research_groups": p["research_groups"]}
                    for w, p in windows.items()},
        "momentum": season_view["momentum"],
        "timeline": week_states(rows, data["latest_week"], _schedule(con, season), reports),
        "league": _league_context(con, season_view["espn_player_id"]),
        "high_opportunity_xfp": high_threshold(season_view["position"]),
        "groups_info": GROUP_INFO,
    }
