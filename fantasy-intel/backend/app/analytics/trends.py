"""Deterministic week-over-week trend helpers.

A trend compares the player's *recent* games to his *baseline* (earlier)
games. Only games the player actually appeared in are used; missing weeks
(bye, inactive) are skipped rather than treated as zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean
from typing import Sequence

MIN_GAMES_FOR_TREND = 4


@dataclass
class Trend:
    metric: str
    recent_avg: float | None
    baseline_avg: float | None
    delta: float | None
    slope: float | None          # least-squares slope per game over all games
    threshold: float
    label: str                   # UP | DOWN | FLAT | INSUFFICIENT
    n_recent: int
    n_baseline: int
    recent_weeks: list[int]
    baseline_weeks: list[int]

    def as_dict(self) -> dict:
        return asdict(self)


def split_windows(n_games: int) -> tuple[int, int]:
    """(recent_n, baseline_n) for a player with n_games appearances."""
    if n_games < MIN_GAMES_FOR_TREND:
        return 0, 0
    recent = 3 if n_games >= 6 else 2
    return recent, min(n_games - recent, 5)


def slope(values: Sequence[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    xs = range(n)
    x_bar, y_bar = (n - 1) / 2, mean(values)
    den = sum((x - x_bar) ** 2 for x in xs)
    return sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, values)) / den


def compute_trend(metric: str, series: Sequence[tuple[int, float | None]], threshold: float) -> Trend:
    """`series` = [(week, value)] in any order; None values are ignored."""
    games = sorted((w, v) for w, v in series if v is not None)
    recent_n, base_n = split_windows(len(games))
    values = [v for _, v in games]
    if recent_n == 0:
        return Trend(metric, mean(values) if values else None, None, None, slope(values), threshold,
                     "INSUFFICIENT", len(values), 0, [w for w, _ in games], [])
    recent = games[-recent_n:]
    baseline = games[-(recent_n + base_n):-recent_n]
    r_avg = mean(v for _, v in recent)
    b_avg = mean(v for _, v in baseline)
    delta = r_avg - b_avg
    label = "UP" if delta >= threshold else "DOWN" if delta <= -threshold else "FLAT"
    return Trend(metric, r_avg, b_avg, delta, slope(values), threshold, label,
                 len(recent), len(baseline), [w for w, _ in recent], [w for w, _ in baseline])


# Per-position usage metrics and the change that counts as meaningful.
TREND_METRICS: dict[str, list[tuple[str, float]]] = {
    "WR": [("offense_pct", 0.08), ("target_share", 0.04), ("targets", 1.5), ("xfp", 2.0)],
    "TE": [("offense_pct", 0.08), ("target_share", 0.04), ("targets", 1.5), ("xfp", 2.0)],
    "RB": [("offense_pct", 0.08), ("backfield_rush_share", 0.10), ("team_rush_share", 0.08),
           ("opportunities", 3.0), ("xfp", 2.0)],
    "QB": [("offense_pct", 0.05), ("rush_attempts", 1.5), ("xfp", 2.0)],
}

TREND_UP_CUTOFF = 0.35


def composite_trend(trends: Sequence[Trend]) -> tuple[float | None, str]:
    """Average of each metric's delta/threshold (clamped to ±2, scaled to ±1)."""
    usable = [t for t in trends if t.label != "INSUFFICIENT" and t.delta is not None]
    if not usable:
        return None, "INSUFFICIENT"
    score = mean(max(-2.0, min(2.0, t.delta / t.threshold)) / 2.0 for t in usable)
    label = "UP" if score >= TREND_UP_CUTOFF else "DOWN" if score <= -TREND_UP_CUTOFF else "FLAT"
    return score, label
