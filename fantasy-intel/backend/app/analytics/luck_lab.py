"""Luck vs. Skill Lab (read-only, historical, explanatory): expected wins from all-play, schedule luck,
schedule difficulty, notable results, scoring consistency and counterfactual opponent schedules.
See docs/v2_luck_lab.md.

Rules:
  * Only completed regular-season matchups count: ESPN winner HOME/AWAY/TIE, regular tier
    (playoff_race.is_regular), a matchup period present. In-progress or future matchups are never
    results; playoff games never mix in.
  * A period is eligible only when it is fully observed: every league team appears in exactly one
    completed two-team matchup with both scores present. Otherwise the whole period is excluded
    (with its reasons) for every team, so expected wins always use the full league pool.
  * Scores are ESPN matchup-period totals (a two-week matchup is compared with two-week totals).
    An observed 0 is a real score; a missing score is unknown and excludes the period.
  * All-play comparisons use playoff_race.period_all_play, the same helper as the Playoff Race Lab.
  * Expected wins describe the weekly scoring rank; schedule luck is an observed gap between actual
    and expected win equivalents. Neither is a probability, a forecast or proof of skill or luck.
"""

from __future__ import annotations

from collections import Counter
from statistics import median, quantiles

from .playoff_race import FINAL_WINNERS, RaceData, is_regular, period_all_play, period_weeks

TOP_N = 3                      # "top three" / "bottom three" descriptive bands ...


def band_size(n: int) -> int:
    """... capped at half the league so the top and bottom bands never overlap (2 in a 4-team league)."""
    return max(1, min(TOP_N, n // 2))

EXPECTED_RULE = ("Expected wins in a period = (teams outscored + 0.5 x teams tied) / (teams - 1), comparing a team's "
                 "matchup-period score with every other team's score in the same period. Season expected wins = the "
                 "sum over eligible periods. An all-play win equivalent, not a probability of winning the actual matchup.")
LUCK_RULE = ("Schedule luck = actual win equivalents (win 1, tie 0.5) minus expected wins, over the same eligible periods. "
             "Positive: more actual wins than the weekly scoring rank implies. An observed result gap, not proof of luck "
             "or skill and not a prediction of regression.")
ELIGIBLE_RULE = ("A period is eligible only when every league team has exactly one completed regular-season two-team "
                 "matchup with both scores present. Otherwise it is excluded for every team (reasons listed), so no "
                 "team is compared with an incomplete pool.")
COUNTERFACTUAL_RULE = ("Counterfactual: keep the selected team's actual score each period and compare it with the actual "
                       "score of the opponent the other manager faced that period. A period where that opponent is the "
                       "selected team itself is excluded. These are hypothetical opponent assignments, not a valid "
                       "reconstructed league schedule; the hypothetical result compares points (ESPN tiebreaks are not "
                       "applied). Actual and counterfactual records use exactly the same periods.")


class NotSynced(LookupError):
    """No ESPN league or teams."""


class NotFound(LookupError):
    """Unknown team, or no team marked as yours."""


def _r(x: float | None, n: int = 3) -> float | None:
    return None if x is None else round(x + 0.0, n) + 0.0


def _wl(rows: list[dict]) -> dict:
    w = sum(1 for x in rows if x == "W")
    l_ = sum(1 for x in rows if x == "L")
    t = sum(1 for x in rows if x == "T")
    return {"wins": w, "losses": l_, "ties": t, "win_equivalents": w + 0.5 * t}


def _rank_desc(scores: dict[int, float]) -> dict[int, int]:
    """1 = highest score; equal scores share the better rank."""
    return {t: 1 + sum(1 for v in scores.values() if v > s) for t, s in scores.items()}


# ------------------------------------------------------------------ periods --

def observed_periods(data: RaceData) -> tuple[dict[int, dict], list[dict], dict]:
    """(eligible periods: period -> {scores, opponent, result, weeks}, excluded periods with reasons,
    data notes). Duplicate matchup ids with identical content are counted once; conflicting
    duplicates make their period ineligible."""
    team_ids = sorted(t["team_id"] for t in data.teams)
    reg = data.league.get("reg_season_matchups")
    notes = {"duplicate_ids_merged": [], "conflicting_ids": [], "missing_ids": 0, "no_period": [],
             "playoff_rows": 0}
    seen: dict = {}
    rows: list[dict] = []
    conflict_periods: set = set()
    key = ("matchup_period", "home_team_id", "away_team_id", "home_points", "away_points", "winner", "playoff_tier")
    for m in data.matchups:
        mid = m.get("matchup_id")
        if mid is None:
            notes["missing_ids"] += 1
        elif mid in seen:
            if tuple(m.get(k) for k in key) == tuple(seen[mid].get(k) for k in key):
                notes["duplicate_ids_merged"].append(mid)
                continue
            notes["conflicting_ids"].append(mid)
            conflict_periods |= {m.get("matchup_period"), seen[mid].get("matchup_period")}
            continue
        else:
            seen[mid] = m
        rows.append(m)
    by_period: dict[int, list[dict]] = {}
    for m in rows:
        if m.get("matchup_period") is None:
            notes["no_period"].append(m.get("matchup_id"))
            continue
        if not is_regular(m, reg):
            notes["playoff_rows"] += 1
            continue
        by_period.setdefault(m["matchup_period"], []).append(m)
    eligible, excluded = {}, []
    for p in sorted(by_period):
        scores, opp, result, appear = {}, {}, {}, Counter()
        one_team, not_final, no_score = [], [], []
        for m in by_period[p]:
            h, a = m["home_team_id"], m["away_team_id"]
            for t in (h, a):
                if t is not None:
                    appear[t] += 1
            if h is None or a is None:
                one_team.append(h if h is not None else a)
                continue
            if m["winner"] not in FINAL_WINNERS:
                not_final.append(m.get("matchup_id"))
                continue
            if m["home_points"] is None or m["away_points"] is None:
                no_score.append(m.get("matchup_id"))
                continue
            res = FINAL_WINNERS[m["winner"]]
            scores[h], scores[a] = float(m["home_points"]), float(m["away_points"])
            opp[h], opp[a] = a, h
            result[h] = "T" if res == "tie" else ("W" if res == "home" else "L")
            result[a] = "T" if res == "tie" else ("W" if res == "away" else "L")
        reasons = []
        if not_final:
            reasons.append(f"{len(not_final)} matchup(s) not final (no ESPN winner yet)")
        if no_score:
            reasons.append(f"matchup(s) {', '.join(map(str, no_score))} have a missing score")
        if one_team:
            reasons.append(f"one-team row (bye or unknown opponent) for team(s) {', '.join(map(str, one_team))}")
        dup = sorted(t for t, n in appear.items() if n > 1)
        if dup:
            reasons.append(f"team(s) {', '.join(map(str, dup))} appear in more than one matchup")
        absent = [t for t in team_ids if appear[t] == 0]
        if absent:
            reasons.append(f"no completed result for team(s) {', '.join(map(str, absent))} (not in any matchup)")
        if p in conflict_periods:
            reasons.append("conflicting duplicate matchup ids")
        unknown = sorted(t for t in scores if t not in team_ids)
        if unknown:
            reasons.append(f"unknown team id(s) {', '.join(map(str, unknown))}")
        if not reasons and len([t for t in scores if t in team_ids]) < len(team_ids):
            reasons.append("not every team has a completed result")
        if reasons:
            # not_completed: nothing final yet (current or future period); incomplete: a data gap.
            kind = "not_completed" if not scores and not no_score and not one_team and not dup else "incomplete"
            excluded.append({"matchup_period": p, "weeks": period_weeks(data.schedule_settings, p), "kind": kind,
                             "teams_with_final_scores": len([t for t in scores if t in team_ids]),
                             "teams": len(team_ids), "reasons": reasons})
            continue
        eligible[p] = {"scores": scores, "opponent": opp, "result": result,
                       "weeks": period_weeks(data.schedule_settings, p)}
    return eligible, excluded, notes


# ------------------------------------------------------------- per period --

def period_rows(eligible: dict[int, dict]) -> dict[int, dict[int, dict]]:
    """period -> team -> the team's row for that period (score, rank, all-play, expected wins, luck,
    opponent rank and share, notable-result flags)."""
    out = {}
    for p, e in eligible.items():
        scores = e["scores"]
        n = len(scores)
        ap = period_all_play(scores)
        rank = _rank_desc(scores)
        med = median(scores.values())
        share = {t: (w + 0.5 * tie) / (n - 1) for t, (w, _, tie) in ap.items()} if n > 1 else {}
        band = band_size(n)
        rows = {}
        for t, s in scores.items():
            w, l_, tie = ap[t]
            o = e["opponent"][t]
            res = e["result"][t]
            actual = 1.0 if res == "W" else 0.5 if res == "T" else 0.0
            flags = []
            if res == "L":
                if rank[t] <= band:
                    flags.append("top_band_loss")
                if s > med:
                    flags.append("above_median_loss")
                if rank[o] == 1:
                    flags.append("lost_to_top_scorer")
            if res == "W":
                if rank[t] > n - band:
                    flags.append("bottom_band_win")
                if s < med:
                    flags.append("below_median_win")
            rows[t] = {"matchup_period": p, "weeks": e["weeks"], "score": s, "rank": rank[t], "teams": n,
                       "median": med, "all_play": {"wins": w, "losses": l_, "ties": tie},
                       "expected_wins": share[t], "result": res, "actual_win_equivalent": actual,
                       "luck": actual - share[t], "opponent_id": o, "opponent_score": scores[o],
                       "opponent_rank": rank[o], "opponent_all_play_share": share[o],
                       "margin": s - scores[o], "flags": flags}
        out[p] = rows
    return out


# ------------------------------------------------------------- summaries --

def _quartiles(vals: list[float]) -> tuple[float | None, float | None]:
    if len(vals) < 2:
        return None, None
    q = quantiles(vals, n=4, method="inclusive")
    return q[0], q[2]


def team_summary(tid: int, rows: list[dict], season_share: dict[int, float | None]) -> dict:
    if not rows:
        return {"team_id": tid, "periods": 0}
    scores = [x["score"] for x in rows]
    q1, q3 = _quartiles(scores)
    rec = _wl([x["result"] for x in rows])
    ap = {k: sum(x["all_play"][k] for x in rows) for k in ("wins", "losses", "ties")}
    apg = sum(ap.values())
    ew = sum(x["expected_wins"] for x in rows)
    opp = [x["opponent_score"] for x in rows]
    opp_norm = [season_share[x["opponent_id"]] for x in rows if season_share.get(x["opponent_id"]) is not None]
    hardest = max(rows, key=lambda x: (x["opponent_score"], -x["matchup_period"]))
    easiest = min(rows, key=lambda x: (x["opponent_score"], x["matchup_period"]))
    return {
        "team_id": tid, "periods": len(rows),
        "actual": rec, "expected_wins": _r(ew), "expected_losses": _r(len(rows) - ew),
        "schedule_luck": _r(rec["win_equivalents"] - ew),
        "all_play": {**ap, "pct": _r((ap["wins"] + 0.5 * ap["ties"]) / apg, 4) if apg else None},
        "scoring": {"total": _r(sum(scores), 2), "mean": _r(sum(scores) / len(scores), 2), "median": _r(median(scores), 2),
                    "min": min(scores), "max": max(scores), "range": _r(max(scores) - min(scores), 2),
                    "q1": _r(q1, 2), "q3": _r(q3, 2), "iqr": _r(q3 - q1, 2) if q1 is not None else None,
                    "top_half_pct": _r(sum(1 for x in rows if x["rank"] <= x["teams"] / 2) / len(rows), 4),
                    # First place: nobody scored more; last place: nobody scored less (ties share either).
                    "first_place": sum(1 for x in rows if x["all_play"]["losses"] == 0),
                    "last_place": sum(1 for x in rows if x["all_play"]["wins"] == 0)},
        "difficulty": {"opponent_points_total": _r(sum(opp), 2), "opponent_points_mean": _r(sum(opp) / len(opp), 2),
                       "opponent_share_mean": _r(sum(x["opponent_all_play_share"] for x in rows) / len(rows), 4),
                       "opponents_top_band": sum(1 for x in rows if x["opponent_rank"] <= band_size(x["teams"])),
                       "opponents_bottom_band": sum(1 for x in rows if x["opponent_rank"] > x["teams"] - band_size(x["teams"])),
                       "opponent_season_share_mean": _r(sum(opp_norm) / len(opp_norm), 4) if opp_norm else None,
                       "hardest": _brief(hardest), "easiest": _brief(easiest)},
        "notable": {"brutal_losses": sorted([_brief(x) for x in rows if x["flags"] and x["result"] == "L"],
                                            key=lambda b: (b["rank"], -b["score"], b["matchup_period"])),
                    "fortunate_wins": sorted([_brief(x) for x in rows if x["flags"] and x["result"] == "W"],
                                             key=lambda b: (-b["rank"], b["score"], b["matchup_period"]))},
    }


def _brief(x: dict) -> dict:
    return {k: x[k] for k in ("matchup_period", "weeks", "score", "rank", "teams", "opponent_id", "opponent_score",
                              "opponent_rank", "margin", "result", "flags")}


# --------------------------------------------------------- counterfactual --

def counterfactual(per: dict[int, dict[int, dict]], a: int, b: int) -> dict:
    """Team a's actual scores against the opponents team b faced, period by period."""
    rows, excluded = [], []
    for p in sorted(per):
        ra, rb = per[p].get(a), per[p].get(b)
        if ra is None or rb is None:
            excluded.append({"matchup_period": p, "reason": "team missing from the period"})
            continue
        o = rb["opponent_id"]
        if o == a:
            excluded.append({"matchup_period": p, "reason": f"team {b} played team {a} this period (self-matchup)"})
            continue
        os_ = per[p][o]["score"]
        hyp = "W" if ra["score"] > os_ else "L" if ra["score"] < os_ else "T"
        rows.append({"matchup_period": p, "weeks": ra["weeks"], "score": ra["score"],
                     "actual_opponent_id": ra["opponent_id"], "actual_opponent_score": ra["opponent_score"],
                     "actual_result": ra["result"], "substituted_opponent_id": o, "substituted_opponent_score": os_,
                     "counterfactual_result": hyp, "margin": _r(ra["score"] - os_, 2),
                     "changed": hyp != ra["result"]})
    actual = _wl([x["actual_result"] for x in rows])
    cf = _wl([x["counterfactual_result"] for x in rows])
    return {"team_id": a, "schedule_of": b, "periods_compared": [x["matchup_period"] for x in rows],
            "excluded": excluded, "actual": actual, "counterfactual": cf,
            "win_equivalent_difference": cf["win_equivalents"] - actual["win_equivalents"],
            "actual_pct": _r(actual["win_equivalents"] / len(rows), 4) if rows else None,
            "counterfactual_pct": _r(cf["win_equivalents"] / len(rows), 4) if rows else None,
            "flips": {"loss_to_win": [x["matchup_period"] for x in rows if x["actual_result"] == "L" and x["counterfactual_result"] == "W"],
                      "win_to_loss": [x["matchup_period"] for x in rows if x["actual_result"] == "W" and x["counterfactual_result"] == "L"],
                      "other_changes": [x["matchup_period"] for x in rows if x["changed"]
                                        and {x["actual_result"], x["counterfactual_result"]} != {"W", "L"}]},
            "rows": rows}


# -------------------------------------------------------------------- build --

def build_luck_lab(data: RaceData, team_id: int | None = None, schedule_of: int | None = None) -> dict:
    """Read-only. Raises NotFound or ValueError."""
    teams = {t["team_id"]: t for t in data.teams}
    if not teams:
        raise NotSynced("No fantasy teams synced for the league.")
    focus = team_id
    if focus is None:
        mine = [t for t in data.teams if t["is_mine"]]
        focus = mine[0]["team_id"] if mine else min(teams)
    if focus not in teams:
        raise NotFound(f"Unknown team {focus}")
    if schedule_of is not None and schedule_of not in teams:
        raise NotFound(f"Unknown team {schedule_of}")
    if schedule_of == focus:
        raise ValueError("choose another manager's schedule (the selected team's own schedule is its actual record)")

    eligible, excluded, notes = observed_periods(data)
    per = period_rows(eligible)
    team_ids = sorted(teams)
    by_team = {t: [per[p][t] for p in sorted(per) if t in per[p]] for t in team_ids}
    # Season all-play share per team (scoring strength over eligible periods), for opponent context.
    season_share = {}
    for t in team_ids:
        rows = by_team[t]
        season_share[t] = sum(x["expected_wins"] for x in rows) / len(rows) if rows else None
    summaries = {}
    for t in team_ids:
        s = team_summary(t, by_team[t], season_share)
        s.update(name=teams[t]["name"], is_mine=bool(teams[t]["is_mine"]),
                 espn_record={"wins": teams[t]["wins"], "losses": teams[t]["losses"], "ties": teams[t]["ties"]},
                 espn_points_for=teams[t]["points_for"], espn_points_against=teams[t]["points_against"])
        summaries[t] = s
    lucks = {t: summaries[t]["schedule_luck"] for t in team_ids if summaries[t]["periods"]}
    luck_rank = {t: 1 + sum(1 for v in lucks.values() if v > lucks[t]) for t in lucks}
    means = {t: summaries[t]["scoring"]["mean"] for t in lucks}
    scoring_rank = {t: 1 + sum(1 for v in means.values() if v > means[t]) for t in means}
    ew = {t: summaries[t]["expected_wins"] for t in lucks}
    ew_rank = {t: 1 + sum(1 for v in ew.values() if v > ew[t]) for t in ew}
    opp_mean = {t: summaries[t]["difficulty"]["opponent_points_mean"] for t in lucks}
    difficulty_rank = {t: 1 + sum(1 for v in opp_mean.values() if v > opp_mean[t]) for t in opp_mean}
    leaderboard = []
    for t in sorted(lucks, key=lambda t: (-lucks[t], t)):
        s = summaries[t]
        leaderboard.append({"team_id": t, "name": s["name"], "is_mine": s["is_mine"], "luck_rank": luck_rank[t],
                            "schedule_luck": s["schedule_luck"], "actual": s["actual"], "expected_wins": s["expected_wins"],
                            "expected_wins_rank": ew_rank[t], "all_play": s["all_play"], "espn_record": s["espn_record"],
                            "scoring_mean": s["scoring"]["mean"], "scoring_rank": scoring_rank[t],
                            "opponent_points_mean": s["difficulty"]["opponent_points_mean"],
                            "difficulty_rank": difficulty_rank[t], "periods": s["periods"]})
    timeline, cum = [], 0.0
    for x in by_team[focus]:
        cum += x["luck"]
        timeline.append({**_brief(x), "expected_wins": _r(x["expected_wins"], 4), "luck": _r(x["luck"], 4),
                         "cumulative_luck": _r(cum, 4), "median": x["median"],
                         "opponent_name": teams[x["opponent_id"]]["name"]})
    contributors = sorted(timeline, key=lambda x: (-abs(x["luck"]), x["matchup_period"]))[:5]
    all_cf = []
    for b in team_ids:
        if b == focus:
            continue
        c = counterfactual(per, focus, b)
        all_cf.append({k: c[k] for k in ("schedule_of", "periods_compared", "excluded", "actual", "counterfactual",
                                         "win_equivalent_difference", "actual_pct", "counterfactual_pct", "flips")}
                      | {"name": teams[b]["name"]})
    base_periods = sorted(per)
    for c in all_cf:
        c["same_periods_as_all_eligible"] = c["periods_compared"] == base_periods
    selected = counterfactual(per, focus, schedule_of) if schedule_of is not None else None
    matrix = {"periods": [{"matchup_period": p, "weeks": eligible[p]["weeks"]} for p in base_periods],
              "teams": [{"team_id": t, "name": teams[t]["name"],
                         "cells": [{"matchup_period": p, "rank": per[p][t]["rank"], "result": per[p][t]["result"],
                                    "score": per[p][t]["score"], "expected_wins": _r(per[p][t]["expected_wins"], 4),
                                    "flags": per[p][t]["flags"]} for p in base_periods if t in per[p]]}
                        for t in team_ids]}
    n = len(team_ids)
    return {
        "league": {k: data.league.get(k) for k in ("league_id", "season", "name", "current_week", "reg_season_matchups")},
        "teams": [{"team_id": t, "name": teams[t]["name"], "is_mine": bool(teams[t]["is_mine"])} for t in team_ids],
        "focus_team_id": focus, "schedule_of": schedule_of,
        "coverage": {"teams": n, "eligible_periods": base_periods, "excluded_periods": excluded,
                     "data_notes": notes, "rule": ELIGIBLE_RULE},
        "leaderboard": leaderboard,
        "summaries": {str(t): summaries[t] for t in team_ids},
        "spotlight": {**summaries[focus], "luck_rank": luck_rank.get(focus), "scoring_rank": scoring_rank.get(focus),
                      "difficulty_rank": difficulty_rank.get(focus), "teams_ranked": len(lucks)},
        "timeline": timeline, "biggest_contributors": contributors,
        "counterfactual": selected, "all_schedules": all_cf,
        "matrix": matrix,
        "rules": {"expected": EXPECTED_RULE, "luck": LUCK_RULE, "eligible": ELIGIBLE_RULE,
                  "counterfactual": COUNTERFACTUAL_RULE,
                  "bands": f"Top/bottom band = the {band_size(n)} highest / lowest weekly scores (three, or half the league "
                           f"when it has fewer than six teams; this league has {n}). Descriptive ranks, not thresholds "
                           "that predict anything.",
                  "band_size": band_size(n),
                  "result_source": "Actual results are ESPN's winner field; a points tie that ESPN broke with a league "
                                   "tiebreak counts as ESPN recorded it."},
        "notes": ["Historical and explanatory only: no projections, no win probabilities, no future expected wins.",
                  "Scores are ESPN matchup-period totals; lineup decisions are taken as they happened (no hypothetical "
                  "optimized past lineups).",
                  "Schedule luck describes how actual matchups compare with weekly scoring ranks; it does not show cause."],
    }


# ------------------------------------------------------------------- loading --

def load_luck_data(con) -> RaceData:
    """League settings, teams and matchups only (no rosters or projections: nothing here looks forward)."""
    import json

    from ..db.connection import fetch_dicts, table_exists
    from .projections import last_regular_season_week

    lg = fetch_dicts(con, """SELECT league_id, season, name, current_week, final_scoring_period, reg_season_matchups,
                                    playoff_teams, settings_raw FROM leagues ORDER BY synced_at DESC LIMIT 1""") \
        if table_exists(con, "leagues") else []
    if not lg:
        raise NotSynced("ESPN league not synced yet.")
    lg = lg[0]
    raw = lg.pop("settings_raw")
    settings = json.loads(raw) if isinstance(raw, str) else (raw or {})
    sched = settings.get("scheduleSettings") or {}
    lg["last_regular_season_week"] = last_regular_season_week(lg["reg_season_matchups"], lg["final_scoring_period"], sched)
    teams = fetch_dicts(con, """SELECT team_id, name, abbrev, is_mine, wins, losses, ties, points_for, points_against
                                FROM fantasy_teams WHERE league_id = ? AND season = ? ORDER BY team_id""",
                        [lg["league_id"], lg["season"]])
    if not teams:
        raise NotSynced("No fantasy teams synced for the league.")
    matchups = fetch_dicts(con, """SELECT matchup_id, matchup_period, home_team_id, away_team_id, home_points, away_points,
                                          winner, playoff_tier FROM matchups WHERE league_id = ? AND season = ?
                                   ORDER BY matchup_period, matchup_id""", [lg["league_id"], lg["season"]]) \
        if table_exists(con, "matchups") else []
    return RaceData(league=lg, schedule_settings=sched, rules_raw={}, teams=teams, matchups=matchups, state=None,
                    window={})
