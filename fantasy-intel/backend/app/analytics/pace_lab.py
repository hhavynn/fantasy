"""Research only: the play-pace and game-script matchup laboratory (issue #211, contract pace-lab.v1).

Questions: how many plays does an NFL offense run, how often does it pass when the game is neutral, and how do its
play volume and run/pass mix move with the game script? What would a HYPOTHETICAL script mix (competitive, leading,
trailing) do to its play volume and opportunities? The frozen walk-forward backtest of the pace estimates lives in
pace_backtest.py (research only, never imported by the app); this module only reads its committed result file.

Data (read-only). Only tables the NFL sync and the historical ingestion (app/nfl/history.py) already write from
nflverse play-by-play and schedules, with the same derivations (app/nfl/sync.py `derive_pbp`, app/nfl/kicking_pbp.py
`derive_team_drives`):
  nfl_team_week          offense team-week: run/pass plays (two-point tries excluded), dropbacks, designed rushes,
                         targets, neutral-situation plays and pass rate (0.20 <= win probability <= 0.80 and more than
                         two minutes left in the half), pass rate over expected
  nfl_team_game_drives   offense team-game: drives with at least one scrimmage play, run/pass plays
  nfl_schedules          regular-season games: teams, final scores, the overtime flag when it is stored
No betting market is read (no spread, total, moneyline or odds column, and never team_game_lines), nothing is fetched
and nothing is written. Raw play-by-play is not stored, so what it alone could answer is listed in GAPS.

Honesty rules: a missing row is missing, never zero; every estimate carries its sample size, its prior and the prior's
strength; observed values and model assumptions are separate fields; a scenario is hypothetical and its range is the
spread of past team-games, not a probability.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Sequence

from ..db.connection import fetch_dicts, table_columns, table_exists

CONTRACT = "pace-lab.v1"
VERSION = "pace-0.1.0"
STATUS = "research_only"

# ----------------------------------------------------------------------------- fixed definitions

SCRIPTS = ("competitive", "leading", "trailing")
SCRIPT_LABEL = MappingProxyType({"competitive": "Competitive", "leading": "Leading", "trailing": "Trailing"})
ONE_SCORE = 8               # |final margin| <= 8 points: competitive; won by 9+: leading; lost by 9+: trailing
PRIOR_GAMES = 4.0           # plays and drives per game shrink toward the league mean by this many games
PRIOR_SCRIPT_GAMES = 3.0    # a team's plays per game in one script toward (its overall estimate + league offset)
PRIOR_NEUTRAL_PLAYS = 150.0  # neutral pass rate toward the league rate by this many neutral plays (~4-5 games)
PRIOR_PASS_PLAYS = 120.0    # overall and per-script pass rate toward the league (or offset) rate by this many plays
PRIOR_TARGET_DROPBACKS = 100.0  # targets per dropback toward the league rate
PRIOR_DRIVE_PLAYS = 60.0    # plays per drive: toward the league rate by this many plays (pseudo drives = 60 / rate)
RANGE_QUANTILES = (0.10, 0.90)  # scenario ranges: the middle 80% of past team-games shaped like the scenario
WINDOWS = MappingProxyType({"season": None, "last8": 8, "last4": 4})
MAX_SPAN = 3                # seasons a query may pool
MIN_REG_WEEK, MAX_REG_WEEK = 1, 18
# Franchise moves and alternate codes: a relocated team keeps its history under one code. Every source is mapped
# before any join, so a pooled 2019-2020 query reads Oakland and Las Vegas as one offense (and says so).
FRANCHISE = MappingProxyType({"OAK": "LV", "SD": "LAC", "STL": "LA", "LAR": "LA", "JAC": "JAX", "WSH": "WAS"})

GAPS = (
    "Seconds per play: the game clock at each snap is not stored (only team-week and team-game totals are), so pace "
    "is measured as plays and drives per game and plays per drive, not seconds per play.",
    "Play-level score state: run and pass by the score at the snap is not stored. The game script is classified per "
    "game from the final margin instead; a final margin is known only after the game and is partly caused by the "
    "plays themselves, so script tendencies here describe games that ended that way.",
    "Overtime length is not stored: overtime games are left out of per-game volume (plays, drives) rather than "
    "rescaled, and kept in per-play rates.",
)

DEFINITIONS = MappingProxyType({
    "plays": "Run and pass plays (dropbacks including sacks and scrambles, plus designed runs; two-point tries, "
             "kneels, spikes and penalty-only snaps excluded), from nfl_team_week.offense_plays.",
    "drives": "Drives with at least one scrimmage play (nflverse fixed_drive); a drive with no scrimmage play (a "
              "kickoff returned for a touchdown, a penalty-only possession) is not a drive.",
    "neutral": "Neutral situation: win probability between 0.20 and 0.80 with more than two minutes left in the half "
               "(nflverse wp). A game with no neutral play has no neutral pass rate (missing, not 0%).",
    "pass_rate": "Dropbacks / (dropbacks + designed runs).",
    "script": f"Game script from the final margin: competitive within {ONE_SCORE} points, leading when the team won "
              f"by {ONE_SCORE + 1}+, trailing when it lost by {ONE_SCORE + 1}+. Outcome-based: it labels the game, "
              "not each play.",
    "overtime": "An overtime game (nflverse schedules) is excluded from per-game volume and kept in per-play rates; "
                "when the flag is not stored, overtime is unknown and the game is counted.",
    "final": "A game counts when the stored schedule has both scores and, when a sync time is recorded, its date is "
             "before that sync; a team-week row without a final score is not counted.",
    "shrinkage": f"Estimate = (team total + k x prior) / (team sample + k). Plays and drives per game: k = "
                 f"{PRIOR_GAMES:g} games toward the league mean. Neutral pass rate: k = {PRIOR_NEUTRAL_PLAYS:g} neutral "
                 f"plays. Pass rate: k = {PRIOR_PASS_PLAYS:g} plays. Targets per dropback: k = "
                 f"{PRIOR_TARGET_DROPBACKS:g} dropbacks. Plays per drive: k = {PRIOR_DRIVE_PLAYS:g} plays. Per script: "
                 f"plays per game with k = {PRIOR_SCRIPT_GAMES:g} games and pass rate with k = {PRIOR_PASS_PLAYS:g} "
                 "plays, each toward the team's overall estimate plus the league's offset for that script. Priors are "
                 "fixed in advance, not fitted.",
    "range": f"Scenario ranges: the {int(RANGE_QUANTILES[0] * 100)}th to {int(RANGE_QUANTILES[1] * 100)}th percentile "
             "of past team-games shaped like the scenario (the team's per-script estimate plus the league's spread "
             "of team-games around their own team's mean in that script, mixed by the scenario's weights). It "
             "describes past variation, not a probability for a future game.",
})

BACKTEST_FILE = Path(__file__).with_name("pace_lab_backtest.json")


class PaceLabError(ValueError):
    """A query the data cannot answer (unknown season or team, empty mix)."""


# ----------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class TeamGame:
    """One offense in one regular-season game. `has_pbp` False: the schedule lists a final game but no team-week row
    is stored (missing, not zero). `final` None: no schedule is stored, so finality, script and overtime are
    unknown."""
    season: int
    week: int
    team: str
    listed_as: str
    opponent: str | None
    game_id: str | None
    home: bool | None
    final: bool | None
    has_pbp: bool
    plays: int | None = None
    dropbacks: int | None = None
    designed_rushes: int | None = None
    targets: int | None = None
    neutral_plays: int | None = None
    neutral_pass_rate: float | None = None
    proe: float | None = None
    proe_plays: int | None = None
    drives: int | None = None
    drive_plays: int | None = None
    points_for: int | None = None
    points_against: int | None = None
    overtime: bool | None = None

    @property
    def margin(self) -> int | None:
        if self.points_for is None or self.points_against is None:
            return None
        return self.points_for - self.points_against

    @property
    def script(self) -> str | None:
        return script_of(self.margin)

    @property
    def counted(self) -> bool:
        """Usable evidence: a final (or schedule-unknown) game with a stored team-week row."""
        return self.final is not False and self.has_pbp

    @property
    def volume_ok(self) -> bool:
        """Counts toward per-game volume: counted, plays stored, not an overtime game."""
        return self.counted and self.plays is not None and self.overtime is not True

    @property
    def neutral_pass(self) -> float | None:
        if not self.neutral_plays or self.neutral_pass_rate is None:
            return None
        return self.neutral_pass_rate * self.neutral_plays

    @property
    def pass_den(self) -> int | None:
        if self.dropbacks is None or self.designed_rushes is None:
            return None
        return self.dropbacks + self.designed_rushes


def canon(team: str | None) -> str | None:
    if team is None:
        return None
    t = str(team).strip().upper()
    return FRANCHISE.get(t, t) or None


def script_of(margin: int | None) -> str | None:
    if margin is None:
        return None
    if margin > ONE_SCORE:
        return "leading"
    if margin < -ONE_SCORE:
        return "trailing"
    return "competitive"


def _int(v) -> int | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else int(round(f))


def _float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _cols(con, table: str) -> set[str]:
    return set(table_columns(con, table)) if table_exists(con, table) else set()


TEAM_WEEK_REQUIRED = ("season", "week", "team", "offense_plays")
TEAM_WEEK_OPTIONAL = ("opponent", "dropbacks", "team_designed_rushes", "team_targets", "neutral_plays",
                      "neutral_pass_rate", "proe", "proe_plays")
SCHEDULE_REQUIRED = ("season", "week", "home_team", "away_team", "home_score", "away_score")
SCHEDULE_OPTIONAL = ("game_id", "game_type", "overtime", "gameday")


def available_seasons(con) -> list[int]:
    if not set(TEAM_WEEK_REQUIRED) <= _cols(con, "nfl_team_week"):
        return []
    return [int(r["season"]) for r in fetch_dicts(
        con, "SELECT DISTINCT season FROM nfl_team_week WHERE season IS NOT NULL ORDER BY season")]


def _sync_cutoff(con) -> str | None:
    """Date (YYYY-MM-DD) the newest recorded NFL sync started, or None."""
    if not {"source", "status", "started_at"} <= _cols(con, "sync_runs"):
        return None
    row = fetch_dicts(con, "SELECT max(started_at) AS at FROM sync_runs WHERE status = 'ok' AND source IN "
                           "('nfl', 'nfl_history')")
    at = row[0]["at"] if row else None
    return str(at)[:10] if at else None


def load_games(con, first_season: int, last_season: int) -> tuple[list[TeamGame], dict]:
    """Every offense-game in the seasons, joined on (season, week, franchise). Returns (games, source notes)."""
    tw_cols, sc_cols, dr_cols = _cols(con, "nfl_team_week"), _cols(con, "nfl_schedules"), _cols(con, "nfl_team_game_drives")
    source = {"team_week": set(TEAM_WEEK_REQUIRED) <= tw_cols,
              "schedule": set(SCHEDULE_REQUIRED) <= sc_cols,
              "drives": {"season", "week", "team", "drives", "plays"} <= dr_cols,
              "overtime_recorded": set(SCHEDULE_REQUIRED) <= sc_cols and "overtime" in sc_cols,
              "neutral_recorded": {"neutral_plays", "neutral_pass_rate"} <= tw_cols,
              "sync_cutoff": None}
    if not source["team_week"]:
        return [], source
    span = [first_season, last_season]
    tw_sel = [c for c in (*TEAM_WEEK_REQUIRED, *TEAM_WEEK_OPTIONAL) if c in tw_cols]
    team_week = {}
    for r in fetch_dicts(con, f"SELECT {', '.join(tw_sel)} FROM nfl_team_week WHERE season BETWEEN ? AND ?", span):
        key = (int(r["season"]), int(r["week"]), canon(r["team"]))
        team_week[key] = r
    drives = {}
    if source["drives"]:
        for r in fetch_dicts(con, "SELECT season, week, team, drives, plays FROM nfl_team_game_drives "
                                  "WHERE season BETWEEN ? AND ?", span):
            key = (int(r["season"]), int(r["week"]), canon(r["team"]))
            prev = drives.get(key)
            if prev is None:
                drives[key] = r
            else:   # never expected (one game per team-week); keep the sum rather than drop a game
                drives[key] = {**prev, "drives": (prev["drives"] or 0) + (r["drives"] or 0),
                               "plays": (prev["plays"] or 0) + (r["plays"] or 0)}
    schedule = {}
    if source["schedule"]:
        cutoff = source["sync_cutoff"] = _sync_cutoff(con)
        sc_sel = [c for c in (*SCHEDULE_REQUIRED, *SCHEDULE_OPTIONAL) if c in sc_cols]
        where = "season BETWEEN ? AND ?" + (" AND game_type = 'REG'" if "game_type" in sc_cols else "")
        for r in fetch_dicts(con, f"SELECT {', '.join(sc_sel)} FROM nfl_schedules WHERE {where}", span):
            if r["week"] is None or r["home_team"] is None or r["away_team"] is None:
                continue
            hs, as_ = _int(r["home_score"]), _int(r["away_score"])
            day = r.get("gameday")
            final = hs is not None and as_ is not None and (cutoff is None or day is None or str(day)[:10] < cutoff)
            ot = r.get("overtime") if "overtime" in sc_cols else None
            ot = None if _int(ot) is None else bool(_int(ot))
            for team, opp, pf, pa, home in ((r["home_team"], r["away_team"], hs, as_, True),
                                            (r["away_team"], r["home_team"], as_, hs, False)):
                schedule[(int(r["season"]), int(r["week"]), canon(team))] = {
                    "listed_as": team, "opponent": canon(opp), "game_id": r.get("game_id"), "home": home,
                    "final": final, "pf": pf if final else None, "pa": pa if final else None,
                    "overtime": ot if final else None}
    games = []
    for key in sorted(set(team_week) | set(schedule)):
        season, week, team = key
        tw, sc, dr = team_week.get(key), schedule.get(key), drives.get(key)
        if tw is None and (sc is None or not sc["final"]):
            continue          # a scheduled, unplayed game: not evidence of anything
        n_plays = _int(tw.get("neutral_plays")) if tw else None
        games.append(TeamGame(
            season=season, week=week, team=team,
            listed_as=(sc["listed_as"] if sc else tw["team"]),
            opponent=sc["opponent"] if sc else canon(tw.get("opponent")),
            game_id=sc["game_id"] if sc else None, home=sc["home"] if sc else None,
            final=sc["final"] if sc else (None if not source["schedule"] else False),
            has_pbp=tw is not None,
            plays=_int(tw["offense_plays"]) if tw else None,
            dropbacks=_int(tw.get("dropbacks")) if tw else None,
            designed_rushes=_int(tw.get("team_designed_rushes")) if tw else None,
            targets=_int(tw.get("team_targets")) if tw else None,
            neutral_plays=n_plays,
            neutral_pass_rate=_float(tw.get("neutral_pass_rate")) if tw and n_plays else None,
            proe=_float(tw.get("proe")) if tw else None,
            proe_plays=_int(tw.get("proe_plays")) if tw else None,
            drives=_int(dr["drives"]) if dr else None,
            drive_plays=_int(dr["plays"]) if dr else None,
            points_for=sc["pf"] if sc else None, points_against=sc["pa"] if sc else None,
            overtime=sc["overtime"] if sc else None))
    return games, source


# ----------------------------------------------------------------------------- estimates


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(float(v), nd)


def mean_estimate(values: Sequence[float], prior: float | None, k: float, unit: str, *, nd: int = 1,
                  missing: int = 0, excluded: int = 0) -> dict:
    """Shrunk mean: (sum + k x prior) / (n + k). With no data the estimate is the prior alone (data weight 0)."""
    n = len(values)
    total = float(sum(values))
    observed = total / n if n else None
    estimate = (total + k * prior) / (n + k) if prior is not None else observed
    return {"observed": _r(observed, nd), "estimate": _r(estimate, nd), "n": n, "unit": unit,
            "prior": _r(prior, nd), "prior_strength": k, "data_weight": _r(n / (n + k), 3) if prior is not None else None,
            "missing": missing, "excluded": excluded}


def rate_estimate(num: float, den: float, prior: float | None, k: float, unit: str, *, nd: int = 3,
                  missing: int = 0, excluded: int = 0) -> dict:
    """Shrunk ratio: (num + k x prior) / (den + k)."""
    observed = num / den if den else None
    estimate = (num + k * prior) / (den + k) if prior is not None else observed
    return {"observed": _r(observed, nd), "estimate": _r(estimate, nd), "n": int(round(den)), "unit": unit,
            "prior": _r(prior, nd), "prior_strength": _r(k, 2),
            "data_weight": _r(den / (den + k), 3) if prior is not None else None,
            "missing": missing, "excluded": excluded}


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _ratio(pairs: Iterable[tuple[float, float]]) -> tuple[float, float]:
    num = den = 0.0
    for a, b in pairs:
        num += a
        den += b
    return num, den


def league_summary(games: Sequence[TeamGame]) -> dict:
    """League reference over counted games: per-game volume (overtime excluded), per-play rates and per-script
    means. Raw numbers only; the priors of every team estimate."""
    counted = [g for g in games if g.counted]
    vol = [g for g in counted if g.volume_ok]
    drives_ok = [g for g in counted if g.drives is not None and g.overtime is not True]
    nnum, nden = _ratio((g.neutral_pass, g.neutral_plays) for g in counted if g.neutral_pass is not None)
    pnum, pden = _ratio((g.dropbacks, g.pass_den) for g in counted if g.pass_den)
    tnum, tden = _ratio((g.targets, g.dropbacks) for g in counted if g.targets is not None and g.dropbacks)
    dnum, dden = _ratio((g.drive_plays, g.drives) for g in counted if g.drives and g.drive_plays is not None)
    plays_pg = _mean([g.plays for g in vol])
    out = {"games": len(counted), "volume_games": len(vol),
           "plays_per_game": plays_pg, "drives_per_game": _mean([g.drives for g in drives_ok]),
           "plays_per_drive": dnum / dden if dden else None,
           "neutral_pass_rate": nnum / nden if nden else None, "neutral_plays": nden,
           "pass_rate": pnum / pden if pden else None, "pass_plays": pden,
           "targets_per_dropback": tnum / tden if tden else None,
           "scripts": {}}
    classified = [g for g in counted if g.script]
    for s in SCRIPTS:
        sg = [g for g in classified if g.script == s]
        sv = [g.plays for g in sg if g.volume_ok]
        sn, sd = _ratio((g.dropbacks, g.pass_den) for g in sg if g.pass_den)
        out["scripts"][s] = {"games": len(sg), "share": len(sg) / len(classified) if classified else None,
                             "plays_per_game": _mean(sv), "volume_games": len(sv),
                             "pass_rate": sn / sd if sd else None, "pass_plays": sd}
    return out


def team_estimates(team_games: Sequence[TeamGame], league: dict) -> dict:
    """Observed and shrunk estimates for one offense over the given games, priors from the league reference."""
    counted = [g for g in team_games if g.counted]
    missing = sum(1 for g in team_games if g.final is not False and not g.has_pbp)
    ot = sum(1 for g in counted if g.overtime is True)
    vol = [g.plays for g in counted if g.volume_ok]
    dr = [g.drives for g in counted if g.drives is not None and g.overtime is not True]
    no_drives = sum(1 for g in counted if g.drives is None)
    nnum, nden = _ratio((g.neutral_pass, g.neutral_plays) for g in counted if g.neutral_pass is not None)
    no_neutral = sum(1 for g in counted if g.neutral_pass is None)
    pnum, pden = _ratio((g.dropbacks, g.pass_den) for g in counted if g.pass_den)
    tnum, tden = _ratio((g.targets, g.dropbacks) for g in counted if g.targets is not None and g.dropbacks)
    dnum, dden = _ratio((g.drive_plays, g.drives) for g in counted if g.drives and g.drive_plays is not None)
    proe_pairs = [(g.proe * g.proe_plays, g.proe_plays) for g in counted if g.proe is not None and g.proe_plays]
    prn, prd = _ratio(proe_pairs)
    ppd_prior = league["plays_per_drive"]
    ppd_k = PRIOR_DRIVE_PLAYS / ppd_prior if ppd_prior else PRIOR_GAMES
    return {
        "games": len(counted), "missing_pbp": missing, "overtime_games": ot,
        "plays_per_game": mean_estimate(vol, league["plays_per_game"], PRIOR_GAMES, "games",
                                        missing=missing, excluded=ot),
        "drives_per_game": mean_estimate(dr, league["drives_per_game"], PRIOR_GAMES, "games",
                                         missing=missing + no_drives, excluded=ot),
        "plays_per_drive": rate_estimate(dnum, dden, ppd_prior, ppd_k, "drives", nd=2,
                                         missing=missing + no_drives),
        "neutral_pass_rate": rate_estimate(nnum, nden, league["neutral_pass_rate"], PRIOR_NEUTRAL_PLAYS,
                                           "neutral plays", missing=missing + no_neutral),
        "pass_rate": rate_estimate(pnum, pden, league["pass_rate"], PRIOR_PASS_PLAYS, "plays", missing=missing),
        "targets_per_dropback": rate_estimate(tnum, tden, league["targets_per_dropback"], PRIOR_TARGET_DROPBACKS,
                                              "dropbacks", missing=missing),
        # PROE is nflverse's own model output (pass_oe), shown observed only: no prior, no shrinkage.
        "proe": {"observed": _r(prn / prd if prd else None, 1), "n": int(prd), "unit": "plays"},
    }


def script_estimates(team_games: Sequence[TeamGame], league: dict, overall: dict) -> dict:
    """Per-script plays per game and pass rate: observed, and shrunk toward the team's overall estimate plus the
    league's offset for that script (a team with no leading game gets its overall estimate shifted by the league's
    leading offset, flagged with data weight 0)."""
    counted = [g for g in team_games if g.counted and g.script]
    out = {}
    lp, lr = league["plays_per_game"], league["pass_rate"]
    for s in SCRIPTS:
        ls = league["scripts"][s]
        sg = [g for g in counted if g.script == s]
        plays_prior = (overall["plays_per_game"]["estimate"] + ls["plays_per_game"] - lp
                       if None not in (overall["plays_per_game"]["estimate"], ls["plays_per_game"], lp) else None)
        rate_prior = (min(1.0, max(0.0, overall["pass_rate"]["estimate"] + ls["pass_rate"] - lr))
                      if None not in (overall["pass_rate"]["estimate"], ls["pass_rate"], lr) else None)
        pn, pd = _ratio((g.dropbacks, g.pass_den) for g in sg if g.pass_den)
        out[s] = {"games": len(sg),
                  "plays_per_game": mean_estimate([g.plays for g in sg if g.volume_ok], plays_prior,
                                                  PRIOR_SCRIPT_GAMES, "games",
                                                  excluded=sum(1 for g in sg if g.overtime is True)),
                  "pass_rate": rate_estimate(pn, pd, rate_prior, PRIOR_PASS_PLAYS, "plays")}
    return out


def residual_pools(games: Sequence[TeamGame]) -> dict[str, list[float]]:
    """Per script: centered spread of team-games around their own team-season mean (leave-one-out; team-seasons
    with fewer than two volume games are skipped), sorted for determinism."""
    by_ts: dict[tuple, list[TeamGame]] = defaultdict(list)
    for g in games:
        if g.volume_ok:
            by_ts[(g.team, g.season)].append(g)
    raw: dict[str, list[float]] = {s: [] for s in SCRIPTS}
    for rows in by_ts.values():
        if len(rows) < 2:
            continue
        total = sum(g.plays for g in rows)
        for g in rows:
            if g.script:
                raw[g.script].append(g.plays - (total - g.plays) / (len(rows) - 1))
    out = {}
    for s, xs in raw.items():
        mu = _mean(xs)
        out[s] = sorted(x - mu for x in xs) if xs else []
    return out


def weighted_quantile(points: Sequence[tuple[float, float]], q: float) -> float | None:
    """Quantile of a weighted sample (value, weight): the smallest value whose cumulative weight reaches q."""
    pts = sorted((v, w) for v, w in points if w > 0)
    total = sum(w for _, w in pts)
    if not pts or total <= 0:
        return None
    acc = 0.0
    for v, w in pts:
        acc += w
        if acc >= q * total - 1e-12:
            return v
    return pts[-1][0]


def normalize_mix(raw: Sequence[float]) -> tuple[float, ...]:
    if len(raw) != len(SCRIPTS) or any(x is None or x < 0 or math.isnan(x) for x in raw):
        raise PaceLabError("A script mix needs three non-negative weights (competitive, leading, trailing).")
    total = float(sum(raw))
    if total <= 0:
        raise PaceLabError("A script mix needs at least one positive weight.")
    return tuple(x / total for x in raw)


def mixture(mix: Sequence[float], per_script: dict, pools: dict[str, list[float]], targets_rate: float | None) -> dict:
    """Central estimate and range of plays, dropbacks, designed runs and targets for a script mix. A script with
    weight 0 contributes nothing; a weighted script without an estimate or a spread leaves the range unknown."""
    measures = ("plays", "dropbacks", "designed_rushes", "targets")
    central = dict.fromkeys(measures, 0.0)
    points: dict[str, list[tuple[float, float]]] = {m: [] for m in measures}
    reasons = []
    for s, w in zip(SCRIPTS, mix):
        if w <= 0:
            continue
        m, p = per_script[s]["plays_per_game"]["estimate"], per_script[s]["pass_rate"]["estimate"]
        if m is None or p is None:
            reasons.append(f"no {s} estimate")
            central = dict.fromkeys(measures)
            points = {}
            break
        t = targets_rate if targets_rate is not None else 0.0
        central["plays"] += w * m
        central["dropbacks"] += w * m * p
        central["designed_rushes"] += w * m * (1 - p)
        central["targets"] += w * m * p * t
        pool = pools.get(s) or []
        if not pool:
            reasons.append(f"no past {s} games to measure the spread")
            continue
        each = w / len(pool)
        for r in pool:
            plays = max(0.0, m + r)
            points["plays"].append((plays, each))
            points["dropbacks"].append((plays * p, each))
            points["designed_rushes"].append((plays * (1 - p), each))
            points["targets"].append((plays * p * t, each))
    out = {}
    for meas in measures:
        c = central.get(meas) if central else None
        if targets_rate is None and meas == "targets":
            c = None
        lo = hi = None
        if c is not None and points and not reasons:
            lo, hi = (weighted_quantile(points[meas], q) for q in RANGE_QUANTILES)
        out[meas] = {"expected": _r(c, 1), "low": _r(lo, 1), "high": _r(hi, 1)}
    return {"measures": out, "range_reason": "; ".join(reasons) or None}


def observed_mix(games: Sequence[TeamGame]) -> tuple[tuple[float, ...] | None, int]:
    cls = [g.script for g in games if g.counted and g.script]
    if not cls:
        return None, 0
    return tuple(cls.count(s) / len(cls) for s in SCRIPTS), len(cls)


# ----------------------------------------------------------------------------- query


@dataclass(frozen=True)
class PaceQuery:
    season: int | None = None
    span: int = 1
    team: str | None = None
    opponent: str | None = None
    window: str = "season"
    through_week: int | None = None
    mix: tuple[float, float, float] | None = None   # raw weights (percent); normalized here


def _in_window(games: Sequence[TeamGame], window: str) -> list[TeamGame]:
    n = WINDOWS[window]
    rows = sorted(games, key=lambda g: (g.season, g.week))
    listed = [g for g in rows if g.final is not False]
    return listed[-n:] if n else listed


def _week_row(g: TeamGame) -> dict:
    status = ("missing_pbp" if not g.has_pbp else "overtime" if g.overtime is True else "ok")
    pass_rate = g.dropbacks / g.pass_den if g.pass_den else None
    return {"season": g.season, "week": g.week, "opponent": g.opponent, "home": g.home, "listed_as": g.listed_as,
            "points_for": g.points_for, "points_against": g.points_against, "margin": g.margin, "script": g.script,
            "overtime": g.overtime, "status": status, "plays": g.plays, "drives": g.drives,
            "plays_per_drive": _r(g.drive_plays / g.drives, 2) if g.drives and g.drive_plays is not None else None,
            "neutral_plays": g.neutral_plays, "neutral_pass_rate": _r(g.neutral_pass_rate, 3),
            "pass_rate": _r(pass_rate, 3), "dropbacks": g.dropbacks, "designed_rushes": g.designed_rushes,
            "targets": g.targets, "proe": _r(g.proe, 1)}


def _league_out(league: dict) -> dict:
    return {"games": league["games"], "volume_games": league["volume_games"],
            "plays_per_game": _r(league["plays_per_game"], 1), "drives_per_game": _r(league["drives_per_game"], 2),
            "plays_per_drive": _r(league["plays_per_drive"], 2),
            "neutral_pass_rate": _r(league["neutral_pass_rate"], 3), "neutral_plays": int(league["neutral_plays"]),
            "pass_rate": _r(league["pass_rate"], 3), "targets_per_dropback": _r(league["targets_per_dropback"], 3),
            "scripts": {s: {"games": v["games"], "share": _r(v["share"], 3), "plays_per_game": _r(v["plays_per_game"], 1),
                            "volume_games": v["volume_games"], "pass_rate": _r(v["pass_rate"], 3)}
                        for s, v in league["scripts"].items()}}


def frozen_backtest() -> dict:
    """The committed result of the frozen walk-forward backtest (scripts/pace_lab_backtest.py), read as data; the
    app never runs or refits it."""
    try:
        data = json.loads(BACKTEST_FILE.read_text())
    except FileNotFoundError:
        return {"status": "not_run", "message": "The frozen backtest has not been run; no claim about forecast value "
                                                "is made."}
    except (OSError, ValueError):
        return {"status": "unreadable", "message": "The stored backtest result could not be read."}
    return {"status": "frozen", **data}


def build_pace_lab(con, q: PaceQuery) -> dict:
    seasons = available_seasons(con)
    base = {"contract": CONTRACT, "version": VERSION, "model_status": STATUS, "gaps": list(GAPS),
            "definitions": dict(DEFINITIONS), "available_seasons": seasons, "backtest": frozen_backtest()}
    if not seasons:
        return {**base, "status": "no_data", "query": None,
                "message": "No NFL team-week play-by-play aggregates are stored. Run the NFL sync."}
    season = q.season if q.season is not None else seasons[-1]
    if season not in seasons:
        raise PaceLabError(f"No play-by-play aggregates are stored for {season}; stored seasons: "
                           f"{', '.join(map(str, seasons))}.")
    if q.window not in WINDOWS:
        raise PaceLabError("Unknown window.")
    span = max(1, min(MAX_SPAN, q.span))
    first = max(seasons[0], season - span + 1)
    games, source = load_games(con, first, season)
    weeks = sorted({g.week for g in games if g.season == season and g.counted})
    through = q.through_week if q.through_week is not None else (weeks[-1] if weeks else None)
    ref = [g for g in games if g.season < season or through is None or g.week <= through]
    teams = sorted({g.team for g in ref if g.counted})
    team, opp = canon(q.team), canon(q.opponent)
    if team is not None and team not in teams:
        raise PaceLabError(f"No stored games for {team} in this filter.")
    if opp is not None and opp not in teams:
        raise PaceLabError(f"No stored games for {opp} in this filter.")
    if team is not None and opp == team:
        raise PaceLabError("A team cannot be its own opponent.")
    league = league_summary(ref)
    pools = residual_pools(ref)
    nfl_synced_at = None
    if {"source", "status", "finished_at"} <= _cols(con, "sync_runs"):
        row = fetch_dicts(con, "SELECT max(finished_at) AS at FROM sync_runs WHERE status = 'ok' AND source = 'nfl'")
        nfl_synced_at = row[0]["at"] if row else None
    out = {**base, "status": "ok",
           "query": {"season": season, "seasons": list(range(first, season + 1)), "span": season - first + 1,
                     "team": team, "opponent": opp, "window": q.window, "through_week": through},
           "weeks": weeks, "teams": teams,
           "source": {"tables": [t for t, ok in (("nfl_team_week", source["team_week"]),
                                                 ("nfl_team_game_drives", source["drives"]),
                                                 ("nfl_schedules", source["schedule"])) if ok],
                      "schedule_stored": source["schedule"], "drives_stored": source["drives"],
                      "overtime_recorded": source["overtime_recorded"], "neutral_recorded": source["neutral_recorded"],
                      "nfl_synced_at": nfl_synced_at,
                      "franchise_codes": sorted({(g.listed_as, g.team) for g in ref if g.listed_as and
                                                 canon(g.listed_as) == g.team and g.listed_as.upper() != g.team})},
           "coverage": {"games": len(ref), "counted": sum(g.counted for g in ref),
                        "missing_pbp": sum(1 for g in ref if g.final is not False and not g.has_pbp),
                        "not_final": sum(1 for g in ref if g.final is False),
                        "overtime": sum(1 for g in ref if g.counted and g.overtime is True),
                        "overtime_unknown": sum(1 for g in ref if g.counted and g.overtime is None),
                        "no_neutral_play": sum(1 for g in ref if g.counted and g.neutral_pass is None),
                        "no_drive": sum(1 for g in ref if g.counted and g.drives is None)},
           "league": _league_out(league),
           "residual_games": {s: len(p) for s, p in pools.items()},
           "overview": [], "team": None, "matchup": None, "scenario": None}
    by_team: dict[str, list[TeamGame]] = defaultdict(list)
    for g in ref:
        by_team[g.team].append(g)
    for t in teams:
        tg = _in_window(by_team[t], q.window)
        est = team_estimates(tg, league)
        out["overview"].append({"team": t, "games": est["games"], "missing_pbp": est["missing_pbp"],
                                "overtime_games": est["overtime_games"],
                                **{k: est[k] for k in ("plays_per_game", "drives_per_game", "plays_per_drive",
                                                       "neutral_pass_rate", "pass_rate")}})
    out["overview"].sort(key=lambda r: (-(r["plays_per_game"]["estimate"] or 0), r["team"]))
    if team is None:
        return out
    window_games = _in_window(by_team[team], q.window)
    shown = [g for g in window_games if opp is None or g.opponent == opp]
    overall = team_estimates(shown, league)
    per_script = script_estimates(shown, league, overall)
    mix_obs, n_classified = observed_mix(shown)
    lmix = tuple(league["scripts"][s]["share"] or 0.0 for s in SCRIPTS)
    baseline_mix, baseline_label = (mix_obs, "observed") if mix_obs else (lmix, "league")
    listed = sorted({g.listed_as for g in by_team[team] if g.listed_as})
    out["team"] = {"team": team, "listed_as": listed, "games": [_week_row(g) for g in shown],
                   "window_games": len(window_games), "summary": overall, "scripts": per_script,
                   "observed_mix": [_r(x, 3) for x in mix_obs] if mix_obs else None,
                   "classified_games": n_classified}
    if opp is not None:
        allowed = [g for g in ref if g.opponent == opp and g.volume_ok]
        lp = league["plays_per_game"]
        opp_est = mean_estimate([g.plays for g in allowed], lp, PRIOR_GAMES, "games")
        team_all = team_estimates(window_games, league)["plays_per_game"]
        additive = (team_all["estimate"] + opp_est["estimate"] - lp
                    if None not in (team_all["estimate"], opp_est["estimate"], lp) else None)
        out["matchup"] = {"opponent": opp, "team_plays_per_game": team_all, "opponent_plays_allowed": opp_est,
                          "league_plays_per_game": _r(lp, 1), "additive_plays": _r(additive, 1),
                          "head_to_head_games": sum(1 for g in shown if g.counted)}
    targets_rate = overall["targets_per_dropback"]["estimate"]
    mix_in = normalize_mix(q.mix) if q.mix is not None else baseline_mix
    base_res = mixture(baseline_mix, per_script, pools, targets_rate)
    scen_res = mixture(mix_in, per_script, pools, targets_rate)
    change = {m: {"expected": (_r(scen_res["measures"][m]["expected"] - base_res["measures"][m]["expected"], 1)
                               if None not in (scen_res["measures"][m]["expected"], base_res["measures"][m]["expected"])
                               else None)} for m in scen_res["measures"]}
    out["scenario"] = {"hypothetical": True, "input_mix": list(q.mix) if q.mix is not None else None,
                       "mix": [_r(x, 3) for x in mix_in], "baseline_mix": [_r(x, 3) for x in baseline_mix],
                       "baseline_source": baseline_label, "baseline": base_res, "result": scen_res, "change": change}
    return out
