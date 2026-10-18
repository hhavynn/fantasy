"""Injury Impact Center (docs/injury_impact.md): every calculation behind GET /api/injury-impact.

Read-only. For the league's current scoring period it lists every QB / RB / WR / TE with a QUESTIONABLE, DOUBTFUL,
OUT, IR or SUSPENDED status (ESPN status or the week's NFL report, the more severe wins: analytics/availability.py)
who has a real role this season, and for each one the teammates whose opportunity depends on it.

Two kinds of numbers, never mixed and always labelled:

  observed   counted from this season's player_week_usage: the player's own workload, and every teammate's usage
             in the team games he played versus the team games he missed (with the number of games behind each).
  estimate   a pro-rata split of the player's recent volume over the healthy teammates in the receiving pool, by
             their recent share (the simplest redistribution rule), and ESPN's own weekly projection. Neither is
             validated for *who* absorbs vacated volume; the payload says so and never names a single winner.

The ifp-ctx teammate-context research challenger is not used here (it is research only and not imported by the app).

Injury freshness is the per-team readiness check in analytics/injury_freshness.py; a missing or stale report is a
visible notice, never read as healthy. The number of queries is constant in the number of players.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

from ..db.connection import fetch_dicts, table_columns, table_exists, utcnow
from . import injury_freshness as fresh
from .availability import AVAILABLE, DOUBTFUL, IR, OUT, QUESTIONABLE, SEVERITY, SUSPENDED, availability, latest_injuries
from .legacy_league import latest_league
from .projections import league_window

POSITIONS = ("QB", "RB", "WR", "TE")
STATUS_FILTERS = ("all", "out", "questionable")
OUT_LEVELS = (OUT, DOUBTFUL, IR, SUSPENDED)
INJURED_LEVELS = (QUESTIONABLE, *OUT_LEVELS)

# A "meaningful" role: in the games he played for his current team, at least this snap share or this much volume
# per game (QB: snap share only). A player on a fantasy roster in the league counts whenever he has played.
MIN_SNAP_SHARE = 0.25
MIN_OPPS_PER_GAME = 3.0
MIN_QB_SNAP_SHARE = 0.5
RECENT_GAMES = 4          # the injured player's recent role: his last 4 games played for the team
POOL_WEEKS = 3            # a teammate's recent share: the team's last 3 games
LONG_TERM_MISSED = 3      # 3+ straight team games missed: already in teammates' recent usage
MAX_TEAMMATES = 5
TOP_SHARE_CONCENTRATED = 0.5

# Receiving pools: who could absorb the volume, and the volume unit.
_POOL = {"RB": (("RB",), "touches"), "WR": (("WR", "TE", "RB"), "targets"), "TE": (("TE", "WR", "RB"), "targets")}
_UNIT_LABEL = {"touches": "carries + targets", "targets": "targets", "pass_attempts": "pass attempts"}
_ROSTER = {"ONTEAM": "ROSTERED", "FREEAGENT": "FREE_AGENT", "WAIVERS": "WAIVERS"}

ESTIMATE_NOTE = ("Pro-rata estimate: the player's recent volume split over healthy teammates by their recent share. "
                 "It is not a validated projection. In this project's research a proportional split gets the "
                 "average total roughly right, but which teammate realizes the extra volume is skewed and hard to "
                 "predict, so treat every split as a range, not a call.")
OBSERVED_NOTE = ("Observed: this season's games for the same team, split by whether the injured player played. "
                 "Samples are small; a different opponent or game script also changes usage.")


class InjuryImpactUnavailable(Exception):
    """No synced league (or its current week is unknown): the API answers 503."""


def _num(v) -> float | None:
    if v is None:
        return None
    x = float(v)
    return None if math.isnan(x) else x


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(v, nd)


def _mean(vals) -> float | None:
    xs = [v for v in (_num(x) for x in vals) if v is not None]
    return sum(xs) / len(xs) if xs else None


def _ts(v) -> str | None:
    return None if v is None else str(v)


def _validate(position, status) -> tuple[str, str]:
    pos = str(position or "ALL").upper()
    if pos != "ALL" and pos not in POSITIONS:
        raise ValueError(f"position must be one of ALL, {', '.join(POSITIONS)}")
    st = str(status or "all").lower()
    if st not in STATUS_FILTERS:
        raise ValueError(f"status must be one of {', '.join(STATUS_FILTERS)}")
    return pos, st


# ----------------------------------------------------------------------------- reads (one query each)


def _usage(con, season: int, week: int) -> list[dict]:
    if not table_exists(con, "player_week_usage"):
        return []
    return fetch_dicts(con, """
        SELECT week, gsis_id, team, position, offense_snaps, offense_pct, targets, carries, target_share,
               backfield_rush_share, opportunities, xfp, fantasy_points_ppr
        FROM player_week_usage WHERE season = ? AND week <= ? AND gsis_id IS NOT NULL AND team IS NOT NULL
        ORDER BY week""", [season, week])


def _pass_attempts(con, season: int, week: int) -> dict[tuple[str, int], int]:
    if not table_exists(con, "nfl_weekly_stats") or "attempts" not in set(table_columns(con, "nfl_weekly_stats")):
        return {}
    gt = "AND season_type = 'REG'" if "season_type" in set(table_columns(con, "nfl_weekly_stats")) else ""
    return {(r["player_id"], r["week"]): int(r["attempts"] or 0) for r in fetch_dicts(con, f"""
        SELECT player_id, week, attempts FROM nfl_weekly_stats
        WHERE season = ? AND week <= ? AND position = 'QB' {gt}""", [season, week])}


_OFF_TEAM = ("CUT", "RET", "TRD", "TRT")        # weekly-roster statuses that mean he no longer belongs to the team
_RESERVE = ("RES", "SUS", "PUP", "NFI", "EXE")   # on the team but unable to play (IR, suspended, ...)


class _Rosters:
    """Weekly NFL rosters: which team games a player belonged to the team, and which he could have played."""

    def __init__(self, rows: list[dict]):
        self.status: dict[tuple[str, str], dict[int, str]] = defaultdict(dict)
        self.covered: set[int] = set()
        for r in rows:
            self.status[(r["gsis_id"], r["team"])][r["week"]] = r["status"]
            self.covered.add(r["week"])

    def tenure(self, t: "_Team", gsis: str) -> list[int]:
        """Team games he belonged to the team: a weekly-roster row that is not released / retired / traded. A week
        with no roster data at all falls back to "from his first usage row for the team on"; a game he played
        always counts."""
        first = min(t.rows[gsis], default=None)
        weeks = self.status.get((gsis, t.team), {})
        out = []
        for w in t.weeks:
            if _played(t.rows[gsis].get(w)):
                out.append(w)
            elif w in self.covered:
                if w in weeks and weeks[w] not in _OFF_TEAM:
                    out.append(w)
            elif first is not None and w >= first:
                out.append(w)
        return out

    def available(self, t: "_Team", gsis: str) -> set[int]:
        """Team games he could have played: in his tenure and not on a reserve list, or he played."""
        weeks = self.status.get((gsis, t.team), {})
        return {w for w in self.tenure(t, gsis) if _played(t.rows[gsis].get(w)) or weeks.get(w) not in _RESERVE}


def _roster_rows(con, season: int, week: int) -> list[dict]:
    if not table_exists(con, "nfl_rosters_weekly"):
        return []
    cols = set(table_columns(con, "nfl_rosters_weekly"))
    if not {"season", "week", "team", "gsis_id", "status"} <= cols:
        return []
    gt = "AND game_type = 'REG'" if "game_type" in cols else ""
    return fetch_dicts(con, f"""SELECT DISTINCT week, team, gsis_id, status FROM nfl_rosters_weekly
                                WHERE season = ? AND week <= ? AND gsis_id IS NOT NULL {gt}""", [season, week])


def _players(con) -> list[dict]:
    """ESPN universe (skill positions) with the best gsis mapping."""
    if not table_exists(con, "fantasy_players"):
        return []
    has_map = table_exists(con, "player_id_map")
    join = ("LEFT JOIN player_id_map m ON m.espn_player_id = fp.espn_player_id AND m.gsis_id IS NOT NULL "
            "AND lower(coalesce(m.notes, '')) NOT LIKE '%conflict%'") if has_map else ""
    gsis = "m.gsis_id" if has_map else "NULL"
    return fetch_dicts(con, f"""
        SELECT fp.espn_player_id, fp.full_name, fp.position, fp.pro_team, fp.injury_status, fp.availability_status,
               fp.on_team_id, fp.percent_owned, coalesce(fp.detail_synced_at, fp.synced_at) AS synced_at,
               {gsis} AS gsis_id
        FROM fantasy_players fp {join} WHERE fp.position IN ('QB', 'RB', 'WR', 'TE')""")


def _report_rows(con, season: int, week: int) -> dict[str, dict]:
    """Week-W NFL report rows with a designation: name, team and position for players ESPN does not carry."""
    if not table_exists(con, "nfl_injuries"):
        return {}
    cols = set(table_columns(con, "nfl_injuries"))
    name = "full_name" if "full_name" in cols else "NULL"
    pos = "position" if "position" in cols else "NULL"
    gt = "AND game_type = 'REG'" if "game_type" in cols else ""
    return {r["gsis_id"]: r for r in fetch_dicts(con, f"""
        SELECT DISTINCT gsis_id, team, {name} AS full_name, {pos} AS position FROM nfl_injuries
        WHERE season = ? AND week = ? AND gsis_id IS NOT NULL AND report_status IS NOT NULL {gt}""", [season, week])}


def _depth(con, season: int) -> dict[str, dict]:
    """gsis -> {team, position, rank} from each team's newest depth-chart snapshot (offense skill slots)."""
    if not table_exists(con, "nfl_depth_charts"):
        return {}
    cols = set(table_columns(con, "nfl_depth_charts"))
    if not {"dt", "team", "gsis_id", "pos_abb", "pos_rank"} <= cols:
        return {}
    season_where = "AND season = ?" if "season" in cols else ""
    params = [season, season] if season_where else []
    out: dict[str, dict] = {}
    for r in fetch_dicts(con, f"""
        SELECT d.team, d.gsis_id, d.player_name, d.pos_abb, d.pos_rank, d.dt FROM nfl_depth_charts d
        JOIN (SELECT team, max(dt) AS dt FROM nfl_depth_charts WHERE gsis_id IS NOT NULL {season_where} GROUP BY team) x
          ON x.team = d.team AND x.dt = d.dt
        WHERE d.gsis_id IS NOT NULL AND d.pos_abb IN ('QB', 'RB', 'WR', 'TE') {season_where.replace('season', 'd.season')}
        ORDER BY d.pos_rank""", params):
        prev = out.get(r["gsis_id"])
        if prev is None or (r["pos_rank"] or 99) < (prev["rank"] or 99):
            out[r["gsis_id"]] = {"team": r["team"], "position": r["pos_abb"], "rank": r["pos_rank"],
                                 "name": r["player_name"], "as_of": r["dt"]}
    return out


def _espn_projections(con, season: int, week: int) -> tuple[dict[int, float], str | None]:
    if not table_exists(con, "espn_player_weekly_points"):
        return {}, None
    rows = fetch_dicts(con, """SELECT espn_player_id, projected_points, synced_at FROM espn_player_weekly_points
                               WHERE season = ? AND scoring_period = ? AND projected_points IS NOT NULL""",
                       [season, week])
    newest = max((r["synced_at"] for r in rows if r["synced_at"] is not None), default=None)
    return {r["espn_player_id"]: float(r["projected_points"]) for r in rows}, _ts(newest)


def _role_trends(con, season: int) -> dict[str, str]:
    if not table_exists(con, "player_scores"):
        return {}
    return {r["gsis_id"]: r["role_trend"] for r in fetch_dicts(
        con, "SELECT gsis_id, role_trend FROM player_scores WHERE season = ? AND gsis_id IS NOT NULL", [season])}


def _report_freshness(con, season: int, week: int) -> dict:
    """What the NFL injury table holds for the season: newest report week and rows for the current week."""
    if not table_exists(con, "nfl_injuries"):
        return {"available": False, "latest_week": None, "current_week_rows": 0}
    latest, rows = con.execute("""SELECT max(week), count(*) FILTER (WHERE week = ?) FROM nfl_injuries
                                  WHERE season = ?""", [week, season]).fetchone()
    return {"available": True, "latest_week": latest, "current_week_rows": int(rows or 0)}


# ----------------------------------------------------------------------------- per-team usage


class _Team:
    """One NFL team's season: the weeks it played and each player's row per week (current-team rows only)."""

    def __init__(self, team: str):
        self.team = team
        self.weeks: list[int] = []
        self.rows: dict[str, dict[int, dict]] = defaultdict(dict)


def _played(row: dict | None) -> bool:
    return bool(row) and ((row.get("offense_snaps") or 0) > 0 or (row.get("opportunities") or 0) > 0)


def _volume(row: dict | None, unit: str, attempts: dict, gsis: str) -> float:
    if not _played(row):
        return 0.0
    if unit == "targets":
        return float(row.get("targets") or 0)
    if unit == "pass_attempts":
        return float(attempts.get((gsis, row["week"]), 0))
    return float((row.get("targets") or 0) + (row.get("carries") or 0))


def _teams(usage: list[dict]) -> tuple[dict[str, _Team], dict[str, str], dict[str, str]]:
    """Teams, each player's latest team and latest usage position."""
    teams: dict[str, _Team] = {}
    weeks: dict[str, set[int]] = defaultdict(set)
    latest_team: dict[str, tuple[int, str]] = {}
    pos: dict[str, str] = {}
    for r in usage:
        t = teams.setdefault(r["team"], _Team(r["team"]))
        t.rows[r["gsis_id"]][r["week"]] = r
        if _played(r):
            weeks[r["team"]].add(r["week"])
        prev = latest_team.get(r["gsis_id"])
        if prev is None or r["week"] >= prev[0]:
            latest_team[r["gsis_id"]] = (r["week"], r["team"])
            if r["position"]:
                pos[r["gsis_id"]] = r["position"]
    for team, t in teams.items():
        t.weeks = sorted(weeks[team])
    return teams, {g: t for g, (_, t) in latest_team.items()}, pos


def _side(t: _Team, gsis: str, weeks: list[int], unit: str, attempts: dict, available: set[int]) -> dict | None:
    """A player's usage over the team games in `weeks` he was available for (on the roster, not on a reserve
    list): per-game volume, snap / target share and PPR, where a game he did not play counts as zero. Conditioning
    on games he played would hide a backup who only got snaps once the starter was out."""
    sample = [w for w in weeks if w in available]
    if not sample:
        return None
    rows = [t.rows[gsis].get(w) for w in sample]

    def per_game(key):
        return _r(sum(float(r.get(key) or 0) if _played(r) else 0.0 for r in rows) / len(rows))

    return {"games": len(sample), "played": sum(1 for r in rows if _played(r)),
            "volume_per_game": _r(sum(_volume(r, unit, attempts, gsis) for r in rows) / len(rows), 2),
            "snap_share": per_game("offense_pct"), "target_share": per_game("target_share"),
            "ppr_per_game": _r(sum(float(r.get("fantasy_points_ppr") or 0) if _played(r) else 0.0
                                   for r in rows) / len(rows), 2)}


def _workload(t: _Team, gsis: str, position: str, attempts: dict, trend: str | None, rosters: _Rosters) -> dict | None:
    """The injured player's observed role in the games he played for this team."""
    tenure = rosters.tenure(t, gsis)
    games = [w for w in tenure if _played(t.rows[gsis].get(w))]
    if not games:
        return None
    rows = [t.rows[gsis][w] for w in games]
    recent = games[-RECENT_GAMES:]
    rrows = [t.rows[gsis][w] for w in recent]
    att = [attempts.get((gsis, w)) for w in games]
    weekly = []
    for w in tenure:
        r = t.rows[gsis].get(w)
        played = _played(r)
        weekly.append({"week": w, "played": played,
                       "snap_share": _r(_num(r.get("offense_pct"))) if played else None,
                       "opportunities": (int((r.get("targets") or 0) + (r.get("carries") or 0)) if played else None)})
    return {
        "games": len(games), "team_games": len(tenure), "last_played_week": games[-1],
        "snap_share": _r(_mean(r.get("offense_pct") for r in rows)),
        "recent_snap_share": _r(_mean(r.get("offense_pct") for r in rrows)),
        "recent_weeks": recent,
        "target_share": _r(_mean(r.get("target_share") for r in rows)),
        "backfield_share": _r(_mean(r.get("backfield_rush_share") for r in rows)) if position == "RB" else None,
        "targets_per_game": _r(_mean(r.get("targets") or 0 for r in rows), 2),
        "carries_per_game": _r(_mean(r.get("carries") or 0 for r in rows), 2),
        "pass_attempts_per_game": _r(_mean(a for a in att if a is not None), 2) if position == "QB" else None,
        "xfp_per_game": _r(_mean(r.get("xfp") for r in rows), 2),
        "ppr_per_game": _r(_mean(r.get("fantasy_points_ppr") for r in rows), 2),
        "role_trend": trend,
        "weekly": weekly,
    }


def _meaningful(w: dict, position: str) -> bool:
    snap = w["snap_share"] or 0
    if position == "QB":
        return snap >= MIN_QB_SNAP_SHARE
    opps = (w["targets_per_game"] or 0) + (w["carries_per_game"] or 0)
    return snap >= MIN_SNAP_SHARE or opps >= MIN_OPPS_PER_GAME


def _absence(w: dict) -> dict:
    missed = sum(1 for p in w["weekly"] if p["week"] > w["last_played_week"])
    kind = "new" if missed == 0 else "long_term" if missed >= LONG_TERM_MISSED else "ongoing"
    return {"kind": kind, "last_played_week": w["last_played_week"], "consecutive_team_games_missed": missed}


# ----------------------------------------------------------------------------- teammates


def _clarity(n_without: int, mates: list[dict]) -> dict:
    """How clear the observed and estimated splits are. Never a single winner: a label plus the reason."""
    est = [m for m in mates if m["estimate"] and m["estimate"]["share"] is not None]
    top = max(est, key=lambda m: m["estimate"]["share"], default=None)
    observed = [m for m in mates if m["observed"]["delta_per_game"] is not None]
    top_obs = max(observed, key=lambda m: m["observed"]["delta_per_game"], default=None)
    if n_without == 0:
        return {"label": "no_history", "text": "He has not missed a game for this team this season, so there is no "
                "observed split. The split shown is a pro-rata estimate only."}
    if n_without == 1:
        return {"label": "thin_history", "text": "One team game without him: the observed change is a single game "
                "and can easily be noise."}
    agree = top is not None and top_obs is not None and top["gsis_id"] == top_obs["gsis_id"] \
        and (top_obs["observed"]["delta_per_game"] or 0) > 0
    if agree and top["estimate"]["share"] >= TOP_SHARE_CONCENTRATED:
        return {"label": "leaning", "text": f"Observed games and the estimate lean the same way, over "
                f"{n_without} games without him. Still a small sample, not a guarantee."}
    return {"label": "split", "text": f"Over {n_without} games without him the volume spread across several "
            "players, or the observed games and the estimate disagree. No clear single beneficiary."}


def _split_weeks(t: _Team, injured: str, rosters: _Rosters) -> tuple[list[int], list[int]]:
    """(team games he played, team games he missed while on the team). Games before he joined the team are
    neither: they are not absences."""
    tenure = rosters.tenure(t, injured)
    played = [w for w in tenure if _played(t.rows[injured].get(w))]
    return played, [w for w in tenure if w not in played]


def _teammates(t: _Team, injured: str, position: str, *, team_of: dict, pos_of: dict, depth: dict,
               status_of: dict, people: dict, attempts: dict, espn: dict, teams_by_id: dict, vacated: float | None,
               unit: str, pool_positions: tuple[str, ...], rosters: _Rosters) -> tuple[list[dict], int]:
    with_weeks, without_weeks = _split_weeks(t, injured, rosters)
    recent_weeks = t.weeks[-POOL_WEEKS:]
    # The split uses each teammate's role in the last games the injured player DID play: after he has already
    # missed time, teammates' recent usage includes the volume they absorbed, and splitting it again would count
    # it twice. For a new absence these are the team's latest games anyway.
    basis_weeks = with_weeks[-POOL_WEEKS:] or recent_weeks
    cands: set[str] = set()
    for g in t.rows:
        if g != injured and team_of.get(g) == t.team and pos_of.get(g) in pool_positions:
            cands.add(g)
    for g, d in depth.items():
        if g != injured and d["team"] == t.team and d["position"] in pool_positions and (d["rank"] or 99) <= 4:
            cands.add(g)

    out = []
    for g in cands:
        person = people.get(g, {})
        st = status_of.get(g, {"status": AVAILABLE})
        healthy = st["status"] not in OUT_LEVELS
        recent = sum(_volume(t.rows[g].get(w), unit, attempts, g) for w in recent_weeks) / max(len(recent_weeks), 1)
        basis = sum(_volume(t.rows[g].get(w), unit, attempts, g) for w in basis_weeks) / max(len(basis_weeks), 1)
        avail = rosters.available(t, g)
        w_side = _side(t, g, with_weeks, unit, attempts, avail)
        wo_side = _side(t, g, without_weeks, unit, attempts, avail)
        delta = None
        if w_side and wo_side:
            delta = _r(wo_side["volume_per_game"] - w_side["volume_per_game"], 2)
        d = depth.get(g)
        out.append({
            "gsis_id": g, "espn_player_id": person.get("espn_player_id"),
            "full_name": person.get("full_name") or (d or {}).get("name") or g,
            "position": pos_of.get(g) or (d or {}).get("position"),
            "depth_rank": d["rank"] if d and d["team"] == t.team else None,
            "availability": st["status"], "healthy": healthy,
            "recent_volume_per_game": _r(recent, 2), "_basis": basis,
            "observed": {"with": w_side, "without": wo_side, "delta_per_game": delta},
            "estimate": None,
            "espn_projection": espn.get(person.get("espn_player_id")) if person.get("espn_player_id") else None,
            "roster": _roster(person, teams_by_id),
        })

    pool = [m for m in out if m["healthy"] and m["_basis"] > 0]
    total = sum(m["_basis"] for m in pool)
    for m in out:
        basis = m.pop("_basis")
        if position == "QB" or vacated is None:
            continue
        if m in pool and total > 0:
            share = basis / total
            m["estimate"] = {"share": _r(share), "extra_per_game": _r(share * vacated, 2),
                             "basis_volume_per_game": _r(basis, 2), "basis_weeks": basis_weeks}
        elif m["healthy"]:
            m["estimate"] = {"share": 0.0 if total > 0 else None, "extra_per_game": 0.0 if total > 0 else None,
                             "basis_volume_per_game": _r(basis, 2), "basis_weeks": basis_weeks}

    def key(m):
        share = (m["estimate"] or {}).get("share") or 0
        return (-share, -(m["recent_volume_per_game"] or 0), m["depth_rank"] or 99, m["full_name"])

    if position == "QB":
        out.sort(key=lambda m: (not m["healthy"], m["depth_rank"] or 99, -(m["recent_volume_per_game"] or 0)))
    else:
        out.sort(key=key)
    relevant = [m for m in out if (m["recent_volume_per_game"] or 0) > 0 or (m["depth_rank"] or 99) <= 3
                or (m["observed"]["without"] or {}).get("played")]
    return relevant[:MAX_TEAMMATES], len(without_weeks)


def _qb_receivers(t: _Team, injured: str, *, team_of: dict, pos_of: dict, people: dict, attempts: dict,
                  espn: dict, status_of: dict, teams_by_id: dict, rosters: _Rosters) -> list[dict]:
    """A QB's top three pass catchers by recent targets, with their observed PPR with / without him."""
    recent_weeks = t.weeks[-POOL_WEEKS:]
    with_weeks, without_weeks = _split_weeks(t, injured, rosters)
    out = []
    for g in t.rows:
        if team_of.get(g) != t.team or pos_of.get(g) not in ("WR", "TE", "RB"):
            continue
        tg = sum(_volume(t.rows[g].get(w), "targets", attempts, g) for w in recent_weeks) / max(len(recent_weeks), 1)
        if tg <= 0:
            continue
        person = people.get(g, {})
        avail = rosters.available(t, g)
        w_side = _side(t, g, with_weeks, "targets", attempts, avail)
        wo_side = _side(t, g, without_weeks, "targets", attempts, avail)
        out.append({
            "gsis_id": g, "espn_player_id": person.get("espn_player_id"), "full_name": person.get("full_name") or g,
            "position": pos_of.get(g), "availability": status_of.get(g, {"status": AVAILABLE})["status"],
            "recent_targets_per_game": _r(tg, 2),
            "observed": {"with": w_side, "without": wo_side,
                         "ppr_delta_per_game": _r(wo_side["ppr_per_game"] - w_side["ppr_per_game"], 2)
                         if w_side and wo_side and w_side["ppr_per_game"] is not None
                         and wo_side["ppr_per_game"] is not None else None},
            "espn_projection": espn.get(person.get("espn_player_id")) if person.get("espn_player_id") else None,
            "roster": _roster(person, teams_by_id),
        })
    out.sort(key=lambda m: -m["recent_targets_per_game"])
    return out[:3]


def _roster(person: dict, teams_by_id: dict[int, dict]) -> dict:
    status = _ROSTER.get(person.get("availability_status") or "", "UNKNOWN") if person else "UNKNOWN"
    team = teams_by_id.get(person.get("on_team_id")) if status == "ROSTERED" else None
    return {"status": status, "fantasy_team_id": person.get("on_team_id") if status == "ROSTERED" else None,
            "fantasy_team": team["name"] if team else None, "is_mine": bool(team and team["is_mine"]),
            "percent_owned": _r(_num(person.get("percent_owned")), 1) if person else None}


# ----------------------------------------------------------------------------- payload


def build_injury_impact(con, *, position: str = "ALL", status: str = "all", team_id: int | None = None,
                        now: datetime | None = None) -> dict:
    """The Injury Impact payload. ValueError (naming the parameter) for a bad value; InjuryImpactUnavailable when the
    league is not synced or its current week is unknown. `team_id` picks whose roster "yours" means (default: the
    owner's team); an unknown team_id raises LookupError."""
    position, status = _validate(position, status)
    lg = latest_league(con, ["league_id", "season", "name"])
    if lg is None:
        raise InjuryImpactUnavailable("ESPN league not synced yet. Run scripts/sync_espn.py.")
    season = lg["season"]
    lw = league_window(con, season)
    if lw is None:
        raise InjuryImpactUnavailable("The league's current scoring period is unknown. Run scripts/sync_espn.py.")
    week = lw.current
    now = now or utcnow()

    teams_rows = fetch_dicts(con, "SELECT team_id, name, is_mine FROM fantasy_teams WHERE league_id = ? AND season = ?",
                             [lg["league_id"], season]) if table_exists(con, "fantasy_teams") else []
    if team_id is not None and not any(t["team_id"] == team_id for t in teams_rows):
        raise LookupError(f"Team {team_id} is not in {lg['name'] or 'this league'} ({season}).")
    mine = team_id if team_id is not None else next((t["team_id"] for t in teams_rows if t["is_mine"]), None)
    teams_by_id = {t["team_id"]: {**t, "is_mine": t["team_id"] == mine} for t in teams_rows}

    usage = _usage(con, season, week)
    attempts = _pass_attempts(con, season, week)
    teams, team_of, pos_of = _teams(usage)
    rosters = _Rosters(_roster_rows(con, season, week))
    depth = _depth(con, season)
    espn, espn_synced = _espn_projections(con, season, week)
    trends = _role_trends(con, season)
    reports = latest_injuries(con, season, per_week=True)
    report_rows = _report_rows(con, season, week)
    players = _players(con)

    people: dict[str, dict] = {}
    for p in players:
        g = p["gsis_id"]
        if g and (g not in people or p["availability_status"] == "ONTEAM"):
            people[g] = p
    for g, d in depth.items():
        pos_of.setdefault(g, d["position"])

    # Every mapped player's combined status for the week (ESPN status and the week's NFL report, most severe wins).
    status_of: dict[str, dict] = {}
    for g in set(people) | set(report_rows):
        p = people.get(g, {})
        rep = reports.get((g, week))
        av = availability(p.get("injury_status"), None, rep["report_status"] if rep else None)
        status_of[g] = {**av, "espn_injury_status": p.get("injury_status"),
                        "nfl_report_status": rep["report_status"] if rep else None,
                        "injury": rep["report_primary_injury"] if rep else None,
                        "practice_status": rep["practice_status"] if rep else None}

    games = fresh.scheduled_games(con, season, week)
    game_of = {g["team"]: g for g in games}
    covered = bool(games)
    diag = fresh.diagnostics(con, season, week, now)
    fresh_of = {t["team"]: t for t in diag["teams"]}

    entries, skipped = [], {"no_games": 0, "small_role": 0, "no_team": 0}
    for g, st in status_of.items():
        if st["status"] not in INJURED_LEVELS:
            continue
        p = people.get(g, {})
        rr = report_rows.get(g, {})
        pos = p.get("position") or pos_of.get(g) or rr.get("position")
        if pos not in POSITIONS:
            continue
        team = rr.get("team") or p.get("pro_team") or team_of.get(g)
        t = teams.get(team) if team else None
        if t is None:
            skipped["no_team" if not team else "no_games"] += 1
            continue
        w = _workload(t, g, pos, attempts, trends.get(g), rosters)
        roster = _roster(p, teams_by_id)
        if w is None:
            skipped["no_games"] += 1
            continue
        if not _meaningful(w, pos) and roster["status"] != "ROSTERED":
            skipped["small_role"] += 1
            continue
        absence = _absence(w)
        if pos == "QB":
            unit, vacated = "pass_attempts", w["pass_attempts_per_game"]
            mates, n_without = _teammates(t, g, pos, team_of=team_of, pos_of=pos_of, depth=depth, status_of=status_of,
                                          people=people, attempts=attempts, espn=espn, teams_by_id=teams_by_id, vacated=None, unit=unit,
                                          pool_positions=("QB",), rosters=rosters)
            receivers = _qb_receivers(t, g, team_of=team_of, pos_of=pos_of, people=people, attempts=attempts,
                                      espn=espn, status_of=status_of, teams_by_id=teams_by_id, rosters=rosters)
        else:
            pool_positions, unit = _POOL[pos]
            recent = [t.rows[g][wk] for wk in w["recent_weeks"]]
            vacated = _r(sum(_volume(r, unit, attempts, g) for r in recent) / len(recent), 2)
            mates, n_without = _teammates(t, g, pos, team_of=team_of, pos_of=pos_of, depth=depth, status_of=status_of,
                                          people=people, attempts=attempts, espn=espn, teams_by_id=teams_by_id, vacated=vacated, unit=unit,
                                          pool_positions=pool_positions, rosters=rosters)
            receivers = []
        gm = game_of.get(team)
        kickoff = fresh.naive_utc(gm["kickoff"]) if gm else None
        tf = fresh_of.get(team)
        entries.append({
            "gsis_id": g, "espn_player_id": p.get("espn_player_id"),
            "full_name": p.get("full_name") or rr.get("full_name") or g, "position": pos, "team": team,
            "status": {"level": st["status"], "sources": st["sources"],
                       "espn_injury_status": st["espn_injury_status"], "nfl_report_status": st["nfl_report_status"],
                       "injury": st["injury"], "practice_status": st["practice_status"]},
            "absence": absence,
            "workload": w,
            "roster": roster,
            "espn_projection": espn.get(p.get("espn_player_id")) if p.get("espn_player_id") else None,
            "game": {"week": week, "opponent": gm["opponent"] if gm else None,
                     "is_home": None,  # scheduled_games is team/opponent only; home/away comes from the schedule
                     "kickoff": None if kickoff is None else kickoff.isoformat() + "Z",
                     "started": bool(kickoff and now >= kickoff),
                     "on_bye": None if not covered else gm is None},
            "freshness": None if tf is None else {
                "status": tf["status"], "reason": tf["reason"], "source_used": tf["source_used"],
                "evidence_retrieved_at": tf["evidence_retrieved_at"],
                "hours_since_retrieval": tf["hours_since_retrieval"]},
            "impact": {
                "unit": unit, "unit_label": _UNIT_LABEL[unit], "vacated_per_game": vacated,
                "team_games_without": n_without, "clarity": _clarity(n_without, mates) if pos != "QB" else
                _qb_clarity(n_without, mates),
                "teammates": mates, "receivers": receivers,
            },
        })

    home = _home_away(con, season, week)
    for e in entries:
        e["game"]["is_home"] = home.get(e["team"])

    for e in entries:
        e["affects_my_roster"] = e["roster"]["is_mine"] or any(
            m["roster"]["is_mine"] for m in e["impact"]["teammates"] + e["impact"]["receivers"])

    def order(e):
        return (e["game"]["started"] or bool(e["game"]["on_bye"]), e["absence"]["kind"] == "long_term",
                -SEVERITY[e["status"]["level"]] if e["status"]["level"] != IR else -SEVERITY[OUT],
                -(e["workload"]["xfp_per_game"] or e["workload"]["ppr_per_game"] or 0), e["full_name"])

    entries.sort(key=order)
    matched = [e for e in entries if (position == "ALL" or e["position"] == position)
               and (status == "all" or (status == "out") == (e["status"]["level"] in OUT_LEVELS))]
    counts = {"total": len(entries), "matched": len(matched),
              "out": sum(1 for e in entries if e["status"]["level"] in OUT_LEVELS),
              "questionable": sum(1 for e in entries if e["status"]["level"] == QUESTIONABLE),
              "long_term": sum(1 for e in entries if e["absence"]["kind"] == "long_term"),
              "affects_my_roster": sum(1 for e in entries if e["affects_my_roster"]),
              "skipped": skipped}

    report = _report_freshness(con, season, week)
    notices = _notices(report, diag, week, now, people, covered, bool(teams_rows), bool(usage))
    return {
        "season": season, "week": week, "position": position, "status": status,
        "generated_at": now.isoformat() + "Z",
        "league_context": {"available": bool(teams_rows), "league_name": lg["name"], "my_team_id": mine,
                           "perspective": "query" if team_id is not None else "owner_default"},
        "freshness": {
            "injury_report": report,
            "summary": diag["summary"],
            "thresholds": diag["thresholds"],
            "espn_players_synced_at": _ts(max((p["synced_at"] for p in players if p["synced_at"]), default=None)),
            "espn_projections_synced_at": espn_synced,
            "usage_through_week": max((r["week"] for r in usage), default=None),
            "depth_chart_as_of": max((d["as_of"] for d in depth.values()), default=None),
        },
        "method": {"observed": OBSERVED_NOTE, "estimate": ESTIMATE_NOTE,
                   "thresholds": {"min_snap_share": MIN_SNAP_SHARE, "min_opportunities_per_game": MIN_OPPS_PER_GAME,
                                  "min_qb_snap_share": MIN_QB_SNAP_SHARE, "recent_games": RECENT_GAMES,
                                  "pool_weeks": POOL_WEEKS, "long_term_missed": LONG_TERM_MISSED}},
        "counts": counts,
        "notices": notices,
        "players": matched,
    }


def _qb_clarity(n_without: int, mates: list[dict]) -> dict:
    backup = next((m for m in mates if m["healthy"]), None)
    if backup is None:
        return {"label": "no_history", "text": "No healthy backup quarterback is listed on the depth chart or in this "
                "season's usage."}
    src = f"depth chart QB{backup['depth_rank']}" if backup["depth_rank"] else "this season's usage"
    played = backup["observed"]["without"]["games"] if backup["observed"]["without"] else 0
    seen = (f" He played in {played} of the team's {n_without} {'game' if n_without == 1 else 'games'} without "
            "the starter." if n_without else "")
    return {"label": "no_history" if n_without == 0 else "thin_history" if n_without == 1 else "split",
            "text": f"Next in line: {backup['full_name']} ({src}).{seen} Pass catchers' production depends on the "
                    "quarterback; the observed change below is a small sample."}


def _home_away(con, season: int, week: int) -> dict[str, bool]:
    if not table_exists(con, "nfl_schedules"):
        return {}
    out = {}
    for h, a in con.execute("SELECT home_team, away_team FROM nfl_schedules WHERE season = ? AND week = ? "
                            "AND game_type = 'REG'", [season, week]).fetchall():
        out[h], out[a] = True, False
    return out


def _notices(report: dict, diag: dict, week: int, now: datetime, people: dict, covered: bool, teams: bool,
             usage: bool) -> list[dict]:
    """Visible data problems: missing or stale injury evidence, an old ESPN sync, no schedule or usage."""
    out = []
    if not report["available"]:
        out.append({"tone": "caution", "code": "no_nfl_report",
                    "text": "The NFL injury report has never been synced, so statuses come from ESPN only."})
    elif report["current_week_rows"] == 0:
        out.append({"tone": "caution", "code": "report_not_published",
                    "text": f"No week {week} NFL injury report rows are synced yet (latest: week "
                            f"{report['latest_week']}). Statuses come from ESPN until the report is published; "
                            "re-run scripts/sync_nfl.py later in the week."})
    s = diag["summary"]
    missing = [t for t in s["teams_by_status"].get(fresh.MISSING, [])]
    stale = [t for t in s["teams_by_status"].get(fresh.STALE, [])]
    upcoming = {i["team"] for i in diag["upcoming_issues"]}
    if stale and upcoming & set(stale):
        out.append({"tone": "caution", "code": "stale_teams",
                    "text": f"Injury evidence is stale for {len(stale)} {'team' if len(stale) == 1 else 'teams'} "
                            f"({', '.join(sorted(stale))}): retrieved more than "
                            f"{diag['thresholds']['stale_after_hours']:.0f} hours ago with kickoff close. Re-sync "
                            "before relying on these statuses."})
    if missing and upcoming & set(missing):
        out.append({"tone": "negative", "code": "missing_teams",
                    "text": f"No injury evidence for {len(missing)} {'team' if len(missing) == 1 else 'teams'} "
                            f"({', '.join(sorted(missing))}). A missing report is not a clean bill of health."})
    newest = max((p["synced_at"] for p in people.values() if p.get("synced_at")), default=None)
    if newest is not None:
        age = (now - fresh.naive_utc(newest)).total_seconds() / 3600 if fresh.naive_utc(newest) else None
        if age is not None and age > diag["thresholds"]["stale_after_hours"]:
            out.append({"tone": "caution", "code": "espn_stale",
                        "text": f"ESPN player statuses were last synced {age:.0f} hours ago. Run "
                                "scripts/sync_espn.py for current designations."})
    elif not people:
        out.append({"tone": "caution", "code": "no_espn_players",
                    "text": "No ESPN players are synced, so ESPN statuses and roster availability are unknown."})
    if not covered:
        out.append({"tone": "info", "code": "no_schedule",
                    "text": "The NFL schedule for this week is not synced, so opponents and kickoffs are unknown."})
    if not usage:
        out.append({"tone": "caution", "code": "no_usage",
                    "text": "No usage history is synced for this season, so workloads and teammate splits are "
                            "unavailable. Run scripts/sync_nfl.py."})
    if not teams:
        out.append({"tone": "info", "code": "no_rosters",
                    "text": "League rosters are not synced, so roster and waiver status are unknown."})
    return out
