"""Position-specific schedule intelligence (Phase A: descriptive, read-only).

For every remaining fantasy scoring period of a player's current NFL team: the scheduled opponent,
home/away or bye, and what that opponent's defense has actually allowed in games presumed complete
before the matchup. Computed on request from existing tables (nfl_schedules, nfl_defense_week, ESPN league
and roster tables); nothing is written, synced or fetched. See docs/v2_schedule_intelligence.md.

What it is not: a projection. No number here changes ESPN projections, player scores, BUY/HOLD/SELL,
waivers, manager scouting, the trade evaluator or trade discovery, and nothing estimates a player's
points or share of volume. A defense's observed allowance mixes defensive quality with the
offenses, game scripts and luck it has faced.

Rules:
  * No lookahead: a matchup in week W uses only the opponent's games in weeks < W.
  * Presumed-complete games only: a defensive week counts only when its game is presumed complete
    (both scores present and both inputs fetched >= 8 h after kickoff; see game_completion). This is
    a conservative heuristic, not an official final-game status, which no stored field provides.
    Rows of a game possibly in progress, or one the schedule does not list, are excluded and listed
    with the reason.
  * Missing is never zero: a metric with no eligible denominator is NULL with a reason; a real 0 is 0.
  * Ranks are among defenses with at least MIN_DEF_GAMES presumed-complete games under the same cutoff;
    rank 1 = the most permissive (highest value allowed to offenses).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..db.connection import fetch_dicts, table_columns, table_exists
from ..espn.constants import IR_SLOT
from .context import FAVORABLE_RANK, MIN_DEF_GAMES, TOUGH_RANK
from .projections import last_regular_season_week
from .trade_data import evaluation_start_week, kickoff_utc

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")
PHASES = ("all", "regular", "playoffs")
# Bands as in analytics/context.py: ranks 1-10 of 32 favorable, ranks 23-32 tough (the 10 lowest).
BAND = 32 - TOUGH_RANK + 1                     # 10
# Below this many rankable defenses the two bands would touch, so no band is assigned.
MIN_RANKED_DEFENSES = FAVORABLE_RANK + BAND + 1   # 21

# metric -> definition. Numerator/denominator are nfl_defense_week columns (sums over sampled games).
METRICS: dict[str, dict] = {
    "pass_epa_per_play": {
        "label": "EPA allowed per eligible pass play",
        "numerator": "pass_epa_sum", "denominator": "pass_plays", "kind": "mean", "unit": "EPA/play",
        "definition": "Sum of offensive EPA on eligible pass plays against this defense divided by their count. "
                      "Eligible pass play: nflverse `pass` = 1 (pass attempts, sacks and QB scrambles), play type "
                      "run/pass (penalty no-plays excluded), not a two-point try, EPA present. This is the "
                      "nflverse pass-play definition, close to but not verified identical to `qb_dropback`.",
        "positions": ("QB", "WR", "TE"),
    },
    "rush_epa_per_play": {
        "label": "EPA allowed per designed rush",
        "numerator": "rush_epa_sum", "denominator": "rush_plays", "kind": "mean", "unit": "EPA/rush",
        "definition": "Sum of offensive EPA on designed rushes against this defense divided by their count. "
                      "Designed rush: play type run with a rusher, not a QB scramble, not a kneel, not a two-point "
                      "try, EPA present.",
        "positions": ("RB",),
    },
    "rush_success_rate": {
        "label": "Designed-rush success rate allowed",
        "numerator": "rush_success", "denominator": "rush_plays", "kind": "rate", "unit": "share",
        "definition": "Designed rushes with nflverse `success` = 1 (EPA > 0) divided by designed rushes with EPA.",
        "positions": ("RB",),
    },
    "rb_target_share": {
        "label": "Share of targets to RBs",
        "numerator": "rb_targets", "denominator": "targets", "kind": "rate", "unit": "share",
        "definition": "Targets to receivers whose nflverse position is RB (FB counted as RB) divided by all targets "
                      "against this defense. Target: a pass attempt with a receiver, not a sack, not a two-point try.",
        "positions": ("RB",),
    },
    "wr_target_share": {
        "label": "Share of targets to WRs",
        "numerator": "wr_targets", "denominator": "targets", "kind": "rate", "unit": "share",
        "definition": "Targets to receivers whose nflverse position is WR divided by all targets against this defense.",
        "positions": ("WR",),
    },
    "te_target_share": {
        "label": "Share of targets to TEs",
        "numerator": "te_targets", "denominator": "targets", "kind": "rate", "unit": "share",
        "definition": "Targets to receivers whose nflverse position is TE divided by all targets against this defense. "
                      "An observed tendency, not proof of a 'TE funnel' or of any player's future targets.",
        "positions": ("TE",),
    },
}
# The one metric per position used for the tougher/permissive band (shown with its rank and sample).
PRIMARY_METRIC = {"QB": "pass_epa_per_play", "RB": "rush_epa_per_play", "WR": "pass_epa_per_play",
                  "TE": "pass_epa_per_play"}
POSITION_METRICS = {pos: [m for m, d in METRICS.items() if pos in d["positions"]] for pos in SKILL_POSITIONS}
SUM_COLUMNS = ("pass_plays", "pass_epa_sum", "rush_plays", "rush_epa_sum", "rush_success", "targets",
               "rb_targets", "wr_targets", "te_targets")

COMPLETION_RULE = ("A defensive week counts only when its game is presumed complete: both scores in the NFL schedule, and "
                   "both the schedule and the defensive play-by-play aggregates fetched (by successful NFL syncs for this "
                   "season that refreshed them) at least 8 hours after the scheduled kickoff, allowing a 1-hour download "
                   "margin. This is a conservative heuristic, not an official final-game status: the stored nflverse data "
                   "has no game-status field and does not document whether a score can be live. Games that cannot be "
                   "presumed complete are excluded and listed with the reason.")
# What the heuristic cannot rule out (shown with the rule; see docs/v2_schedule_intelligence.md).
COMPLETION_LIMITS = [
    "Games can be suspended and never finished (Bills-Bengals, 2 January 2023: suspended in the first quarter, "
    "later canceled). If the schedule shows a partial score for such a game, it is presumed complete once 8 hours "
    "pass and its partial defensive rows are counted; once the schedule drops a canceled game, its rows are "
    "excluded again.",
    "A game resumed on a later day, or delayed past 8 hours, can likewise be presumed complete while unfinished.",
    "The rule measures when this app downloaded the nflverse files, not when nflverse generated them: a file built "
    "upstream during a game and downloaded later would pass.",
    "The 1-hour download margin is an assumption, not a guaranteed bound: a sync's start is recorded after all "
    "downloads finish, and nflreadpy limits each request's wait (30 s by default), not a whole download. With "
    "nflreadpy's filesystem cache enabled (NFLREADPY_CACHE=filesystem), a sync can reuse downloads up to 24 hours "
    "old, so its recorded time overstates freshness.",
]

# Why a game (or a defensive row) is not presumed complete.
NO_SCORE, KICKOFF_UNKNOWN, FETCH_UNKNOWN, FETCHED_TOO_EARLY, NOT_IN_SCHEDULE = (
    "no_score", "kickoff_unknown", "fetch_time_unknown", "fetched_too_early", "not_in_schedule")
INPUT_NAMES = {"schedules": "the NFL schedule (nfl_schedules)",
               "defense": "the defensive play-by-play aggregates (nfl_defense_week)"}

LABELS = {
    "permissive": "Observed more permissive",
    "middle": "Middle of the league",
    "tougher": "Observed tougher",
    "insufficient": "Insufficient evidence",
    "unavailable": "Unavailable",
    "bye": "Bye",
}
LABEL_RULE = (f"Band from the position's primary metric, ranked among defenses with >= {MIN_DEF_GAMES} presumed-complete "
              f"games before the matchup week (rank 1 = most permissive): ranks 1-{FAVORABLE_RANK} 'observed more "
              f"permissive', the {BAND} lowest ranks 'observed tougher', otherwise 'middle'. Insufficient when the "
              f"opponent has < {MIN_DEF_GAMES} sampled games, the metric has no eligible plays, or fewer than "
              f"{MIN_RANKED_DEFENSES} defenses can be ranked. The same thresholds as the existing next-week matchup "
              "label (analytics/context.py). Descriptive only: not a projection or a predictive confidence.")
TEAM_SNAPSHOT_NOTE = ("Player teams are the current ESPN snapshot applied to every remaining week; future trades, "
                      "releases and signings are not predicted.")
OBSERVATION_NOTE = ("A defense's allowed numbers describe what offenses did against it; they also reflect the offenses "
                    "it faced and game situations, and say nothing about an individual player's role or usage.")


class NotSynced(LookupError):
    """No ESPN league (or its current/final scoring period) in the database."""


class NotFound(LookupError):
    """Unknown team or player, or no team marked as yours."""


def _json(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


# ------------------------------------------------------------------ schedule --

# No stored field proves a game is final: nflverse schedules carry scores, result and overtime but no
# game status, document scores only as "NA for games which haven't yet been played", and do not say
# whether a score can be live. So completion is *presumed*, by a conservative heuristic: both scores
# present AND both inputs fetched at least FINAL_AFTER_KICKOFF after the scheduled kickoff, by which
# time an ordinary game (overtime and usual weather delays included) has ended. Each input's fetch time
# is the start of the latest successful NFL sync for the league's season that refreshed it (recorded
# right after the nflverse downloads), less FETCH_MARGIN for the downloads themselves; the earlier of
# the two decides. Residual risks (suspended or resumed games, upstream build time, the margin itself)
# are in COMPLETION_LIMITS. Anything else is excluded, with a reason code.
FINAL_AFTER_KICKOFF = timedelta(hours=8)
FETCH_MARGIN = timedelta(hours=1)


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def nfl_input_runs(con, season: int) -> dict:
    """For the league's season: the latest successful NFL sync run that actually refreshed
    nfl_schedules, the latest that refreshed nfl_defense_week (a run can succeed with the play-by-play
    download failed, leaving older defensive rows in place; sync stats only list the tables a run
    wrote), and the latest successful run of any kind. Each is {started_at, finished_at, refreshed}
    (UTC; stored naive) or None. A run that refreshed only other NFL tables says nothing about these
    two inputs."""
    out = {"schedules": None, "defense": None, "latest": None}
    if not table_exists(con, "sync_runs"):
        return out
    for run in fetch_dicts(con, "SELECT started_at, finished_at, stats FROM sync_runs WHERE source = 'nfl' AND "
                                "status = 'ok' AND started_at IS NOT NULL ORDER BY started_at DESC"):
        stats = _json(run["stats"])
        if stats.get("season") != season:
            continue
        info = {"started_at": _utc(run["started_at"]), "finished_at": _utc(run["finished_at"]),
                "refreshed": {"schedules": "nfl_schedules" in stats, "defense": "nfl_defense_week" in stats}}
        if out["latest"] is None:
            out["latest"] = info
        for key in ("schedules", "defense"):
            if out[key] is None and info["refreshed"][key]:
                out[key] = info
        if out["schedules"] and out["defense"]:
            break
    return out


def nfl_fetch_times(con, season: int) -> dict:
    """When each stored input was fetched: the start of the latest successful same-season sync that
    refreshed it, or None."""
    runs = nfl_input_runs(con, season)
    return {k: runs[k]["started_at"] if runs[k] else None for k in ("schedules", "defense")}


def nfl_fetch_time(con, season: int) -> datetime | None:
    """The earlier of the schedule and the defensive-rows fetches (both must postdate the game), or
    None when either is unknown."""
    t = nfl_fetch_times(con, season)
    return min(t["schedules"], t["defense"]) if t["schedules"] and t["defense"] else None


def game_completion(h, a, kickoff: datetime | None, fetches: dict) -> tuple[bool, str | None, str | None]:
    """(presumed complete, reason code, reason) for one scheduled game; `fetches` is nfl_fetch_times.
    A 0-0 or a shutout counts once the timing rule holds; the rule cannot detect a suspended game."""
    if h is None or a is None:
        return False, NO_SCORE, "no score in the stored NFL schedule (not played, not yet published, or not recorded)"
    if kickoff is None:
        return False, KICKOFF_UNKNOWN, ("score present, but the scheduled kickoff time is unknown, so completion "
                                        "cannot be presumed (the score may be live)")
    unknown = [INPUT_NAMES[k] for k in ("schedules", "defense") if not fetches.get(k)]
    if unknown:
        return False, FETCH_UNKNOWN, (f"score present, but no successful NFL sync for this season is recorded as "
                                      f"having refreshed {' or '.join(unknown)}, so when the data was fetched is "
                                      "unknown and completion cannot be presumed")
    lag = min(fetches["schedules"], fetches["defense"]) - FETCH_MARGIN - kickoff
    if lag < FINAL_AFTER_KICKOFF:
        hours = FINAL_AFTER_KICKOFF.seconds // 3600
        when = ("before the scheduled kickoff" if lag.total_seconds() < 0
                else f"only about {lag.total_seconds() / 3600:.1f} h after the scheduled kickoff")
        return False, FETCHED_TOO_EARLY, (f"score present, but the data was fetched {when} (the rule needs >= {hours} h, "
                                          "after a 1 h download margin), so it may be from a game in progress")
    return True, None, None


def load_schedule(con, season: int, fetches: dict | None = None) -> dict:
    """REG games of the season: (team, week) -> game info, the weeks covered, the set of (team, week)
    whose game is presumed complete (game_completion), {code, reason} for every other game, and the
    number of games per outcome. `fetches` is nfl_fetch_times (None: both unknown)."""
    fetches = fetches or {"schedules": None, "defense": None}
    out = {"games": {}, "covered": set(), "presumed_complete": set(), "excluded": {}, "available": False,
           "has_scores": False, "game_counts": defaultdict(int)}
    if not table_exists(con, "nfl_schedules"):
        return out
    cols = set(table_columns(con, "nfl_schedules"))
    out["available"] = True
    out["has_scores"] = {"home_score", "away_score"} <= cols
    sel = ["week", "home_team", "away_team"] + [c for c in ("game_id", "gameday", "gametime", "home_score",
                                                           "away_score") if c in cols]
    for g in fetch_dicts(con, f"SELECT {', '.join(sel)} FROM nfl_schedules WHERE season = ? AND game_type = 'REG' "
                              "ORDER BY week, home_team", [season]):
        w = g["week"]
        out["covered"].add(w)
        ko = kickoff_utc(g.get("gameday"), g.get("gametime"))
        presumed, code, why = game_completion(g.get("home_score"), g.get("away_score"), ko, fetches)
        out["game_counts"][code or "presumed_complete"] += 1
        for team, opp, home in ((g["home_team"], g["away_team"], True), (g["away_team"], g["home_team"], False)):
            out["games"][(team, w)] = {"opponent": opp, "home": home, "game_id": g.get("game_id"),
                                       "kickoff": _iso(ko), "presumed_final": presumed}
            if presumed:
                out["presumed_complete"].add((team, w))
            else:
                out["excluded"][(team, w)] = {"code": code, "reason": why}
    out["game_counts"] = dict(out["game_counts"])
    return out


def completion_warnings(season: int, runs: dict, sched: dict, defense_rows: list[dict]) -> list[dict]:
    """User-facing warnings about the inputs the completion rule depends on, each {code, message}:
    unknown refresh times, inputs refreshed by different syncs, a latest sync that refreshed neither,
    and games or rows excluded for unknown kickoff, a too-early fetch or no matching schedule game."""
    out = []

    def add(code: str, message: str) -> None:
        out.append({"code": code, "message": message})

    for key, code in (("schedules", "schedule_refresh_unknown"), ("defense", "defense_refresh_unknown")):
        if runs[key] is None:
            add(code, f"No successful {season} NFL sync is recorded as having refreshed {INPUT_NAMES[key]}, so its "
                      "freshness is unknown: no game can be presumed complete and no defensive evidence is shown.")
    s, d = runs["schedules"], runs["defense"]
    if s and d and s["started_at"] != d["started_at"]:
        newer, older = ("schedules", "defense") if s["started_at"] > d["started_at"] else ("defense", "schedules")
        add("inputs_refreshed_separately",
            f"The inputs were last refreshed by different syncs: {INPUT_NAMES[newer]} by the sync started "
            f"{_iso(runs[newer]['started_at'])}, {INPUT_NAMES[older]} only by an earlier one started "
            f"{_iso(runs[older]['started_at'])} (a later sync did not refresh it, for example because a download "
            "failed). Completion is judged as of the earlier fetch, so games played after it cannot be presumed "
            "complete yet.")
    latest = runs["latest"]
    if latest and not any(latest["refreshed"].values()):
        add("latest_sync_refreshed_neither",
            f"The latest successful {season} NFL sync (started {_iso(latest['started_at'])}) refreshed neither the NFL "
            "schedule nor the defensive aggregates; it is not treated as evidence that either is current.")
    counts = sched["game_counts"]
    if counts.get(KICKOFF_UNKNOWN):
        add(KICKOFF_UNKNOWN, f"{counts[KICKOFF_UNKNOWN]} scored game(s) have no known kickoff time, so they cannot be "
                             "presumed complete; their defensive rows are excluded.")
    if counts.get(FETCHED_TOO_EARLY):
        add(FETCHED_TOO_EARLY, f"{counts[FETCHED_TOO_EARLY]} scored game(s) were fetched less than "
                               f"{FINAL_AFTER_KICKOFF.seconds // 3600} h after kickoff (after the 1 h download margin) "
                               "and may have been in progress; their defensive rows are excluded until a later NFL sync.")
    orphans = sum(1 for r in defense_rows if (r["defteam"], r["week"]) not in sched["games"])
    if orphans and sched["available"]:
        add(NOT_IN_SCHEDULE, f"{orphans} defensive team-week row(s) have no matching game in the stored NFL schedule "
                             "(for example a canceled game) and are excluded.")
    return out


# --------------------------------------------------------- defense samples --

NOT_IN_SCHEDULE_REASON = {"code": NOT_IN_SCHEDULE,
                          "reason": "defensive rows exist but the game is not in the stored NFL schedule (for example "
                                    "canceled, removed or not loaded)"}


def defense_samples(rows: list[dict], presumed_complete: set, before_week: int,
                    reasons: dict | None = None) -> dict[str, dict]:
    """Per defense: sums over its games in weeks < before_week whose game is presumed complete.
    Rows of other games (no score, possibly in progress, unknown timing, not in the schedule) are
    excluded and listed with their {code, reason}, never silently used."""
    reasons = reasons or {}
    out: dict[str, dict] = {}
    for r in rows:
        if r["week"] >= before_week:
            continue
        d = out.setdefault(r["defteam"], {"games": 0, "weeks": [], "excluded_weeks": [],
                                          "excluded_reasons": {}, **{c: 0 for c in SUM_COLUMNS},
                                          "missing": defaultdict(int)})
        if (r["defteam"], r["week"]) not in presumed_complete:
            d["excluded_weeks"].append(r["week"])
            d["excluded_reasons"][r["week"]] = reasons.get((r["defteam"], r["week"]), NOT_IN_SCHEDULE_REASON)
            continue
        d["games"] += 1
        d["weeks"].append(r["week"])
        for c in SUM_COLUMNS:
            if r.get(c) is None:
                d["missing"][c] += 1           # that game contributes nothing to metrics using c
            else:
                d[c] += r[c]
    for d in out.values():
        d["missing"] = dict(d["missing"])
        d["weeks"].sort()
    return out


def metric_value(sample: dict, metric: str) -> tuple[float | None, str | None]:
    m = METRICS[metric]
    num, den = m["numerator"], m["denominator"]
    if sample["games"] == 0:
        return None, "no presumed-complete games sampled"
    if sample["missing"].get(num) or sample["missing"].get(den):
        return None, f"{num}/{den} missing for {max(sample['missing'].get(num, 0), sample['missing'].get(den, 0))} game(s)"
    if not sample[den]:
        return None, f"no eligible {den.replace('_', ' ')} in the sample"
    return sample[num] / sample[den], None


def rank_permissive(values: dict[str, float]) -> dict[str, int]:
    """Rank 1 = highest value (most permissive to offenses); ties share the better rank; team breaks order."""
    ordered = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
    out, prev, rank = {}, None, 0
    for i, (team, v) in enumerate(ordered, start=1):
        if v != prev:
            rank, prev = i, v
        out[team] = rank
    return out


def band(rank: int | None, pool: int) -> str:
    if rank is None or pool < MIN_RANKED_DEFENSES:
        return "insufficient"
    if rank <= FAVORABLE_RANK:
        return "permissive"
    if rank > pool - BAND:
        return "tougher"
    return "middle"


class DefenseCutoffs:
    """Defensive summaries and ranks per cutoff (games before week W), computed once per cutoff."""

    def __init__(self, rows: list[dict], presumed_complete: set, reasons: dict | None = None):
        self.rows, self.presumed_complete, self.reasons = rows, presumed_complete, reasons or {}
        self._cache: dict[int, dict] = {}

    def at(self, before_week: int) -> dict:
        if before_week in self._cache:
            return self._cache[before_week]
        samples = defense_samples(self.rows, self.presumed_complete, before_week, self.reasons)
        metrics: dict[str, dict] = {d: {} for d in samples}
        ranks: dict[str, dict] = {}
        for m in METRICS:
            eligible = {}
            for d, s in samples.items():
                v, why = metric_value(s, m)
                metrics[d][m] = (v, why)
                if v is not None and s["games"] >= MIN_DEF_GAMES:
                    eligible[d] = v
            ranks[m] = (rank_permissive(eligible), len(eligible))
        through = max((w for s in samples.values() for w in s["weeks"]), default=None)
        out = {"samples": samples, "metrics": metrics, "ranks": ranks, "through_week": through}
        self._cache[before_week] = out
        return out

    def describe(self, defteam: str, before_week: int, position: str) -> dict:
        """The opponent's evidence for one matchup: sample, every metric of the position with numerator,
        denominator, rank and pool, the band on the primary metric, and a plain sentence."""
        cut = self.at(before_week)
        s = cut["samples"].get(defteam)
        base = {"defteam": defteam, "games_before_week": before_week, "through_week": cut["through_week"],
                "games": s["games"] if s else 0, "weeks_sampled": s["weeks"] if s else [],
                "excluded_weeks": s["excluded_weeks"] if s else [],
                "excluded_reasons": {str(w): why for w, why in s["excluded_reasons"].items()} if s else {}}
        metrics = []
        for m in POSITION_METRICS[position]:
            d = METRICS[m]
            v, why = cut["metrics"].get(defteam, {}).get(m, (None, "no presumed-complete games sampled"))
            rk, pool = cut["ranks"][m]
            rank = rk.get(defteam)
            enough = s is not None and s["games"] >= MIN_DEF_GAMES
            reason = why or (None if enough else f"only {base['games']} presumed-complete game(s) sampled "
                                                 f"(< {MIN_DEF_GAMES})")
            metrics.append({"metric": m, "label": d["label"], "value": v,
                            "numerator": s[d["numerator"]] if s and not why else None,
                            "denominator": s[d["denominator"]] if s and not why else None,
                            "rank": rank if enough else None, "ranked_defenses": pool,
                            "band": band(rank if enough else None, pool) if v is not None else "insufficient",
                            "missing_reason": reason})
            if m.endswith("target_share") and s and not why:
                attributed = sum(s[c] for c in ("rb_targets", "wr_targets", "te_targets"))
                metrics[-1]["position_coverage"] = attributed / s["targets"] if s["targets"] else None
        primary = next(x for x in metrics if x["metric"] == PRIMARY_METRIC[position])
        label = primary["band"]
        return {**base, "metrics": metrics, "primary_metric": primary["metric"], "label": label,
                "label_text": LABELS[label], "sentence": _sentence(defteam, primary, base, before_week)}


def _fmt(metric: str, v: float) -> str:
    return f"{v:+.3f}" if METRICS[metric]["kind"] == "mean" else f"{v:.1%}"


def _sentence(defteam: str, p: dict, base: dict, before_week: int) -> str:
    m = METRICS[p["metric"]]
    if p["value"] is None:
        return f"{defteam}: {m['label']} unavailable before week {before_week} ({p['missing_reason']})."
    noun = "pass plays" if p["metric"] == "pass_epa_per_play" else "designed rushes"
    s = (f"{defteam}: offenses averaged {_fmt(p['metric'], p['value'])} EPA per "
         f"{'eligible pass play' if noun == 'pass plays' else 'designed rush'} against this defense through week "
         f"{base['through_week']} ({p['denominator']} {noun} across {base['games']} presumed-complete game"
         f"{'s' if base['games'] != 1 else ''})")
    if p["rank"] is not None:
        s += f"; {_ordinal(p['rank'])} most permissive of {p['ranked_defenses']} defenses with >= {MIN_DEF_GAMES} games"
    elif p["missing_reason"]:
        s += f"; not ranked: {p['missing_reason']}"
    return s + "."


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# ------------------------------------------------------------------- league --

def league_info(con, season: int | None = None) -> dict | None:
    lg = fetch_dicts(con, "SELECT league_id, season, name, current_week, final_scoring_period, reg_season_matchups, "
                          "settings_raw, synced_at FROM leagues" + (" WHERE season = ?" if season else "")
                     + " ORDER BY synced_at DESC LIMIT 1", [season] if season else [])
    if not lg or not lg[0]["current_week"] or not lg[0]["final_scoring_period"]:
        return None
    lg = lg[0]
    sched = (_json(lg.pop("settings_raw")) or {}).get("scheduleSettings") or {}
    final = int(lg["final_scoring_period"])
    lg["last_regular_season_week"] = last_regular_season_week(lg["reg_season_matchups"], final, sched)
    period_of = {}
    for mp, periods in (sched.get("matchupPeriods") or {}).items():
        for p in periods:
            period_of[int(p)] = int(mp)
    lg["matchup_period_of"] = period_of
    return lg


def _phase(week: int, last_regular: int | None) -> str | None:
    if last_regular is None:
        return None
    return "regular" if week <= last_regular else "playoffs"


def _freshness(season: int, runs: dict, defense_rows: list[dict]) -> dict:
    """When each input this view depends on was last refreshed, from nfl_input_runs: a newer sync of
    another season, or one that refreshed only other NFL tables, is not evidence that they are current."""
    def run(info: dict | None) -> dict | None:
        return {"fetched_at": _iso(info["started_at"]), "refreshed_at": _iso(info["finished_at"])} if info else None

    latest = runs["latest"]
    return {"season": season,
            "nfl_schedules": run(runs["schedules"]), "nfl_defense_week": run(runs["defense"]),
            "latest_nfl_sync": {**run(latest), "refreshed_nfl_schedules": latest["refreshed"]["schedules"],
                                "refreshed_nfl_defense_week": latest["refreshed"]["defense"]} if latest else None,
            "defense_weeks_loaded": sorted({r["week"] for r in defense_rows}),
            "source": "nflverse play-by-play aggregated per defense and week (nfl_defense_week) + nflverse schedules"}


# ------------------------------------------------------------------ players --

def _roster(con, lg: dict, team_id: int) -> list[dict]:
    return fetch_dicts(con, """
        SELECT fp.espn_player_id, fp.full_name AS name, fp.position, fp.pro_team, fp.injury_status,
               l.lineup_slot_id
        FROM lineup_snapshots l JOIN fantasy_players fp USING (espn_player_id)
        WHERE l.league_id = ? AND l.season = ? AND l.scoring_period = ? AND l.team_id = ?
        ORDER BY fp.espn_player_id""", [lg["league_id"], lg["season"], min(int(lg["current_week"]),
                                                                          int(lg["final_scoring_period"])), team_id])


def _identity(con, season: int, pids: list[int]) -> dict[int, dict]:
    """ESPN->GSIS mapping confidence and the latest nflverse weekly-roster team, to flag team changes."""
    if not pids:
        return {}
    ph = ", ".join("?" * len(pids))
    ids = {r["espn_player_id"]: r for r in fetch_dicts(
        con, f"SELECT espn_player_id, gsis_id, match_confidence FROM player_id_map WHERE espn_player_id IN ({ph})",
        pids)} if table_exists(con, "player_id_map") else {}
    latest = {}
    gs = sorted({r["gsis_id"] for r in ids.values() if r["gsis_id"]})
    if gs and table_exists(con, "nfl_rosters_weekly"):
        ph2 = ", ".join("?" * len(gs))
        for r in fetch_dicts(con, f"""SELECT gsis_id, team, week FROM nfl_rosters_weekly WHERE season = ?
                                      AND gsis_id IN ({ph2}) QUALIFY row_number() OVER
                                      (PARTITION BY gsis_id ORDER BY week DESC) = 1""", [season, *gs]):
            latest[r["gsis_id"]] = r
    out = {}
    for pid in pids:
        m = ids.get(pid) or {}
        lt = latest.get(m.get("gsis_id")) if m.get("gsis_id") else None
        out[pid] = {"gsis_id": m.get("gsis_id"), "match_confidence": m.get("match_confidence") or "none",
                    "nflverse_latest_team": lt["team"] if lt else None,
                    "nflverse_latest_week": lt["week"] if lt else None}
    return out


def player_schedule(p: dict, ident: dict, weeks: list[int], sched: dict, defs: DefenseCutoffs, lg: dict,
                    start_week: int | None) -> dict:
    team, pos = p["pro_team"], p["position"]
    notes = []
    if not team:
        notes.append("No current NFL team in ESPN (free agent or unknown): no schedule can be assigned.")
    if ident["match_confidence"] not in ("high",):
        notes.append(f"ESPN-to-nflverse ID mapping confidence: {ident['match_confidence']}. The schedule uses ESPN's "
                     "team, which does not depend on the mapping.")
    lt = ident["nflverse_latest_team"]
    if team and lt and lt != team:
        notes.append(f"ESPN lists {team}; the latest nflverse weekly roster (week {ident['nflverse_latest_week']}) "
                     f"lists {lt}. The ESPN team is used; verify a recent move.")
    rows = []
    for w in weeks:
        base = {"week": w, "phase": _phase(w, lg["last_regular_season_week"]),
                "matchup_period": lg["matchup_period_of"].get(w),
                "actionable": start_week is not None and w >= start_week}
        if not base["actionable"]:
            base["window_note"] = "current week already under way: informational only"
        if not team:
            rows.append({**base, "status": "team_unknown", "label": "unavailable", "label_text": LABELS["unavailable"],
                         "reason": "no current NFL team"})
            continue
        if w not in sched["covered"]:
            rows.append({**base, "status": "schedule_missing", "label": "unavailable",
                         "label_text": LABELS["unavailable"], "reason": f"no NFL REG schedule loaded for week {w}"})
            continue
        g = sched["games"].get((team, w))
        if g is None:
            rows.append({**base, "status": "bye", "label": "bye", "label_text": LABELS["bye"], "opponent": None,
                         "reason": f"{team} has no game in week {w}"})
            continue
        ev = defs.describe(g["opponent"], w, pos)
        rows.append({**base, "status": "scheduled", "opponent": g["opponent"], "home": g["home"],
                     "game_id": g["game_id"], "kickoff": g["kickoff"], "game_presumed_final": g["presumed_final"],
                     "label": ev["label"], "label_text": ev["label_text"], "evidence": ev})
    return {"espn_player_id": p["espn_player_id"], "name": p["name"], "position": pos, "pro_team": team,
            "injury_status": p.get("injury_status"), "on_ir": p.get("lineup_slot_id") == IR_SLOT,
            "identity": ident, "notes": notes, "weeks": rows, "summary": _summaries(rows)}


def _summaries(rows: list[dict]) -> dict:
    """Counts of weeks per label for each window: descriptive tallies, never a composite score."""
    act = [r for r in rows if r["actionable"]]
    windows = {
        "next": act[:1],
        "regular": [r for r in act if r["phase"] == "regular"],
        "playoffs": [r for r in act if r["phase"] == "playoffs"],
        "all": act,
    }
    out = {}
    for name, rs in windows.items():
        counts = {k: 0 for k in LABELS}
        for r in rs:
            counts[r["label"]] += 1
        out[name] = {"weeks": [r["week"] for r in rs], "counts": counts,
                     "opponents": [r.get("opponent") or ("BYE" if r["label"] == "bye" else None) for r in rs]}
    return out


# ---------------------------------------------------------------------- API --

def build_schedule_intelligence(con, team_id: int | None = None, position: str = "all", phase: str = "all",
                                player_id: int | None = None, now: datetime | None = None) -> dict:
    """Read-only. Raises NotSynced, NotFound or ValueError (bad filter)."""
    if position not in ("all", *SKILL_POSITIONS):
        raise ValueError(f"position must be one of all, {', '.join(SKILL_POSITIONS)}")
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {', '.join(PHASES)}")
    lg = league_info(con)
    if lg is None:
        raise NotSynced("ESPN league not synced yet (current/final scoring period unknown).")
    season = lg["season"]
    teams = fetch_dicts(con, "SELECT team_id, name, abbrev, is_mine FROM fantasy_teams WHERE league_id = ? AND "
                             "season = ? ORDER BY team_id", [lg["league_id"], season])
    if team_id is None and player_id is None:
        mine = [t for t in teams if t["is_mine"]]
        if not mine:
            raise NotFound("No team is marked as yours (is_mine); pass team_id.")
        team_id = mine[0]["team_id"]
    if team_id is not None and team_id not in {t["team_id"] for t in teams}:
        raise NotFound(f"Unknown team {team_id}")

    current, final = int(lg["current_week"]), int(lg["final_scoring_period"])
    window = evaluation_start_week(con, season, current, final, now)
    start = window["evaluation_start_week"]
    first = min(current, final)
    weeks = [w for w in range(first, final + 1)
             if phase == "all" or _phase(w, lg["last_regular_season_week"]) == phase]

    runs = nfl_input_runs(con, season)
    fetches = {k: runs[k]["started_at"] if runs[k] else None for k in ("schedules", "defense")}
    sched = load_schedule(con, season, fetches)
    defense_rows = fetch_dicts(con, "SELECT * FROM nfl_defense_week WHERE season = ? ORDER BY week, defteam",
                               [season]) if table_exists(con, "nfl_defense_week") else []
    defs = DefenseCutoffs(defense_rows, sched["presumed_complete"], sched["excluded"])
    reference = (min(fetches["schedules"], fetches["defense"]) - FETCH_MARGIN
                 if fetches["schedules"] and fetches["defense"] else None)

    if player_id is not None:
        players = fetch_dicts(con, "SELECT espn_player_id, full_name AS name, position, pro_team, injury_status, "
                                   "NULL AS lineup_slot_id FROM fantasy_players WHERE espn_player_id = ?", [player_id])
        if not players:
            raise NotFound(f"Unknown ESPN player {player_id}")
        if players[0]["position"] not in SKILL_POSITIONS:
            raise ValueError("schedule intelligence covers QB, RB, WR and TE")
    else:
        players = [p for p in _roster(con, lg, team_id) if p["position"] in SKILL_POSITIONS
                   and (position == "all" or p["position"] == position)]
    order = {p: i for i, p in enumerate(SKILL_POSITIONS)}
    players.sort(key=lambda p: (order[p["position"]], p["name"] or "", p["espn_player_id"]))
    ident = _identity(con, season, [p["espn_player_id"] for p in players])
    out_players = [player_schedule(p, ident[p["espn_player_id"]], weeks, sched, defs, lg, start) for p in players]

    return {
        "league": {k: lg[k] for k in ("league_id", "season", "name", "current_week", "final_scoring_period",
                                      "last_regular_season_week")},
        "team": next((t for t in teams if t["team_id"] == team_id), None), "teams": teams,
        "query": {"team_id": team_id, "position": position, "phase": phase, "player_id": player_id},
        "window": {"espn_current_week": current, "first_actionable_week": start, "weeks": weeks,
                   "remaining_weeks": start is not None, "uncertain": window.get("uncertain"),
                   "warning": window.get("warning"), "reason": window.get("reason"),
                   "regular_season_through": lg["last_regular_season_week"],
                   "playoff_weeks": [w for w in range(first, final + 1)
                                     if _phase(w, lg["last_regular_season_week"]) == "playoffs"],
                   "matchup_periods": {str(w): lg["matchup_period_of"].get(w) for w in range(first, final + 1)}},
        "schedule": {"available": sched["available"], "has_scores": sched["has_scores"],
                     "weeks_covered": sorted(sched["covered"]),
                     "games_by_completion": sched["game_counts"],
                     "schedules_fetched_at": _iso(fetches["schedules"]),
                     "defense_rows_fetched_at": _iso(fetches["defense"]),
                     "completion_reference_time": _iso(reference),
                     "completion_rule": COMPLETION_RULE, "completion_limits": COMPLETION_LIMITS,
                     "warnings": completion_warnings(season, runs, sched, defense_rows)},
        "freshness": _freshness(season, runs, defense_rows),
        "players": out_players,
        "metrics": {m: {k: d[k] for k in ("label", "numerator", "denominator", "unit", "definition", "positions")}
                    for m, d in METRICS.items()},
        "primary_metric": PRIMARY_METRIC,
        "labels": LABELS, "label_rule": LABEL_RULE,
        "notes": [TEAM_SNAPSHOT_NOTE, OBSERVATION_NOTE,
                  "No lookahead: each week uses only the opponent's games presumed complete before that week. " + COMPLETION_RULE,
                  "Receiver positions for target shares come from each receiver's latest nflverse roster position this "
                  "season (FB counted as RB); targets to other or unidentified positions count only in total targets "
                  "(see position coverage).",
                  "Not available from current data: passing success rate allowed, pressure, coverage scheme, slot/outside "
                  "alignment, cornerback assignments, routes."],
    }
