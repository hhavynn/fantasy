"""Playoff Race Lab (read-only): actual standings and all-play, projected remaining matchups, one
deterministic projected-standings scenario, the playoff cutline and a scenario explorer with
explicit, reversible winner overrides. See docs/v2_playoff_race.md.

Rules:
  * Actual results come from ESPN: team records (fantasy_teams) and matchups whose ESPN `winner`
    is HOME/AWAY/TIE. A matchup without a winner is never a result.
  * Forward numbers reuse the canonical engine unchanged: ESPN weekly projections and the weekly
    optimal lineup (team_lineups.project_team, current-IR players inactive), loaded by
    trade_data.load_league_state. A matchup's projected total is the sum of its scoring periods'
    optimized weekly totals (multi-week matchups from ESPN scheduleSettings.matchupPeriods).
  * Missing is not zero: if either side has an incomplete week (a lineup-relevant player with an NFL
    team and no projection, or no roster snapshot), the matchup has no projected winner and stays
    unresolved; known-only totals are shown as such. A projected 0 is a real value.
  * Only matchups whose every scoring period is at or after the first actionable week (kickoff-aware,
    trade_data.evaluation_start_week) are projected; a matchup already under way stays unresolved
    unless the user overrides it.
  * The projected scenario applies the higher ESPN-projected side as a hypothetical winner. It is a
    deterministic scenario, not a probability or a prediction.
  * Standings order by winning percentage (wins + half ties over games). No tiebreak rule is applied:
    ESPN's raw seeding/tie settings are reported but their vocabulary is not verified here, so teams
    with equal records share a rank and a tie across the cutline is shown as unresolved. Divisions
    are not modeled. No clinch or elimination is claimed.
  * What-if (issue #192): only when the user sets at least one result does the answer carry a
    `whatif` block (inputs, assumptions, deterministic method with no sampling, completeness,
    changes against the same baseline without the user's results, and exact record bounds behind
    a conclusion gate). With no result set the answer is unchanged.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

from ..db.connection import fetch_dicts, table_exists
from .projections import last_regular_season_week
from .lineup import slots_left_out
from .team_lineups import project_team, r
from .trade_data import load_league_state
from .trades import LeagueState

FINAL_WINNERS = {"HOME": "home", "AWAY": "away", "TIE": "tie"}
OUTCOMES = ("home", "away", "tie", "none")       # "none": force a game unresolved
BASES = ("projected", "actual")
# Display rule for "close" projected matchups: |margin| <= 5% of the two sides' mean projected total.
# A descriptive threshold, not a probability.
CLOSE_SHARE = 0.05
MAX_EXHAUSTIVE_GAMES = 12                        # path search over own games; greedy above this

ORDERING_RULE = ("Standings are ordered by winning percentage: (wins + half of ties) / games. This is the usual ESPN "
                 "head-to-head order but it is not read from a league setting; ESPN's own current playoff seeds are "
                 "shown next to it. No tiebreak rule is applied: teams with the same winning percentage share a rank.")
PROJECTION_RULE = ("Projected winner of a remaining matchup = the side with the higher ESPN-projected optimal starting "
                   "lineup total (each scoring period optimized separately with the league's lineup slots; current IR "
                   "players inactive). Only matchups whose scoring periods have not kicked off are projected, and only "
                   "when both sides' projections are complete. This is a deterministic scenario, not a probability.")
CLOSE_RULE = (f"A projected matchup is marked close (a potential swing game) when the projected margin is at most "
              f"{CLOSE_SHARE:.0%} of the two sides' mean projected total. A display threshold, not a probability.")
CLINCH_NOTE = ("No team is described as clinched or eliminated: that needs the league's full seeding and tiebreak rules "
               "(and division rules), which are not verifiably available.")
# Tiebreak / seeding settings whose meaning has been verified and can be applied. None yet: every reported ESPN value
# stays unmapped, which keeps clinch and elimination conclusions blocked (issue #192).
MAPPED_TIEBREAK_RULES: dict[str, set] = {}
WHATIF_METHOD = ("Deterministic: one exact scenario computed from the results listed in its inputs. No random sampling, "
                 "so there is no seed, no sample of simulated seasons and no probability.")
BOUNDS_RULE = ("Record bounds: each team's record if it wins, and if it loses, every open game, with your set results "
               "fixed and every other remaining game open (projections are not used). They are exact under the record "
               "order above and are never turned into a clinch or elimination claim.")


class NotSynced(LookupError):
    """No ESPN league, current/final week or teams in the database."""


class NotFound(LookupError):
    """Unknown team, or no team is marked as yours."""


def _json(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


@dataclass
class RaceData:
    """Everything the race needs, loaded once (load_race_data) or built directly in tests."""
    league: dict            # league_id season name current_week current_matchup_period final_scoring_period
                            # reg_season_matchups playoff_teams last_regular_season_week
    schedule_settings: dict
    rules_raw: dict         # ESPN seeding / tie settings as reported (never applied)
    teams: list[dict]       # fantasy_teams rows
    matchups: list[dict]    # matchups rows
    state: LeagueState      # canonical rosters, projections, slots and first actionable week
    window: dict            # trade_data.evaluation_start_week output


# ------------------------------------------------------------------- loading --

def load_race_data(con, now: datetime | None = None) -> RaceData:
    state = load_league_state(con, now=now)
    if state is None:
        raise NotSynced("ESPN league not synced yet (current/final scoring period unknown).")
    lg = fetch_dicts(con, """SELECT league_id, season, name, size, current_week, current_matchup_period,
                                    final_scoring_period, reg_season_matchups, playoff_teams, settings_raw
                             FROM leagues WHERE league_id = ? AND season = ?""", [state.league_id, state.season])[0]
    settings = _json(lg.pop("settings_raw"))
    sched = settings.get("scheduleSettings") or {}
    scoring = settings.get("scoringSettings") or {}
    lg["last_regular_season_week"] = last_regular_season_week(lg["reg_season_matchups"], lg["final_scoring_period"],
                                                              sched)
    rules = {k: v for k, v in (("scheduleSettings.playoffSeedingRule", sched.get("playoffSeedingRule")),
                               ("scheduleSettings.playoffSeedingRuleBy", sched.get("playoffSeedingRuleBy")),
                               ("scoringSettings.matchupTieRule", scoring.get("matchupTieRule")),
                               ("scoringSettings.matchupTieRuleBy", scoring.get("matchupTieRuleBy")),
                               ("scoringSettings.playoffMatchupTieRule", scoring.get("playoffMatchupTieRule")))
             if v is not None}
    teams = fetch_dicts(con, """SELECT team_id, name, abbrev, is_mine, division_id, wins, losses, ties, points_for,
                                       points_against, playoff_seed FROM fantasy_teams
                                WHERE league_id = ? AND season = ? ORDER BY team_id""", [state.league_id, state.season])
    if not teams:
        raise NotSynced("No fantasy teams synced for the league.")
    matchups = fetch_dicts(con, """SELECT matchup_id, matchup_period, home_team_id, away_team_id, home_points,
                                          away_points, winner, playoff_tier FROM matchups
                                   WHERE league_id = ? AND season = ? ORDER BY matchup_period, matchup_id""",
                           [state.league_id, state.season]) if table_exists(con, "matchups") else []
    return RaceData(league=lg, schedule_settings=sched, rules_raw=rules, teams=teams, matchups=matchups,
                    state=state, window=state.start_week_reason or {})


# ------------------------------------------------------------------ schedule --

def period_weeks(sched: dict, period: int | None) -> list[int] | None:
    """Scoring periods of a matchup period: ESPN scheduleSettings.matchupPeriods, else the period
    itself when matchups last one week (or the length is unreported); otherwise unknown."""
    if period is None:
        return None
    periods = (sched.get("matchupPeriods") or {}).get(str(period))
    if periods:
        return sorted(int(p) for p in periods)
    if (sched.get("matchupPeriodLength") or 1) == 1:
        return [int(period)]
    return None


def is_regular(m: dict, reg_periods: int | None) -> bool:
    if m.get("playoff_tier") not in (None, "NONE"):
        return False
    return not (reg_periods and m.get("matchup_period") and m["matchup_period"] > reg_periods)


def _side(team: dict | None, weeks: list[int], names: dict[int, str]) -> dict:
    """One team's projected total over a matchup's scoring periods (canonical weekly lineups)."""
    if team is None:
        return {"projected_points": None, "known_points": None, "complete": False, "weekly": [],
                "missing_projection_ids": [], "missing_players": [], "gap": "no roster snapshot for this team", "_total": None}
    by_week = {w["week"]: w for w in team["weeks"]}
    ws = [by_week.get(w) for w in weeks]
    if any(w is None for w in ws):
        return {"projected_points": None, "known_points": None, "complete": False, "weekly": [],
                "missing_projection_ids": [], "missing_players": [],
                "gap": "scoring period outside the projection window", "_total": None}
    known = sum(w["_total"] for w in ws)
    complete = all(w["projection_complete"] for w in ws)
    unsupported = next((w["unsupported_slots"] for w in ws if w.get("unsupported_slots")), None)
    missing = sorted({pid for w in ws for pid in w["missing_projection_ids"]})
    return {"projected_points": r(known) if complete else None, "known_points": r(known), "complete": complete,
            "weekly": [{"week": w["week"], "projected_lineup_points": w["projected_lineup_points"],
                        "known_lineup_points": w["known_lineup_points"], "projection_complete": w["projection_complete"]}
                       for w in ws],
            "missing_projection_ids": missing, "missing_players": [names.get(p) or str(p) for p in missing],
            "gap": None if complete else
            f"{len(missing)} lineup-relevant player(s) without an ESPN projection" if missing or not unsupported else
            slots_left_out(unsupported),
            "_total": known if complete else None}


def classify_matchups(data: RaceData, team_proj: dict[int, dict | None]) -> tuple[list[dict], list[dict], list[int]]:
    """(regular-season matchups with status and projection, known playoff matchups, matchups ESPN
    sent without a matchup period: they cannot be placed in the schedule, so they are left out of
    every schedule-dependent grouping and reported; their final results still count for records)."""
    lg, sched, start = data.league, data.schedule_settings, data.state.start_week
    names = {p.espn_player_id: p.name for ps in data.state.rosters.values() for p in ps}
    team_names = {t["team_id"]: t["name"] for t in data.teams}
    regular, playoff, no_period = [], [], []
    for m in data.matchups:
        if m["matchup_period"] is None:
            no_period.append(m)
            continue
        weeks = period_weeks(sched, m["matchup_period"])
        row = {"matchup_id": m["matchup_id"], "matchup_period": m["matchup_period"], "weeks": weeks,
               "home_team_id": m["home_team_id"], "away_team_id": m["away_team_id"],
               "home_name": team_names.get(m["home_team_id"]), "away_name": team_names.get(m["away_team_id"]),
               "espn_winner": m["winner"], "playoff_tier": m["playoff_tier"]}
        if not is_regular(m, lg.get("reg_season_matchups")):
            playoff.append({**row, "home_points": m["home_points"], "away_points": m["away_points"]})
            continue
        h, a = m["home_team_id"], m["away_team_id"]
        if h is None or a is None:
            row.update(status="bye", detail="only one team listed (a bye or an unknown opponent): no result")
        elif m["winner"] in FINAL_WINNERS:
            row.update(status="final", result=FINAL_WINNERS[m["winner"]], home_points=m["home_points"],
                       away_points=m["away_points"], detail="final in ESPN")
        elif weeks is None:
            row.update(status="unmapped", detail="its scoring periods cannot be determined from league settings, so it "
                                                 "cannot be projected")
        elif start is None or min(weeks) < start:
            row.update(status="in_progress", home_points=m["home_points"], away_points=m["away_points"],
                       detail="under way or not yet final in ESPN (points are as of the last sync, not final): "
                              "unresolved unless overridden")
        else:
            hs, as_ = _side(team_proj.get(h), weeks, names), _side(team_proj.get(a), weeks, names)
            row.update(status="upcoming", home_projection=hs, away_projection=as_)
            if hs["_total"] is not None and as_["_total"] is not None:
                margin = hs["_total"] - as_["_total"]
                row["projected_margin"] = r(margin)
                mean = (hs["_total"] + as_["_total"]) / 2
                row["close"] = abs(margin) <= CLOSE_SHARE * mean
                row["weekly_margins"] = [{"week": x["week"], "margin": r(x["projected_lineup_points"]
                                                                          - y["projected_lineup_points"])}
                                         for x, y in zip(hs["weekly"], as_["weekly"])]
                if r(margin) == 0:
                    row.update(projected_winner=None, detail="equal projected totals: no projected winner")
                else:
                    row.update(projected_winner="home" if margin > 0 else "away", detail="projected from ESPN weekly lineups")
            else:
                row.update(projected_margin=None, close=None, projected_winner=None,
                           detail="projection incomplete for "
                                  + " and ".join(n for n, s in ((row["home_name"], hs), (row["away_name"], as_))
                                                 if s["_total"] is None) + ": unresolved")
            for s in (hs, as_):
                s.pop("_total")
        regular.append(row)
    return regular, playoff, no_period


# ------------------------------------------------------------------ records --

def _record(t: dict) -> tuple[int, int, int] | None:
    if t["wins"] is None or t["losses"] is None:
        return None
    return int(t["wins"]), int(t["losses"]), int(t["ties"] or 0)


def derived_records(regular: list[dict], team_ids: list[int]) -> dict[int, list[int]]:
    out = {t: [0, 0, 0] for t in team_ids}
    for m in regular:
        if m["status"] != "final":
            continue
        h, a, res = m["home_team_id"], m["away_team_id"], m["result"]
        for tid, won in ((h, res == "home"), (a, res == "away")):
            if tid in out:
                out[tid][2 if res == "tie" else (0 if won else 1)] += 1
    return out


def period_all_play(scores: dict[int, float]) -> dict[int, tuple[int, int, int]]:
    """One period's all-play: each team's (wins, losses, ties) against every other team's score that
    period. Shared by all_play here and by the Luck vs. Skill Lab."""
    return {tid: (sum(1 for o, v in scores.items() if o != tid and pts > v),
                  sum(1 for o, v in scores.items() if o != tid and pts < v),
                  sum(1 for o, v in scores.items() if o != tid and pts == v)) for tid, pts in scores.items()}


def all_play(regular: list[dict], team_ids: list[int]) -> dict[int, dict]:
    """Each final regular-season period: every team's score against every other team's score that
    period. Periods with fewer than two scored teams are skipped."""
    by_period: dict[int, dict[int, float]] = {}
    for m in regular:
        if m["status"] != "final":
            continue
        for tid, pts in ((m["home_team_id"], m["home_points"]), (m["away_team_id"], m["away_points"])):
            if tid is not None and pts is not None:
                by_period.setdefault(m["matchup_period"], {})[tid] = float(pts)
    out = {t: {"wins": 0, "losses": 0, "ties": 0, "periods": []} for t in team_ids}
    for period in sorted(by_period):
        scores = by_period[period]
        if len(scores) < 2:
            continue
        for tid, (w, l, t) in period_all_play(scores).items():
            if tid not in out:
                continue
            o = out[tid]
            o["periods"].append(period)
            o["wins"] += w
            o["losses"] += l
            o["ties"] += t
    for o in out.values():
        g = o["wins"] + o["losses"] + o["ties"]
        o["pct"] = round((o["wins"] + o["ties"] / 2) / g, 4) if g else None
    return out


# ---------------------------------------------------------------- scenarios --

def _pct(rec) -> Fraction | None:
    w, l, t = rec
    g = w + l + t
    return Fraction(2 * w + t, 2 * g) if g else None


def apply_results(base: dict[int, tuple], regular: list[dict], results: dict[int, str]) -> dict[int, dict]:
    """Base records plus every decided non-final game in `results` (matchup_id -> home/away/tie);
    other non-final two-team games count as unresolved for both teams."""
    recs = {tid: {"wins": rec[0], "losses": rec[1], "ties": rec[2], "unresolved": 0} if rec else None
            for tid, rec in base.items()}
    for m in regular:
        if m["status"] in ("final", "bye") or m["home_team_id"] is None or m["away_team_id"] is None:
            continue
        res = results.get(m["matchup_id"])
        for tid, side in ((m["home_team_id"], "home"), (m["away_team_id"], "away")):
            rec = recs.get(tid)
            if rec is None:
                continue
            if res is None:
                rec["unresolved"] += 1
            elif res == "tie":
                rec["ties"] += 1
            else:
                rec["wins" if res == side else "losses"] += 1
    return recs


def standings(recs: dict[int, dict | None], spots: int | None) -> list[dict]:
    """Rows ordered by winning percentage; equal percentages share a rank (no tiebreak applied).
    status vs the cutline: inside / outside / cutline_tie (a tie straddles the last spot)."""
    pct = {tid: (_pct((r_["wins"], r_["losses"], r_["ties"])) if r_ else None) for tid, r_ in recs.items()}
    order = sorted(recs, key=lambda t: (pct[t] is None, -(pct[t] or 0), t))
    rows = []
    for tid in order:
        rec, p = recs[tid], pct[tid]
        if p is None:
            rows.append({"team_id": tid, "record": rec, "pct": None, "rank": None, "tied": False, "status": None})
            continue
        better = sum(1 for o in pct.values() if o is not None and o > p)
        group = sum(1 for o in pct.values() if o is not None and o == p)
        status = None
        if spots:
            status = "inside" if better + group <= spots else "outside" if better >= spots else "cutline_tie"
        rows.append({"team_id": tid, "record": rec, "pct": round(float(p), 4), "rank": better + 1,
                     "tied": group > 1, "status": status})
    return rows


def _row(rows: list[dict], tid: int) -> dict:
    return next(x for x in rows if x["team_id"] == tid)


class Scenario:
    """Records, standings and what-ifs under one set of decided results."""

    def __init__(self, data: RaceData, regular: list[dict], base_records: dict, results: dict[int, str]):
        self.regular, self.base, self.results = regular, base_records, dict(results)
        self.spots = data.league.get("playoff_teams")
        self.open_games = [m for m in regular if m["status"] in ("upcoming", "in_progress", "unmapped")
                           and m["home_team_id"] is not None and m["away_team_id"] is not None]

    def table(self, results: dict[int, str] | None = None) -> list[dict]:
        return standings(apply_results(self.base, self.regular, self.results if results is None else results),
                         self.spots)

    def with_(self, changes: dict[int, str]) -> list[dict]:
        return self.table({**self.results, **changes})

    def own_games(self, tid: int) -> list[dict]:
        return [m for m in self.open_games if tid in (m["home_team_id"], m["away_team_id"])]


def _win_for(m: dict, tid: int) -> str:
    return "home" if m["home_team_id"] == tid else "away"


def _loss_for(m: dict, tid: int) -> str:
    return "away" if m["home_team_id"] == tid else "home"


def leverage(sc: Scenario, tid: int) -> list[dict]:
    """For each of a team's open games: rank and cutline status if it wins vs if it loses, every
    other result as in the scenario. High leverage = the two statuses differ."""
    out = []
    for m in sc.own_games(tid):
        win, loss = _row(sc.with_({m["matchup_id"]: _win_for(m, tid)}), tid), \
            _row(sc.with_({m["matchup_id"]: _loss_for(m, tid)}), tid)
        cur = sc.results.get(m["matchup_id"])
        out.append({"matchup_id": m["matchup_id"], "matchup_period": m["matchup_period"], "weeks": m["weeks"],
                    "opponent_id": m["away_team_id"] if m["home_team_id"] == tid else m["home_team_id"],
                    "status": m["status"], "close": m.get("close"),
                    "scenario_result": None if cur is None else "tie" if cur == "tie" else
                    ("win" if cur == _win_for(m, tid) else "loss"),
                    "if_win": {"rank": win["rank"], "status": win["status"]},
                    "if_loss": {"rank": loss["rank"], "status": loss["status"]},
                    "high_leverage": win["status"] != loss["status"]})
    return out


def _better(a: dict, b: dict) -> bool:
    order = {"inside": 2, "cutline_tie": 1, "outside": 0, None: -1}
    return (order[a["status"]], -(a["rank"] or 99)) > (order[b["status"]], -(b["rank"] or 99))


def pathways(sc: Scenario, tid: int) -> dict:
    """Deterministic routes for one team under the scenario: win out / lose out, the fewest own wins
    that put it inside (every other result as in the scenario), and other games whose flip helps."""
    games = sc.own_games(tid)
    now = _row(sc.table(), tid)
    win_out = _row(sc.with_({m["matchup_id"]: _win_for(m, tid) for m in games}), tid)
    lose_out = _row(sc.with_({m["matchup_id"]: _loss_for(m, tid) for m in games}), tid)
    fewest = None
    if games and sc.spots and win_out["status"] == "inside":
        def cost(combo):   # prefer games the scenario already gives the team, then the closest
            return (sum(1 for m in combo if sc.results.get(m["matchup_id"]) != _win_for(m, tid)),
                    sum(abs(m.get("projected_margin") or 0) for m in combo), [m["matchup_id"] for m in combo])
        if len(games) <= MAX_EXHAUSTIVE_GAMES:
            for k in range(len(games) + 1):
                hits = []
                for combo in itertools.combinations(games, k):
                    wins = {m["matchup_id"] for m in combo}
                    row = _row(sc.with_({m["matchup_id"]: _win_for(m, tid) if m["matchup_id"] in wins
                                         else _loss_for(m, tid) for m in games}), tid)
                    if row["status"] == "inside":
                        hits.append(combo)
                if hits:
                    best = min(hits, key=cost)
                    fewest = {"wins_needed": k, "of_games": len(games), "method": "exhaustive",
                              "games": [m["matchup_id"] for m in best]}
                    break
        else:
            fewest = {"wins_needed": None, "of_games": len(games), "method": "not searched",
                      "games": [], "note": f"more than {MAX_EXHAUSTIVE_GAMES} open games"}
    helpful = []
    for m in sc.open_games:
        if tid in (m["home_team_id"], m["away_team_id"]):
            continue
        for outcome in ("home", "away"):
            if sc.results.get(m["matchup_id"]) == outcome:
                continue
            row = _row(sc.with_({m["matchup_id"]: outcome}), tid)
            if _better(row, now):
                loser = m["away_team_id"] if outcome == "home" else m["home_team_id"]
                helpful.append({"matchup_id": m["matchup_id"], "matchup_period": m["matchup_period"],
                                "result": outcome, "loser_id": loser, "rank": row["rank"], "status": row["status"]})
    helpful.sort(key=lambda h: ({"inside": 0, "cutline_tie": 1}.get(h["status"], 2), h["rank"] or 99,
                                h["matchup_period"], h["matchup_id"], h["result"]))
    return {"scenario": {"rank": now["rank"], "status": now["status"], "record": now["record"]},
            "win_out": {"rank": win_out["rank"], "status": win_out["status"], "record": win_out["record"]},
            "lose_out": {"rank": lose_out["rank"], "status": lose_out["status"], "record": lose_out["record"]},
            "open_games": len(games), "fewest_wins_to_get_inside": fewest, "helpful_other_results": helpful[:12]}


# ------------------------------------------------------------------ what-if --
# Issue #192: when the user sets at least one result, the answer carries a `whatif` block that states the scenario's
# inputs, assumptions and method, compares it with the same baseline without the user's results, and reports record
# bounds behind a conclusion gate that never yields a clinch or elimination claim while any rule is unmapped.

def _place(row: dict) -> dict:
    return {"rank": row["rank"], "tied": row["tied"], "status": row["status"], "record": row["record"]}


def _unresolved_reason(m: dict, base: str, overrides: dict[int, str]) -> str:
    if overrides.get(m["matchup_id"]) == "none":
        return "set_unresolved"
    if m["status"] in ("in_progress", "unmapped"):
        return m["status"]
    if base == "actual":
        return "actual_baseline"
    return "projection_incomplete" if m.get("projected_margin") is None else "equal_projection"


def conclusion_gate(data: RaceData, divisions: int, seeds_disagree: bool) -> dict:
    """Which league rules a clinch or elimination conclusion would need, and whether each is mapped. A conclusion is
    only possible when nothing blocks it; today the record order itself is assumed, so it is always blocked."""
    raw = data.rules_raw
    mapped = bool(raw) and all(isinstance(v, (str, int)) and v in MAPPED_TIEBREAK_RULES.get(k, set())
                               for k, v in raw.items())
    blocked = []
    if not mapped:
        blocked.append({"rule": "tiebreak", "state": "unmapped", "reported": dict(raw),
                        "detail": "The league's tiebreak and seeding settings are not mapped to a verified rule"
                                  + (" (reported values are listed but not applied)." if raw else
                                     " (none were reported).")})
    blocked.append({"rule": "standings_order", "state": "assumed", "reported": {},
                    "detail": "Standings order by winning percentage is assumed, not read from a league setting."})
    if divisions > 1:
        blocked.append({"rule": "divisions", "state": "not_modeled", "reported": {"divisions": divisions},
                        "detail": f"{divisions} divisions: division-based playoff spots or seeding are not modeled."})
    if seeds_disagree:
        blocked.append({"rule": "seeding", "state": "conflicts", "reported": {},
                        "detail": "The provider's current seeds do not follow overall record order."})
    if not data.league.get("playoff_teams"):
        blocked.append({"rule": "playoff_spots", "state": "unknown", "reported": {},
                        "detail": "The number of playoff spots is not reported."})
    return {"status": "blocked" if blocked else "open", "blocked_by": blocked, "claims": []}


def record_bounds(sc: Scenario, fixed: dict[int, str], abstain: list[str]) -> dict:
    """Each team's best and worst record over every remaining outcome, with `fixed` results held. Exact under the
    record order: `above_line` when, even losing every open game, fewer other teams than there are spots can match its
    winning percentage; `below_line` when, even winning every open game, at least that many finish strictly ahead in
    every outcome. Never a claim (see conclusion_gate). Abstains (record_order None) on any reason in `abstain`."""
    team_ids = sorted(sc.base)
    open_games = [m for m in sc.open_games if m["matchup_id"] not in fixed]

    def bound(tid: int, wins_open: bool) -> tuple[int, int, int] | None:
        if sc.base[tid] is None:
            return None
        w, l, t = sc.base[tid]
        for m in sc.open_games:
            if tid not in (m["home_team_id"], m["away_team_id"]):
                continue
            res = fixed.get(m["matchup_id"]) or (_win_for(m, tid) if wins_open else _loss_for(m, tid))
            if res == "tie":
                t += 1
            elif res == _win_for(m, tid):
                w += 1
            else:
                l += 1
        return w, l, t

    best = {t: bound(t, True) for t in team_ids}
    worst = {t: bound(t, False) for t in team_ids}
    bp = {t: _pct(b) if b else None for t, b in best.items()}
    wp = {t: _pct(b) if b else None for t, b in worst.items()}
    as_rec = lambda x: None if x is None else {"wins": x[0], "losses": x[1], "ties": x[2]}   # noqa: E731
    rows = []
    for t in team_ids:
        row = {"team_id": t, "open_games": sum(1 for m in open_games if t in (m["home_team_id"], m["away_team_id"])),
               "best": as_rec(best[t]), "worst": as_rec(worst[t]), "teams_able_to_match_worst": None,
               "teams_certain_ahead_of_best": None, "record_order": None, "claim": None}
        if not abstain and wp[t] is not None and bp[t] is not None:
            level = sum(1 for o in team_ids if o != t and bp[o] is not None and bp[o] >= wp[t])
            ahead = sum(1 for o in team_ids if o != t and wp[o] is not None and wp[o] > bp[t])
            row.update(teams_able_to_match_worst=level, teams_certain_ahead_of_best=ahead)
            if sc.spots:
                row["record_order"] = ("above_line" if level < sc.spots else
                                       "below_line" if ahead >= sc.spots else "open")
        rows.append(row)
    return {"rule": BOUNDS_RULE, "fixed_results": len(fixed), "open_games": len(open_games), "abstain": abstain,
            "teams": rows}


def whatif_report(data: RaceData, sc: Scenario, regular: list[dict], base_records: dict, base: str,
                  baseline_results: dict[int, str], overrides: dict[int, str], *, coverage: dict,
                  no_period: list[dict], divisions: int, seeds_disagree: bool) -> dict:
    by_id = {m["matchup_id"]: m for m in regular}
    baseline_rows = Scenario(data, regular, base_records, baseline_results).table()
    base_by = {x["team_id"]: x for x in baseline_rows}
    changes = []
    for x in sc.table():
        b = base_by[x["team_id"]]
        changes.append({"team_id": x["team_id"], "baseline": _place(b), "whatif": _place(x),
                        "rank_change": b["rank"] - x["rank"] if b["rank"] and x["rank"] else None,
                        "status_changed": b["status"] != x["status"]})
    set_results = []
    for mid, outcome in sorted(overrides.items()):
        m = by_id[mid]
        before = baseline_results.get(mid)
        set_results.append({"matchup_id": mid, "matchup_period": m["matchup_period"], "weeks": m["weeks"],
                            "home_team_id": m["home_team_id"], "away_team_id": m["away_team_id"],
                            "status": m["status"], "outcome": outcome, "baseline_result": before,
                            "changes_baseline": (None if outcome == "none" else outcome) != before})
    unresolved = [m for m in sc.open_games if m["matchup_id"] not in sc.results]
    reasons: dict[str, int] = {}
    for m in unresolved:
        k = _unresolved_reason(m, base, overrides)
        reasons[k] = reasons.get(k, 0) + 1
    abstain = []
    if any(rec is None for rec in base_records.values()):
        abstain.append("A team's record is unknown.")
    if not coverage.get("checked"):
        abstain.append("The regular-season length is unknown, so games not in the stored schedule may remain.")
    elif coverage.get("missing"):
        abstain.append("The stored schedule is missing regular-season matchups, so unseen games may remain.")
    if any(m["winner"] not in FINAL_WINNERS and m.get("playoff_tier") in (None, "NONE") and m["home_team_id"] is not None
           and m["away_team_id"] is not None for m in no_period):
        abstain.append("Some undecided matchups have no matchup period and cannot be placed.")
    fixed = {mid: o for mid, o in overrides.items() if o != "none"}
    assumptions = [
        "Results you set are hypothetical. Actual records, points for and against, and the provider's seeds are "
        "unchanged.",
        ("Every other eligible upcoming game uses the higher projected lineup total as a hypothetical winner (the "
         "baseline). A game whose projection is incomplete is never decided by projection: missing is not zero.")
        if base == "projected" else "Every other remaining game stays unresolved (actual-results baseline).",
        "Unresolved games count for nobody: they are left out of the records, never scored as a loss or as 0 points.",
        "Rosters are the current snapshot for every remaining week; no waiver, trade or lineup change is modeled.",
        ORDERING_RULE,
    ]
    return {
        "label": "hypothetical",
        "method": WHATIF_METHOD,
        "sampling": {"method": "deterministic", "scenarios": 1, "random_samples": 0, "seed": None,
                     "probabilities": None},
        "inputs": {"base": base, "first_actionable_week": data.state.start_week, "set_results": set_results},
        "assumptions": assumptions,
        "completeness": {"status": "partial" if unresolved else "complete", "unresolved_games": len(unresolved),
                         "by_reason": dict(sorted(reasons.items()))},
        "labels": {"current_standings": "actual", "projected_standings": "hypothetical",
                   "baseline_standings": "hypothetical baseline (no results set)"},
        "baseline_standings": baseline_rows,
        "changes": changes,
        "conclusions": {**conclusion_gate(data, divisions, seeds_disagree),
                        "record_bounds": record_bounds(sc, fixed, abstain)},
    }


# ---------------------------------------------------------------- the build --

def parse_overrides(values: list[str] | None) -> dict[int, str]:
    """["12:home", "15:away", ...] -> {12: "home", 15: "away"}; the last value for a matchup wins."""
    out: dict[int, str] = {}
    for v in values or []:
        mid, sep, outcome = v.partition(":")
        if not sep or not mid.strip().lstrip("-").isdigit() or outcome.strip().lower() not in OUTCOMES:
            raise ValueError(f"override must look like MATCHUP_ID:{'|'.join(OUTCOMES)} (got {v!r})")
        out[int(mid)] = outcome.strip().lower()
    return out


def _schedule_coverage(data: RaceData, regular: list[dict]) -> dict:
    periods = data.league.get("reg_season_matchups")
    team_ids = [t["team_id"] for t in data.teams]
    if not periods:
        return {"checked": False, "reason": "regular-season matchup count unknown", "missing": []}
    seen = {(tid, m["matchup_period"]) for m in regular for tid in (m["home_team_id"], m["away_team_id"]) if tid}
    missing = [{"team_id": t, "periods": [p for p in range(1, periods + 1) if (t, p) not in seen]} for t in team_ids]
    missing = [x for x in missing if x["periods"]]
    unmapped = sorted({m["matchup_period"] for m in regular if m["weeks"] is None})
    return {"checked": True, "regular_season_periods": periods, "missing": missing, "complete": not missing,
            "unmapped_periods": unmapped}


def build_playoff_race(data: RaceData, team_id: int | None = None, base: str = "projected",
                       overrides: dict[int, str] | None = None) -> dict:
    """Read-only. Raises NotFound or ValueError (bad base or override)."""
    if base not in BASES:
        raise ValueError(f"base must be one of {', '.join(BASES)}")
    teams = {t["team_id"]: t for t in data.teams}
    team_ids = sorted(teams)
    focus = team_id
    if focus is None:
        mine = [t for t in data.teams if t["is_mine"]]
        focus = mine[0]["team_id"] if mine else None
    if focus is not None and focus not in teams:
        raise NotFound(f"Unknown team {focus}")

    state = data.state
    window = state.window()
    team_proj = {tid: (project_team(state.rosters[tid], state.slots, window, state.projections)
                       if state.rosters.get(tid) else None) for tid in team_ids}
    regular, playoff, no_period = classify_matchups(data, team_proj)
    by_id = {m["matchup_id"]: m for m in regular}

    overrides = overrides or {}
    for mid, outcome in overrides.items():
        m = by_id.get(mid)
        if m is None:
            raise ValueError(f"matchup {mid} is not a regular-season matchup of this league")
        if m["status"] in ("final", "bye") or m["home_team_id"] is None or m["away_team_id"] is None:
            raise ValueError(f"matchup {mid} is {m['status']}: only unresolved regular-season games can be overridden")
    results = {m["matchup_id"]: m["projected_winner"] for m in regular
               if base == "projected" and m["status"] == "upcoming" and m.get("projected_winner")}
    baseline_results = dict(results)
    for mid, outcome in overrides.items():
        if outcome == "none":
            results.pop(mid, None)
        else:
            results[mid] = outcome
    for m in regular:
        if m["matchup_id"] in overrides:
            m["override"] = overrides[m["matchup_id"]]
        m["scenario_result"] = results.get(m["matchup_id"]) if m["status"] != "final" else m["result"]

    # Period-less finals cannot be scheduled, but their regular-season results are still results.
    period_less_finals = [{"status": "final", "result": FINAL_WINNERS[m["winner"]], "home_team_id": m["home_team_id"],
                           "away_team_id": m["away_team_id"]} for m in no_period
                          if m["winner"] in FINAL_WINNERS and m["home_team_id"] is not None
                          and m["away_team_id"] is not None and m.get("playoff_tier") in (None, "NONE")]
    derived = derived_records(regular + period_less_finals, team_ids)
    base_records, record_notes = {}, []
    for tid in team_ids:
        rec = _record(teams[tid])
        if rec is None:
            rec = tuple(derived[tid])
            record_notes.append(f"{teams[tid]['name']}: ESPN record missing; derived from final matchups {rec}")
        elif list(rec) != derived[tid]:
            record_notes.append(f"{teams[tid]['name']}: ESPN record {rec[0]}-{rec[1]}-{rec[2]} differs from the final "
                                f"matchups stored {derived[tid][0]}-{derived[tid][1]}-{derived[tid][2]}; ESPN's record is used")
        base_records[tid] = rec

    actual = Scenario(data, regular, base_records, {})
    sc = Scenario(data, regular, base_records, results)
    current_rows, projected_rows = actual.table(), sc.table()
    spots = data.league.get("playoff_teams")
    ap = all_play(regular, team_ids)

    warnings = []
    divisions = sorted({t["division_id"] for t in data.teams if t["division_id"] is not None})
    if len(divisions) > 1:
        warnings.append(f"The league has {len(divisions)} divisions. Division-based playoff spots or seeding are not "
                        "modeled: the cutline here is by overall record only.")
    seeds = {t["team_id"]: t["playoff_seed"] for t in data.teams}
    seeds_disagree = False
    if all(seeds.values()):
        pct = {x["team_id"]: x["pct"] for x in current_rows}
        if any(pct[a] is not None and pct[b] is not None and pct[a] < pct[b] and seeds[a] < seeds[b]
               for a in team_ids for b in team_ids):
            seeds_disagree = True
            warnings.append("ESPN's current playoff seeds do not follow overall record order (divisions or seeding "
                            "rules apply). The projected cutline uses record order and may not match ESPN's seeding.")
    if not spots:
        warnings.append("The number of playoff spots is not reported in league settings: no cutline is drawn.")
    open_unresolved = [m for m in sc.open_games if m["matchup_id"] not in results]
    if open_unresolved:
        warnings.append(f"{len(open_unresolved)} remaining regular-season game(s) are unresolved in this scenario "
                        "(under way, projection incomplete, equal projections, or unmapped). Standings count decided "
                        "games only; override them to see their effect.")
    if no_period:
        warnings.append(f"ESPN sent {len(no_period)} matchup(s) without a matchup period (ids "
                        f"{', '.join(str(m['matchup_id']) for m in sorted(no_period, key=lambda m: str(m['matchup_id'])))}); they are "
                        "left out of all-play, schedules and scenarios, but final results among them still count "
                        "toward records.")
    coverage = _schedule_coverage(data, regular)
    if coverage.get("missing"):
        warnings.append(f"The ESPN schedule is missing regular-season matchups for {len(coverage['missing'])} team(s); "
                        "remaining-game counts may be incomplete.")
    if state.start_week is None:
        warnings.append("No remaining fantasy scoring periods can be projected (every week has kicked off).")
    elif data.window.get("uncertain") and data.window.get("warning"):
        warnings.append(data.window["warning"])

    lev = {tid: leverage(sc, tid) for tid in team_ids}
    names = {tid: teams[tid]["name"] for tid in team_ids}
    power = []
    cur_by, proj_by = {x["team_id"]: x for x in current_rows}, {x["team_id"]: x for x in projected_rows}
    for tid in team_ids:
        t, tp = teams[tid], team_proj[tid]
        s = tp["summary"] if tp else None
        upcoming = []
        for m in regular:
            if tid not in (m["home_team_id"], m["away_team_id"]) or m["status"] == "final":
                continue
            home = m["home_team_id"] == tid
            opp = m["away_team_id"] if home else m["home_team_id"]
            margin = m.get("projected_margin")
            upcoming.append({"matchup_id": m["matchup_id"], "matchup_period": m["matchup_period"], "weeks": m["weeks"],
                             "opponent_id": opp, "opponent_name": names.get(opp), "home": home, "status": m["status"],
                             "projected_margin": None if margin is None else (margin if home else r(-margin)),
                             "close": m.get("close"),
                             "scenario_result": None if m["scenario_result"] is None else
                             "tie" if m["scenario_result"] == "tie" else
                             ("win" if m["scenario_result"] == ("home" if home else "away") else "loss")})
        power.append({
            "team_id": tid, "name": t["name"], "abbrev": t["abbrev"], "is_mine": bool(t["is_mine"]),
            "division_id": t["division_id"], "espn_seed": t["playoff_seed"],
            "record": {"wins": base_records[tid][0], "losses": base_records[tid][1], "ties": base_records[tid][2]},
            "points_for": t["points_for"], "points_against": t["points_against"],
            "current": cur_by[tid], "projected": proj_by[tid], "all_play": ap[tid],
            "regular_season_projected_points": s["regular_season_projected_starting_points"] if s else None,
            "regular_season_known_points": s["regular_season_known_starting_points"] if s else None,
            "projection_weeks_incomplete": [w for w in s["missing_weeks"]
                                            if state.last_regular is None or w <= state.last_regular] if s else None,
            "projection_gap": None if s else "no roster snapshot for this team",
            "remaining": upcoming,
            "high_leverage_games": [g["matchup_id"] for g in lev[tid] if g["high_leverage"]],
        })

    cut = None
    if spots:
        # Boundary teams are whole rank groups, never one member of a tie picked by team id: the
        # lowest-ranked group fully inside, the highest-ranked group fully outside, and any group
        # straddling the last spot with the number of spots its members would share.
        inside = [x for x in projected_rows if x["status"] == "inside"]
        outside = [x for x in projected_rows if x["status"] == "outside"]
        tied = [x for x in projected_rows if x["status"] == "cutline_tie"]
        last_rank = max((x["rank"] for x in inside), default=None)
        first_rank = min((x["rank"] for x in outside), default=None)
        cut = {"playoff_spots": spots,
               "last_inside": [x for x in inside if x["rank"] == last_rank],
               "first_outside": [x for x in outside if x["rank"] == first_rank],
               "cutline_tie": {"team_ids": [x["team_id"] for x in tied], "spots_left": spots - len(inside)}
               if tied else None,
               "bubble": sorted({tid for tid in team_ids if any(g["high_leverage"] for g in lev[tid])}
                                | {x["team_id"] for x in projected_rows if x["status"] == "cutline_tie"})}

    out = {
        "league": {**{k: data.league.get(k) for k in ("league_id", "season", "name", "current_week",
                                                      "current_matchup_period", "final_scoring_period",
                                                      "reg_season_matchups", "playoff_teams",
                                                      "last_regular_season_week")},
                   "divisions": len(divisions),
                   "matchup_periods": {str(m["matchup_period"]): m["weeks"] for m in regular + playoff}},
        "window": {"first_actionable_week": state.start_week, "reason": data.window.get("reason"),
                   "uncertain": data.window.get("uncertain"), "current_week_status": data.window.get("current_week_status")},
        "focus_team_id": focus,
        "teams": power,
        "matchups": {"regular": regular, "playoff_known": playoff},
        "scenario": {
            "base": base,
            "overrides": [{"matchup_id": mid, "outcome": o} for mid, o in sorted(overrides.items())],
            "decided_open_games": sum(1 for m in sc.open_games if m["matchup_id"] in results),
            "unresolved_open_games": len(open_unresolved),
            "current_standings": current_rows,
            "projected_standings": projected_rows,
            "cutline": cut,
            "leverage": {str(tid): lev[tid] for tid in team_ids},
            "focus": pathways(sc, focus) if focus is not None else None,
        },
        "schedule_coverage": coverage,
        "record_check": record_notes,
        "rules": {"ordering": ORDERING_RULE, "projection": PROJECTION_RULE, "close": CLOSE_RULE, "clinch": CLINCH_NOTE,
                  "espn_rules_reported": data.rules_raw,
                  "espn_rules_note": ("Reported as ESPN sends them; their meaning is not verified here, so none is "
                                      "applied.") if data.rules_raw else "ESPN sent no seeding or tie rule settings."},
        "warnings": warnings,
        "notes": ["Actual records are ESPN's. Projected records add hypothetical results to them; nothing here is a "
                  "probability, a guarantee or a prediction.",
                  "Points for and against are actual ESPN totals; projected games do not add points.",
                  "Playoff matchups are shown as ESPN lists them; future playoff opponents are not modeled."],
    }
    if overrides:      # scenarios off (no result set): the answer is exactly what it was before issue #192
        out["whatif"] = whatif_report(data, sc, regular, base_records, base, baseline_results, overrides,
                                      coverage=coverage, no_period=no_period, divisions=len(divisions),
                                      seeds_disagree=seeds_disagree)
    return out
