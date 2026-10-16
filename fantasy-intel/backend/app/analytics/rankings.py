"""Player Rankings & Value Explorer (docs/player_rankings.md): every calculation behind GET /api/rankings.

Read-only. The pool is every QB / RB / WR / TE in the league's ESPN universe that has an ESPN projection in the
window, a player_scores row this season, or sits on a fantasy roster. Two different things are shown and never
mixed:

  projected points  absolute fantasy points in league scoring (ESPN's number unmodified; Ours opt-in). THIS ranks.
  ratings           position-relative 0-100.0 percentiles through `ratings_adapter`; sortable, never ranked on.

Ranks are competition ranks ("1, 2, 2, 4") of the ranked value over the whole pool and within each position,
computed BEFORE the position / search / roster filters, so a filter never renumbers a player. A player who cannot
be ranked says why (`projection.status`): missing, bye, incomplete, no_ours.

Windows: weekly = the league's current scoring period; rest of season = current .. last regular-season period (the
same weeks for every player and both sources). A total is NULL unless every required week is available
(analytics/projections.py: ESPN, analytics/forward_ours.py: Ours); a partial sum is never shown.

The number of queries is constant in the number of players (the pool, ESPN projections, usage aggregate, injuries,
schedule and roster names are each read once; Ours adds forward_ours._select_batch's bounded set).
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Hashable, Mapping
from typing import TypeVar

from ..db.connection import fetch_dicts
from . import forward_ours
from . import projection_source as ps
from .availability import availability, latest_injuries
from .projections import league_window, load_schedule
from .ratings_adapter import CONTRACT, public_rating

K = TypeVar("K", bound=Hashable)

POSITIONS = ("QB", "RB", "WR", "TE")
HORIZONS = ("week", "ros")
SORT_KEYS = ("rank", "name", "espn", "ours", "rating", "opportunity", "production", "xfp", "ppr", "consistency", "trend")
STATUSES = ("all", "rostered", "available")
MAX_LIMIT = 300

_ASC_DEFAULT = {"rank", "name", "consistency"}      # lower is better / alphabetical; every other key defaults to desc
_TREND_ORDER = {"UP": 2, "FLAT": 1, "DOWN": 0}
_ROSTER_STATUS = {"ONTEAM": "ROSTERED", "FREEAGENT": "FREE_AGENT", "WAIVERS": "WAIVERS"}
_SCORE_COLS = ("games", "opportunity_basis", "opportunity_tier", "role_trend", "market_signal", "opportunity_score",
               "production_score", "usage_trend_score", "fp_per_game", "xfp_per_game", "fp_minus_xfp", "value_score",
               "computed_at AS scores_computed_at", "as_of_week AS scores_as_of_week")
_SCORE_COMPONENTS = {"recent_opp": "recent_opportunity_per_game", "recent_ppr": "recent_ppr_per_game"}
_UNRANKED = ("missing", "bye", "incomplete", "no_ours")
_AVAIL_LABEL = {"QUESTIONABLE": "Questionable", "DOUBTFUL": "Doubtful", "OUT": "Out", "IR": "On injured reserve",
                "SUSPENDED": "Suspended"}
_TIER_WHY = {"HIGH": "High-volume role", "MEDIUM": "Mid-sized role", "LOW": "Small role"}
_SUSTAIN_WHY = {"ABOVE_EXPECTATION": "Scoring above what the role implies",
                "BELOW_EXPECTATION": "Role implies more points than scored", "ALIGNED": "Production in line with role"}
_SCORING_NOTE = ("Projected points are in league scoring; opportunity, production and consistency are full PPR "
                 "(nflverse) and are not comparable to projections.")


class RankingsUnavailable(Exception):
    """No synced league (or its current week is unknown): the API answers 503."""


def competition_ranks(values: Mapping[K, float | None]) -> dict[K, tuple[int | None, bool]]:
    """Descending competition rank ("1, 2, 2, 4") -> (rank, tied). Values equal after round(v, 2) tie, and every
    member of a tie group has tied=True. None / NaN -> (None, False)."""
    rounded: dict[K, float | None] = {}
    for key, v in values.items():
        rounded[key] = None if v is None or math.isnan(float(v)) else round(float(v), 2)
    first: dict[float, int] = {}
    size: dict[float, int] = {}
    for i, v in enumerate(sorted((v for v in rounded.values() if v is not None), reverse=True), 1):
        first.setdefault(v, i)
        size[v] = size.get(v, 0) + 1
    return {key: (None, False) if v is None else (first[v], size[v] > 1) for key, v in rounded.items()}


def _fold(text: str | None) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).casefold()


def _num(v) -> float | None:
    if v is None:
        return None
    x = float(v)
    return None if math.isnan(x) else x


def _ts(v) -> str | None:
    return None if v is None else str(v)


def _newest(values) -> str | None:
    vals = [v for v in values if v is not None]
    return str(max(vals)) if vals else None


def _validate(position, horizon, source, sort, dir, q, status, limit, offset) -> tuple:
    pos = str(position or "ALL").upper()
    if pos != "ALL" and pos not in POSITIONS:
        raise ValueError(f"position must be one of ALL, {', '.join(POSITIONS)}")
    hor = str(horizon or "").lower()
    if hor not in HORIZONS:
        raise ValueError(f"horizon must be one of {', '.join(HORIZONS)}")
    src = ps.normalize(source)     # ValueError: "source must be one of espn, independent"
    key = str(sort or "").lower()
    if key not in SORT_KEYS:
        raise ValueError(f"sort must be one of {', '.join(SORT_KEYS)}")
    if dir is None or dir == "":
        direction = "asc" if key in _ASC_DEFAULT else "desc"
    else:
        direction = str(dir).lower()
        if direction not in ("asc", "desc"):
            raise ValueError("dir must be one of asc, desc")
    stat = str(status or "").lower()
    if stat not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be an integer >= 0")
    return pos, hor, src, key, direction, _fold(str(q or "").strip()), stat, limit, offset


def _window(lw, horizon: str) -> dict:
    """The ranked weeks. ROS needs the end of the regular season; without it ESPN uses current..final (noted) and
    Ours is unavailable."""
    note = None
    if horizon == "week":
        weeks = [lw.current]
        regular = lw.last_regular is not None and lw.current <= lw.last_regular
    elif lw.last_regular is not None and lw.last_regular >= lw.current:
        weeks, regular = list(range(lw.current, lw.last_regular + 1)), True
    else:
        weeks, regular = lw.weeks, False
        note = ("The end of the regular season can't be derived from the league settings, so ESPN ranks the full "
                f"window (week {lw.current} to {lw.final}) and Ours is unavailable."
                if lw.last_regular is None else
                f"The regular season is over, so ESPN ranks the remaining weeks ({lw.current} to {lw.final}) "
                "and Ours is unavailable.")
    first, last = weeks[0], weeks[-1]
    return {"weeks": weeks, "first": first, "last": last,
            "label": f"Week {first}" if first == last else f"Weeks {first}-{last}",
            "regular_season_only": regular, "last_regular_season_week": lw.last_regular, "note": note}


def _tables(con) -> set[str]:
    return {r["table_name"] for r in fetch_dicts(
        con, "SELECT table_name FROM information_schema.tables WHERE table_name IN ('player_scores', 'player_week_usage')")}


def _pool(con, season: int, first: int, last: int, has_scores: bool) -> list[dict]:
    if has_scores:
        cols = ", ".join(f"s.{c}" for c in _SCORE_COLS) + ", " + ", ".join(
            f"TRY_CAST(s.components->>'{k}' AS DOUBLE) AS {a}" for a, k in _SCORE_COMPONENTS.items()) + \
            ", s.components->>'production_sustainability' AS sustainability"
        join, score_or, params = "LEFT JOIN player_scores s ON s.gsis_id = m.gsis_id AND s.season = ?", \
            " OR s.gsis_id IS NOT NULL", [season]
    else:
        names = [c.split(" AS ")[-1] for c in _SCORE_COLS] + list(_SCORE_COMPONENTS) + ["sustainability"]
        cols, join, score_or, params = ", ".join(f"NULL AS {n}" for n in names), "", "", []
    return fetch_dicts(con, f"""
        SELECT fp.espn_player_id, fp.full_name, fp.position, fp.pro_team, fp.injury_status, fp.availability_status,
               fp.on_team_id, fp.synced_at AS fp_synced_at, fp.detail_synced_at, m.gsis_id, {cols}
        FROM fantasy_players fp LEFT JOIN player_id_map m ON m.espn_player_id = fp.espn_player_id {join}
        WHERE fp.position IN ('QB', 'RB', 'WR', 'TE')
          AND (fp.availability_status = 'ONTEAM' OR fp.espn_player_id IN (
                 SELECT espn_player_id FROM espn_player_weekly_points
                 WHERE season = ? AND scoring_period BETWEEN ? AND ? AND projected_points IS NOT NULL){score_or})
    """, params + [season, first, last])


def _ours(con, season: int, win: dict, source: str, fp_ok: bool, why_scoring: str | None) -> dict | None:
    """The Ours selection for the window: None unless source=independent; otherwise a dict whose `available` says
    whether any run was found (False carries the reason)."""
    if source != "independent":
        return None
    weeks = win["weeks"] if win["regular_season_only"] else []
    if not fp_ok:
        return _ours_off(f"No independent projections: {why_scoring}.")
    if not weeks:
        return _ours_off("Ours covers the remaining regular-season weeks only, and the end of the regular "
                                   "season is unknown or already past")
    selected, unavailable = forward_ours._select_batch(con, season, weeks)
    runs = [selected[w]["run"] for w in weeks if selected.get(w, {}).get("run")]
    if not runs:
        skipped = [x for w in weeks for x in selected.get(w, {}).get("skipped", [])]
        if unavailable:
            return _ours_off(unavailable)
        if any(ps._gate_rejected(x) for x in skipped):
            return _ours_off(f"No {ps.MODEL_VERSION} projection run with an approved artifact is available "
                                       f"for {win['label'].lower()}")
        if skipped:
            return _ours_off(f"No stored independent projection run for {win['label'].lower()} passed the "
                                       "integrity checks")
        return _ours_off(f"No {ps.MODEL_VERSION} projection run is stored for {win['label'].lower()} yet")
    by_pid = {w: {r["espn_player_id"]: r for r in selected[w]["rows"] or []}
              for w in weeks if selected.get(w, {}).get("run")}
    versions = sorted({r["model_version"] for r in runs})
    served = ps.MODEL_VERSION if ps.MODEL_VERSION in versions else versions[0]
    reasons: list[str] = []
    for w in weeks:
        sel = selected.get(w)
        if sel and sel["run"]:
            r = forward_ours._week_reason(w, sel)
            if r and r not in reasons:
                reasons.append(r)
    no_run = [w for w in weeks if not (selected.get(w) or {}).get("run")]
    if no_run:
        reasons.append(f"No independent projection run is stored for week(s) {', '.join(map(str, no_run))}, so a "
                       "player's Ours total is unavailable while any required week is missing.")
    return {"available": True, "weeks": weeks, "selected": selected, "by_pid": by_pid, "versions": versions,
            "served": served, "runs": runs, "reason": " ".join(reasons) or None,
            "experimental": any(r.get("horizon_note") for r in runs)}


def _ours_off(reason: str) -> dict:
    return {"available": False, "reason": reason.rstrip(".")}


def _ours_week(state: dict, pid: int, week: int, *, production_only: bool) -> tuple[float, str] | None:
    """(value, origin) of one week for one player; None = no independent number for that week. With production_only
    (rest of season) a week served by another model than the production one does not count (forward_ours)."""
    sel = state["selected"].get(week)
    if not sel or not sel["run"] or (production_only and sel["run"]["model_version"] != ps.MODEL_VERSION):
        return None
    r = state["by_pid"].get(week, {}).get(pid)
    if r is None:
        return None
    if r["coverage_status"] == "projected" and r["expected_points"] is not None:
        return float(r["expected_points"]), ps.OURS
    if r["coverage_status"] in ps.ZERO_STATUSES:
        return 0.0, ps.OURS_ZERO
    return None


def _projection(p: dict, *, horizon: str, weeks: list[int], basis: str, espn: dict[int, float], state: dict | None,
                games: dict, covered: set[int]) -> dict:
    """One player's projection block (docs/player_rankings.md): value, both sources, status and week bookkeeping."""
    team = p["pro_team"]
    byes = [w for w in weeks if team and w in covered and (team, w) not in games]
    pid = p["espn_player_id"]
    e_weeks = {w: espn[w] for w in weeks if w in espn}
    ours_on = state is not None and state.get("available")
    ours_value = ours_origin = None
    o_missing: list[int] = []
    o_expected = o_available = 0
    if ours_on:
        if horizon == "week":
            got = _ours_week(state, pid, weeks[0], production_only=False)
            ours_value, ours_origin = got if got else (None, None)
            o_expected, o_available, o_missing = 1, int(bool(got)), [] if got else list(weeks)
        else:
            got_by = {w: _ours_week(state, pid, w, production_only=True) for w in weeks}
            required = [w for w in weeks if got_by[w] or w not in byes]
            o_missing = [w for w in required if not got_by[w]]
            o_expected, o_available = len(required), len(required) - len(o_missing)
            if not o_missing and required:
                counted = [got_by[w] for w in required]
                ours_value = round(sum(v for v, _ in counted), 2)
                ours_origin = ps.OURS if any(o == ps.OURS for _, o in counted) else ps.OURS_ZERO
    if horizon == "week":
        espn_value = e_weeks.get(weeks[0])
        missing = [] if espn_value is not None else list(weeks)
        e_expected, e_available = 1, len(e_weeks)
        if byes:        # on a bye ESPN's row (often 0) is not a projection of a game: never a ranked 0
            espn_value = ours_value = ours_origin = None
            e_expected = e_available = 0
            o_expected = o_available = 0
            missing = o_missing = []
    else:
        # a schedule bye is not required (ESPN's row, usually 0, counts when present), as for Ours
        required = [w for w in weeks if w in e_weeks or w not in byes]
        missing = [w for w in required if w not in e_weeks]
        espn_value = round(sum(e_weeks.values()), 2) if not missing else None
        e_expected, e_available = len(required), len(required) - len(missing)
    ranks_on_ours = basis == "ours"
    value = ours_value if ranks_on_ours else espn_value
    if horizon == "week" and byes:
        status = "bye"
    elif value is not None:
        status = "ok"
    elif ranks_on_ours:
        status = "no_ours"
    else:
        status = "missing" if horizon == "week" else "incomplete"
    return {"value": value, "espn": espn_value, "ours": ours_value if ours_on else None,
            "ours_origin": ours_origin if ours_on else None, "status": status,
            "weeks_expected": o_expected if ranks_on_ours else e_expected,
            "weeks_available": o_available if ranks_on_ours else e_available,
            "missing_weeks": o_missing if ranks_on_ours else missing, "bye_weeks": byes}


def _avail_why(av: dict) -> str | None:
    label = _AVAIL_LABEL.get(av["status"])
    if label is None:
        return None
    src = "NFL report" if any(s.startswith("NFL report") for s in av["sources"]) else "ESPN"
    return f"{label} ({src})"


def _why(p: dict, proj: dict, av: dict, horizon: str, basis: str) -> list[str]:
    """1-3 short data-derived reasons: warnings first (availability, bye, missing projection), then the role."""
    out: list[str] = []
    if (w := _avail_why(av)):
        out.append(w)
    if proj["bye_weeks"]:
        b = proj["bye_weeks"]
        out.append(f"{'On bye' if horizon == 'week' else 'Bye'} in week {b[0]}" if len(b) == 1 else
                   f"{len(b)} bye weeks in the window")
    if proj["status"] == "missing":
        out.append("No ESPN projection this week")
    elif proj["status"] == "incomplete":
        out.append("No ESPN projection for week " + ", ".join(map(str, proj["missing_weeks"])) if proj["missing_weeks"]
                   else "No ESPN projection")
    elif proj["status"] == "no_ours":
        out.append("No independent (Ours) estimate")
    tier = _TIER_WHY.get(p["opportunity_tier"] or "")
    if tier:
        shown = p["recent_opp"] if p["recent_opp"] is not None else p["xfp_per_game"]
        unit = "xFP/g" if p["opportunity_basis"] == "xfp" or p["recent_opp"] is None else "touches/g"
        out.append(f"{tier} ({shown:.1f} {unit})" if p["opportunity_tier"] == "HIGH" and shown is not None else tier)
    if p["role_trend"] == "UP":
        out.append("Role trending up")
    elif p["role_trend"] == "DOWN":
        out.append("Role shrinking")
    if (s := _SUSTAIN_WHY.get(p["sustainability"] or "")):
        out.append(s)
    if not out:
        out.append("Ranked on projection only: no usage profile yet")
    return out[:3]


def _row_key(p: dict) -> tuple:
    """Display order under sort=rank asc: rank, then higher overall rating, name, id; unranked after, by rating."""
    rank, rating = p["rank"]["overall"], p["rating"]["overall"]
    return (rank is None, rank or 0, rating is None, -(rating or 0.0), _fold(p["full_name"]), p["espn_player_id"])


def _sort_value(p: dict, key: str):
    return {"rank": lambda: p["rank"]["overall"], "name": lambda: _fold(p["full_name"]),
            "espn": lambda: p["projection"]["espn"], "ours": lambda: p["projection"]["ours"],
            "rating": lambda: p["rating"]["overall"], "opportunity": lambda: p["rating"]["opportunity"],
            "production": lambda: p["rating"]["production"], "xfp": lambda: p["opportunity"]["xfp_per_game"],
            "ppr": lambda: p["production"]["ppr_per_game"], "consistency": lambda: p["consistency"]["ppr_sd"],
            "trend": lambda: _TREND_ORDER.get(p["role_trend"])}[key]()


def _ordered(players: list[dict], key: str, direction: str) -> list[dict]:
    """Nulls last in both directions; equal values keep the rank order (sorted() is stable, also with reverse)."""
    base = sorted(players, key=_row_key)
    present = [p for p in base if _sort_value(p, key) is not None]
    absent = [p for p in base if _sort_value(p, key) is None]
    present.sort(key=lambda p: _sort_value(p, key), reverse=direction == "desc")
    return present + absent


def build_rankings(con, *, position: str = "ALL", horizon: str = "week", source: str = "espn", sort: str = "rank",
                   dir: str | None = None, q: str = "", status: str = "all", limit: int = 100,
                   offset: int = 0) -> dict:
    """The Player Rankings payload. ValueError (naming the parameter) for a bad value; RankingsUnavailable when
    the league is not synced or its current week is unknown."""
    position, horizon, source, sort_key, direction, needle, status, limit, offset = _validate(
        position, horizon, source, sort, dir, q, status, limit, offset)
    lg = fetch_dicts(con, """SELECT league_id, season, name FROM leagues ORDER BY synced_at DESC LIMIT 1""")
    if not lg:
        raise RankingsUnavailable("ESPN league not synced yet. Run scripts/sync_espn.py.")
    league_id, season = lg[0]["league_id"], lg[0]["season"]
    lw = league_window(con, season)
    if lw is None:
        raise RankingsUnavailable("The league's current scoring period is unknown. Run scripts/sync_espn.py.")
    win = _window(lw, horizon)
    weeks, first = win["weeks"], win["first"]
    tables = _tables(con)
    fp, why_scoring = ps._scoring(con, season)
    notes: list[str] = []

    pool = _pool(con, season, first, win["last"], "player_scores" in tables)
    pids = {p["espn_player_id"] for p in pool}
    espn: dict[int, dict[int, float]] = {}
    espn_synced = []
    for r in fetch_dicts(con, """SELECT espn_player_id, scoring_period, projected_points, synced_at
                                 FROM espn_player_weekly_points
                                 WHERE season = ? AND scoring_period BETWEEN ? AND ? AND projected_points IS NOT NULL""",
                         [season, first, win["last"]]):
        if r["espn_player_id"] in pids:
            espn.setdefault(r["espn_player_id"], {})[r["scoring_period"]] = float(r["projected_points"])
            espn_synced.append(r["synced_at"])
    games, covered = load_schedule(con, season, weeks)
    injuries = latest_injuries(con, season, per_week=True)
    teams = {t["team_id"]: t for t in fetch_dicts(
        con, "SELECT team_id, name, is_mine FROM fantasy_teams WHERE league_id = ? AND season = ?",
        [league_id, season])}
    usage: dict[str, dict] = {}
    if "player_week_usage" in tables:
        usage = {r["gsis_id"]: r for r in fetch_dicts(con, """
            SELECT gsis_id, count(*) AS games, round(stddev_samp(fantasy_points_ppr), 2) AS ppr_sd
            FROM player_week_usage WHERE season = ? AND gsis_id IS NOT NULL AND (offense_snaps > 0 OR opportunities > 0)
            GROUP BY gsis_id""", [season])}

    ours = _ours(con, season, win, source, fp is not None, why_scoring)
    ours_on = bool(ours and ours.get("available"))
    basis = "ours" if ours_on else "espn"
    if ours is not None and not ours_on:
        notes.append(f"Ours unavailable: {ours['reason']}; ranked on ESPN.")
    if win["note"]:
        notes.append(win["note"])
    if not covered:
        notes.append("The NFL schedule is not synced for this window, so byes and opponents are unknown.")
    if "player_scores" not in tables:
        notes.append("Player ratings are not computed yet (run the analytics pipeline).")

    players = []
    for p in pool:
        pid = p["espn_player_id"]
        proj = _projection(p, horizon=horizon, weeks=weeks, basis=basis, espn=espn.get(pid, {}), state=ours,
                           games=games, covered=covered)
        report = injuries.get((p["gsis_id"], first)) if p["gsis_id"] else None
        nfl_status = report["report_status"] if report else None
        av = availability(p["injury_status"], None, nfl_status)
        game = games.get((p["pro_team"], first)) if p["pro_team"] else None
        u = usage.get(p["gsis_id"]) if p["gsis_id"] else None
        team = teams.get(p["on_team_id"]) if p["availability_status"] == "ONTEAM" else None
        rstatus = _ROSTER_STATUS.get(p["availability_status"] or "", "UNKNOWN")
        players.append({
            "espn_player_id": pid, "gsis_id": p["gsis_id"], "full_name": p["full_name"], "position": p["position"],
            "pro_team": p["pro_team"],
            "rank": {"overall": None, "position": None, "position_label": None, "tied": False},
            "projection": proj,
            "rating": public_rating(p),
            "opportunity": {"basis": p["opportunity_basis"], "xfp_per_game": _num(p["xfp_per_game"]),
                            "recent_per_game": _num(p["recent_opp"]), "tier": p["opportunity_tier"]},
            "production": {"ppr_per_game": _num(p["fp_per_game"]), "recent_ppr_per_game": _num(p["recent_ppr"]),
                           "fp_minus_xfp": _num(p["fp_minus_xfp"]), "sustainability": p["sustainability"]},
            "consistency": {"ppr_sd": _num(u["ppr_sd"]) if u and u["games"] >= 2 else None,
                            "games": u["games"] if u else None},
            "games": p["games"], "role_trend": p["role_trend"], "usage_trend_score": _num(p["usage_trend_score"]),
            "market_signal": p["market_signal"],
            "availability": {**av, "espn_injury_status": p["injury_status"], "nfl_report_status": nfl_status},
            "game": {"week": first, "opponent": game[0] if game else None, "is_home": game[1] if game else None,
                     "on_bye": None if not p["pro_team"] or first not in covered else game is None},
            "roster": {"status": rstatus, "fantasy_team_id": p["on_team_id"] if rstatus == "ROSTERED" else None,
                       "fantasy_team": team["name"] if team else None, "is_mine": bool(team and team["is_mine"])},
            "why": _why(p, proj, av, horizon, basis),
        })
    overall = competition_ranks({x["espn_player_id"]: x["projection"]["value"] if x["projection"]["status"] == "ok"
                                 else None for x in players})
    by_pos = {pos: competition_ranks({x["espn_player_id"]: x["projection"]["value"]
                                      if x["projection"]["status"] == "ok" else None
                                      for x in players if x["position"] == pos}) for pos in POSITIONS}
    counts = {s: 0 for s in _UNRANKED}
    for x in players:
        rank, tied = overall[x["espn_player_id"]]
        prank, ptied = by_pos[x["position"]][x["espn_player_id"]]
        x["rank"] = {"overall": rank, "position": prank, "tied": tied or ptied,
                     "position_label": f"{x['position']}{prank}" if prank else None}
        if x["projection"]["status"] in counts:
            counts[x["projection"]["status"]] += 1

    matched = [x for x in players
               if (position == "ALL" or x["position"] == position)
               and (not needle or needle in _fold(x["full_name"]))
               and (status == "all"
                    or (status == "rostered" and x["roster"]["status"] == "ROSTERED")
                    or (status == "available" and x["roster"]["status"] in ("FREE_AGENT", "WAIVERS")))]
    page = _ordered(matched, sort_key, direction)[offset:offset + limit]
    if sort_key == "ours" and not ours_on:
        notes.append("Sorting by Ours has no effect: Ours is not loaded, so rank order is kept.")

    ranked = sum(1 for x in players if x["rank"]["overall"] is not None)
    scored = [p for p in pool if p["scores_computed_at"] is not None]
    espn_label = (f"ESPN weekly projection, week {first} (league scoring)" if horizon == "week" else
                  f"ESPN rest-of-season projection, {win['label'].lower()} (league scoring)")
    ours_block = None
    if ours is not None:
        if ours_on:
            served = ours["served"]
            exp = ours["experimental"]
            label = (forward_ours.label_for(ours["versions"]) if horizon == "ros" else
                     f"Fantasy Intel projection ({', '.join(ours['versions'])}), week {first} (league scoring)")
            gov = ps.governance(served)["status_note"]
            ours_block = {"available": True, "reason": ours["reason"], "model_version": served,
                          "model_versions": ours["versions"],
                          "run_ids": [r["run_id"] for r in ours["runs"]],
                          "run_created_at": _newest(r["created_at"] for r in ours["runs"]),
                          "experimental": exp, "label": label, "governance_note": gov}
            if horizon == "ros":
                notes.append("Ours rest of season is experimental: ifp-2 horizons beyond the next game remain "
                             "experimental.")
            if gov:
                notes.append(gov)
        else:
            ours_block = {"available": False, "reason": ours["reason"], "model_version": ps.MODEL_VERSION,
                          "model_versions": [], "run_ids": [], "run_created_at": None, "experimental": False,
                          "label": forward_ours.LABEL, "governance_note": None}
    basis_label = ours_block["label"] if ours_on else espn_label
    notes.append(_SCORING_NOTE)
    if not teams:
        notes.append("League rosters are not synced, so roster status is unknown.")

    my_team = next((t["team_id"] for t in teams.values() if t["is_mine"]), None)
    return {
        "season": season, "week": lw.current, "horizon": horizon, "source": source, "position": position,
        "sort": {"key": sort_key, "dir": direction},
        "window": win,
        "projection": {"basis": basis, "label": basis_label,
                       "espn": {"synced_at": _newest(espn_synced), "label": espn_label}, "ours": ours_block},
        "rating": {**CONTRACT, "as_of_week": max((p["scores_as_of_week"] for p in scored), default=None),
                   "computed_at": _newest(p["scores_computed_at"] for p in scored)},
        "scoring": {"projection_units": "league scoring", "rating_units": "full PPR (nflverse)",
                    "ours_supported": fp is not None,
                    "ours_reason": None if fp is not None else f"No independent projections: {why_scoring}."},
        "league_context": {"available": bool(teams), "league_name": lg[0]["name"], "teams": len(teams) or None,
                           "my_team_id": my_team},
        "freshness": {"espn_projections_synced_at": _newest(espn_synced),
                      "player_scores_computed_at": _newest(p["scores_computed_at"] for p in scored),
                      "player_scores_as_of_week": max((p["scores_as_of_week"] for p in scored), default=None),
                      "players_synced_at": _newest(p["detail_synced_at"] or p["fp_synced_at"] for p in pool),
                      "ours_run_created_at": ours_block["run_created_at"] if ours_block else None},
        "counts": {"pool": len(players), "ranked": ranked, "unranked": counts, "matched": len(matched),
                   "returned": len(page)},
        "notes": notes,
        "players": page,
    }
