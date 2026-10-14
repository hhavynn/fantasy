"""Position-specific schedule intelligence (Phase A: descriptive, read-only).

For every remaining fantasy scoring period of a player's current NFL team: the scheduled opponent,
home/away or bye, and what that opponent's defense has actually allowed in games completed before
the matchup. Computed on request from existing tables (nfl_schedules, nfl_defense_week, ESPN league
and roster tables); nothing is written, synced or fetched. See docs/v2_schedule_intelligence.md.

What it is not: a projection. No number here changes ESPN projections, player scores, BUY/HOLD/SELL,
waivers, manager scouting, the trade evaluator or trade discovery, and nothing estimates a player's
points or share of volume. A defense's observed allowance mixes defensive quality with the
offenses, game scripts and luck it has faced.

Rules:
  * No lookahead: a matchup in week W uses only the opponent's games in weeks < W.
  * Completed games only: a defensive week counts only when its game is verified complete (both
    scores present and the NFL data fetched >= 8 h after kickoff; see game_completion). Rows of a
    game in progress, possibly live scores, or games the schedule cannot confirm are excluded and
    listed with the reason.
  * Missing is never zero: a metric with no eligible denominator is NULL with a reason; a real 0 is 0.
  * Ranks are among defenses with at least MIN_DEF_GAMES completed games under the same cutoff;
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

COMPLETION_RULE = ("A defensive week counts only when its game is verified complete: both scores in the NFL schedule, and "
                   "both the schedule and the defensive play-by-play aggregates fetched (by successful NFL syncs for this "
                   "season that wrote them) at least 8 hours after kickoff; nflverse does not document whether a score can "
                   "be live. Unverified games are excluded and listed with the reason.")

LABELS = {
    "permissive": "Observed more permissive",
    "middle": "Middle of the league",
    "tougher": "Observed tougher",
    "insufficient": "Insufficient evidence",
    "unavailable": "Unavailable",
    "bye": "Bye",
}
LABEL_RULE = (f"Band from the position's primary metric, ranked among defenses with >= {MIN_DEF_GAMES} completed "
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

# nflverse documents scores only as "NA for games which haven't yet been played"; it does not say
# whether a score can be a live, in-progress score. So a score alone is never proof of a final.
# A game counts as complete when both scores are present AND the NFL data was fetched at least
# FINAL_AFTER_KICKOFF after the scheduled kickoff: no NFL game (overtime, weather delays included)
# lasts that long, so such a score can only be the final one. The fetch time is the earlier of the
# latest successful NFL syncs (for the league's season) that wrote the schedule and that wrote the
# defensive aggregates, each recorded right after the nflverse downloads, less FETCH_MARGIN for the
# download itself. Anything else is unverified and excluded, with its reason.
FINAL_AFTER_KICKOFF = timedelta(hours=8)
FETCH_MARGIN = timedelta(hours=1)


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def nfl_fetch_times(con, season: int) -> dict:
    """When the stored inputs were fetched: for the league's season, the latest successful NFL sync
    run that actually wrote nfl_schedules and, separately, the latest one that wrote nfl_defense_week
    (a run can succeed with the play-by-play download failed, leaving older defensive rows in place;
    sync stats only list the tables a run wrote). Run starts are naive UTC in the database."""
    out = {"schedules": None, "defense": None}
    if not table_exists(con, "sync_runs"):
        return out
    for run in fetch_dicts(con, "SELECT started_at, stats FROM sync_runs WHERE source = 'nfl' AND status = 'ok' "
                                "AND started_at IS NOT NULL ORDER BY started_at DESC"):
        stats = _json(run["stats"])
        if stats.get("season") != season:
            continue
        if out["schedules"] is None and "nfl_schedules" in stats:
            out["schedules"] = _utc(run["started_at"])
        if out["defense"] is None and "nfl_defense_week" in stats:
            out["defense"] = _utc(run["started_at"])
        if out["schedules"] and out["defense"]:
            break
    return out


def nfl_fetch_time(con, season: int) -> datetime | None:
    """The fetch time the completion rule uses: the earlier of the schedule and the defensive-rows
    fetches (both must postdate the game), or None when either is unknown."""
    t = nfl_fetch_times(con, season)
    return min(t["schedules"], t["defense"]) if t["schedules"] and t["defense"] else None


def game_completion(h, a, kickoff: datetime | None, fetched_at: datetime | None) -> tuple[bool, str | None]:
    """(complete, reason when not). A 0-0 or a shutout counts once the timing rule holds."""
    if h is None or a is None:
        return False, "no score in the NFL schedule (not played or not yet published)"
    if kickoff is None:
        return False, "score present but the kickoff time is unknown, so it may be a live score"
    if fetched_at is None:
        return False, "score present but the NFL data fetch time is unknown, so it may be a live score"
    lag = fetched_at - FETCH_MARGIN - kickoff
    if lag < FINAL_AFTER_KICKOFF:
        hours = max(lag.total_seconds() / 3600, 0)
        return False, (f"score fetched about {hours:.1f} h after kickoff (< {FINAL_AFTER_KICKOFF.seconds // 3600} h), "
                       "so it may be a live score")
    return True, None


def load_schedule(con, season: int, fetched_at: datetime | None = None) -> dict:
    """REG games of the season: (team, week) -> game info, the weeks covered, the set of (team, week)
    whose game is verified complete (game_completion), and the reason for every unverified game."""
    out = {"games": {}, "covered": set(), "completed": set(), "unverified": {}, "available": False,
           "has_scores": False, "fetched_at": fetched_at.isoformat() if fetched_at else None}
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
        final, why = game_completion(g.get("home_score"), g.get("away_score"), ko, fetched_at)
        for team, opp, home in ((g["home_team"], g["away_team"], True), (g["away_team"], g["home_team"], False)):
            out["games"][(team, w)] = {"opponent": opp, "home": home, "game_id": g.get("game_id"),
                                       "kickoff": ko.isoformat() if ko else None, "final": final}
            if final:
                out["completed"].add((team, w))
            else:
                out["unverified"][(team, w)] = why
    return out


# --------------------------------------------------------- defense samples --

def defense_samples(rows: list[dict], completed: set, before_week: int,
                    reasons: dict | None = None) -> dict[str, dict]:
    """Per defense: sums over its games in weeks < before_week whose game is verified complete.
    Rows of unverified games (no score, possibly live score, unknown timing, not in the schedule) are
    excluded and listed with their reason, never silently used."""
    reasons = reasons or {}
    out: dict[str, dict] = {}
    for r in rows:
        if r["week"] >= before_week:
            continue
        d = out.setdefault(r["defteam"], {"games": 0, "weeks": [], "excluded_unverified": [],
                                          "excluded_reasons": {}, **{c: 0 for c in SUM_COLUMNS},
                                          "missing": defaultdict(int)})
        if (r["defteam"], r["week"]) not in completed:
            d["excluded_unverified"].append(r["week"])
            d["excluded_reasons"][r["week"]] = reasons.get((r["defteam"], r["week"]),
                                                           "game not found in the NFL schedule")
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
        return None, "no completed games sampled"
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

    def __init__(self, rows: list[dict], completed: set, reasons: dict | None = None):
        self.rows, self.completed, self.reasons = rows, completed, reasons or {}
        self._cache: dict[int, dict] = {}

    def at(self, before_week: int) -> dict:
        if before_week in self._cache:
            return self._cache[before_week]
        samples = defense_samples(self.rows, self.completed, before_week, self.reasons)
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
                "excluded_unverified_weeks": s["excluded_unverified"] if s else [],
                "excluded_unverified_reasons": {str(w): why for w, why in s["excluded_reasons"].items()} if s else {}}
        metrics = []
        for m in POSITION_METRICS[position]:
            d = METRICS[m]
            v, why = cut["metrics"].get(defteam, {}).get(m, (None, "no completed games sampled"))
            rk, pool = cut["ranks"][m]
            rank = rk.get(defteam)
            enough = s is not None and s["games"] >= MIN_DEF_GAMES
            reason = why or (None if enough else f"only {base['games']} completed game(s) sampled "
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
         f"{base['through_week']} ({p['denominator']} {noun} across {base['games']} completed game"
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


def _freshness(con, season: int, defense_rows: list[dict]) -> dict:
    run = fetch_dicts(con, "SELECT finished_at FROM sync_runs WHERE source = 'nfl' AND status = 'ok' "
                           "ORDER BY started_at DESC LIMIT 1") if table_exists(con, "sync_runs") else []
    return {"nfl_sync_finished_at": run[0]["finished_at"].isoformat() if run and run[0]["finished_at"] else None,
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
                     "game_id": g["game_id"], "kickoff": g["kickoff"], "game_final": g["final"],
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

    fetches = nfl_fetch_times(con, season)
    sched = load_schedule(con, season, nfl_fetch_time(con, season))
    defense_rows = fetch_dicts(con, "SELECT * FROM nfl_defense_week WHERE season = ? ORDER BY week, defteam",
                               [season]) if table_exists(con, "nfl_defense_week") else []
    defs = DefenseCutoffs(defense_rows, sched["completed"], sched["unverified"])

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
                     "weeks_covered": sorted(sched["covered"]), "completed_team_games": len(sched["completed"]),
                     "unverified_team_games_with_score": sum(1 for why in sched["unverified"].values()
                                                             if why and not why.startswith("no score")),
                     "nfl_data_fetched_at": sched["fetched_at"],
                     "schedules_fetched_at": fetches["schedules"].isoformat() if fetches["schedules"] else None,
                     "defense_rows_fetched_at": fetches["defense"].isoformat() if fetches["defense"] else None,
                     "completion_rule": COMPLETION_RULE},
        "freshness": _freshness(con, season, defense_rows),
        "players": out_players,
        "metrics": {m: {k: d[k] for k in ("label", "numerator", "denominator", "unit", "definition", "positions")}
                    for m, d in METRICS.items()},
        "primary_metric": PRIMARY_METRIC,
        "labels": LABELS, "label_rule": LABEL_RULE,
        "notes": [TEAM_SNAPSHOT_NOTE, OBSERVATION_NOTE,
                  "No lookahead: each week uses only the opponent's games completed before that week. " + COMPLETION_RULE,
                  "Receiver positions for target shares come from each receiver's latest nflverse roster position this "
                  "season (FB counted as RB); targets to other or unidentified positions count only in total targets "
                  "(see position coverage).",
                  "Not available from current data: passing success rate allowed, pressure, coverage scheme, slot/outside "
                  "alignment, cornerback assignments, routes."],
    }
