"""Research only: a deterministic Breakout Radar (issue #212, v1) and its walk-forward evaluation.

Nothing in the app imports this module (tests/test_breakout_radar.py); only scripts/breakout_radar_research.py
does. It reads `player_week_usage` rows (one per skill player per game played; app/analytics/usage.py) and changes
no forecast, table or artifact. See docs/research/breakout_radar.md.

The question: when a player's opportunity has risen over his last games, is it a lasting change in role or noise?

Opportunity metric: `xfp`, expected PPR points from the player's realized opportunity (nflverse ff_opportunity),
per game played. A game he did not play has no row: it is never a zero. A row whose xfp is null stays unknown.

State of a player as of week W of a season, from his games in weeks <= W of that season only:
  base(run)    mean xfp of up to BASE_MAX games before the most recent `run` games (at least BASE_MIN of them)
  up(b)        b + max(ABS_UP, REL_UP * b);  down(b) = b - max(ABS_DOWN, REL_DOWN * b)
  k            the largest run of most-recent games each with xfp >= up(base(k)), base(k) taken before that run
  emerging     k == 2: two straight games well above his earlier level
  sustained    k >= 3
  fading       not rising, and his last RECENT games each with xfp <= down(base(RECENT))
  steady       none of the above (a single-game spike, k == 1, is steady: one game is not a role change)
  insufficient too few games, last game older than week W-1, a missing xfp or team, or a team or listed position
               that changes inside the window

Thresholds are fixed here before any outcome was looked at; nothing is tuned on outcomes.

Evaluation (walk-forward): for every week W, every eligible player is labelled from his games in weeks W+1..W+H
only (H in HORIZONS). Elevated: the mean xfp of those games >= up(B), where B = base(RECENT) at W, the level before
his last two games, for every player alike. Fell: <= down(B). Fewer than ceil(H/2) future games (at H = 2: none of
the two) or a missing value: unlabelled, counted per state, never a negative. For a "sustained" player B already
holds part of his run, so his label is a harder bar than an "emerging" player's; sustained rates are reported, not
compared.

The radar's "emerging" flags are compared with naive recent-volume baselines on the same labelled player-weeks: a
one-game spike (last game >= up(B)) and, per (season, week, position), the same number of flags as the radar given to
the largest increases over B, by his last game ("matched volume") or by the mean of his last two games ("matched two
games": the same games the radar reads, so it isolates the radar's rule from having seen two games). The precision
gaps carry 95% paired bootstrap intervals clustered by week and, separately, by player-season.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
import random

BR_VERSION = "breakout-radar-0.1.0"
BR_STATUS = "research_only"
POSITIONS = ("RB", "WR", "TE")
METRIC = "xfp"

ABS_UP, REL_UP = 3.0, 0.5
ABS_DOWN, REL_DOWN = 3.0, 0.33
RECENT = 2
BASE_MIN, BASE_MAX = 2, 4
HORIZONS = (2, 4)
FIRST_WEEK = BASE_MIN + RECENT          # the earliest week that can have a full window
LAST_REGULAR_WEEK = 18                  # 2021 on; postseason rows never enter a window or a label

STATES = ("emerging", "sustained", "fading", "steady", "insufficient")
BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED = 2000, 212


def up(base: float) -> float:
    return base + max(ABS_UP, REL_UP * base)


def down(base: float) -> float:
    return base - max(ABS_DOWN, REL_DOWN * base)


@dataclass(frozen=True)
class Game:
    week: int
    team: str | None
    value: float | None
    position: str | None = None


@dataclass(frozen=True)
class State:
    state: str
    reason: str | None = None
    base: float | None = None           # B: the level before his last RECENT games
    run: int = 0
    last: float | None = None
    recent_mean: float | None = None    # mean of his last RECENT games


def _base(games: list[Game], run: int) -> float | None:
    before = games[:-run][-BASE_MAX:]
    if len(before) < BASE_MIN:
        return None
    return sum(g.value for g in before) / len(before)


def state_at(games: list[Game], week: int) -> State:
    """His state as of `week`, from games with week <= `week` (later rows are dropped here, whatever the caller
    passes: no look-ahead by construction)."""
    g = sorted((x for x in games if x.week <= week), key=lambda x: x.week)
    if len(g) < BASE_MIN + RECENT:
        return State("insufficient", "too_few_games")
    if g[-1].week < week - 1:
        return State("insufficient", "no_recent_game")
    window = g[-(BASE_MAX + RECENT):]
    if any(x.value is None for x in window):
        return State("insufficient", "missing_metric")
    if any(x.team is None for x in window):
        return State("insufficient", "missing_team")
    if len({x.team for x in window}) > 1:
        return State("insufficient", "team_change")
    if len({x.position for x in window}) > 1:
        return State("insufficient", "position_change")     # e.g. listed FB for some games: not one role
    base = _base(window, RECENT)
    # The longest run whose games all clear the bar over the games before it (each k has its own baseline, so a
    # smaller k is not a prefix test: its baseline already holds the raised games).
    run = max((k for k in range(1, len(window) - BASE_MIN + 1)
               if all(x.value >= up(_base(window, k)) for x in window[-k:])), default=0)
    common = {"base": base, "run": run, "last": window[-1].value,
              "recent_mean": sum(x.value for x in window[-RECENT:]) / RECENT}
    if run >= 3:
        return State("sustained", **common)
    if run == 2:
        return State("emerging", **common)
    if all(x.value <= down(base) for x in window[-RECENT:]):
        return State("fading", **common)
    return State("steady", **common)


def label(future: list[Game], base: float, horizon: int) -> str | None:
    """'elevated' / 'fell' / 'neither' from his games in the next `horizon` weeks; None (unlabelled) when he played
    fewer than half of them (at H = 2: neither) or a value is missing."""
    if len(future) < math.ceil(horizon / 2) or any(x.value is None for x in future):
        return None
    mean = sum(x.value for x in future) / len(future)
    if mean >= up(base):
        return "elevated"
    if mean <= down(base):
        return "fell"
    return "neither"


def player_games(rows: list[dict]) -> dict[tuple[int, str], list[Game]]:
    """(season, gsis_id) -> every regular-season game he played, whatever position the row lists (a game at another
    position is a game played, never a gap; a window that mixes positions is insufficient)."""
    out: dict[tuple[int, str], list[Game]] = defaultdict(list)
    for r in rows:
        if r.get("gsis_id") is None or int(r["week"]) > LAST_REGULAR_WEEK:
            continue
        v = r.get(METRIC)
        out[(int(r["season"]), r["gsis_id"])].append(
            Game(int(r["week"]), r.get("team"), None if v is None else float(v), r.get("position")))
    for k in out:
        out[k].sort(key=lambda x: x.week)
    return dict(out)


def position_at(games: list[Game], week: int) -> str | None:
    """His position as of `week`: the one listed for his latest game up to then (never a later row)."""
    seen = [x for x in games if x.week <= week]
    return seen[-1].position if seen else None


def radar(rows: list[dict], season: int, week: int) -> list[dict]:
    """Every player whose position as of `week` is a radar position, with his state as of `week`."""
    out = []
    for (s, pid), games in player_games([r for r in rows if int(r["season"]) == season and int(r["week"]) <= week]).items():
        p = position_at(games, week)
        if p not in POSITIONS:
            continue
        st = state_at(games, week)
        out.append({"season": s, "week": week, "gsis_id": pid, "position": p, "state": st.state,
                    "reason": st.reason, "base": st.base, "run": st.run, "last": st.last})
    return sorted(out, key=lambda r: (r["position"], r["gsis_id"]))


def _rates(flags: list[bool], labels: list[str], positive: str) -> dict:
    tp = sum(1 for f, y in zip(flags, labels) if f and y == positive)
    fp = sum(1 for f, y in zip(flags, labels) if f and y != positive)
    pos = sum(1 for y in labels if y == positive)
    neg = len(labels) - pos
    return {
        "flags": tp + fp, "hits": tp,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / pos if pos else None,
        "false_positive_rate": fp / neg if neg else None,
        "positives": pos, "negatives": neg,
    }


def evaluate(rows: list[dict], horizons: tuple[int, ...] = HORIZONS) -> dict:
    """Walk-forward over every (season, week) whose W+H is still in the season. Per horizon and position (and 'all'):
    player-weeks (every week from his first game on, at a radar position as of that week), eligible, labelled and
    unlabelled (also per state), the base rates, the radar's states and the naive baselines with their gaps."""
    games = player_games(rows)
    last_week = defaultdict(int)
    for (s, _), g in games.items():
        last_week[s] = max(last_week[s], g[-1].week)
    out: dict = {"version": BR_VERSION, "status": BR_STATUS, "metric": METRIC,
                 "thresholds": {"abs_up": ABS_UP, "rel_up": REL_UP, "abs_down": ABS_DOWN, "rel_down": REL_DOWN,
                                "recent": RECENT, "base_min": BASE_MIN, "base_max": BASE_MAX}, "horizons": {}}
    for h in horizons:
        cells: dict[str, dict] = defaultdict(lambda: {"rows": [], "seen": 0, "insufficient": defaultdict(int),
                                                      "unlabelled": defaultdict(int)})
        for (s, pid), g in games.items():
            for w in range(FIRST_WEEK, last_week[s] - h + 1):
                p = position_at(g, w)
                if p not in POSITIONS:
                    continue
                keys = (p, "all")
                st = state_at(g, w)
                for k in keys:
                    cells[k]["seen"] += 1
                if st.state == "insufficient":
                    for k in keys:
                        cells[k]["insufficient"][st.reason] += 1
                    continue
                y = label([x for x in g if w < x.week <= w + h], st.base, h)
                if y is None:
                    for k in keys:
                        cells[k]["unlabelled"][st.state] += 1
                    continue
                row = {"season": s, "week": w, "gsis_id": pid, "position": p, "state": st.state, "label": y,
                       "spike": st.last >= up(st.base), "increase": st.last - st.base,
                       "increase2": st.recent_mean - st.base}
                for k in keys:
                    cells[k]["rows"].append(row)
        out["horizons"][h] = {k: _summary(c) for k, c in sorted(cells.items())}
    return out


def _matched_volume(rows: list[dict], key: str = "increase") -> list[bool]:
    """Per (season, week, position): as many flags as the radar's emerging ones, to the largest `key` increases over B
    (`increase`: his last game; `increase2`: the mean of his last two, the same games the radar reads)."""
    by: dict[tuple, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        by[(r["season"], r["week"], r["position"])].append(i)
    flags = [False] * len(rows)
    for idx in by.values():
        n = sum(1 for i in idx if rows[i]["state"] == "emerging")
        for i in sorted(idx, key=lambda i: (-rows[i][key], i))[:n]:
            flags[i] = True
    return flags


def _summary(c: dict) -> dict:
    rows = c["rows"]
    labels = [r["label"] for r in rows]
    unlabelled = sum(c["unlabelled"].values())
    eligible = len(rows) + unlabelled
    emerging = [r["state"] == "emerging" for r in rows]
    last_game = _matched_volume(rows, "increase")
    two_games = _matched_volume(rows, "increase2")
    return {
        "player_weeks": c["seen"], "eligible": eligible, "labelled": len(rows), "unlabelled": unlabelled,
        "unlabelled_by_state": dict(sorted(c["unlabelled"].items())),
        "insufficient": dict(sorted(c["insufficient"].items())),
        "coverage": eligible / c["seen"] if c["seen"] else None,
        "base_rate_elevated": sum(1 for y in labels if y == "elevated") / len(rows) if rows else None,
        "base_rate_fell": sum(1 for y in labels if y == "fell") / len(rows) if rows else None,
        "states": {s: sum(1 for r in rows if r["state"] == s) for s in STATES if s != "insufficient"},
        "emerging": _rates(emerging, labels, "elevated"),
        "sustained": _rates([r["state"] == "sustained" for r in rows], labels, "elevated"),
        "baseline_spike": _rates([r["spike"] for r in rows], labels, "elevated"),
        "baseline_matched_volume": _rates(last_game, labels, "elevated"),
        "baseline_matched_two_games": _rates(two_games, labels, "elevated"),
        "fading": _rates([r["state"] == "fading" for r in rows], labels, "fell"),
        "emerging_minus_matched_volume": _paired_precision_gap(rows, emerging, last_game),
        "emerging_minus_matched_two_games": _paired_precision_gap(rows, emerging, two_games),
    }


def _percentile(sorted_draws: list[float], q: float) -> float:
    """Linear-interpolated percentile of sorted values (q in 0..1)."""
    pos = q * (len(sorted_draws) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_draws) - 1)
    return sorted_draws[lo] + (sorted_draws[hi] - sorted_draws[lo]) * (pos - lo)


def _paired_precision_gap(rows: list[dict], flags_a: list[bool], flags_b: list[bool]) -> dict:
    """Precision of flags_a minus flags_b on the same player-weeks, with two 95% paired bootstrap intervals: one
    resampling whole (season, week) cuts, one resampling whole player-seasons (the same player recurs in adjacent
    weeks, and at H = 4 adjacent labels share games). Read the wider one."""
    def clusters(key) -> list[list[int]]:
        cs: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0, 0])   # a flags, a hits; b flags, b hits
        for r, a, b in zip(rows, flags_a, flags_b):
            c = cs[key(r)]
            hit = r["label"] == "elevated"
            c[0] += a
            c[1] += a and hit
            c[2] += b
            c[3] += b and hit
        return [cs[k] for k in sorted(cs)]

    def gap(cs) -> float | None:
        af, ah, bf, bh = (sum(c[i] for c in cs) for i in range(4))
        return ah / af - bh / bf if af and bf else None

    out: dict = {"difference": None, "resamples": BOOTSTRAP_RESAMPLES}
    for name, key in (("by_week", lambda r: (r["season"], r["week"])), ("by_player_season", lambda r: (r["season"], r["gsis_id"]))):
        cs = clusters(key)
        point = gap(cs)
        out["difference"] = point
        if point is None:
            out[name] = {"ci95": None, "clusters": len(cs)}
            continue
        rng = random.Random(BOOTSTRAP_SEED)
        draws = sorted(d for d in (gap([cs[rng.randrange(len(cs))] for _ in cs]) for _ in range(BOOTSTRAP_RESAMPLES))
                       if d is not None)
        out[name] = {"ci95": [_percentile(draws, 0.025), _percentile(draws, 0.975)], "clusters": len(cs)}
    return out
