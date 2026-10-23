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
  insufficient too few games, last game older than week W-1, a team change inside the window, or a missing xfp

Thresholds are fixed here before any outcome was looked at; nothing is tuned on outcomes.

Evaluation (walk-forward): for every week W, every eligible player is labelled from his games in weeks W+1..W+H
only (H in HORIZONS). Elevated: the mean xfp of those games >= up(B), where B = base(RECENT) at W, the level before
his last two games, for every player alike. Fell: <= down(B). For a "sustained" player B already holds part of his
run, so his label is a harder bar than an "emerging" player's; sustained rates are reported, not compared. Fewer than ceil(H/2) future games: unlabelled (counted, never a negative).
The radar's "emerging" flags are compared with two naive recent-volume baselines on the same player-weeks: a
one-game spike (last game >= up(B)) and, per (season, week, position), the same number of flags as the radar given
to the players with the largest last-game increase over B.
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


@dataclass(frozen=True)
class State:
    state: str
    reason: str | None = None
    base: float | None = None           # B: the level before his last RECENT games
    run: int = 0
    last: float | None = None


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
    if len({x.team for x in window}) > 1 or any(x.team is None for x in window):
        return State("insufficient", "team_change")
    base = _base(window, RECENT)
    # The longest run whose games all clear the bar over the games before it (each k has its own baseline, so a
    # smaller k is not a prefix test: its baseline already holds the raised games).
    run = max((k for k in range(1, len(window) - BASE_MIN + 1)
               if all(x.value >= up(_base(window, k)) for x in window[-k:])), default=0)
    last = window[-1].value
    if run >= 3:
        return State("sustained", base=base, run=run, last=last)
    if run == 2:
        return State("emerging", base=base, run=run, last=last)
    if all(x.value <= down(base) for x in window[-RECENT:]):
        return State("fading", base=base, run=run, last=last)
    return State("steady", base=base, run=run, last=last)


def label(future: list[Game], base: float, horizon: int) -> str | None:
    """'elevated' / 'fell' / 'neither' from his games in the next `horizon` weeks; None (unlabelled) when he played
    fewer than half of them or a value is missing."""
    if len(future) < math.ceil(horizon / 2) or any(x.value is None for x in future):
        return None
    mean = sum(x.value for x in future) / len(future)
    if mean >= up(base):
        return "elevated"
    if mean <= down(base):
        return "fell"
    return "neither"


def player_games(rows: list[dict]) -> dict[tuple[int, str], list[Game]]:
    """(season, gsis_id) -> games, from player_week_usage-shaped rows of the radar's positions."""
    out: dict[tuple[int, str], list[Game]] = defaultdict(list)
    for r in rows:
        if r.get("position") not in POSITIONS or r.get("gsis_id") is None or int(r["week"]) > LAST_REGULAR_WEEK:
            continue
        v = r.get(METRIC)
        out[(int(r["season"]), r["gsis_id"])].append(Game(int(r["week"]), r.get("team"), None if v is None else float(v)))
    for k in out:
        out[k].sort(key=lambda x: x.week)
    return dict(out)


def radar(rows: list[dict], season: int, week: int) -> list[dict]:
    """Every radar-position player with a game in `season` up to `week`, and his state as of `week`."""
    pos = {(int(r["season"]), r["gsis_id"]): r["position"] for r in rows if r.get("position") in POSITIONS}
    out = []
    for (s, pid), games in player_games([r for r in rows if int(r["season"]) == season and int(r["week"]) <= week]).items():
        st = state_at(games, week)
        out.append({"season": s, "week": week, "gsis_id": pid, "position": pos[(s, pid)], "state": st.state,
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
    """Walk-forward over every (season, week) with a full window and a labelled future. Returns, per horizon and
    position (and 'all'), the base rate, the radar's 'emerging' and 'fading' rates, both naive baselines, coverage
    (eligible share of player-weeks with a game in the window) and N."""
    games = player_games(rows)
    pos = {(int(r["season"]), r["gsis_id"]): r["position"] for r in rows if r.get("position") in POSITIONS}
    last_week = defaultdict(int)
    for (s, _), g in games.items():
        last_week[s] = max(last_week[s], g[-1].week)
    out: dict = {"version": BR_VERSION, "status": BR_STATUS, "metric": METRIC,
                 "thresholds": {"abs_up": ABS_UP, "rel_up": REL_UP, "abs_down": ABS_DOWN, "rel_down": REL_DOWN,
                                "recent": RECENT, "base_min": BASE_MIN, "base_max": BASE_MAX}, "horizons": {}}
    for h in horizons:
        cells: dict[str, dict] = defaultdict(lambda: {"rows": [], "seen": 0, "insufficient": defaultdict(int),
                                                      "unlabelled": 0})
        for (s, pid), g in games.items():
            p = pos[(s, pid)]
            for w in range(FIRST_WEEK, last_week[s] - h + 1):
                if g[0].week > w:
                    continue
                st = state_at(g, w)
                for key in (p, "all"):
                    cells[key]["seen"] += 1
                if st.state == "insufficient":
                    for key in (p, "all"):
                        cells[key]["insufficient"][st.reason] += 1
                    continue
                y = label([x for x in g if w < x.week <= w + h], st.base, h)
                if y is None:
                    for key in (p, "all"):
                        cells[key]["unlabelled"] += 1
                    continue
                row = {"season": s, "week": w, "position": p, "state": st.state, "label": y,
                       "spike": st.last >= up(st.base), "increase": st.last - st.base}
                for key in (p, "all"):
                    cells[key]["rows"].append(row)
        out["horizons"][h] = {k: _summary(c) for k, c in sorted(cells.items())}
    return out


def _matched_volume(rows: list[dict]) -> list[bool]:
    """Per (season, week, position): as many flags as the radar's emerging ones, to the largest last-game increases."""
    by: dict[tuple, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        by[(r["season"], r["week"], r["position"])].append(i)
    flags = [False] * len(rows)
    for idx in by.values():
        n = sum(1 for i in idx if rows[i]["state"] == "emerging")
        for i in sorted(idx, key=lambda i: (-rows[i]["increase"], i))[:n]:
            flags[i] = True
    return flags


def _summary(c: dict) -> dict:
    rows = c["rows"]
    labels = [r["label"] for r in rows]
    eligible = len(rows) + c["unlabelled"]
    return {
        "player_weeks": c["seen"], "eligible": eligible, "labelled": len(rows), "unlabelled": c["unlabelled"],
        "insufficient": dict(sorted(c["insufficient"].items())),
        "coverage": eligible / c["seen"] if c["seen"] else None,
        "base_rate_elevated": sum(1 for y in labels if y == "elevated") / len(rows) if rows else None,
        "base_rate_fell": sum(1 for y in labels if y == "fell") / len(rows) if rows else None,
        "states": {s: sum(1 for r in rows if r["state"] == s) for s in STATES if s != "insufficient"},
        "emerging": _rates([r["state"] == "emerging" for r in rows], labels, "elevated"),
        "sustained": _rates([r["state"] == "sustained" for r in rows], labels, "elevated"),
        "baseline_spike": _rates([r["spike"] for r in rows], labels, "elevated"),
        "baseline_matched_volume": _rates(_matched_volume(rows), labels, "elevated"),
        "fading": _rates([r["state"] == "fading" for r in rows], labels, "fell"),
        "emerging_minus_matched_volume": _paired_precision_gap(rows, _matched_volume(rows)),
    }


def _paired_precision_gap(rows: list[dict], matched: list[bool]) -> dict:
    """Radar 'emerging' precision minus the matched-volume baseline's on the same player-weeks, with a 95% interval
    from a paired bootstrap that resamples whole (season, week) cuts (players in one week are not independent)."""
    cells: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0, 0])    # radar flags, hits; baseline flags, hits
    for r, m in zip(rows, matched):
        c = cells[(r["season"], r["week"])]
        hit = r["label"] == "elevated"
        if r["state"] == "emerging":
            c[0] += 1
            c[1] += hit
        if m:
            c[2] += 1
            c[3] += hit

    def gap(cs) -> float | None:
        rf, rh, bf, bh = (sum(c[i] for c in cs) for i in range(4))
        return rh / rf - bh / bf if rf and bf else None

    keys = sorted(cells)
    point = gap([cells[k] for k in keys])
    if point is None:
        return {"difference": None, "ci95": None, "cuts": len(keys)}
    rng = random.Random(BOOTSTRAP_SEED)
    draws = sorted(d for d in (gap([cells[rng.choice(keys)] for _ in keys]) for _ in range(BOOTSTRAP_RESAMPLES))
                   if d is not None)
    lo, hi = draws[int(0.025 * len(draws))], draws[min(len(draws) - 1, int(0.975 * len(draws)))]
    return {"difference": point, "ci95": [lo, hi], "cuts": len(keys), "resamples": BOOTSTRAP_RESAMPLES}
