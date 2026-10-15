"""League-agnostic trade market: buy-low / sell-high scoring, a usage value index, offer generation.

Public trade advice cannot see anyone's league, so nothing here reads ESPN rosters, league scoring,
lineup slots or projections. Inputs are nflverse tables only (player_week_usage, nfl_team_week,
team_game_lines, defense_context, nfl_injuries, nfl_rosters_weekly, nfl_players).

Two values per RB/WR/TE (QBs are out of scope: their usage signal is weak and 1QB trades are rare):

  usage_value   0-100  OUR view. Position-normalized role (snap share, target share, WOPR, air-yard
                       share, backfield share, red-zone opportunity share), role trend, team context
                       and upcoming schedule, mapped to a cross-position scale by how far inside the
                       startable pool the player's role ranks. Never uses fantasy points, PPG, xFP or
                       projections.
  market_value  0-100  What a typical manager is likely to PAY. Until a free public price (ADP) is
                       wired in, it is a proxy: half usage, half box score (scrimmage yards and
                       touchdowns per game, NOT fantasy points). `build_market(..., adp_ranks=...)`
                       replaces the box-score half with a position ADP rank when one is supplied.

Buy-low  = usage_value well above market_value (the role is bigger than the box score shows).
Sell-high = market_value well above usage_value (the box score is bigger than the role).

Offers are priced in market_value (what the other manager sees) and must still win in usage_value
(what we believe). Fairness is a qualitative label from the other manager's side of the deal; no
acceptance probability is computed. Lopsided deals in either direction are never generated.

Every number an offer or candidate cites is an Evidence item naming (player, metric, weeks) so a
claim checker can recompute it with `derive_metric` from the same player_week_usage rows.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from itertools import combinations
from statistics import mean
from typing import Iterable, Mapping, Sequence

from ..db.connection import fetch_dicts, table_exists
from .context import matchup_label
from .player_scores import MIN_GAMES_CLASSIFY, RECENT_GAMES, percentile_scores, played
from .trade_assets import BALANCED_RATIO, SIGNIFICANT_RATIO
from .trends import TREND_METRICS, composite_trend, compute_trend

POSITIONS = ("RB", "WR", "TE")
MIN_GAMES_POOL = 2               # games to enter the percentile pool
RECENT_WEIGHT = 0.6              # role metric = 0.6 * last-3-games + 0.4 * season
SCHEDULE_WEEKS = 4               # upcoming weeks rated for schedule

# Startable pool for a neutral 12-team league: 2 RB, 2 WR, 1 TE, 1 FLEX (flex split ~RB 6 / WR 6).
STARTERS = {"RB": 30, "WR": 30, "TE": 12}
VALUE_CURVE = 1.6                # convex: elite roles are worth more than two mid ones

# Role metrics and weights per position. rz_opp_share = (rz carries + rz targets) / team rz plays.
ROLE_WEIGHTS: dict[str, dict[str, float]] = {
    "RB": {"offense_pct": 0.20, "backfield_rush_share": 0.30, "target_share": 0.20, "rz_opp_share": 0.30},
    "WR": {"offense_pct": 0.15, "target_share": 0.25, "air_yards_share": 0.20, "wopr": 0.20, "rz_opp_share": 0.20},
    "TE": {"offense_pct": 0.25, "target_share": 0.30, "wopr": 0.25, "rz_opp_share": 0.20},
}
# Role index multipliers: trend in -1..+1, context/schedule 0-100 (50 = neutral).
ADJUST = {"trend": 0.10, "context": 0.06, "schedule": 0.04}
PRODUCTION_BLEND = {"yards_pg": 0.6, "tds_pg": 0.4}     # box score the market sees; never fantasy points
INDEX_CAP = 1.6                  # a metric counts at most 1.6x the startable benchmark
PARTIAL_GAME = 0.5               # snap share under half the player's median = partial game

# Candidate thresholds.
GAP_MIN = 10.0                   # usage_value - market_value mismatch on the 0-100 value scale
ELITE_SHARE = 0.25               # usage rank inside the top quarter of starters = elite role: never a sell
UNAVAILABLE_STATUSES = {"Out", "Doubtful"}
UNAVAILABLE_ROSTER = {"RES", "CUT", "RET", "EXE", "DEV"}
INJURED_DISCOUNT = 0.85          # usage/market value multiplier while Out/Doubtful/on a reserve list
VACATED_SHARE = {"RB": ("backfield_rush_share", 0.30), "WR": ("target_share", 0.15), "TE": ("target_share", 0.15)}

# Fairness bands on r = market(what the other manager receives) / market(what they give).
LOWBALL, OPENING, FAIR, HIGH = 0.92, 1.00, 1.10, 1.22
FAIRNESS_LABELS = {"opening": "opening offer", "fair": "fair range", "high": "likely high price"}
DEPTH_DISCOUNT = 0.5             # second piece in a 2-for-1 counts half: one roster spot, one starter
MIN_2FOR1_MARKET = 30.0          # below this a target is not worth two roster pieces
POOL_SIZE = 40                   # pieces considered per offer search


# ----------------------------------------------------------------------- data shapes --

@dataclass(frozen=True)
class Evidence:
    """One re-derivable number: `derive_metric(rows_of(gsis_id), metric, weeks) == value`."""
    gsis_id: str
    metric: str
    weeks: tuple[int, ...]
    value: float | None
    text: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlayerMarket:
    gsis_id: str
    name: str
    position: str
    team: str
    games: int
    weeks: tuple[int, ...]
    recent_weeks: tuple[int, ...]
    role: dict[str, float | None]           # blended metric values
    role_pct: dict[str, float | None]       # 0-100 position percentiles
    role_score: float
    trend_score: float | None
    trend_label: str
    context_score: float
    schedule_score: float
    schedule: list[dict]
    production: dict[str, float]
    production_score: float
    usage_score: float
    usage_rank: int | None = None
    market_rank: int | None = None
    usage_value: float = 0.0
    market_value: float = 0.0
    partial_weeks: tuple[int, ...] = ()     # games excluded from role metrics (see full_games)
    status: str | None = None               # latest injury report / roster status
    available: bool = True
    flags: list[str] = field(default_factory=list)
    vacated: list[dict] = field(default_factory=list)
    totals: dict[str, float] = field(default_factory=dict)     # season touchdowns, yards, yards_pg
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def gap(self) -> float:
        return round(self.usage_value - self.market_value, 1)


@dataclass
class MarketInputs:
    season: int
    usage: list[dict]
    team_week: list[dict]
    game_lines: list[dict]
    defense: list[dict]
    injuries: list[dict]
    roster_status: list[dict]
    names: dict[str, str]


# ------------------------------------------------------------------- metric helpers --

def _num(v) -> float:
    return float(v) if v is not None else 0.0


def derive_metric(rows: Sequence[Mapping], metric: str, weeks: Iterable[int] | None = None) -> float | None:
    """Recompute one evidence metric from a player's player_week_usage rows (plus `team_rz_plays`).

    Shares are per-game means over the weeks; rz_opp_share and rates are ratios of sums; counts are
    totals; *_pg values are per-game means. Returns None when the metric has no data in the window.
    """
    ws = set(weeks) if weeks is not None else None
    rs = [r for r in rows if (ws is None or r["week"] in ws) and played(r)]
    if not rs:
        return None
    if metric in ("offense_pct", "target_share", "air_yards_share", "wopr", "backfield_rush_share",
                  "team_rush_share"):
        vals = [r[metric] for r in rs if r.get(metric) is not None]
        return mean(vals) if vals else None
    if metric == "rz_opp_share":
        den = sum(_num(r.get("team_rz_plays")) for r in rs)
        return (sum(_num(r.get("rz_carries")) + _num(r.get("rz_targets")) for r in rs) / den) if den else None
    if metric == "rz_opportunities":
        return sum(_num(r.get("rz_carries")) + _num(r.get("rz_targets")) for r in rs)
    if metric == "touchdowns":
        return sum(_num(r.get("rushing_tds")) + _num(r.get("receiving_tds")) for r in rs)
    if metric == "scrimmage_yards":
        return sum(_num(r.get("rushing_yards")) + _num(r.get("receiving_yards")) for r in rs)
    if metric in ("targets", "carries", "receptions", "explosive_runs", "designed_rushes", "opportunities"):
        return sum(_num(r.get(metric)) for r in rs)
    if metric == "yards_pg":
        return derive_metric(rs, "scrimmage_yards") / len(rs)
    if metric == "tds_pg":
        return derive_metric(rs, "touchdowns") / len(rs)
    if metric == "games":
        return float(len(rs))
    if metric == "explosive_run_rate":
        d = sum(_num(r.get("designed_rushes")) for r in rs)
        return sum(_num(r.get("explosive_runs")) for r in rs) / d if d else None
    if metric == "yards_per_target":
        t = sum(_num(r.get("targets")) for r in rs)
        return sum(_num(r.get("receiving_yards")) for r in rs) / t if t else None
    if metric == "yards_per_opportunity":
        o = sum(_num(r.get("opportunities")) for r in rs)
        return derive_metric(rs, "scrimmage_yards") / o if o else None
    if metric == "td_per_opportunity":
        o = sum(_num(r.get("opportunities")) for r in rs)
        return derive_metric(rs, "touchdowns") / o if o else None
    raise KeyError(f"unknown metric {metric!r}")


def _plural(v: float, one: str, many: str | None = None) -> str:
    n = round(v)
    return f"{n} {one if n == 1 else many or one + 's'}"


def _pct_text(v: float | None) -> str:
    return "n/a" if v is None else f"{100 * v:.0f}%"


METRIC_LABELS = {
    "offense_pct": "snap share", "target_share": "target share", "air_yards_share": "air-yard share",
    "wopr": "WOPR", "backfield_rush_share": "backfield share", "rz_opp_share": "red-zone opportunity share",
}


def _metric_text(metric: str, v: float | None) -> str:
    if v is None:
        return f"{METRIC_LABELS[metric]} n/a"
    return f"{METRIC_LABELS[metric]} {v:.2f}" if metric == "wopr" else f"{METRIC_LABELS[metric]} {_pct_text(v)}"


def _ev(p_id: str, rows: Sequence[Mapping], metric: str, weeks: Sequence[int], text_fn) -> Evidence:
    v = derive_metric(rows, metric, weeks)
    return Evidence(p_id, metric, tuple(weeks), None if v is None else round(v, 4), text_fn(v))


def value_curve(rank: int | None, position: str) -> float:
    """0-100 from a position rank: 100 for rank 1, ~33 just outside the startable pool, 0 at 2x."""
    if rank is None:
        return 0.0
    x = min(2.0, (rank - 1) / STARTERS[position])
    return round(100.0 * (1.0 - x / 2.0) ** VALUE_CURVE, 1)


def _ranks(scores: dict[str, float]) -> dict[str, int]:
    ordered = sorted(scores, key=lambda g: (-scores[g], g))
    return {g: i for i, g in enumerate(ordered, start=1)}


# ------------------------------------------------------------------------- loading --

def load_market_inputs(con, season: int) -> MarketInputs:
    """Read the nflverse-derived tables. No ESPN or league tables are touched."""
    usage = fetch_dicts(con, """
        SELECT u.*, coalesce(tw.team_rz_carries, 0) + coalesce(tw.team_rz_targets, 0) AS team_rz_plays
        FROM player_week_usage u
        LEFT JOIN nfl_team_week tw ON tw.season = u.season AND tw.week = u.week AND tw.team = u.team
        WHERE u.season = ? AND u.position IN ('RB', 'WR', 'TE')""", [season])
    through = max((r["week"] for r in usage), default=0)
    # Reports for the last played week and the current scoring week, one row per player-week with the
    # contract's conservative tie-break (Out > Doubtful > Questionable). Only the current week's report
    # decides availability (docs/nfl_metrics_verified_contract.md section 8); last week's is risk context.
    injuries = fetch_dicts(con, """
        SELECT gsis_id, team, position, week, report_status, report_primary_injury
        FROM nfl_injuries WHERE season = ? AND week IN (?, ?) AND gsis_id IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY gsis_id, week ORDER BY CASE report_status
            WHEN 'Out' THEN 0 WHEN 'Doubtful' THEN 1 WHEN 'Questionable' THEN 2 ELSE 3 END) = 1""",
        [season, through, through + 1]) if table_exists(con, "nfl_injuries") else []
    roster_status = []
    if table_exists(con, "nfl_rosters_weekly"):
        roster_status = fetch_dicts(con, """
            SELECT gsis_id, team, week, status FROM nfl_rosters_weekly
            WHERE season = ? AND gsis_id IS NOT NULL AND week = (
                SELECT max(week) FROM nfl_rosters_weekly WHERE season = ? AND week <= ?)""",
            [season, season, through + 1])
    return MarketInputs(
        season=season,
        usage=usage,
        team_week=fetch_dicts(con, "SELECT * FROM nfl_team_week WHERE season = ?", [season]),
        game_lines=fetch_dicts(con, "SELECT * FROM team_game_lines WHERE season = ?", [season]),
        defense=[{**d, "ranks": json.loads(d["ranks"]) if isinstance(d["ranks"], str) else d["ranks"]}
                 for d in fetch_dicts(con, "SELECT * FROM defense_context WHERE season = ?", [season])],
        injuries=injuries,
        roster_status=roster_status,
        names={r["gsis_id"]: r["display_name"] for r in fetch_dicts(
            con, "SELECT gsis_id, display_name FROM nfl_players WHERE gsis_id IS NOT NULL")},
    )


# ------------------------------------------------------------------------- scoring --

def _team_context(inputs: MarketInputs, through: int) -> dict[str, dict]:
    """Per team: season implied total and neutral pass rate, as 0-100 percentiles over 32 teams."""
    implied: dict[str, list[float]] = defaultdict(list)
    for g in inputs.game_lines:
        if g.get("implied_total") is not None and g["week"] <= through + 1:
            implied[g["team"]].append(g["implied_total"])
    npr: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for t in inputs.team_week:
        if t.get("neutral_pass_rate") is not None and t.get("neutral_plays"):
            npr[t["team"]].append((t["neutral_pass_rate"], t["neutral_plays"]))
    imp = {t: mean(v) for t, v in implied.items()}
    rate = {t: sum(r * n for r, n in v) / sum(n for _, n in v) for t, v in npr.items()}
    imp_pct, rate_pct = percentile_scores(imp), percentile_scores(rate)
    return {t: {"implied_total": imp.get(t), "neutral_pass_rate": rate.get(t),
                "implied_pct": 10 * imp_pct[t] if t in imp_pct else 50.0,
                "pass_rate_pct": 10 * rate_pct[t] if t in rate_pct else 50.0}
            for t in set(imp) | set(rate)}


def _schedule(inputs: MarketInputs, team: str, position: str, through: int) -> tuple[float, list[dict]]:
    """Mean defensive generosity of the next SCHEDULE_WEEKS opponents (context.matchup_label ranks)."""
    defense = {d["defteam"]: d for d in inputs.defense}
    games = {g["week"]: g for g in inputs.game_lines if g["team"] == team}
    out, ranks = [], []
    for w in range(through + 1, through + 1 + SCHEDULE_WEEKS):
        g = games.get(w)
        label, detail = matchup_label(position, defense.get(g["opponent"]) if g else None, on_bye=g is None)
        out.append({"week": w, "opponent": g["opponent"] if g else None, "label": label,
                    "avg_rank": detail.get("avg_rank")})
        if detail.get("avg_rank") is not None:
            ranks.append(detail["avg_rank"])
    score = 100.0 * (32 - mean(ranks)) / 31 if ranks else 50.0
    return round(score, 1), out


def _status(inputs: MarketInputs, current_week: int) -> dict[str, str]:
    """Reserve-list status, overridden by a current-week Out/Doubtful designation; other current-week
    designations kept as-is. Reports without a week are treated as current."""
    st = {r["gsis_id"]: r["status"] for r in inputs.roster_status if r["status"] in UNAVAILABLE_ROSTER}
    for r in inputs.injuries:
        if r.get("week", current_week) != current_week:
            continue
        if r["report_status"] and (r["gsis_id"] not in st or r["report_status"] in UNAVAILABLE_STATUSES):
            st[r["gsis_id"]] = r["report_status"]
    return st


def full_games(rows: Sequence[Mapping]) -> list[Mapping]:
    """Drop partial games (left early, limited return): snap share under PARTIAL_GAME of the player's
    median when that median is a real role. One in-game injury should not read as a lost role."""
    pcts = sorted(r["offense_pct"] for r in rows if r.get("offense_pct") is not None)
    if not pcts:
        return list(rows)
    med = pcts[len(pcts) // 2] if len(pcts) % 2 else 0.5 * (pcts[len(pcts) // 2 - 1] + pcts[len(pcts) // 2])
    if med < 0.4:
        return list(rows)
    return [r for r in rows if r.get("offense_pct") is None or r["offense_pct"] >= PARTIAL_GAME * med]


def _blend(rows: Sequence[Mapping], metric: str, weeks: Sequence[int], recent: Sequence[int]) -> float | None:
    s, rc = derive_metric(rows, metric, weeks), derive_metric(rows, metric, recent)
    if s is None and rc is None:
        return None
    s = rc if s is None else s
    rc = s if rc is None else rc
    return RECENT_WEIGHT * rc + (1 - RECENT_WEIGHT) * s


def _benchmarks(values: dict[str, float], n: int) -> float | None:
    """Mean of the top-n values: what a typical startable player at the position looks like."""
    top = sorted(values.values(), reverse=True)[:n]
    return mean(top) if top and mean(top) > 0 else None


def build_market(inputs: MarketInputs, adp_ranks: Mapping[str, int] | None = None) -> dict[str, PlayerMarket]:
    """Score every RB/WR/TE with games this season. `adp_ranks` (gsis -> position ADP rank), when
    given, replaces the box-score half of market_value with a public price.

    Role and production are indexed to the position's startable benchmark (mean of the top
    STARTERS[pos] players; 100 = a typical starter), so elite roles stay separated from mid ones.
    Position percentiles are kept as evidence for narration."""
    by_player: dict[str, list[dict]] = defaultdict(list)
    for r in inputs.usage:
        if played(r) and r.get("position") in POSITIONS:
            by_player[r["gsis_id"]].append(r)
    if not by_player:
        return {}
    for rows in by_player.values():
        rows.sort(key=lambda r: r["week"])
    through = max(r["week"] for rows in by_player.values() for r in rows)
    teams = _team_context(inputs, through)
    status = _status(inputs, through + 1)

    raw: dict[str, dict] = {}
    for g, rows in by_player.items():
        pos = rows[-1]["position"]
        full = full_games(rows)
        role_weeks = [r["week"] for r in full]
        recent = role_weeks[-RECENT_GAMES:]
        weeks = [r["week"] for r in rows]
        raw[g] = {
            "rows": rows, "full": full, "pos": pos, "weeks": weeks, "role_weeks": role_weeks, "recent": recent,
            "role": {m: _blend(rows, m, role_weeks, recent) for m in ROLE_WEIGHTS[pos]},
            "prod": {m: _blend(rows, m, weeks, weeks[-RECENT_GAMES:]) for m in PRODUCTION_BLEND},
        }

    role_pct: dict[str, dict[str, float]] = defaultdict(dict)
    role_idx: dict[str, dict[str, float]] = defaultdict(dict)
    prod_idx: dict[str, dict[str, float]] = defaultdict(dict)
    for pos in POSITIONS:
        pool = {g: r for g, r in raw.items() if r["pos"] == pos and len(r["full"]) >= MIN_GAMES_POOL}
        for m in ROLE_WEIGHTS[pos]:
            vals = {g: r["role"][m] for g, r in pool.items() if r["role"][m] is not None}
            for g, p in percentile_scores(vals).items():
                role_pct[g][m] = 10 * p
            bench = _benchmarks(vals, STARTERS[pos])
            for g, r in raw.items():
                if r["pos"] == pos and r["role"][m] is not None and bench:
                    role_idx[g][m] = min(INDEX_CAP, r["role"][m] / bench)
        for m in PRODUCTION_BLEND:
            bench = _benchmarks({g: r["prod"][m] for g, r in pool.items() if r["prod"][m] is not None}, STARTERS[pos])
            for g, r in raw.items():
                if r["pos"] == pos and r["prod"][m] is not None and bench:
                    prod_idx[g][m] = min(INDEX_CAP, r["prod"][m] / bench)

    out: dict[str, PlayerMarket] = {}
    for g, r in raw.items():
        pos, rows, full = r["pos"], r["rows"], r["full"]
        team = rows[-1]["team"]
        weights = ROLE_WEIGHTS[pos]
        role_score = 100.0 * sum(w * role_idx[g].get(m, 0.0) for m, w in weights.items())
        trends = [compute_trend(m, [(x["week"], x.get(m)) for x in full], t)
                  for m, t in TREND_METRICS[pos] if m != "xfp"]       # xFP is points-denominated
        trend_score, trend_label = composite_trend(trends)
        tc = teams.get(team, {"implied_pct": 50.0, "pass_rate_pct": 50.0})
        context = tc["implied_pct"] if pos == "RB" else 0.5 * tc["implied_pct"] + 0.5 * tc["pass_rate_pct"]
        schedule_score, schedule = _schedule(inputs, team, pos, through)
        usage = role_score * (1 + ADJUST["trend"] * (trend_score or 0.0)) \
            * (1 + ADJUST["context"] * (context - 50) / 50) * (1 + ADJUST["schedule"] * (schedule_score - 50) / 50)
        production = 100.0 * sum(w * prod_idx[g].get(m, 0.0) for m, w in PRODUCTION_BLEND.items())
        st = status.get(g)
        out[g] = PlayerMarket(
            gsis_id=g, name=inputs.names.get(g, g), position=pos, team=team, games=len(rows),
            weeks=tuple(r["weeks"]), recent_weeks=tuple(r["recent"]),
            role={m: (round(v, 4) if v is not None else None) for m, v in r["role"].items()},
            role_pct={m: role_pct[g].get(m) for m in weights}, role_score=round(role_score, 1),
            trend_score=round(trend_score, 3) if trend_score is not None else None, trend_label=trend_label,
            context_score=round(context, 1), schedule_score=schedule_score, schedule=schedule,
            production={m: round(v, 3) for m, v in r["prod"].items()}, production_score=round(production, 1),
            usage_score=round(usage, 1), status=st,
            available=st not in UNAVAILABLE_STATUSES and st not in UNAVAILABLE_ROSTER,
            partial_weeks=tuple(w for w in r["weeks"] if w not in set(r["role_weeks"])),
        )

    _value(out, adp_ranks)
    _flags(out, by_player, inputs, status)
    return out


def _value(players: dict[str, PlayerMarket], adp_ranks: Mapping[str, int] | None) -> None:
    for pos in POSITIONS:
        pool = {g: p for g, p in players.items() if p.position == pos and p.games >= MIN_GAMES_POOL}
        u_rank = _ranks({g: p.usage_score for g, p in pool.items()})
        if adp_ranks:
            perceived = {g: -float(adp_ranks[g]) for g in pool if g in adp_ranks}
            perceived.update({g: -10_000.0 - (1000 - pool[g].usage_score) for g in pool if g not in adp_ranks})
        else:
            perceived = {g: 0.5 * p.usage_score + 0.5 * p.production_score for g, p in pool.items()}
        m_rank = _ranks(perceived)
        for g, p in pool.items():
            k = 1.0 if p.available else INJURED_DISCOUNT
            p.usage_rank, p.market_rank = u_rank[g], m_rank[g]
            p.usage_value = round(k * value_curve(u_rank[g], pos), 1)
            p.market_value = round(k * value_curve(m_rank[g], pos), 1)


def _flags(players: dict[str, PlayerMarket], rows_by: dict[str, list[dict]], inputs: MarketInputs,
           status: dict[str, str]) -> None:
    """Regression flags, teammate context and the evidence each candidate cites."""
    # Position conversion rates from the same data (no hard-coded TD rates).
    conv: dict[str, float] = {}
    for pos in POSITIONS:
        rs = [r for g, p in players.items() if p.position == pos for r in rows_by[g]]
        rz = sum(_num(r.get("rz_carries")) + _num(r.get("rz_targets")) for r in rs)
        tds = sum(_num(r.get("rushing_tds")) + _num(r.get("receiving_tds")) for r in rs)
        conv[pos] = tds / rz if rz else 0.0

    pct_cache: dict[tuple[str, str], dict[str, float]] = {}

    def pct(pos: str, metric: str) -> dict[str, float]:
        key = (pos, metric)
        if key not in pct_cache:
            vals = {g: derive_metric(rows_by[g], metric) for g, p in players.items()
                    if p.position == pos and p.games >= MIN_GAMES_CLASSIFY}
            pct_cache[key] = {g: 10 * v for g, v in percentile_scores(
                {g: v for g, v in vals.items() if v is not None}).items()}
        return pct_cache[key]

    through = max(w for p in players.values() for w in p.weeks)
    team_played = {(t["team"], t["week"]) for t in inputs.team_week}
    last_report = {r["gsis_id"]: r["report_status"] for r in inputs.injuries if r.get("week") == through}

    # Teammates whose role is vacated this week (Out/Doubtful/reserve list).
    by_team_pos: dict[tuple[str, str], list[PlayerMarket]] = defaultdict(list)
    for p in players.values():
        by_team_pos[(p.team, p.position)].append(p)

    for g, p in players.items():
        rows, weeks, pos = rows_by[g], list(p.weeks), p.position
        say = lambda m, fn: _ev(g, rows, m, weeks, fn)  # noqa: E731
        for m in ROLE_WEIGHTS[pos]:
            if p.role.get(m) is not None:
                p.evidence.append(_ev(g, rows, m, list(p.recent_weeks),
                                      lambda v, m=m: f"{_metric_text(m, v)} over the last {len(p.recent_weeks)} games"))
        p.evidence.append(say("touchdowns", lambda v: f"{_plural(v, 'touchdown')} in {len(weeks)} games"))
        p.evidence.append(say("scrimmage_yards", lambda v: f"{v:.0f} scrimmage yards in {len(weeks)} games"))
        rz = derive_metric(rows, "rz_opportunities") or 0.0
        tds = derive_metric(rows, "touchdowns") or 0.0
        p.totals = {"touchdowns": tds, "scrimmage_yards": derive_metric(rows, "scrimmage_yards") or 0.0,
                    "yards_pg": derive_metric(rows, "yards_pg") or 0.0}
        p.evidence.append(say("yards_pg", lambda v: f"{v:.0f} scrimmage yards per game"))
        expected_tds = rz * conv[pos]
        p.evidence.append(say("rz_opportunities", lambda v: f"{_plural(v, 'red-zone opportunity', 'red-zone opportunities')}"))
        rz_pct = pct(pos, "rz_opp_share").get(g)
        if p.games >= MIN_GAMES_CLASSIFY:
            if tds >= 3 and tds >= expected_tds + 2 and (rz_pct is None or rz_pct < 60):
                p.flags.append("TD_HEAVY")
            if rz_pct is not None and rz_pct >= 70 and expected_tds >= 2 and tds <= expected_tds / 2:
                p.flags.append("TD_LIGHT")
            if pos == "RB":
                ex = pct(pos, "explosive_run_rate").get(g)
                bf = p.role_pct.get("backfield_rush_share")
                if ex is not None and ex >= 85 and (bf or 0) < 50:
                    p.flags.append("EXPLOSIVE_DEPENDENT")
                    p.evidence.append(say("explosive_run_rate", lambda v: f"{_pct_text(v)} of designed runs went 10+ yards"))
            else:
                ypt = pct(pos, "yards_per_target").get(g)
                ts = p.role_pct.get("target_share")
                if ypt is not None and ypt >= 85 and (ts or 0) < 50:
                    p.flags.append("EXPLOSIVE_DEPENDENT")
                    p.evidence.append(say("yards_per_target", lambda v: f"{v:.1f} yards per target"))
        if p.trend_label in ("UP", "DOWN"):
            p.flags.append(f"ROLE_{p.trend_label}")
        if p.partial_weeks and p.partial_weeks[-1] == p.weeks[-1]:
            p.flags.append("LEFT_LAST_GAME_EARLY")
            p.evidence.append(_ev(g, rows, "offense_pct", [p.weeks[-1]],
                                  lambda v, w=p.weeks[-1]: f"played {_pct_text(v)} of snaps in week {w}"))
        if p.weeks[-1] < through and (p.team, through) in team_played:
            p.flags.append("MISSED_LAST_GAME")
        if last_report.get(g) in UNAVAILABLE_STATUSES:
            p.flags.append("LISTED_OUT_LAST_WEEK")
        if p.status:
            p.flags.append(f"STATUS_{p.status.upper()}")
        metric, threshold = VACATED_SHARE[pos]
        for mate in by_team_pos[(p.team, pos)]:
            if mate.gsis_id == g or status.get(mate.gsis_id) is None or mate.available:
                continue
            share = derive_metric(rows_by[mate.gsis_id], metric, list(mate.recent_weeks))
            if share is not None and share >= threshold:
                p.vacated.append({"gsis_id": mate.gsis_id, "name": mate.name, "status": status[mate.gsis_id],
                                  "metric": metric, "share": round(share, 4)})
                p.evidence.append(_ev(mate.gsis_id, rows_by[mate.gsis_id], metric, list(mate.recent_weeks),
                                      lambda v, n=mate.name, s=status[mate.gsis_id]:
                                      f"teammate {n} ({_metric_text(metric, v)}) is listed {s}"))
        if p.vacated:
            p.flags.append("VACATED_ROLE")
    for p in players.values():
        p.flags = sorted(set(p.flags))


# ---------------------------------------------------------------------- candidates --

def buy_low_score(p: PlayerMarket) -> float | None:
    """A startable role the market prices lower than we do. None = not a candidate."""
    if (p.games < MIN_GAMES_CLASSIFY or not p.available or p.gap < GAP_MIN or p.usage_rank is None
            or p.usage_rank > STARTERS[p.position] or p.market_value <= 0
            or ("LEFT_LAST_GAME_EARLY" in p.flags and p.status is None)):   # health unknown until a report
        return None
    s = p.gap + (8 if "ROLE_UP" in p.flags else 0) + (5 if "TD_LIGHT" in p.flags else 0) \
        + (4 if "VACATED_ROLE" in p.flags else 0) - (8 if "ROLE_DOWN" in p.flags else 0) \
        + 0.05 * (p.schedule_score - 50)
    return round(s, 1)


def sell_high_score(p: PlayerMarket) -> float | None:
    """A box score the market pays for that the role doesn't support. Elite roles are never sells
    (same policy as player_scores: a high-volume role with hot production is a hold)."""
    if (p.games < MIN_GAMES_CLASSIFY or not p.available or -p.gap < GAP_MIN or p.usage_rank is None
            or p.usage_rank <= ELITE_SHARE * STARTERS[p.position] or p.market_rank is None
            or p.market_rank > STARTERS[p.position] or "ROLE_UP" in p.flags):
        return None
    s = -p.gap + (6 if "TD_HEAVY" in p.flags else 0) + (5 if "EXPLOSIVE_DEPENDENT" in p.flags else 0) \
        + (6 if "ROLE_DOWN" in p.flags else 0) \
        - 0.05 * (p.schedule_score - 50)
    return round(s, 1)


def candidates(players: Mapping[str, PlayerMarket], kind: str, limit: int = 10,
               position: str | None = None) -> list[dict]:
    """Ranked buy-low (`kind="buy"`) or sell-high (`kind="sell"`) candidates with their evidence."""
    fn = buy_low_score if kind == "buy" else sell_high_score
    rows = []
    for p in players.values():
        if position and p.position != position:
            continue
        s = fn(p)
        if s is not None:
            rows.append((s, p))
    rows.sort(key=lambda x: (-x[0], x[1].gsis_id))
    return [{"score": s, **summary(p)} for s, p in rows[:limit]]


def summary(p: PlayerMarket) -> dict:
    return {
        "gsis_id": p.gsis_id, "name": p.name, "position": p.position, "team": p.team, "games": p.games,
        "usage_value": p.usage_value, "market_value": p.market_value, "gap": p.gap,
        "usage_rank": p.usage_rank, "market_rank": p.market_rank,
        "role_score": p.role_score, "production_score": p.production_score, "trend": p.trend_label,
        "context_score": p.context_score, "schedule_score": p.schedule_score, "schedule": p.schedule,
        "status": p.status, "flags": p.flags, "vacated": p.vacated,
        "evidence": [e.as_dict() for e in p.evidence],
    }


# -------------------------------------------------------------------------- offers --

def package_value(values: Sequence[float]) -> float:
    """Best piece in full, every further piece at DEPTH_DISCOUNT (one lineup spot, one roster spot)."""
    vs = sorted(values, reverse=True)
    return vs[0] + DEPTH_DISCOUNT * sum(vs[1:]) if vs else 0.0


def fairness(ratio: float) -> str | None:
    """Label for r = market(other manager receives) / market(other manager gives). None = excluded."""
    if ratio < LOWBALL or ratio > HIGH:
        return None
    if ratio < OPENING:
        return FAIRNESS_LABELS["opening"]
    if ratio < FAIR:
        return FAIRNESS_LABELS["fair"]
    return FAIRNESS_LABELS["high"]


@dataclass
class Offer:
    shape: str                  # "1-for-1" | "2-for-1"
    you_give: list[str]
    you_get: list[str]
    fairness: str
    market_ratio: float         # internal pricing ratio; not an acceptance probability, not for display
    position_check: str
    balance_check: str
    incentive: list[str]
    risks: list[str]
    evidence: list[dict]

    def as_dict(self) -> dict:
        return asdict(self)


def _piece_ok(p: PlayerMarket, exclude: set[str]) -> bool:
    return p.available and p.games >= MIN_GAMES_CLASSIFY and p.gsis_id not in exclude and p.market_value > 0


def _position_check(pieces: Sequence[PlayerMarket], they_lose: PlayerMarket) -> str:
    """The other manager should get back someone who can fill the lineup spot they give up."""
    if any(x.position == they_lose.position for x in pieces):
        return f"ok: they get a {they_lose.position} back for the {they_lose.position} they give up"
    if they_lose.position == "TE":
        return "needs TE depth: they give up a TE without getting one back"
    return f"flex swap: they give up a {they_lose.position} and get {'/'.join(sorted({x.position for x in pieces}))} back"


def _shared_risks(players: Sequence[PlayerMarket], incoming: set[str]) -> list[str]:
    risks = []
    for p in players:
        side = "you get" if p.gsis_id in incoming else "you give"
        if p.games < 4:
            risks.append(f"Small sample: {p.name} ({side}) has {p.games} games.")
        if p.gsis_id in incoming:
            if "TD_HEAVY" in p.flags:
                risks.append(f"{p.name}'s touchdowns are running ahead of his red-zone role.")
            if "EXPLOSIVE_DEPENDENT" in p.flags:
                risks.append(f"{p.name}'s production leans on big plays more than volume.")
            if "ROLE_DOWN" in p.flags:
                risks.append(f"{p.name}'s role has trended down.")
            if p.status:
                risks.append(f"{p.name} is listed {p.status} on the latest report.")
            if "LEFT_LAST_GAME_EARLY" in p.flags:
                risks.append(f"{p.name} played a partial game in week {p.weeks[-1]}.")
            if "MISSED_LAST_GAME" in p.flags:
                risks.append(f"{p.name} did not play in his team's most recent game.")
            if "LISTED_OUT_LAST_WEEK" in p.flags and not p.status:
                risks.append(f"{p.name} was listed Out last week.")
            if p.partial_weeks and "LEFT_LAST_GAME_EARLY" not in p.flags:
                risks.append(f"{p.name} played partial games in week(s) "
                             f"{', '.join(str(w) for w in p.partial_weeks)}.")
            if p.vacated:
                names = ", ".join(v["name"] for v in p.vacated)
                risks.append(f"Part of {p.name}'s role comes from {names} being out; it can shrink on their return.")
            tough = [s for s in p.schedule if s["label"] == "TOUGH"]
            if len(tough) >= 2:
                risks.append(f"{p.name} faces {len(tough)} tough matchups in the next {SCHEDULE_WEEKS} weeks.")
    return risks


def _incentive(they_get: Sequence[PlayerMarket], they_give: Sequence[PlayerMarket], ratio: float) -> list[str]:
    """Why the other manager says yes, in box-score terms they can see (season totals)."""
    lines = []
    got_tds, gave_tds = (sum(p.totals["touchdowns"] for p in side) for side in (they_get, they_give))
    got_ypg, gave_ypg = (sum(p.totals["yards_pg"] for p in side) for side in (they_get, they_give))
    if got_tds > gave_tds:
        lines.append(f"They get more touchdowns so far ({got_tds:.0f} vs {gave_tds:.0f}).")
    if got_ypg > gave_ypg:
        lines.append(f"They get more scrimmage yards per game ({got_ypg:.0f} vs {gave_ypg:.0f}).")
    for p in they_give:
        if "ROLE_DOWN" in p.flags:
            lines.append(f"{p.name}'s role has dipped, so they may be ready to move him.")
        if "TD_LIGHT" in p.flags:
            lines.append(f"{p.name} hasn't turned his red-zone work into touchdowns, so his box score looks "
                         "worse than his role.")
    if len(they_get) > len(they_give):
        lines.append("They turn one roster spot into two playable pieces.")
    if len(they_give) > len(they_get):
        lines.append("They consolidate two pieces into one better player and free a roster spot.")
    if lines:
        return lines
    return ["On box-score value they come out even or ahead." if ratio >= OPENING else
            "This is an opening ask that leans your way: expect a counter."]


def generate_offer(players: Mapping[str, PlayerMarket], target_id: str, mode: str = "buy",
                   max_alternatives: int = 2) -> dict:
    """Offers for acquiring (`mode="buy"`) or selling (`mode="sell"`) `target_id`.

    buy:  you get the target and give pieces the market rates at least as highly as we do.
    sell: you give the target and get pieces whose role (usage_value) beats it.
    Every offer must (1) land in a fairness band from the other manager's side, (2) improve your
    side in usage_value, and for 2-for-1s (3) be anchored by a piece worth >= BALANCED_RATIO of the
    target and (4) contain no piece below SIGNIFICANT_RATIO of it (no bench scraps for a star).
    Returns {"target", "mode", "opening": Offer | None, "alternatives": [Offer], "notes": [...]}.
    """
    if mode not in ("buy", "sell"):
        raise ValueError("mode must be 'buy' or 'sell'")
    t = players[target_id]
    eligible = [p for p in players.values() if _piece_ok(p, {target_id}) and p.market_rank is not None]
    if mode == "sell":
        # Reputation guard: the box-score proxy underrates stars in a quiet stretch, but no manager
        # sells an elite role at that price. Elite-usage players are never a "return" for a sell.
        eligible = [p for p in eligible if p.usage_rank is None or p.usage_rank > ELITE_SHARE * STARTERS[p.position]]
    # Buy: hand over what the market overrates. Sell: take back what it underrates.
    lean = (lambda p: p.market_value - p.usage_value) if mode == "buy" else (lambda p: p.usage_value - p.market_value)

    def build(pieces: Sequence[PlayerMarket]) -> Offer | None:
        pk_market = package_value([p.market_value for p in pieces])
        pk_usage = package_value([p.usage_value for p in pieces])
        if mode == "buy":       # they receive the pieces and give the target
            ratio = pk_market / t.market_value if t.market_value else 0.0
            we_win = t.usage_value > pk_usage
            give, get, they_get, they_give = list(pieces), [t], list(pieces), [t]
            they_lose = t
        else:                   # they receive the target and give the pieces
            ratio = t.market_value / pk_market if pk_market else 0.0
            we_win = pk_usage > t.usage_value
            give, get, they_get, they_give = [t], list(pieces), [t], list(pieces)
            they_lose = max(pieces, key=lambda p: (p.market_value, p.gsis_id))
        label = fairness(ratio)
        if label is None or not we_win:
            return None
        if len(pieces) > 1:
            values = [p.market_value for p in pieces]
            if max(values) < BALANCED_RATIO * t.market_value or min(values) < SIGNIFICANT_RATIO * t.market_value:
                return None
        balance = (f"you open a roster spot ({len(give)} out, {len(get)} in)" if len(give) > len(get) else
                   f"you need an open roster spot ({len(give)} out, {len(get)} in)" if len(get) > len(give) else
                   "even roster spots")
        everyone = give + get
        return Offer(
            shape=f"{max(len(give), len(get))}-for-{min(len(give), len(get))}",
            you_give=[p.gsis_id for p in give], you_get=[p.gsis_id for p in get],
            fairness=label, market_ratio=round(ratio, 3),
            position_check=_position_check(they_get, they_lose),
            balance_check=balance,
            incentive=_incentive(they_get, they_give, ratio),
            risks=_shared_risks(everyone, {p.gsis_id for p in get}),
            evidence=[e.as_dict() for p in everyone for e in p.evidence],
        )

    near = sorted(eligible, key=lambda p: (abs(p.market_value - t.market_value), p.gsis_id))[:POOL_SIZE]
    singles = []
    for p in near:
        o = build([p])
        if o:
            singles.append(((o.fairness == FAIRNESS_LABELS["high"], not o.position_check.startswith("ok"),
                             o.fairness != FAIRNESS_LABELS["opening"], -lean(p), p.gsis_id), o))
    singles.sort(key=lambda x: x[0])
    opening = singles[0][1] if singles else None
    used = (set(opening.you_give + opening.you_get) - {target_id}) if opening else set()

    smaller = sorted((p for p in eligible if p.market_value < t.market_value and p.gsis_id not in used),
                     key=lambda p: (-lean(p), p.gsis_id))[:POOL_SIZE] if t.market_value >= MIN_2FOR1_MARKET else []
    doubles = []
    for a, b in combinations(smaller, 2):
        o = build([a, b])
        if o:
            doubles.append(((not o.position_check.startswith("ok"), o.fairness != FAIRNESS_LABELS["fair"],
                             -(lean(a) + lean(b)), a.gsis_id, b.gsis_id), o))
    doubles.sort(key=lambda x: x[0])
    alternatives, seen = [], set()
    for _, o in doubles:
        ids = set(o.you_give + o.you_get) - {target_id}
        if not ids & seen:
            alternatives.append(o)
            seen |= ids
        if len(alternatives) >= max_alternatives:
            break

    notes = []
    if opening is None:
        notes.append("No 1-for-1 fits the fairness bands while still improving your side on usage.")
    if not alternatives:
        notes.append("No 2-for-1 passes the balance guards." if t.market_value >= MIN_2FOR1_MARKET else
                     "Target is priced too low for a 2-for-1 to make sense.")
    return {"target": summary(t), "mode": mode,
            "opening": opening.as_dict() if opening else None,
            "alternatives": [o.as_dict() for o in alternatives], "notes": notes}


def sleeper_market_ranks(sleeper_players: Mapping[str, Mapping], sleeper_to_gsis: Mapping[str, str]) -> dict[str, int]:
    """Position ranks (gsis -> rank) from Sleeper's public /players/nfl `search_rank`, Sleeper's default
    player ordering. It is a free public market signal, not true ADP; pass the result to
    build_market(adp_ranks=...) to replace the box-score half of market_value. Unranked players
    (Sleeper uses a huge sentinel) are left out."""
    by_pos: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for sid, p in sleeper_players.items():
        rank, pos, gsis = p.get("search_rank"), p.get("position"), sleeper_to_gsis.get(str(sid))
        if gsis and pos in POSITIONS and isinstance(rank, int) and 0 < rank < 9_999_999:
            by_pos[pos].append((rank, gsis))
    out = {}
    for rows in by_pos.values():
        for i, (_, gsis) in enumerate(sorted(rows), start=1):
            out[gsis] = i
    return out


def sleeper_id_map(con) -> dict[str, str]:
    return {str(r["sleeper_id"]): r["gsis_id"] for r in fetch_dicts(
        con, "SELECT sleeper_id, gsis_id FROM nfl_players WHERE sleeper_id IS NOT NULL AND gsis_id IS NOT NULL")}


def build_trade_market(con, season: int, adp_ranks: Mapping[str, int] | None = None) -> dict[str, PlayerMarket]:
    return build_market(load_market_inputs(con, season), adp_ranks)
