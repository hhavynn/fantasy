"""Offensive / defensive environment and position-aware player context.

Everything here is a transparent label backed by stored numbers (evidence).
No master score: each label answers one question, e.g. "is the upcoming
matchup favorable for an RB?" from the opponent's season-to-date rush EPA
allowed rank, with the sample size shown.
"""

from __future__ import annotations

import json
from collections import defaultdict
from statistics import mean

from ..db.connection import fetch_dicts, replace_where, table_exists, utcnow
from .player_scores import percentile_scores

FAVORABLE_RANK, TOUGH_RANK = 10, 23     # of 32; rank 1 = most generous defense
MIN_DEF_GAMES = 3
MIN_RB_CARRIES_EFFICIENCY = 30
IMPLIED_GOOD, IMPLIED_POOR = 24.5, 20.5
RECENT_GAMES = 3


# ---------------------------------------------------------------- lines --

def implied_totals(total_line: float | None, spread_line: float | None) -> tuple[float | None, float | None]:
    """(home, away). nflverse: spread_line > 0 means the HOME team is favored."""
    if total_line is None or spread_line is None:
        return None, None
    return (total_line + spread_line) / 2.0, (total_line - spread_line) / 2.0


def build_team_game_lines(con, season: int) -> int:
    if not table_exists(con, "nfl_schedules"):
        return 0
    rows = []
    for g in fetch_dicts(con, """SELECT game_id, week, home_team, away_team, spread_line, total_line
                                 FROM nfl_schedules WHERE season = ? AND game_type = 'REG'""", [season]):
        home, away = implied_totals(g["total_line"], g["spread_line"])
        for team, opp, is_home, imp in ((g["home_team"], g["away_team"], True, home),
                                        (g["away_team"], g["home_team"], False, away)):
            rows.append({"season": season, "week": g["week"], "game_id": g["game_id"], "team": team,
                         "opponent": opp, "is_home": is_home, "spread_line": g["spread_line"],
                         "total_line": g["total_line"], "implied_total": imp})
    return replace_where(con, "team_game_lines", rows, "season = ?", [season])


# --------------------------------------------------------- team context --

def _wavg(rows, value, weight):
    pairs = [(r[value], r[weight]) for r in rows if r[value] is not None and r[weight]]
    den = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / den if den else None


def _ratio_sum(rows, num, den):
    pairs = [(r[num], r[den]) for r in rows if r[num] is not None and r[den]]
    d = sum(b for _, b in pairs)
    return sum(a for a, _ in pairs) / d if d else None


def summarize_team(rows: list[dict]) -> dict:
    """Season-to-date offense context from nfl_team_week rows of one team."""
    return {
        "games": len(rows),
        "proe": _wavg(rows, "proe", "proe_plays"),
        "neutral_pass_rate": _wavg(rows, "neutral_pass_rate", "neutral_plays"),
        "sack_rate": _ratio_sum(rows, "sacks", "dropbacks"),
        "hit_or_sack_rate": _ratio_sum(rows, "hit_or_sack", "dropbacks"),
        "pfr_pressure_rate": _ratio_sum(rows, "pfr_pressures", "dropbacks"),
        "dropbacks": sum(r["dropbacks"] or 0 for r in rows),
    }


def build_team_context(con, season: int, next_week: int | None) -> int:
    by_team = defaultdict(list)
    for r in fetch_dicts(con, "SELECT * FROM nfl_team_week WHERE season = ?", [season]):
        by_team[r["team"]].append(r)
    through = max((r["week"] for rows in by_team.values() for r in rows), default=None)
    lines = {}
    if next_week is not None:
        lines = {r["team"]: r for r in fetch_dicts(con, "SELECT * FROM team_game_lines WHERE season=? AND week=?",
                                                   [season, next_week])}
    out = []
    for team, rows in by_team.items():
        ln = lines.get(team)
        out.append({"season": season, "through_week": through, "team": team, **summarize_team(rows),
                    "next_week": next_week, "next_opponent": ln["opponent"] if ln else None,
                    "next_implied_total": ln["implied_total"] if ln else None})
    return replace_where(con, "team_context", out, "season = ?", [season])


# ------------------------------------------------------ defense context --

DEF_RANKED = ["pass_epa_allowed", "rush_epa_allowed", "rush_success_allowed", "explosive_run_rate_allowed",
              "rb_target_share_allowed", "te_target_share_allowed"]


def summarize_defense(rows: list[dict]) -> dict:
    """Season-to-date defensive rates from nfl_defense_week rows of one defense (sums, not means of means)."""
    s = {k: sum((r[k] or 0) for r in rows) for k in ("pass_plays", "pass_epa_sum", "rush_plays", "rush_epa_sum",
                                                      "rush_success", "explosive_runs", "targets", "rb_targets",
                                                      "te_targets")}
    div = lambda a, b: a / b if b else None  # noqa: E731
    return {
        "games": len(rows), "pass_plays": s["pass_plays"], "rush_plays": s["rush_plays"], "targets": s["targets"],
        "pass_epa_allowed": div(s["pass_epa_sum"], s["pass_plays"]),
        "rush_epa_allowed": div(s["rush_epa_sum"], s["rush_plays"]),
        "rush_success_allowed": div(s["rush_success"], s["rush_plays"]),
        "explosive_run_rate_allowed": div(s["explosive_runs"], s["rush_plays"]),
        "rb_target_share_allowed": div(s["rb_targets"], s["targets"]),
        "te_target_share_allowed": div(s["te_targets"], s["targets"]),
    }


def rank_generous(values: dict[str, float | None]) -> dict[str, int | None]:
    """Rank 1 = highest value (most generous to offenses). None stays unranked."""
    ordered = sorted((v, k) for k, v in values.items() if v is not None)
    ordered.reverse()
    out: dict[str, int | None] = {k: None for k in values}
    for i, (_, k) in enumerate(ordered, start=1):
        out[k] = i
    return out


def build_defense_context(con, season: int) -> int:
    by_def = defaultdict(list)
    for r in fetch_dicts(con, "SELECT * FROM nfl_defense_week WHERE season = ?", [season]):
        by_def[r["defteam"]].append(r)
    through = max((r["week"] for rows in by_def.values() for r in rows), default=None)
    summaries = {d: summarize_defense(rows) for d, rows in by_def.items()}
    ranks = {m: rank_generous({d: s[m] for d, s in summaries.items()}) for m in DEF_RANKED}
    out = [{"season": season, "through_week": through, "defteam": d, **s,
            "ranks": {m: ranks[m][d] for m in DEF_RANKED}} for d, s in summaries.items()]
    return replace_where(con, "defense_context", out, "season = ?", [season])


# ------------------------------------------------------- player context --

MATCHUP_METRIC = {"RB": ["rush_epa_allowed", "rb_target_share_allowed"], "WR": ["pass_epa_allowed"],
                  "TE": ["pass_epa_allowed", "te_target_share_allowed"], "QB": ["pass_epa_allowed"]}


def matchup_label(position: str | None, opp_ctx: dict | None, on_bye: bool) -> tuple[str, dict]:
    if on_bye:
        return "BYE", {}
    if not opp_ctx:
        return "UNKNOWN", {}
    if (opp_ctx.get("games") or 0) < MIN_DEF_GAMES:
        return "INSUFFICIENT", {"games": opp_ctx.get("games")}
    metrics = MATCHUP_METRIC.get(position or "", [])
    rks = [opp_ctx["ranks"].get(m) for m in metrics if opp_ctx["ranks"].get(m) is not None]
    if not rks:
        return "UNKNOWN", {}
    avg_rank = mean(rks)
    label = "FAVORABLE" if avg_rank <= FAVORABLE_RANK else "TOUGH" if avg_rank >= TOUGH_RANK else "NEUTRAL"
    return label, {"opponent": opp_ctx.get("defteam"), "games": opp_ctx["games"], "avg_rank": avg_rank,
                   "ranks": {m: opp_ctx["ranks"].get(m) for m in metrics},
                   "rule": f"mean generosity rank of {metrics} (1 = most generous of 32): "
                           f"<= {FAVORABLE_RANK} FAVORABLE, >= {TOUGH_RANK} TOUGH"}


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return mean(vals) if vals else None


def player_recent(rows: list[dict]) -> dict:
    """Recent-window and season aggregates for one player's played games (sorted by week)."""
    recent = rows[-RECENT_GAMES:]
    n = len(recent)
    targets = sum(r["targets"] or 0 for r in recent)
    air = sum(r["air_yards"] or 0 for r in recent if r["air_yards"] is not None)
    designed_season = sum(r["designed_rushes"] or 0 for r in rows)
    success = sum((r["rushing_success_rate"] or 0) * (r["designed_rushes"] or 0) for r in rows
                  if r["rushing_success_rate"] is not None)
    explosive = sum(r["explosive_runs"] or 0 for r in rows)
    carry_w = [(r["ybc_per_att"], r["yac_per_att"], r["carries"] or 0) for r in rows
               if r["ybc_per_att"] is not None and r["carries"]]
    w = sum(c for _, _, c in carry_w)
    return {
        "games_recent": n, "recent_weeks": [r["week"] for r in recent], "games_season": len(rows),
        "snap_pct": _mean(r["offense_pct"] for r in recent),
        "target_share": _mean(r["target_share"] for r in recent),
        "air_yards_share": _mean(r["air_yards_share"] for r in recent),
        "wopr": _mean(r["wopr"] for r in recent),
        "adot": air / targets if targets else None,
        "first_read_target_share": _mean(r["first_read_target_share"] for r in recent),
        "first_read_targets": (sum(r["first_read_targets"] for r in recent if r["first_read_targets"] is not None)
                               if any(r["first_read_targets"] is not None for r in recent) else None),
        "designed_targets": (sum(r["designed_targets"] for r in recent if r["designed_targets"] is not None)
                             if any(r["designed_targets"] is not None for r in recent) else None),
        "team_rush_share": _mean(r["team_rush_share"] for r in recent),
        "backfield_rush_share": _mean(r["backfield_rush_share"] for r in recent),
        "rz_targets_pg": _mean(r["rz_targets"] for r in recent),
        "rz_carries_pg": _mean(r["rz_carries"] for r in recent),
        "gl_carries_recent": (sum(r["gl_carries"] for r in recent if r["gl_carries"] is not None)
                              if any(r["gl_carries"] is not None for r in recent) else None),
        "designed_rushes_season": designed_season,
        "rushing_success_rate": success / designed_season if designed_season else None,
        "explosive_run_rate": explosive / designed_season if designed_season else None,
        "ybc_per_att": sum(y * c for y, _, c in carry_w) / w if w else None,
        "yac_per_att": sum(a * c for _, a, c in carry_w) / w if w else None,
        "broken_tackles": (sum(r["broken_tackles"] for r in rows if r["broken_tackles"] is not None)
                           if any(r["broken_tackles"] is not None for r in rows) else None),
        "ngs_rush_yoe_per_att": _mean(r["ngs_rush_yoe_per_att"] for r in recent),
        "ngs_avg_separation": _mean(r["ngs_avg_separation"] for r in recent),
    }


def _tier3(v, hi, mid, labels=("HIGH", "MEDIUM", "LOW")):
    if v is None:
        return "UNKNOWN"
    return labels[0] if v >= hi else labels[1] if v >= mid else labels[2]


def headlines(position: str | None, ev: dict, labels: dict) -> list[str]:
    """Short, number-backed explanation lines (used by the waiver page)."""
    out = []
    n = ev.get("games_recent") or 0
    window = f"last {n} game{'s' if n != 1 else ''}"
    if labels.get("role_trend") == "UP":
        out.append("Role rising")
    elif labels.get("role_trend") == "DOWN":
        out.append("Role shrinking")
    if position == "RB":
        if ev.get("backfield_rush_share") is not None:
            out.append(f"{ev['backfield_rush_share']:.0%} backfield rush share over {window}")
        if ev.get("target_share") is not None and ev["target_share"] >= 0.08:
            out.append(f"{ev['target_share']:.0%} target share")
        if ev.get("gl_carries_recent"):
            n_gl = ev["gl_carries_recent"]
            out.append(f"{n_gl} goal-line carr{'y' if n_gl == 1 else 'ies'} over {window}")
    elif position in ("WR", "TE"):
        if ev.get("snap_pct") is not None and ev.get("target_share") is not None:
            out.append(f"{ev['snap_pct']:.0%} snaps, {ev['target_share']:.0%} target share over {window}")
        if ev.get("first_read_target_share") is not None:
            out.append(f"{ev['first_read_target_share']:.0%} of team first-read targets")
    m = labels.get("matchup")
    mev = ev.get("matchup") or {}
    if m in ("FAVORABLE", "TOUGH") and mev.get("ranks"):
        metric = next(iter(mev["ranks"]))
        third = "bottom-third" if m == "FAVORABLE" else "top-third"
        out.append(f"opponent {mev.get('opponent')} {third} defense by {metric.replace('_', ' ')} "
                   f"(rank {mev['ranks'][metric]}/32 generosity)")
    return out


def build_player_context(con, season: int, next_week: int | None) -> int:
    usage = defaultdict(list)
    for r in fetch_dicts(con, "SELECT * FROM player_week_usage WHERE season = ? ORDER BY week", [season]):
        if (r["offense_snaps"] or 0) > 0 or (r["opportunities"] or 0) > 0:
            usage[r["gsis_id"]].append(r)
    scores = {r["gsis_id"]: r for r in fetch_dicts(con, "SELECT * FROM player_scores WHERE season = ?", [season])}
    mapping = {r["gsis_id"]: ("high" if r["any_high"] else "medium") for r in fetch_dicts(
        con, "SELECT gsis_id, bool_or(match_confidence = 'high') AS any_high FROM player_id_map "
             "WHERE gsis_id IS NOT NULL GROUP BY 1")}
    teams = {r["team"]: r for r in fetch_dicts(con, "SELECT * FROM team_context WHERE season = ?", [season])}
    defs = {}
    for r in fetch_dicts(con, "SELECT * FROM defense_context WHERE season = ?", [season]):
        r["ranks"] = json.loads(r["ranks"]) if isinstance(r["ranks"], str) else r["ranks"]
        defs[r["defteam"]] = r
    has_lines = next_week is not None and bool(con.execute(
        "SELECT count(*) FROM team_game_lines WHERE season=? AND week=?", [season, next_week]).fetchone()[0])

    recents = {g: player_recent(rows) for g, rows in usage.items()}
    rb_pool = {g: e for g, e in recents.items() if scores.get(g, {}).get("position") == "RB"
               and e["designed_rushes_season"] >= MIN_RB_CARRIES_EFFICIENCY}
    yac_pct = percentile_scores({g: e["yac_per_att"] for g, e in rb_pool.items() if e["yac_per_att"] is not None})
    succ_pct = percentile_scores({g: e["rushing_success_rate"] for g, e in rb_pool.items()
                                  if e["rushing_success_rate"] is not None})
    ybc_pct = percentile_scores({g: e["ybc_per_att"] for g, e in rb_pool.items() if e["ybc_per_att"] is not None})

    now, out = utcnow(), []
    for g, rows in usage.items():
        sc = scores.get(g)
        if not sc:
            continue
        pos, team = sc["position"], rows[-1]["team"]
        ev = recents[g]
        tctx = teams.get(team) or {}
        on_bye = has_lines and tctx.get("next_opponent") is None
        mlabel, mev = matchup_label(pos, defs.get(tctx.get("next_opponent")), on_bye)
        ev["matchup"] = mev
        ev["team"] = {k: tctx.get(k) for k in ("proe", "neutral_pass_rate", "sack_rate", "hit_or_sack_rate",
                                               "pfr_pressure_rate", "next_opponent", "next_implied_total")}
        if pos == "RB":
            if g in rb_pool and g in yac_pct and g in succ_pct:
                avg = (yac_pct[g] + succ_pct[g]) / 20.0
                eff = "ABOVE_AVERAGE" if avg >= 0.67 else "BELOW_AVERAGE" if avg <= 0.33 else "AVERAGE"
            else:
                eff = "INSUFFICIENT_SAMPLE"
            blocking = _tier3(ybc_pct.get(g), 6.7, 3.4, ("SUPPORTIVE", "AVERAGE", "POOR")) if g in rb_pool \
                else "INSUFFICIENT_SAMPLE"
            scoring = _tier3((ev["rz_carries_pg"] or 0) + (ev["rz_targets_pg"] or 0)
                             if ev["rz_carries_pg"] is not None else None, 3.0, 1.5)
        else:
            eff = {"PRODUCTION_EXCEEDS_USAGE": "ABOVE_EXPECTED", "USAGE_EXCEEDS_PRODUCTION": "BELOW_EXPECTED",
                   "ALIGNED": "AS_EXPECTED"}.get(sc["value_gap_label"], "INSUFFICIENT_SAMPLE")
            blocking = None
            scoring = _tier3(ev["rz_targets_pg"], 1.2, 0.6) if pos in ("WR", "TE") else "N/A"
        imp = tctx.get("next_implied_total")
        env = "BYE" if on_bye else _tier3(imp, IMPLIED_GOOD, IMPLIED_POOR + 0.01, ("GOOD", "AVERAGE", "POOR"))
        # No ESPN mapping needed for NFL-only context; an unmapped player can still be HIGH on NFL data.
        conf = ("HIGH" if sc["games"] >= 4 and sc["opportunity_basis"] == "xfp" and mapping.get(g, "high") == "high"
                else "MEDIUM" if sc["games"] >= 3 else "LOW")
        labels = {
            "role": {"HIGH": "STRONG", "MEDIUM": "MODERATE", "LOW": "LIMITED"}.get(sc["opportunity_tier"], "UNKNOWN"),
            "role_trend": sc["role_trend"], "efficiency": eff, "scoring_opportunity": scoring,
            "offensive_environment": env, "matchup": mlabel, "data_confidence": conf,
        }
        if blocking is not None:
            labels["run_blocking"] = blocking
        ev["headlines"] = headlines(pos, ev, labels)
        ev["rules"] = {
            "role": "opportunity_tier (absolute per-position xFP/game thresholds)",
            "efficiency": (f"RB: mean percentile of YAC/att and rushing success among RBs with >= "
                           f"{MIN_RB_CARRIES_EFFICIENCY} designed rushes" if pos == "RB"
                           else "PPR vs expected PPR (value gap)"),
            "offensive_environment": f"next implied team total: >= {IMPLIED_GOOD} GOOD, <= {IMPLIED_POOR} POOR",
            "data_confidence": "HIGH: >= 4 games, xFP basis, ID matched by crosswalk; MEDIUM: >= 3 games",
        }
        out.append({"season": season, "gsis_id": g, "position": pos, "team": team, "labels": labels,
                    "evidence": ev, "computed_at": now})
    return replace_where(con, "player_context", out, "season = ?", [season])


def build_context(con, season: int, next_week: int | None) -> dict:
    return {
        "team_game_lines": build_team_game_lines(con, season),
        "team_context": build_team_context(con, season, next_week),
        "defense_context": build_defense_context(con, season),
        "player_context": build_player_context(con, season, next_week),
        "next_week": next_week,
    }
