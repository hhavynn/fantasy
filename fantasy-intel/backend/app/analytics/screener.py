"""Advanced multi-metric player screener (issue #226, contract screener/v1). Read-only and deterministic.

One typed query over the whole NFL WR / RB / TE pool, built only from data the app already stores: nflverse usage in
`player_week_usage` (snaps, targets, team shares, red-zone counts, PPR points and ffopportunity xFP), nflverse team
target / air-yard totals, and route data only where an authorized external utilization import supplied it. No new
source, no scraping, no paid API, no estimate of anything that is not stored.

Query grammar (the API takes it as query parameters; app/api/screener.py):

  pos         WR | RB | TE, repeatable (default all three). A filter on a metric a position does not have is false
              for that position's players (a WR has no backfield share), never unknown and never 0.
  season      default: the synced season.
  from, to    an exact regular-season week range (1..18). `to` may not pass the latest week with data.
  last        optional: only each player's last N games played inside the range (1..8).
  min_games   games played in the window (default 1); min_routes: routes run in the window (route data needed).
  f           up to MAX_FILTERS filters, each `<group>:<metric>:<op>:<value>` with group 1..MAX_GROUPS, an allowlisted
              metric key, op gte | gt | lte | lt and a decimal value in the metric's own unit (percent for shares,
              per game for counts and points), inside the metric's bounds.
              Precedence: filters in the same group are ANDed; groups are ORed. So `1:a`, `1:b`, `2:c` means
              (a AND b) OR c. There is no negation, no expression text and no SQL from the client.
  owner       any | available | rostered (league ownership; unknown ownership is never available or rostered).
  sort, dir   a metric key, `name` or `games`; asc | desc. Missing values sort last either way; ties break on name,
              then the nflverse id, so the order is total and stable.
  limit       1..MAX_LIMIT rows (default DEFAULT_LIMIT).

Missing data never becomes zero. A game counts toward a metric only when every input of that metric is known for that
game; a metric with no such game is missing. Filters use three-valued logic: a condition on a missing metric is
unknown; a group is false when any condition is false, unknown when none is false but one is unknown; the screen
matches when any group is true. A row whose result is unknown is excluded and reported in `incomplete` with the metrics
it lacks; a matched row that still lacks (or only partly covers) a metric lists it in its own `incomplete`. Filters
compare the value the response shows (rounded to 2 decimals), so what a reader sees is what was filtered.

Percentiles are position-relative mid-rank percentiles (ties share the average rank) among the players at the same
position who pass the sample thresholds and have the metric; they are omitted when that pool has fewer than
PERCENTILE_MIN_POOL players, and never mix positions.

League ownership is annotated per row from the league's own player pool, after the caller was authorized for the league
(the /v2/leagues/{lg} mount runs league_scope before the handler; the classic site is the single-league deployment).
Only a verified one-to-one nflverse-to-league player link counts; anything else is "unknown", never available.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType

from ..db.connection import fetch_dicts, table_columns, table_exists
from .legacy_league import latest_league
from .player_scores import played
from .research import resolve_season

CONTRACT = "screener/v1"
POSITIONS = ("WR", "RB", "TE")
OPS = MappingProxyType({"gte": "≥", "gt": ">", "lte": "≤", "lt": "<"})
DIRS = ("asc", "desc")
OWNER_FILTERS = ("any", "available", "rostered")
MAX_FILTERS = 8
MAX_GROUPS = 4
MAX_LIMIT = 100
DEFAULT_LIMIT = 50
MAX_LAST = 8
MAX_WEEK = 18
MAX_MIN_GAMES = 18
MAX_MIN_ROUTES = 1000
PERCENTILE_MIN_POOL = 5
INCOMPLETE_CAP = 50
DIGITS = 2
DEFAULT_SORT = "xfp_pg"
SORT_SPECIAL = ("name", "games")
GROUP_NAMES = ("A", "B", "C", "D")

FILTER_RE = re.compile(r"([1-9]):([a-z][a-z0-9_]{0,31}):(gte|gt|lte|lt):(-?[0-9]{1,4}(?:\.[0-9]{1,2})?)", re.ASCII)

NFLVERSE_STATS = "nflverse weekly player stats"
NFLVERSE_SNAPS = "nflverse snap counts (Pro Football Reference)"
NFLVERSE_PBP = "nflverse play-by-play"
NFLVERSE_XFP = "nflverse ffopportunity expected points"
EXTERNAL = "authorized external utilization import (missing when none is imported)"


class ScreenerError(ValueError):
    """An invalid screen (HTTP 422). The message names the parameter and the allowed values."""


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    short: str
    positions: tuple[str, ...]
    unit: str                     # percent | per_game | points_per_game
    kind: str                     # mean | share | mean_sum | mean_diff
    inputs: tuple[str, ...]       # usage columns that must all be known for a game to count
    denominator: str
    source: str
    lo: float
    hi: float
    definition: str

    @property
    def scale(self) -> float:
        return 100.0 if self.unit == "percent" else 1.0

    def public(self) -> dict:
        return {"key": self.key, "label": self.label, "short": self.short, "positions": list(self.positions),
                "unit": self.unit, "unit_label": UNIT_LABEL[self.unit], "aggregation": self.kind,
                "inputs": list(self.inputs), "denominator": self.denominator, "source": self.source,
                "bounds": [self.lo, self.hi], "definition": self.definition}


UNIT_LABEL = MappingProxyType({"percent": "%", "per_game": "per game", "points_per_game": "PPR points per game"})
ALL = POSITIONS

METRICS: tuple[Metric, ...] = (
    Metric("snap_share", "Snap share", "Snap %", ALL, "percent", "mean", ("offense_pct",),
           "the team's offensive snaps in each game; the mean of the weekly shares", NFLVERSE_SNAPS, 0, 100,
           "Share of the team's offensive snaps the player was on the field for, averaged over his games with a "
           "snap record."),
    Metric("snaps_pg", "Offensive snaps per game", "Snaps/G", ALL, "per_game", "mean", ("offense_snaps",),
           "games with a snap record", NFLVERSE_SNAPS, 0, 100, "Offensive snaps per game played."),
    Metric("route_participation", "Route participation", "Route %", ALL, "percent", "mean", ("route_participation",),
           "provider-defined (usually team dropbacks); the mean of the weekly values", EXTERNAL, 0, 100,
           "Share of pass plays the player ran a route on, as the authorized import reports it. Never estimated "
           "from snaps."),
    Metric("routes_pg", "Routes per game", "Routes/G", ALL, "per_game", "mean", ("routes",),
           "games with imported route data", EXTERNAL, 0, 80, "Routes run per game, from the authorized import."),
    Metric("targets_pg", "Targets per game", "Tgt/G", ALL, "per_game", "mean", ("targets",),
           "games played", NFLVERSE_STATS, 0, 30, "Targets per game played."),
    Metric("target_share", "Target share", "Tgt %", ALL, "percent", "share", ("targets", "team_targets"),
           "the team's targets in the same games (sum of the player's targets / sum of the team's)", NFLVERSE_STATS,
           0, 100, "The player's targets over his team's targets, summed over the same games."),
    Metric("air_yards_share", "Air-yard share", "Air %", ("WR", "TE"), "percent", "share",
           ("air_yards", "team_air_yards"), "the team's air yards in the same games (sums)", NFLVERSE_STATS, 0, 100,
           "The player's intended air yards over his team's, summed over the same games."),
    Metric("team_rush_share", "Team rush share", "Rush %", ("RB",), "percent", "share",
           ("rush_attempts", "team_rush_attempts"),
           "the team's rush plays in the same games, quarterback runs and scrambles included, kneels excluded",
           NFLVERSE_PBP, 0, 100, "The player's rush plays over all of his team's rush plays."),
    Metric("backfield_share", "Backfield share", "Backfield %", ("RB",), "percent", "share",
           ("designed_rushes", "team_rb_carries"), "the team's running-back designed rushes in the same games",
           NFLVERSE_PBP, 0, 100, "The player's designed rushes over all running-back designed rushes on his team."),
    Metric("carries_pg", "Carries per game", "Car/G", ("RB",), "per_game", "mean", ("carries",),
           "games played", NFLVERSE_STATS, 0, 40, "Official carries per game played."),
    Metric("opportunities_pg", "Carries + targets per game", "Opp/G", ALL, "per_game", "mean", ("opportunities",),
           "games played", NFLVERSE_STATS, 0, 50, "Carries plus targets per game played."),
    Metric("rz_opportunities_pg", "Red-zone opportunities per game", "RZ opp/G", ALL, "per_game", "mean_sum",
           ("rz_targets", "rz_carries"), "games with play-by-play red-zone counts", NFLVERSE_PBP, 0, 20,
           "Targets plus designed rushes inside the opponent's 20, per game."),
    Metric("rz_targets_pg", "Red-zone targets per game", "RZ tgt/G", ALL, "per_game", "mean", ("rz_targets",),
           "games with play-by-play red-zone counts", NFLVERSE_PBP, 0, 15, "Targets inside the opponent's 20, per game."),
    Metric("rz_carries_pg", "Red-zone carries per game", "RZ car/G", ("RB",), "per_game", "mean", ("rz_carries",),
           "games with play-by-play red-zone counts", NFLVERSE_PBP, 0, 15,
           "Designed rushes inside the opponent's 20, per game."),
    Metric("xfp_pg", "Expected points per game", "xFP/G", ALL, "points_per_game", "mean", ("xfp",),
           "games with a published xFP", NFLVERSE_XFP, 0, 60,
           "Full-PPR points an average player would score from the same targets and carries, per game."),
    Metric("ppr_pg", "PPR points per game", "PPR/G", ALL, "points_per_game", "mean", ("fantasy_points_ppr",),
           "games played", NFLVERSE_STATS, -10, 80, "Full-PPR fantasy points per game played (1 per reception)."),
    Metric("fpoe_pg", "Points vs xFP per game", "FPOE/G", ALL, "points_per_game", "mean_diff",
           ("fantasy_points_ppr", "xfp"), "games with both PPR points and a published xFP", NFLVERSE_XFP, -40, 40,
           "Actual PPR minus xFP, averaged over the same games. Negative: the box score trails the role."),
)
METRIC_BY_KEY = MappingProxyType({m.key: m for m in METRICS})

# Usage columns the screen reads (identifiers are these constants only; every value is a bound parameter).
USAGE_COLUMNS = ("offense_pct", "offense_snaps", "route_participation", "routes", "targets", "air_yards",
                 "rush_attempts", "team_rush_attempts", "designed_rushes", "team_rb_carries", "carries", "opportunities",
                 "rz_targets", "rz_carries", "xfp", "fantasy_points_ppr", "route_data_provider", "route_data_dataset",
                 "route_data_updated_at")

# Five-plus ready-made screens (the UI's presets; #219 can name them too). Values are in each metric's unit.
def _freeze(value):
    """Registry tables are immutable: mappings become MappingProxyType, lists become tuples."""
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


PRESETS: tuple[MappingProxyType, ...] = tuple(_freeze(p) for p in (
    {"key": "wr_routes_targets_last3", "title": "WR routes and targets, last 3 games",
     "summary": "Receivers running a route on at least 70% of pass plays and seeing 5+ targets a game over their last "
                "three games. Needs imported route data; without it every row is reported as incomplete.",
     "query": {"pos": ["WR"], "last": 3, "min_games": 3,
               "f": ["1:route_participation:gte:70", "1:targets_pg:gte:5"], "sort": "targets_pg", "dir": "desc"}},
    {"key": "wr_te_target_earners", "title": "Target earners (WR, TE)",
     "summary": "A target share of 22%+ or 7+ targets a game, with 3+ games in the window.",
     "query": {"pos": ["WR", "TE"], "min_games": 3, "f": ["1:target_share:gte:22", "2:targets_pg:gte:7"],
               "sort": "target_share", "dir": "desc"}},
    {"key": "rb_workhorse", "title": "Workhorse backs",
     "summary": "Backs with 60%+ of their team's running-back designed rushes and 3+ targets a game.",
     "query": {"pos": ["RB"], "min_games": 3, "f": ["1:backfield_share:gte:60", "1:targets_pg:gte:3"],
               "sort": "xfp_pg", "dir": "desc"}},
    {"key": "unlucky_volume", "title": "Volume without the points",
     "summary": "12+ expected points a game while scoring at least 2 points a game under that expectation.",
     "query": {"min_games": 3, "f": ["1:xfp_pg:gte:12", "1:fpoe_pg:lte:-2"], "sort": "fpoe_pg", "dir": "asc"}},
    {"key": "red_zone_roles", "title": "Red-zone roles",
     "summary": "1.5+ red-zone opportunities a game (targets plus designed rushes inside the 20), last 4 games.",
     "query": {"last": 4, "min_games": 3, "f": ["1:rz_opportunities_pg:gte:1.5"], "sort": "rz_opportunities_pg",
               "dir": "desc"}},
    {"key": "te_full_time", "title": "Full-time tight ends",
     "summary": "Tight ends on 75%+ of snaps with 15%+ of their team's targets.",
     "query": {"pos": ["TE"], "min_games": 3, "f": ["1:snap_share:gte:75", "1:target_share:gte:15"],
               "sort": "target_share", "dir": "desc"}},
    {"key": "available_upside", "title": "Available with a real role",
     "summary": "Players this league lists as available (free agent, on waivers or not rostered) with 9+ expected "
                "points a game. Unknown ownership is excluded and counted.",
     "query": {"owner": "available", "min_games": 2, "f": ["1:xfp_pg:gte:9"], "sort": "xfp_pg", "dir": "desc"}},
))


# --------------------------------------------------------------------------------------------------- validation --

@dataclass(frozen=True)
class Filter:
    group: int
    metric: Metric
    op: str
    value: float

    def token(self) -> str:
        return f"{self.group}:{self.metric.key}:{self.op}:{_fmt(self.value)}"

    def text(self) -> str:
        unit = "%" if self.metric.unit == "percent" else ""
        return f"{self.metric.short} {OPS[self.op]} {_fmt(self.value)}{unit}"


@dataclass(frozen=True)
class Query:
    season: int | None
    positions: tuple[str, ...]
    week_from: int | None
    week_to: int | None
    last: int | None
    min_games: int
    min_routes: int | None
    filters: tuple[Filter, ...]
    owner: str
    sort: str
    dir: str
    limit: int


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def _int(name: str, value, lo: int, hi: int, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,4}", str(value)):
        raise ScreenerError(f"{name} must be a whole number from {lo} to {hi}")
    v = int(value)
    if not lo <= v <= hi:
        raise ScreenerError(f"{name} must be from {lo} to {hi}")
    return v


def parse_filter(token: str) -> Filter:
    m = FILTER_RE.fullmatch(token) if isinstance(token, str) else None
    if not m:
        raise ScreenerError("each filter is group:metric:op:value, for example 1:targets_pg:gte:5 (group 1 to "
                            f"{MAX_GROUPS}; op gte, gt, lte or lt; a plain decimal value)")
    group, key, op, raw = int(m.group(1)), m.group(2), m.group(3), m.group(4)
    if group > MAX_GROUPS:
        raise ScreenerError(f"filter group must be from 1 to {MAX_GROUPS}")
    metric = METRIC_BY_KEY.get(key)
    if metric is None:
        raise ScreenerError(f"unknown metric {key!r}; see /api/screener/metrics")
    value = float(raw)
    if not math.isfinite(value) or not metric.lo <= value <= metric.hi:
        raise ScreenerError(f"{metric.key} takes a value from {_fmt(metric.lo)} to {_fmt(metric.hi)} "
                            f"({UNIT_LABEL[metric.unit]})")
    return Filter(group, metric, op, value)


def validate(*, season: int | None = None, positions=None, week_from=None, week_to=None, last=None,
             min_games=None, min_routes=None, filters=None, owner: str | None = None, sort: str | None = None,
             direction: str | None = None, limit=None) -> Query:
    """A typed, bounded query from raw parameters, or ScreenerError naming what is wrong."""
    pos = list(positions or POSITIONS)
    if len(pos) > len(POSITIONS) or any(p not in POSITIONS for p in pos):
        raise ScreenerError(f"pos must be among {', '.join(POSITIONS)}")
    pos_t = tuple(p for p in POSITIONS if p in pos)
    raw = list(filters or [])
    if len(raw) > MAX_FILTERS:
        raise ScreenerError(f"at most {MAX_FILTERS} filters")
    fs = tuple(sorted((parse_filter(t) for t in raw), key=lambda f: f.group))      # stable: input order per group
    for f in fs:
        if not set(f.metric.positions) & set(pos_t):
            raise ScreenerError(f"{f.metric.key} is not defined for {', '.join(pos_t)} "
                                f"(only {', '.join(f.metric.positions)})")
    wf = _int("from", week_from, 1, MAX_WEEK, None)
    wt = _int("to", week_to, 1, MAX_WEEK, None)
    if wf is not None and wt is not None and wf > wt:
        raise ScreenerError("from must not be after to")
    owner = owner or "any"
    if owner not in OWNER_FILTERS:
        raise ScreenerError(f"owner must be one of {', '.join(OWNER_FILTERS)}")
    sort = sort or DEFAULT_SORT
    if sort not in METRIC_BY_KEY and sort not in SORT_SPECIAL:
        raise ScreenerError(f"sort must be a metric key, {' or '.join(SORT_SPECIAL)}")
    direction = direction or ("asc" if sort == "name" else "desc")
    if direction not in DIRS:
        raise ScreenerError("dir must be asc or desc")
    if season is not None and not 1999 <= int(season) <= 2100:
        raise ScreenerError("season must be from 1999 to 2100")
    return Query(season=season, positions=pos_t, week_from=wf, week_to=wt,
                 last=_int("last", last, 1, MAX_LAST, None),
                 min_games=_int("min_games", min_games, 1, MAX_MIN_GAMES, 1),
                 min_routes=_int("min_routes", min_routes, 1, MAX_MIN_ROUTES, None),
                 filters=fs, owner=owner, sort=sort, dir=direction,
                 limit=_int("limit", limit, 1, MAX_LIMIT, DEFAULT_LIMIT))


def expression(filters: tuple[Filter, ...]) -> str | None:
    """The screen as text: groups in parentheses joined by OR, conditions joined by AND."""
    groups: dict[int, list[Filter]] = defaultdict(list)
    for f in filters:
        groups[f.group].append(f)
    if not groups:
        return None
    parts = [" AND ".join(f.text() for f in fs) for _, fs in sorted(groups.items())]
    return parts[0] if len(parts) == 1 else " OR ".join(f"({p})" for p in parts)


# ------------------------------------------------------------------------------------------------------ loading --

def _usage_sql(con, cols: set[str]) -> str:
    """One parameterized read of the window (season, from, to). Column names come from USAGE_COLUMNS only; a column an
    older file lacks is read as NULL (missing), never 0."""
    select = ", ".join(f"u.{c}" if c in cols else f"NULL AS {c}" for c in USAGE_COLUMNS)
    if table_exists(con, "nfl_weekly_stats"):
        totals = ("SELECT team, week, sum(targets)::DOUBLE AS team_targets, "
                  "sum(receiving_air_yards)::DOUBLE AS team_air_yards "
                  "FROM nfl_weekly_stats WHERE season = ? AND season_type = 'REG' GROUP BY 1, 2")
    else:
        totals = ("SELECT team, week, sum(targets)::DOUBLE AS team_targets, sum(air_yards)::DOUBLE AS team_air_yards "
                  "FROM player_week_usage WHERE season = ? GROUP BY 1, 2")
    return (f"SELECT u.week, u.gsis_id, u.team, u.position, {select}, tm.team_targets, tm.team_air_yards "
            f"FROM player_week_usage u LEFT JOIN ({totals}) tm ON tm.team = u.team AND tm.week = u.week "
            "WHERE u.season = ? AND u.week BETWEEN ? AND ? AND u.position IN ('WR', 'RB', 'TE') "
            "ORDER BY u.gsis_id, u.week")


def _data(con, season: int) -> dict:
    weeks = fetch_dicts(con, """SELECT week, count(*) AS rows, count(xfp) AS with_xfp FROM player_week_usage
                                WHERE season = ? AND position IN ('WR', 'RB', 'TE') GROUP BY week ORDER BY week""",
                        [season])
    nfl_sync = None
    if table_exists(con, "sync_runs"):
        row = con.execute("SELECT max(finished_at) FROM sync_runs WHERE status = 'ok' AND source = 'nfl'").fetchone()
        nfl_sync = row[0] if row else None
    return {"season": season,
            "latest_week": max((w["week"] for w in weeks), default=None),
            "xfp_through_week": max((w["week"] for w in weeks if w["with_xfp"]), default=None),
            "weeks_with_data": [w["week"] for w in weeks],
            "nfl_synced_at": nfl_sync,
            "team_totals_source": "nfl_weekly_stats" if table_exists(con, "nfl_weekly_stats") else "player_week_usage",
            "scoring_basis": "nflverse full PPR (1 point per reception); xFP is nflverse ffopportunity expected "
                             "full-PPR points"}


def _names(con, season: int, wf: int, wt: int) -> dict[str, str]:
    """Display names of the window's players (a join on the same window: no id list is sent as a parameter)."""
    if not table_exists(con, "nfl_players"):
        return {}
    return {r["gsis_id"]: r["display_name"] for r in fetch_dicts(con, """
        SELECT gsis_id, display_name FROM nfl_players WHERE gsis_id IN (
            SELECT DISTINCT gsis_id FROM player_week_usage WHERE season = ? AND week BETWEEN ? AND ?)""",
        [season, wf, wt])}


# ------------------------------------------------------------------------------------------------------ compute --

def metric_value(metric: Metric, games: list[dict]) -> tuple[float | None, int, str | None]:
    """(value in the metric's unit rounded to DIGITS, games counted, reason when missing)."""
    if len(metric.inputs) == 1:
        c = metric.inputs[0]
        vals = [g[c] for g in games if g[c] is not None]
        n = len(vals)
        if n == 0:
            return None, 0, f"no game in the window has {c}"
        v = sum(vals) / n
    else:
        a, b = metric.inputs
        pairs = [(g[a], g[b]) for g in games if g[a] is not None and g[b] is not None]
        n = len(pairs)
        if n == 0:
            return None, 0, f"no game in the window has both {a} and {b}"
        if metric.kind == "share":
            d = sum(y for _, y in pairs)
            if not d:
                return None, n, f"{b} is 0 over the window (share undefined)"
            v = sum(x for x, _ in pairs) / d
        elif metric.kind == "mean_sum":
            v = sum(x + y for x, y in pairs) / n
        else:                                             # mean_diff
            v = sum(x - y for x, y in pairs) / n
    return round(v * metric.scale, DIGITS), n, None


def summarize(rows: list[dict], last: int | None) -> dict:
    """One player's window: games played in the range (the last N when asked), every applicable metric with its
    sample, and routes (None when no game has route data: unknown, not 0)."""
    games = [r for r in rows if played(r)]
    if last is not None:
        games = games[-last:]
    position = rows[-1]["position"]
    metrics = {}
    for m in METRICS:
        if position not in m.positions:
            continue
        v, n, why = metric_value(m, games)
        metrics[m.key] = {"value": v, "n": n, "of": len(games), **({"reason": why} if why else {})}
    route_games = [g["routes"] for g in games if g.get("routes") is not None]
    team = (games or [r for r in rows if played(r)] or rows)[-1].get("team")
    return {"position": position, "team": team, "games": len(games), "weeks": [g["week"] for g in games],
            "routes": {"total": sum(route_games) if route_games else None, "n": len(route_games), "of": len(games)},
            "metrics": metrics}


def evaluate(f: Filter, s: dict) -> str:
    """"true" | "false" | "unknown" | "not_applicable" (false: the position has no such metric)."""
    if s["position"] not in f.metric.positions:
        return "not_applicable"
    v = s["metrics"][f.metric.key]["value"]
    if v is None:
        return "unknown"
    ok = {"gte": v >= f.value, "gt": v > f.value, "lte": v <= f.value, "lt": v < f.value}[f.op]
    return "true" if ok else "false"


def combine(filters: tuple[Filter, ...], results: list[str]) -> str:
    """Kleene three-valued logic: AND inside a group, OR across groups. not_applicable is false."""
    if not filters:
        return "true"
    groups: dict[int, list[str]] = defaultdict(list)
    for f, r in zip(filters, results):
        groups[f.group].append("false" if r == "not_applicable" else r)
    verdicts = []
    for rs in groups.values():
        verdicts.append("false" if "false" in rs else "unknown" if "unknown" in rs else "true")
    return "true" if "true" in verdicts else "unknown" if "unknown" in verdicts else "false"


def sample_gate(q: Query, s: dict) -> str:
    """"pass" | "below_min_games" | "below_min_routes" | "routes_unknown"."""
    if s["games"] < q.min_games:
        return "below_min_games"
    if q.min_routes is not None:
        r = s["routes"]
        if r["total"] is not None and r["total"] >= q.min_routes:
            return "pass"
        if r["n"] < r["of"]:
            return "routes_unknown"           # some games have no route data: the true total could still clear it
        return "below_min_routes"
    return "pass"


def _percentiles(pool: list[dict]) -> None:
    """Mid-rank percentile per metric among same-position players with a value (pool >= PERCENTILE_MIN_POOL)."""
    by_pos: dict[str, list[dict]] = defaultdict(list)
    for p in pool:
        by_pos[p["position"]].append(p)
    for players in by_pos.values():
        for m in METRICS:
            vals = sorted(p["metrics"][m.key]["value"] for p in players
                          if m.key in p["metrics"] and p["metrics"][m.key]["value"] is not None)
            n = len(vals)
            if n < PERCENTILE_MIN_POOL:
                continue
            for p in players:
                cell = p["metrics"].get(m.key)
                if cell and cell["value"] is not None:
                    v = cell["value"]
                    below, equal = bisect_left(vals, v), bisect_right(vals, v) - bisect_left(vals, v)
                    cell["percentile"] = round(100.0 * (below + 0.5 * equal) / n)
                    cell["pool"] = n


def _sort_key(q: Query):
    def key(p: dict):
        if q.sort == "name":
            v = p["name"].casefold()
        elif q.sort == "games":
            v = p["games"]
        else:
            cell = p["metrics"].get(q.sort)
            v = cell["value"] if cell else None
        missing = v is None
        if not missing and q.dir == "desc" and q.sort != "name":
            v = -v
        return (missing, v if not missing else 0, p["name"].casefold(), p["gsis_id"])
    return key


def _sorted(q: Query, rows: list[dict]) -> list[dict]:
    if q.sort == "name" and q.dir == "desc":
        known = sorted(rows, key=lambda p: (p["name"].casefold(), p["gsis_id"]), reverse=True)
        return known
    return sorted(rows, key=_sort_key(q))


# ---------------------------------------------------------------------------------------------------- ownership --

def _provider(con) -> str:
    import json
    lg = latest_league(con, ["settings_raw"])
    raw = (lg or {}).get("settings_raw")
    try:
        settings = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        settings = {}
    return str(settings.get("provider")) if isinstance(settings, dict) and settings.get("provider") else "espn"


UNKNOWN_OWNERSHIP = "Roster status unknown"


def _mapping(con, season: int, wf: int, wt: int) -> dict[str, dict]:
    """gsis -> {espn_player_id, status}: the research lab's rule (analytics/research.espn_mapping): one high-confidence
    league id is verified, two are ambiguous (neither is used), anything else is a name match only. Read with a join on
    the window (no id list is sent as a parameter)."""
    if not table_exists(con, "player_id_map"):
        return {}
    by: dict[str, set] = defaultdict(set)
    seen: set[str] = set()
    for r in fetch_dicts(con, """
            SELECT gsis_id, espn_player_id, match_confidence FROM player_id_map WHERE gsis_id IN (
                SELECT DISTINCT gsis_id FROM player_week_usage WHERE season = ? AND week BETWEEN ? AND ?)""",
            [season, wf, wt]):
        seen.add(r["gsis_id"])
        if r["match_confidence"] == "high":
            by[r["gsis_id"]].add(r["espn_player_id"])
    out = {}
    for g in seen:
        high = sorted(by.get(g, ()))
        out[g] = ({"espn_player_id": high[0], "status": "verified"} if len(high) == 1
                  else {"espn_player_id": None, "status": "ambiguous"} if high
                  else {"espn_player_id": None, "status": "name_match_only"})
    return out


def ownership(con, gsis_ids: list[str], season: int, wf: int, wt: int) -> tuple[dict, dict[str, dict]]:
    """(summary, gsis -> ownership). Statuses: rostered | free_agent | waivers | not_rostered | unknown. `available` is
    true only for a known free agent, waiver player or unrostered player; unknown is never available."""
    if not table_exists(con, "fantasy_players") or not table_exists(con, "leagues"):
        return ({"available": False, "reason": "No league player pool is stored, so ownership is unknown for every "
                                               "player."}, {})
    lg = latest_league(con, ["league_id", "season", "name"])
    provider = _provider(con)
    teams = {}
    if lg and table_exists(con, "fantasy_teams"):
        teams = {t["team_id"]: t for t in fetch_dicts(
            con, "SELECT team_id, name, is_mine FROM fantasy_teams WHERE league_id = ? AND season = ?",
            [lg["league_id"], lg["season"]])}
    mapping = _mapping(con, season, wf, wt)
    pool = {r["espn_player_id"]: r for r in fetch_dicts(
        con, "SELECT espn_player_id, availability_status, on_team_id FROM fantasy_players")}
    synced = con.execute("SELECT max(coalesce(detail_synced_at, synced_at)) FROM fantasy_players").fetchone()[0]
    out = {}
    for g in gsis_ids:
        mp = mapping.get(g, {"status": "unmapped", "espn_player_id": None})
        pid = mp.get("espn_player_id")
        base = {"player_id": pid, "mapping": mp["status"], "team_id": None, "team_name": None, "is_mine": False,
                "available": False}
        if pid is None:
            why = ("two league players match this nflverse player" if mp["status"] == "ambiguous"
                   else "no verified link between this nflverse player and a league player")
            out[g] = {**base, "status": "unknown", "label": UNKNOWN_OWNERSHIP, "reason": why}
            continue
        fp = pool.get(pid)
        status = fp["availability_status"] if fp else None
        if fp is None:
            out[g] = {**base, "status": "unknown", "label": UNKNOWN_OWNERSHIP,
                      "reason": "the player is not in this league's stored player pool"}
        elif status == "ONTEAM":
            t = teams.get(fp["on_team_id"])
            out[g] = {**base, "status": "rostered", "label": "Rostered", "team_id": fp["on_team_id"],
                      "team_name": t["name"] if t else None, "is_mine": bool(t and t["is_mine"])}
        elif status == "WAIVERS":
            out[g] = {**base, "status": "waivers", "label": "On waivers", "available": True}
        elif status == "FREEAGENT" and provider == "espn":
            out[g] = {**base, "status": "free_agent", "label": "Free agent", "available": True}
        elif status == "FREEAGENT":
            # The provider reports only rostered or not: a waiver period is not synced, so no "free agent" claim.
            out[g] = {**base, "status": "not_rostered", "label": "Not rostered", "available": True}
        else:
            out[g] = {**base, "status": "unknown", "label": UNKNOWN_OWNERSHIP,
                      "reason": "the league's roster status for this player is not stored"}
    mine = next((tid for tid, t in teams.items() if t["is_mine"]), None)
    return ({"available": True, "provider": provider, "league_name": lg["name"] if lg else None,
             "my_team_id": mine, "rosters_synced_at": synced,
             "rule": "Only a verified one-to-one link between the nflverse player and a league player is annotated; "
                     "anything else is reported as unknown and is never treated as available."}, out)


# -------------------------------------------------------------------------------------------------------- build --

def build_screen(con, q: Query) -> dict:
    """Run one validated screen. ScreenerError for a week range outside the stored data."""
    season = resolve_season(con, q.season)
    base = {"contract": CONTRACT, "limits": limits(), "metrics": registry(),
            "expression": expression(q.filters)}
    if season is None or not table_exists(con, "player_week_usage"):
        return {**base, "status": "no_data", "season": season, "query": canonical(q, None, None), "data": None,
                "counts": None, "rows": [], "incomplete": [], "ownership": None,
                "message": "No NFL usage data is stored yet."}
    data = _data(con, season)
    latest = data["latest_week"]
    if latest is None:
        return {**base, "status": "no_data", "season": season, "query": canonical(q, None, None), "data": data,
                "counts": None, "rows": [], "incomplete": [], "ownership": None,
                "message": f"No WR, RB or TE usage is stored for {season}."}
    wf = q.week_from or 1
    wt = q.week_to or latest
    if wt > latest or wf > latest:
        which, week = ("to", wt) if wt > latest else ("from", wf)
        raise ScreenerError(f"{which} (week {week}) is after the latest week with data (week {latest})")
    if wf > wt:
        raise ScreenerError("from must not be after to")
    cols = set(table_columns(con, "player_week_usage"))
    rows = fetch_dicts(con, _usage_sql(con, cols), [season, season, wf, wt])
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["gsis_id"]].append(r)
    names = _names(con, season, wf, wt)
    counts = {"pool": 0, "below_min_games": 0, "below_min_routes": 0, "routes_unknown": 0, "matched": 0,
              "not_matched": 0, "incomplete": 0, "ownership_unknown": 0, "ownership_excluded": 0, "shown": 0}
    pool, matched, incomplete = [], [], []
    for g, rs in by.items():
        if rs[-1]["position"] not in q.positions:
            continue
        s = summarize(rs, q.last)
        if s["games"] == 0:
            continue                       # no game played in the window: not in the pool
        counts["pool"] += 1
        p = {"gsis_id": g, "name": names.get(g) or g, **s}
        gate = sample_gate(q, s)
        if gate != "pass":
            counts[gate] += 1
            if gate == "routes_unknown":
                incomplete.append(_incomplete(p, ["routes"], "route data missing for some games, so min_routes "
                                                             "cannot be checked"))
            continue
        pool.append(p)
        results = [evaluate(f, s) for f in q.filters]
        p["conditions"] = [{"filter": f.token(), "result": r} for f, r in zip(q.filters, results)]
        verdict = combine(q.filters, results)
        if verdict == "true":
            matched.append(p)
        elif verdict == "unknown":
            missing = sorted({f.metric.key for f, r in zip(q.filters, results) if r == "unknown"})
            incomplete.append(_incomplete(p, missing, "a filter's metric is missing, so the screen cannot decide"))
        else:
            counts["not_matched"] += 1
    _percentiles(pool)
    counts["incomplete"] = len(incomplete)
    counts["matched"] = len(matched)
    own_summary, owned = ownership(con, sorted(p["gsis_id"] for p in matched), season, wf, wt)
    for p in matched:
        p["ownership"] = owned.get(p["gsis_id"]) if own_summary.get("available") else None
        if p["ownership"] is None:
            p["ownership"] = {"status": "unknown", "label": UNKNOWN_OWNERSHIP, "available": False, "player_id": None,
                              "mapping": None, "team_id": None, "team_name": None, "is_mine": False,
                              "reason": own_summary.get("reason")}
        if p["ownership"]["status"] == "unknown":
            counts["ownership_unknown"] += 1
    if q.owner != "any":
        keep = [p for p in matched if (p["ownership"]["available"] if q.owner == "available"
                                       else p["ownership"]["status"] == "rostered")]
        counts["ownership_excluded"] = len(matched) - len(keep)
        matched = keep
    shown = [_public_row(p) for p in _sorted(q, matched)[:q.limit]]
    counts["shown"] = len(shown)
    incomplete.sort(key=lambda x: (x["name"].casefold(), x["gsis_id"]))
    return {**base, "status": "ok" if shown else "empty", "season": season, "query": canonical(q, wf, wt),
            "data": {**data, "window": {"from": wf, "to": wt, "last": q.last}, "cutoffs": _cutoffs(rows),
                     "route_data": _route_data(rows)},
            "counts": counts, "truncated": len(matched) > len(shown), "rows": shown,
            "incomplete": incomplete[:INCOMPLETE_CAP], "incomplete_truncated": len(incomplete) > INCOMPLETE_CAP,
            "ownership": own_summary}


def _incomplete(p: dict, missing: list[str], reason: str) -> dict:
    return {"gsis_id": p["gsis_id"], "name": p["name"], "position": p["position"], "team": p["team"],
            "games": p["games"], "missing": missing, "reason": reason}


def _public_row(p: dict) -> dict:
    gaps = [{"metric": k, "n": c["n"], "of": c["of"], "state": "missing" if c["value"] is None else "partial"}
            for k, c in p["metrics"].items() if c["value"] is None or c["n"] < c["of"]]
    return {"gsis_id": p["gsis_id"], "name": p["name"], "position": p["position"], "team": p["team"],
            "games": p["games"], "weeks": p["weeks"], "routes": p["routes"], "metrics": p["metrics"],
            "conditions": p["conditions"], "incomplete": gaps, "ownership": p["ownership"]}


def _cutoffs(rows: list[dict]) -> dict:
    """Per metric: the latest week in the window with any row that has every input (its data cutoff)."""
    latest_first = sorted(rows, key=lambda r: -r["week"])
    return {m.key: next((r["week"] for r in latest_first if all(r[c] is not None for c in m.inputs)), None)
            for m in METRICS}


def _route_data(rows: list[dict]) -> dict:
    with_routes = [r for r in rows if r.get("routes") is not None or r.get("route_participation") is not None]
    providers = sorted({r["route_data_provider"] for r in with_routes if r.get("route_data_provider")})
    updated = [r["route_data_updated_at"] for r in with_routes if r.get("route_data_updated_at")]
    return {"rows": len(with_routes), "providers": providers, "updated_at": max(updated) if updated else None,
            "note": None if with_routes else "No route data is imported for this window: route participation, "
                                             "routes per game and min_routes are unavailable (never 0)."}


def canonical(q: Query, wf: int | None, wt: int | None) -> dict:
    """The normalized query (what ran), reusable as-is for the same answer."""
    return {"season": q.season, "pos": list(q.positions), "from": wf, "to": wt, "last": q.last,
            "min_games": q.min_games, "min_routes": q.min_routes, "f": [f.token() for f in q.filters],
            "owner": q.owner, "sort": q.sort, "dir": q.dir, "limit": q.limit}


def limits() -> dict:
    return {"max_filters": MAX_FILTERS, "max_groups": MAX_GROUPS, "max_limit": MAX_LIMIT, "default_limit": DEFAULT_LIMIT,
            "max_last": MAX_LAST, "max_week": MAX_WEEK, "max_min_games": MAX_MIN_GAMES,
            "max_min_routes": MAX_MIN_ROUTES, "percentile_min_pool": PERCENTILE_MIN_POOL,
            "incomplete_cap": INCOMPLETE_CAP, "ops": dict(OPS), "groups": list(GROUP_NAMES[:MAX_GROUPS]),
            "positions": list(POSITIONS), "owner": list(OWNER_FILTERS), "sort_special": list(SORT_SPECIAL),
            "precedence": "Filters in the same group are combined with AND; groups are combined with OR.",
            "missing": "A condition on a missing metric is unknown; an unknown screen result excludes the row and "
                       "reports it as incomplete. A metric a position does not have is false for that position.",
            "rounding": f"Values are rounded to {DIGITS} decimals and filters compare the rounded value."}


def registry() -> list[dict]:
    return [m.public() for m in METRICS]


def presets() -> list[dict]:
    return [{"key": p["key"], "title": p["title"], "summary": p["summary"],
             "query": {k: list(v) if isinstance(v, tuple) else v for k, v in p["query"].items()}} for p in PRESETS]
