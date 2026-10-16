"""Player Comparison Lab: two players side by side from numbers the app already stores.

Nothing here forecasts. It reads ESPN's weekly projections and the opt-in Fantasy Intel series
(`projections.player_forward`), nflverse weekly usage (`player_week_usage`), `player_scores` (ratings, trend, role
evidence), ESPN weekly league points and the latest injury designations, then aligns and compares them. The
contract (field names, units, thresholds, finding keys) is docs/player_comparison_lab.md.

Leakage rule (pregame only): `as_of_week` is the league's current_week, the first unplayed fantasy week, and
`completed_through_week = as_of_week - 1`. Every historical number (usage, league points per game, the weekly series,
recent games, stability) uses weeks <= completed_through_week for BOTH players, even when one of them has already
played this week. Projections are the stored pregame `projected_points` and never an actual. ESPN's
`season_avg_points` is not used: it can include the in-progress week.

Conservative by construction: NULL is unknown (never 0), an edge is claimed only past a materiality threshold,
ratings are compared only on the same scale/basis/position, and the absence of an injury report is never described
as healthy.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from statistics import mean, stdev

from ..db.connection import fetch_dicts, table_exists
from . import forward_ours
from .availability import ESPN_STATUS, NFL_REPORT_STATUS, SEVERITY, injury_order_sql
from .player_scores import played
from .projections import league_window, load_schedule, player_forward

MIN_USAGE_GAMES = 3          # a usage row needs this many completed games for BOTH players to claim an edge
MIN_STABILITY_GAMES = 4      # coefficient of variation needs at least this many games with the metric
STABILITY_GAP = 0.10         # CV gap that counts as "more stable"
RECENT_GAMES = 4
NEXT_WEEK_EDGE = 0.5
ROS_EDGE_FLOOR, ROS_EDGE_PCT = 3.0, 0.03
TREND_EDGE = 0.15

SAME_PLAYER_NOTE = "Same player selected twice."
POSITION_RELATIVE_NOTE = ("Position-relative ratings: a WR 80 and an RB 80 are each 80th percentile of their own "
                          "position, not equal value")
DISCLAIMER = ("Research comparison, not a start/sit or trade verdict. It only reads and lines up stored projections "
              "and past-week usage; it does not estimate points, win probability or trade value.")
NO_DESIGNATION_NOTE = ("No injury designation is listed. That is not confirmation he will play; reports can lag.")

NO_USAGE = {"K", "D/ST", "DST"}
SKILL = {"QB", "RB", "WR", "TE"}
# Position validity of each usage metric (docs/player_comparison_lab.md); league_pts_pg is valid for everyone.
VALID_POSITIONS: dict[str, set[str]] = {
    "snap_share": {"QB", "RB", "WR", "TE"},
    "carries_pg": {"QB", "RB"},
    "targets_pg": {"RB", "WR", "TE"},
    "target_share": {"RB", "WR", "TE"},
    "air_yards_pg": {"WR", "TE"},
    "xfp_pg": {"QB", "RB", "WR", "TE"},
    "ppr_pg": {"QB", "RB", "WR", "TE"},
    "league_pts_pg": {"QB", "RB", "WR", "TE", "K", "D/ST", "DST"},
}
WHY_QB_RECEIVING = "Receiving usage is not a QB role measure; passing volume (dropbacks) is not in the usage data."
WHY_RB_AIR_YARDS = "Air yards describe downfield receiving roles; not meaningful for a running back."
WHY_REC_CARRIES = "Carries are not a receiver role measure."
WHY_NO_USAGE_DATA = "No NFL usage data for kickers and team defenses."
WHY_NO_RECORD = "No usage record"

# key, label, unit, usage column (None: ESPN league points), decimals. Table order of the contract.
USAGE_METRICS: list[tuple[str, str, str, str | None, int]] = [
    ("snap_share", "Snap share", "share", "offense_pct", 4),
    ("carries_pg", "Carries per game", "per_game", "rush_attempts", 2),
    ("targets_pg", "Targets per game", "per_game", "targets", 2),
    ("target_share", "Target share", "share", "target_share", 4),
    ("air_yards_pg", "Air yards per game", "yards_per_game", "air_yards", 1),
    ("xfp_pg", "Expected PPR points per game (xFP)", "pts_per_game", "xfp", 2),
    ("ppr_pg", "PPR points per game (nflverse)", "pts_per_game", "fantasy_points_ppr", 2),
    ("league_pts_pg", "League points per game (ESPN)", "pts_per_game", None, 2),
]
PCT_TREND_METRICS = {"offense_pct", "target_share", "backfield_rush_share", "team_rush_share"}
STATUS_LABELS = {"QUESTIONABLE": "Questionable", "DOUBTFUL": "Doubtful", "OUT": "Out", "IR": "Injured reserve",
                 "SUSPENDED": "Suspended"}


class CompareError(Exception):
    """Base for the errors the API maps to HTTP statuses."""


class UnknownPlayer(CompareError):
    def __init__(self, which: str, pid: int):
        super().__init__(f"Unknown ESPN player id {pid} ({which})")
        self.which, self.pid = which, pid


class LeagueUnavailable(CompareError):
    """No synced league, or its current week is unknown (the leakage cutoff cannot be derived)."""


# ----------------------------------------------------------------------------------------------- pure helpers


def applicability(position: str | None, key: str) -> tuple[bool, str | None]:
    """(applicable, why not) for a usage metric at a position."""
    if position in VALID_POSITIONS[key]:
        return True, None
    if position in NO_USAGE:
        return False, WHY_NO_USAGE_DATA
    if position == "QB" and key in ("targets_pg", "target_share", "air_yards_pg"):
        return False, WHY_QB_RECEIVING
    if position == "RB" and key == "air_yards_pg":
        return False, WHY_RB_AIR_YARDS
    if position in ("WR", "TE") and key == "carries_pg":
        return False, WHY_REC_CARRIES
    return False, f"No usage metrics are defined for position {position or 'unknown'}."


def completed_games(usage: list[dict], completed_through: int) -> list[dict]:
    """Usage rows of games actually played (`player_scores.played`) in weeks <= completed_through, oldest first.
    A week with no row (bye) or a row with no snaps and no opportunities (inactive) is not a game."""
    return sorted((r for r in usage if r["week"] <= completed_through and played(r)), key=lambda r: r["week"])


def aggregate_usage(games: list[dict]) -> dict[str, float | None]:
    """Per-game means of the nflverse usage metrics over `games`; a NULL cell is skipped, never read as 0, and a
    metric with no value at all is None."""
    out: dict[str, float | None] = {}
    for key, _, _, col, decimals in USAGE_METRICS:
        if col is None:
            continue
        vals = [g[col] for g in games if g.get(col) is not None]
        out[key] = round(float(mean(vals)), decimals) if vals else None
    return out


def scored_game_weeks(points: dict[int, float], team: str | None, sched_games: dict, covered: set[int],
                      played_weeks: set[int]) -> tuple[dict[int, float], list[int]]:
    """ESPN league points of weeks he could have played: (week -> points, weeks left out because the schedule
    cannot say whether a 0 was a bye). A bye week (his team has no game in a week the schedule covers) is never a
    0; where the schedule does not cover the week a 0 without a played usage row is left out, not counted."""
    out: dict[int, float] = {}
    unknown: list[int] = []
    for week, pts in sorted(points.items()):
        if team and week in covered:
            if (team, week) not in sched_games:
                continue
        elif pts == 0 and week not in played_weeks:
            unknown.append(week)
            continue
        out[week] = pts
    return out, unknown


def coefficient_of_variation(values: list[float]) -> float | None:
    """Sample standard deviation over the mean; None below MIN_STABILITY_GAMES values or for a non-positive mean."""
    if len(values) < MIN_STABILITY_GAMES:
        return None
    m = mean(values)
    return round(stdev(values) / m, 4) if m > 0 else None


def _values(games: list[dict], col: str) -> list[float]:
    return [g[col] for g in games if g.get(col) is not None]


def stability_block(games: list[dict]) -> dict:
    """{"cv", "games", "metric"}: weekly xFP, or opportunities when xFP cannot support the measure."""
    for metric, col in (("xfp", "xfp"), ("opportunities", "opportunities")):
        vals = _values(games, col)
        cv = coefficient_of_variation(vals)
        if cv is not None:
            return {"cv": cv, "games": len(vals), "metric": metric}
    return {"cv": None, "games": len(games), "metric": None}


def pair_stability(games_a: list[dict], games_b: list[dict]) -> dict:
    """CVs of both players on ONE metric: xFP, else opportunities (when xFP is short for either)."""
    for metric, col in (("xfp", "xfp"), ("opportunities", "opportunities")):
        va, vb = _values(games_a, col), _values(games_b, col)
        ca, cb = coefficient_of_variation(va), coefficient_of_variation(vb)
        if ca is not None and cb is not None:
            return {"metric": metric, "a": ca, "b": cb, "games_a": len(va), "games_b": len(vb)}
    return {"metric": None, "a": None, "b": None,
            "games_a": max(len(_values(games_a, "xfp")), len(_values(games_a, "opportunities"))),
            "games_b": max(len(_values(games_b, "xfp")), len(_values(games_b, "opportunities")))}


def edge_threshold(unit: str, key: str = "", a: float | None = None, b: float | None = None) -> float:
    """Materiality threshold of a row (docs/player_comparison_lab.md)."""
    if key in ("ros_espn", "ros_ours"):
        return max(ROS_EDGE_FLOOR, ROS_EDGE_PCT * max(abs(a or 0.0), abs(b or 0.0)))
    return {"pts": NEXT_WEEK_EDGE, "per_game": 0.5, "pts_per_game": 0.5, "yards_per_game": 5.0, "share": 0.02,
            "games": 1.0, "trend": TREND_EDGE, "rating": 5.0}[unit]


def edge_for(a: float | None, b: float | None, threshold: float) -> str:
    """"a" / "b" when the gap reaches the threshold, "even" when it does not, "n/a" when a side is unknown.
    Higher is better. The gap is rounded so float noise cannot flip a gap of exactly the threshold."""
    if a is None or b is None:
        return "n/a"
    diff = round(a - b, 9)
    if abs(diff) < threshold - 1e-9:
        return "even"
    return "a" if diff > 0 else "b"


def scoring_label(reception_points: float | None, synced: bool = True) -> str:
    if not synced or reception_points is None:
        return "League scoring (settings not synced)"
    rp = float(reception_points)
    if rp == 1:
        return "PPR (1 pt per reception)"
    if rp == 0.5:
        return "Half PPR (0.5 pt per reception)"
    if rp == 0:
        return "Standard (no reception points)"
    return f"{rp:g} pt per reception"


def scoring_block(lg: dict) -> dict:
    items = lg.get("scoring_items")
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            items = None
    synced = bool(items) and lg.get("reception_points") is not None
    rp = lg.get("reception_points")
    if synced:
        note = ("ESPN league points and projections use this league's scoring. nflverse PPR per game and xFP are "
                "always full PPR (1 pt per reception), so they can differ from league points.")
    else:
        note = ("League scoring settings are not synced. ESPN league points and projections are still ESPN's own "
                "numbers in this league's scoring, while nflverse PPR per game is always full PPR.")
    return {"scoring_type": lg.get("scoring_type"), "reception_points": None if rp is None else float(rp),
            "settings_synced": synced, "label": scoring_label(rp, synced), "note": note}


def ratings_block(score: dict | None) -> dict:
    """Opportunity / production ratings: the 0-100 column when present (ratings-100.v1), else the legacy 0-10
    column, flagged and never rescaled."""
    out = {"opportunity": None, "production": None, "basis": None, "version": None, "as_of_week": None}
    if not score:
        return out

    def rating(col100: str, col10: str) -> dict | None:
        if score.get(col100) is not None:
            return {"value": float(score[col100]), "scale": 100, "legacy": False}
        if score.get(col10) is not None:
            return {"value": float(score[col10]), "scale": 10, "legacy": True}
        return None

    out["opportunity"] = rating("opportunity_score_100", "opportunity_score")
    out["production"] = rating("production_score_100", "production_score")
    out["basis"] = score.get("opportunity_basis")
    out["version"] = score.get("rating_scale_version")
    out["as_of_week"] = score.get("as_of_week")
    return out


def role_evidence(components: dict | None) -> list[dict]:
    """player_scores trends with an UP or DOWN label, as role evidence."""
    out = []
    for t in (components or {}).get("trends") or []:
        if t.get("label") in ("UP", "DOWN"):
            out.append({"metric": t["metric"], "label": t["label"], "baseline_avg": t.get("baseline_avg"),
                        "recent_avg": t.get("recent_avg"), "delta": t.get("delta"),
                        "unit": "pct" if t["metric"] in PCT_TREND_METRICS else "per_game",
                        "baseline_weeks": t.get("baseline_weeks") or [], "recent_weeks": t.get("recent_weeks") or []})
    return out


def availability_block(espn_status: str | None, report: dict | None, as_of_week: int) -> dict:
    """What is known about his availability. ESPN ACTIVE/NORMAL is no designation, and no designation is stated as
    exactly that: never as healthy or available."""
    signals: list[tuple[int, str, str]] = []       # (severity, status label, source)
    espn = (espn_status or "").strip().upper()
    if espn and espn not in ("ACTIVE", "NORMAL"):
        status = ESPN_STATUS.get(espn)
        signals.append((SEVERITY.get(status, 1), STATUS_LABELS.get(status) or espn.replace("_", " ").capitalize(),
                        "ESPN"))
    current = bool(report) and report["week"] == as_of_week
    if current and report.get("report_status") in NFL_REPORT_STATUS:
        status = NFL_REPORT_STATUS[report["report_status"]]
        signals.append((SEVERITY[status], STATUS_LABELS[status], f"NFL report, week {report['week']}"))
    if not signals:
        label, note = "No designation reported", NO_DESIGNATION_NOTE
    else:
        top = max(s[0] for s in signals)
        lead = [s for s in signals if s[0] == top]
        label = f"{lead[0][1]} ({' and '.join(s[2] for s in lead)})"
        other = [s for s in signals if s[0] != top]
        if other:
            label += f"; also {other[0][1]} ({other[0][2]})"
        note = "Designations can change before kickoff; check the latest official report before acting."
    if report and not current:
        note += f" The latest NFL report on file is from week {report['week']}, not week {as_of_week}."
    return {"espn_status": espn_status, "has_designation": bool(signals),
            "nfl_report": None if not report else {k: report.get(k) for k in
                                                   ("week", "report_status", "report_primary_injury",
                                                    "practice_status")},
            "report_is_current": None if not report else current, "label": label, "note": note}


# ------------------------------------------------------------------------------------------------- loading


@dataclass
class Side:
    pid: int
    name: str
    position: str | None
    card: dict
    games: list[dict]
    league_games: int
    notes: list[str] = field(default_factory=list)       # limitations raised while loading this player


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def load_league(con) -> dict:
    rows = fetch_dicts(con, """SELECT league_id, season, name, scoring_type, reception_points, current_week,
                               final_scoring_period, scoring_items FROM leagues ORDER BY synced_at DESC LIMIT 1""")
    if not rows:
        raise LeagueUnavailable("ESPN league not synced yet. Run scripts/sync_espn.py.")
    if not rows[0]["current_week"]:
        raise LeagueUnavailable("League current week unknown. Run scripts/sync_espn.py.")
    return rows[0]


def _player_row(con, season: int, pid: int) -> dict | None:
    rows = fetch_dicts(con, """
        SELECT fp.espn_player_id, fp.full_name, fp.position, fp.pro_team, fp.injury_status, fp.availability_status,
               fp.on_team_id, fp.percent_owned, m.gsis_id, m.match_method, m.match_confidence
        FROM fantasy_players fp LEFT JOIN player_id_map m ON m.espn_player_id = fp.espn_player_id
        WHERE fp.espn_player_id = ?""", [pid])
    return rows[0] if rows else None


def projection_block(fwd: dict | None, name: str, as_of_week: int, source: str) -> tuple[dict | None, list[str]]:
    """The card's `projection` from `player_forward`'s output, and the limitations it implies. ESPN's numbers are
    taken as stored; a bye is never a 0 and a missing projection is None."""
    if fwd is None:
        return None, [f"{name}: the league window is unknown, so no projection is available."]
    notes: list[str] = []
    independent = source == "independent"
    ours = fwd.get("ours") if independent else None
    ours_by_week = {e["week"]: e for e in (ours or {}).get("weeks", [])}
    def ours_cell(entry: dict | None, bye: bool | None) -> tuple[float | None, str | None]:
        """(value, origin) of the independent series for a week. A bye is reported as a bye, never as the run's 0."""
        if entry is None:
            return None, None
        if bye:
            return None, forward_ours.BYE
        return (entry["value"] if entry["origin"] in forward_ours.OURS_ORIGINS else None), entry["origin"]

    weeks = []
    for w in fwd["weeks"]:
        ours_value, ours_origin = ours_cell(ours_by_week.get(w["week"]), w["on_bye"])
        weeks.append({
            "week": w["week"], "opponent": w["opponent"], "is_home": w["is_home"], "on_bye": w["on_bye"],
            "phase": w["phase"], "espn": None if w["on_bye"] else w["espn_projected_points"],
            "ours": ours_value, "ours_origin": ours_origin,
        })
    nw_src = next((w for w in fwd["weeks"] if w["week"] == as_of_week), None)
    entry = ours_by_week.get(as_of_week)
    nw_ours, nw_origin = ours_cell(entry, nw_src["on_bye"] if nw_src else None)
    next_week = {
        "week": as_of_week, "on_bye": nw_src["on_bye"] if nw_src else None,
        "opponent": nw_src["opponent"] if nw_src else None, "is_home": nw_src["is_home"] if nw_src else None,
        "espn": None if not nw_src or nw_src["on_bye"] else nw_src["espn_projected_points"],
        "ours": nw_ours, "ours_origin": nw_origin, "ours_horizon": entry["horizon"] if entry else None,
    }
    summary = fwd["summary"]
    games_remaining = None
    if weeks and all(w["on_bye"] is not None for w in weeks):
        games_remaining = sum(1 for w in weeks if not w["on_bye"])
    ros = {
        "espn": summary["ros_projected_points"], "espn_weeks_available": summary["ros_weeks_available"],
        "espn_weeks_expected": summary["ros_weeks_expected"],
        "ours": ours["ros_projected_points"] if ours else None,
        "ours_available": bool(ours["available"]) if independent and ours else (False if independent else None),
        "ours_reason": None, "ours_weeks_available": ours["ros_weeks_available"] if ours else None,
        "ours_weeks_expected": ours["ros_weeks_expected"] if ours else None, "ours_experimental": True,
    }
    if independent:
        ros["ours_reason"] = (ours or {}).get("reason") or (
            None if ros["ours"] is not None else
            "Not every remaining non-bye regular-season week has an independent estimate.")
        if entry is not None and entry.get("horizon") not in (None, 0):
            notes.append(f"{name}: the independent number for week {as_of_week} comes from a run made "
                         f"{entry['horizon']} week(s) earlier, so it is experimental.")
    if summary["ros_weeks_available"] == 0:
        notes.append(f"{name}: ESPN has no projection for any remaining week.")
    elif summary["ros_projected_points"] is None:
        notes.append(f"{name}: ESPN projects {summary['ros_weeks_available']} of {summary['ros_weeks_expected']} "
                     "remaining weeks, so a rest-of-season total is unavailable.")
    if nw_src and not nw_src["on_bye"] and nw_src["espn_projected_points"] is None and summary["ros_weeks_available"]:
        notes.append(f"{name}: ESPN has no projection for week {as_of_week}.")
    if not fwd.get("schedule_available"):
        notes.append("The NFL schedule is not loaded, so bye weeks and games remaining are unknown.")
    return {"next_week": next_week, "ros": ros, "bye_weeks": summary["bye_weeks"],
            "bye_weeks_remaining": summary["bye_weeks_remaining"], "games_remaining": games_remaining,
            "weeks": weeks, "projection_synced_at": _iso(fwd.get("projection_synced_at"))}, notes


def _load_side(con, ctx: dict, pid: int, which: str) -> Side:
    season, as_of, done = ctx["season"], ctx["as_of"], ctx["completed"]
    p = _player_row(con, season, pid)
    if p is None:
        raise UnknownPlayer(which, pid)
    name = p["full_name"] or f"Player {pid}"
    position, team, gsis = p["position"], p["pro_team"], p["gsis_id"]
    notes: list[str] = []
    fantasy_team = None
    if p["availability_status"] == "ONTEAM" and p["on_team_id"] is not None:
        t = fetch_dicts(con, "SELECT name FROM fantasy_teams WHERE league_id = ? AND season = ? AND team_id = ?",
                        [ctx["league_id"], season, p["on_team_id"]])
        fantasy_team = t[0]["name"] if t else None

    usage = fetch_dicts(con, "SELECT * FROM player_week_usage WHERE season = ? AND gsis_id = ? AND week <= ?",
                        [season, gsis, done]) if gsis else []
    games = completed_games(usage, done)
    if position in SKILL and not games:
        notes.append(f"{name}: no nflverse usage record through week {done}"
                     + ("" if gsis else " (no nflverse match)") + ", so usage metrics are unavailable.")
    played_by_week = {g["week"]: g for g in games}

    points = {r["scoring_period"]: r["actual_points"] for r in fetch_dicts(
        con, """SELECT scoring_period, actual_points FROM espn_player_weekly_points
                WHERE season = ? AND espn_player_id = ? AND scoring_period <= ? AND actual_points IS NOT NULL""",
        [season, pid, done])}
    scored, unknown = scored_game_weeks(points, team, ctx["sched_games"], ctx["covered"], set(played_by_week))
    if unknown:
        notes.append(f"{name}: the NFL schedule does not cover week(s) {', '.join(map(str, unknown))}, so 0-point "
                     "weeks without a usage record were left out of league points per game (they may be byes).")

    metrics = {}
    means = aggregate_usage(games)
    for key, _, _, col, decimals in USAGE_METRICS:
        ok, why = applicability(position, key)
        if not ok:
            metrics[key] = {"value": None, "applicable": False, "why": why}
            continue
        if col is None:
            value = round(float(mean(scored.values())), decimals) if scored else None
            why = None if value is not None else "No ESPN league points for completed weeks"
        else:
            value = means[key]
            why = None if value is not None else (WHY_NO_RECORD if not games else "Not reported in the usage data")
        metrics[key] = {"value": value, "applicable": True, "why": why}

    score = None
    if gsis:
        sc = fetch_dicts(con, "SELECT * FROM player_scores WHERE season = ? AND gsis_id = ?", [season, gsis])
        score = sc[0] if sc else None
    components = _json(score["components"]) if score else None
    if score and score["as_of_week"] is not None and score["as_of_week"] >= as_of:
        notes.append(f"{name}: ratings and usage trend were computed through week {score['as_of_week']}, which "
                     "includes games from the current week; treat them as post-game.")
    ratings = ratings_block(score)
    if any(r and r["legacy"] for r in (ratings["opportunity"], ratings["production"])):
        notes.append(f"{name}: ratings are on the legacy 0-10 scale and are not compared with 0-100 ratings.")

    report = None
    if gsis and table_exists(con, "nfl_injuries"):
        r = fetch_dicts(con, f"""SELECT week, report_status, report_primary_injury, practice_status FROM nfl_injuries
                                 WHERE season = ? AND gsis_id = ? AND week <= ? ORDER BY {injury_order_sql(con)}
                                 LIMIT 1""", [season, gsis, as_of])
        report = r[0] if r else None
    depth = None
    if gsis and table_exists(con, "nfl_depth_charts"):
        d = fetch_dicts(con, """SELECT pos_abb, pos_rank, dt, team FROM nfl_depth_charts
                                WHERE season = ? AND gsis_id = ? ORDER BY dt DESC LIMIT 1""", [season, gsis])
        depth = {k: _iso(d[0][k]) if k == "dt" else d[0][k] for k in ("pos_abb", "pos_rank", "dt", "team")} \
            if d else None

    fwd = player_forward(con, season, pid, team, source=ctx["source"])
    projection, proj_notes = projection_block(fwd, name, as_of, ctx["source"])
    notes += proj_notes

    weekly = []
    for week in sorted(set(played_by_week) | set(scored)):
        u = played_by_week.get(week) or {}
        game = ctx["sched_games"].get((team, week)) if team else None
        weekly.append({
            "week": week, "opponent": u.get("opponent") or (game[0] if game else None),
            "league_points": scored.get(week),
            "ppr": None if u.get("fantasy_points_ppr") is None else round(u["fantasy_points_ppr"], 2),
            "xfp": None if u.get("xfp") is None else round(u["xfp"], 2),
            "snap_share": None if u.get("offense_pct") is None else round(u["offense_pct"], 4),
            "carries": u.get("rush_attempts"), "targets": u.get("targets"),
        })

    card = {
        "player": {"espn_player_id": pid, "full_name": p["full_name"], "position": position, "pro_team": team,
                   "fantasy_team": fantasy_team, "availability_status": p["availability_status"],
                   "percent_owned": p["percent_owned"], "gsis_id": gsis, "match_method": p["match_method"],
                   "match_confidence": p["match_confidence"]},
        "projection": projection,
        "usage": {
            "games": len(games), "weeks": [g["week"] for g in games], "metrics": metrics,
            "trend": {"label": score["trend_label"] if score else None,
                      "score": score["usage_trend_score"] if score else None},
            "ratings": ratings, "stability": stability_block(games),
        },
        "weekly": weekly,
        "availability": availability_block(p["injury_status"], report, as_of),
        "role": {"depth_chart": depth, "role_trend": score["role_trend"] if score else None,
                 "opportunity_tier": score["opportunity_tier"] if score else None,
                 "evidence": role_evidence(components)},
        "recent_games": list(reversed(weekly))[:RECENT_GAMES],
    }
    return Side(pid, name, position, card, games, len(scored), notes)


# --------------------------------------------------------------------------------------------------- rows


def _row(key: str, label: str, group: str, unit: str, a, b, problems: list[str], identical: bool,
         note: str | None = None) -> dict:
    """One advantages-table row. `a`/`b` are the values to show (None when missing or not applicable); any problem,
    or an identical pair, makes the edge n/a."""
    if identical:
        edge, note = "n/a", SAME_PLAYER_NOTE
    elif problems:
        edge, note = "n/a", "; ".join(problems)
    else:
        edge = edge_for(a, b, edge_threshold(unit, key, a, b))
    return {"key": key, "label": label, "group": group, "unit": unit, "a": a, "b": b, "edge": edge, "note": note}


def _proj(s: Side) -> dict | None:
    return s.card["projection"]


def build_rows(sa: Side, sb: Side, source: str, identical: bool) -> list[dict]:
    independent = source == "independent"
    rows: list[dict] = []

    def next_week(field_: str, label: str, key: str):
        va, vb, problems = None, None, []
        for s, side in ((sa, "a"), (sb, "b")):
            proj = _proj(s)
            if proj is None:
                problems.append(f"No projection for {s.name} (league window unknown)")
                continue
            nw = proj["next_week"]
            if nw["on_bye"]:
                problems.append(f"{s.name} is on bye in week {nw['week']}")
            elif nw[field_] is None:
                if field_ == "ours" and nw["ours_origin"] == "espn":
                    problems.append(f"No independent estimate for {s.name} (ESPN's number is not shown here)")
                else:
                    problems.append(f"No {'independent' if field_ == 'ours' else 'ESPN'} projection for {s.name}")
            elif side == "a":
                va = nw[field_]
            else:
                vb = nw[field_]
        rows.append(_row(key, label, "projection", "pts", va, vb, problems, identical))

    next_week("espn", "Next week projection (ESPN)", "next_week_espn")
    if independent:
        next_week("ours", "Next week projection (Fantasy Intel)", "next_week_ours")

    def ros(field_: str, label: str, key: str, note: str | None):
        vals, problems = [], []
        for s in (sa, sb):
            proj = _proj(s)
            v = None if proj is None else proj["ros"][field_]
            vals.append(v)
            if v is None:
                if field_ == "ours":    # the (often long) reason is listed once under the limitations
                    problems.append(f"Independent rest-of-season total unavailable for {s.name}")
                elif proj is None:
                    problems.append(f"No projection for {s.name} (league window unknown)")
                else:
                    r = proj["ros"]
                    problems.append(f"ESPN rest-of-season total incomplete for {s.name} "
                                    f"({r['espn_weeks_available']} of {r['espn_weeks_expected']} weeks)")
        rows.append(_row(key, label, "projection", "pts", vals[0], vals[1], problems, identical, note))

    ros("espn", "Rest-of-season projection (ESPN)", "ros_espn", None)
    if independent:
        ros("ours", "Rest-of-season projection (Fantasy Intel)", "ros_ours",
            "Experimental beyond next week; remaining regular-season weeks only")

    gr = [None if _proj(s) is None else _proj(s)["games_remaining"] for s in (sa, sb)]
    problems = [f"Games remaining unknown for {s.name} (schedule incomplete)" for s, v in zip((sa, sb), gr) if v is None]
    rows.append(_row("games_remaining", "Games remaining", "projection", "games", gr[0], gr[1], problems, identical))

    for key, label, unit, col, _ in USAGE_METRICS:
        ma, mb = sa.card["usage"]["metrics"][key], sb.card["usage"]["metrics"][key]
        problems = []
        for s, m in ((sa, ma), (sb, mb)):
            if not m["applicable"]:
                # the why is on the card's metric (shown in the cell); repeating it here doubles it on the page
                problems.append(f"Not applicable for {s.name} ({s.position})")
            elif m["value"] is None:
                problems.append(f"{s.name}: {m['why']}")
        if not problems:
            n = (lambda s: s.league_games) if col is None else (lambda s: s.card["usage"]["games"])
            low = [s.name for s in (sa, sb) if n(s) < MIN_USAGE_GAMES]
            if low:
                problems.append("Fewer than 3 games for " + " and ".join(low))
        rows.append(_row(key, label, "usage", unit, ma["value"] if ma["applicable"] else None,
                         mb["value"] if mb["applicable"] else None, problems, identical))

    ta, tb = sa.card["usage"]["trend"], sb.card["usage"]["trend"]
    problems = [f"Usage trend not measurable for {s.name}" for s, t in ((sa, ta), (sb, tb)) if t["score"] is None]
    rows.append(_row("trend_score", "Usage trend score", "usage", "trend", ta["score"], tb["score"], problems,
                     identical))

    ra, rb = sa.card["usage"]["ratings"], sb.card["usage"]["ratings"]
    for key, label, field_ in (("opportunity_rating", "Opportunity rating", "opportunity"),
                               ("production_rating", "Production rating", "production")):
        xa, xb = ra[field_], rb[field_]
        problems = [f"No {field_} rating for {s.name}" for s, x in ((sa, xa), (sb, xb)) if x is None]
        if xa and xb:
            if sa.position != sb.position:
                problems.append(POSITION_RELATIVE_NOTE)
            if xa["legacy"] or xb["legacy"]:
                problems.append("Legacy 0-10 ratings are not compared with 0-100 ratings")
            elif field_ == "opportunity" and ra["basis"] != rb["basis"]:
                problems.append(f"Opportunity bases differ ({ra['basis']} vs {rb['basis']})")
        rows.append(_row(key, label, "rating", "rating", xa["value"] if xa else None, xb["value"] if xb else None,
                         problems, identical))
    return rows


# ------------------------------------------------------------------------------------------------ findings


def _finding(key: str, title: str, leader: str | None, text: str, basis: str) -> dict:
    return {"key": key, "title": title, "leader": leader, "text": text, "basis": basis}


def _lead(edge: str) -> str | None:
    return None if edge == "n/a" else edge


def _who(leader: str, sa: Side, sb: Side) -> tuple[Side, Side]:
    return (sa, sb) if leader == "a" else (sb, sa)


def finding_next_week(sa: Side, sb: Side, source: str, as_of_week: int) -> dict:
    title = "Higher current projected points"
    basis = (f"ESPN projected_points for week {as_of_week}, as stored before the games. Actual points are never "
             "used, and a bye or a missing projection is never treated as 0.")
    vals, problems = [], []
    for s in (sa, sb):
        proj = _proj(s)
        if proj is None:
            problems.append(f"{s.name} has no projection (league window unknown).")
            vals.append(None)
            continue
        nw = proj["next_week"]
        if nw["on_bye"]:
            problems.append(f"{s.name} is on bye in week {as_of_week}.")
        elif nw["espn"] is None:
            problems.append(f"{s.name} has no ESPN projection for week {as_of_week}.")
        vals.append(nw["espn"])
    if problems:
        return _finding("next_week", title, None, " ".join(problems) + " No next-week comparison is made.", basis)
    a, b = vals
    edge = edge_for(a, b, NEXT_WEEK_EDGE)
    if edge == "even":
        text = (f"{sa.name} ({a:.1f}) and {sb.name} ({b:.1f}) are within {NEXT_WEEK_EDGE} points of each other "
                f"for week {as_of_week} (ESPN); no meaningful edge.")
    else:
        win, lose = _who(edge, sa, sb)
        hi, lo = max(a, b), min(a, b)
        text = (f"{win.name} is projected for {hi:.1f} points in week {as_of_week} (ESPN) vs {lo:.1f} for "
                f"{lose.name}, a gap of {hi - lo:.1f}.")
    if source == "independent":
        oa, ob = (_proj(s)["next_week"]["ours"] for s in (sa, sb))
        if oa is not None and ob is not None:
            text += (f" Fantasy Intel (independent) projects {oa:.1f} for {sa.name} and {ob:.1f} for {sb.name}; "
                     "shown for reference, the call above is ESPN's.")
    return _finding("next_week", title, edge, text, basis)


def finding_stability(sa: Side, sb: Side) -> dict:
    title = "More stable opportunity profile"
    basis = (f"Coefficient of variation (sample standard deviation / mean) of weekly xFP, or of opportunities when "
             f"xFP is missing for either player, over completed games only. Needs {MIN_STABILITY_GAMES}+ games each "
             f"on the same metric; a gap of {STABILITY_GAP:.2f} or more counts as more stable. Lower is steadier; "
             "it says nothing about how high the numbers are.")
    st = pair_stability(sa.games, sb.games)
    if st["metric"] is None:
        return _finding("stability", title, None,
                        f"At least {MIN_STABILITY_GAMES} completed games with the same usage metric are needed for "
                        f"both players ({sa.name}: {st['games_a']}, {sb.name}: {st['games_b']}), so the data does "
                        "not support a call.", basis)
    metric = "weekly xFP" if st["metric"] == "xfp" else "weekly opportunities (targets + carries)"
    head = (f"On {metric}, {sa.name} has a CV of {st['a']:.2f} over {st['games_a']} games and {sb.name} "
            f"{st['b']:.2f} over {st['games_b']}.")
    if round(abs(st["a"] - st["b"]), 9) < STABILITY_GAP - 1e-9:
        return _finding("stability", title, "even", head + " The gap is too small to call.", basis)
    leader = "a" if st["a"] < st["b"] else "b"
    win, _ = _who(leader, sa, sb)
    return _finding("stability", title, leader, head + f" {win.name}'s weekly opportunity has varied less.", basis)


def finding_ros(sa: Side, sb: Side, source: str) -> dict:
    title = "Stronger rest-of-season projection"
    basis = ("Sum of ESPN's weekly projections from this week to the end of the season; a total exists only when "
             "every remaining week is projected.")
    ra = [None if _proj(s) is None else _proj(s)["ros"] for s in (sa, sb)]
    if any(r is None or r["espn"] is None for r in ra):
        why = []
        for s, r in zip((sa, sb), ra):
            if r is None:
                why.append(f"{s.name} has no projection (league window unknown).")
            elif r["espn"] is None:
                why.append(f"{s.name} has ESPN projections for {r['espn_weeks_available']} of "
                           f"{r['espn_weeks_expected']} remaining weeks.")
        return _finding("ros", title, None, " ".join(why) + " A rest-of-season total needs every remaining week, "
                        "so no comparison is made.", basis)
    a, b = ra[0]["espn"], ra[1]["espn"]
    thr = edge_threshold("pts", "ros_espn", a, b)
    edge = edge_for(a, b, thr)
    ga, gb = (_proj(s)["games_remaining"] for s in (sa, sb))
    games = (f" Games remaining: {sa.name} {ga}, {sb.name} {gb}." if ga is not None and gb is not None else "")
    if edge == "even":
        text = (f"{sa.name} ({a:.1f}) and {sb.name} ({b:.1f}) are within {thr:.1f} points over the rest of the "
                "season (ESPN); no meaningful edge." + games)
    else:
        win, lose = _who(edge, sa, sb)
        text = (f"{win.name} has {max(a, b):.1f} ESPN projected points over the rest of the season vs "
                f"{min(a, b):.1f} for {lose.name}." + games)
    if source == "independent":
        oa, ob = ra[0]["ours"], ra[1]["ours"]
        if oa is not None and ob is not None:
            text += (f" Fantasy Intel (experimental, regular season only) totals {oa:.1f} for {sa.name} and "
                     f"{ob:.1f} for {sb.name}.")
    return _finding("ros", title, edge, text, basis)


def finding_trend(sa: Side, sb: Side) -> dict:
    title = "Bigger apparent usage trend"
    basis = ("player_scores usage_trend_score: how far recent games moved from earlier ones across position-specific "
             "usage metrics (-1 to +1, needs 4+ games). A direction of usage, not a forecast.")
    ta, tb = sa.card["usage"]["trend"], sb.card["usage"]["trend"]
    unknown = [s.name for s, t in ((sa, ta), (sb, tb)) if t["score"] is None or t["label"] == "INSUFFICIENT"]
    if unknown:
        return _finding("trend", title, None, "Usage trend is not measurable for " + " and ".join(unknown)
                        + " (too few games), so no comparison is made.", basis)
    edge = edge_for(ta["score"], tb["score"], TREND_EDGE)
    head = (f"{sa.name}'s usage trend is {ta['label']} ({ta['score']:+.2f}); {sb.name}'s is {tb['label']} "
            f"({tb['score']:+.2f}).")
    if edge == "even":
        return _finding("trend", title, "even", head + " The gap is too small to call.", basis)
    win, _ = _who(edge, sa, sb)
    lead_score = ta["score"] if edge == "a" else tb["score"]
    move = "moved up more" if lead_score > 0 else "fallen less"
    return _finding("trend", title, edge, head + f" {win.name}'s usage has {move}.", basis)


# ----------------------------------------------------------------------------------------------- the loader


def _freshness(con) -> dict:
    last = {r["source"]: r["last_ok"] for r in fetch_dicts(
        con, "SELECT source, max(finished_at) AS last_ok FROM sync_runs WHERE status = 'ok' GROUP BY 1")}
    return {"espn_synced_at": _iso(last.get("espn")), "nfl_synced_at": _iso(last.get("nfl"))}


def compare(con, a: int, b: int, source: str = "espn") -> dict:
    """The comparison payload (docs/player_comparison_lab.md). Raises ValueError for an unknown `source`,
    UnknownPlayer (`a` first) for an unknown id and LeagueUnavailable without a synced league."""
    source = forward_ours.normalize(source)
    lg = load_league(con)
    season, as_of = lg["season"], int(lg["current_week"])
    done = as_of - 1
    for which, pid in (("a", a), ("b", b)):
        if _player_row(con, season, pid) is None:
            raise UnknownPlayer(which, pid)
    sched_games, covered = load_schedule(con, season, list(range(1, done + 1))) if done >= 1 else ({}, set())
    ctx = {"season": season, "league_id": lg["league_id"], "as_of": as_of, "completed": done, "source": source,
           "sched_games": sched_games, "covered": covered}
    sa = _load_side(con, ctx, a, "a")
    identical = a == b
    sb = Side(sa.pid, sa.name, sa.position, deepcopy(sa.card), sa.games, sa.league_games, list(sa.notes)) \
        if identical else _load_side(con, ctx, b, "b")

    window = league_window(con, season)
    limitations: list[str] = []
    scoring = scoring_block(lg)
    if not scoring["settings_synced"]:
        limitations.append("League scoring settings are not synced; ESPN numbers are still in this league's scoring.")

    if identical:
        summary = [_finding("same_player", "Same player selected twice", "even",
                            f"{sa.name} was selected twice, so there is nothing to compare. Pick a second player "
                            "to see differences.", "Both cards are the same player's stored data.")]
        notes = list(sa.notes)
    else:
        summary = [finding_next_week(sa, sb, source, as_of), finding_stability(sa, sb),
                   finding_ros(sa, sb, source), finding_trend(sa, sb)]
        notes = sa.notes + sb.notes
    if source == "independent":
        # One line per distinct reason: the run-level reasons are usually the same for both players.
        reasons: dict[str, list[str]] = {}
        for s in ((sa,) if identical else (sa, sb)):
            proj = s.card["projection"]
            if proj and proj["ros"]["ours_reason"]:
                reasons.setdefault(proj["ros"]["ours_reason"], []).append(s.name)
        for reason, names in reasons.items():
            who = "" if len(names) == 2 else f" for {names[0]}"
            notes.append(f"Fantasy Intel projections{who}: {reason}")
    for n in notes:
        if n not in limitations:
            limitations.append(n)

    return {
        "as_of_week": as_of, "completed_through_week": done, "final_week": window.final if window else None,
        "last_regular_season_week": window.last_regular if window else None, "source": source, "identical": identical,
        "same_position": sa.position == sb.position, "scoring": scoring, "players": [sa.card, sb.card],
        "rows": build_rows(sa, sb, source, identical), "summary": summary, "limitations": limitations,
        "disclaimer": DISCLAIMER, "freshness": _freshness(con),
    }

