"""Stage 2: pick the week's usage risers and fallers and turn every number into a sourced claim.

Selection is deterministic Python; the LLM never picks players or computes a stat. Each claim records
how to re-derive its value from snapshot.json (`source`), and `evaluate_claim` does exactly that, so
the QC gate can prove every number in the narration matches the data.

Usage only: snap share, target share, air-yard share, backfield rush share, team context (PROE, QB)
and the schedule. No fantasy points, xFP, PPG or projections.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from statistics import mean, median

from ..core import STUDIO_ROOT, Episode, read_json, run_stage, write_json
from ..numbers import render, round_half_up

sys.path.insert(0, str(STUDIO_ROOT.parent / "backend"))
from app.analytics.trends import compute_trend, composite_trend  # noqa: E402  (pure stdlib module)

VERSION = 5
PLAYOFF_WEEKS = [15, 16, 17]

# (metric column, label, trend threshold). Thresholds for offense_pct/target_share/backfield_rush_share
# are the backend's TREND_METRICS values; air_yards_share has no backend threshold, 0.06 is ours.
METRICS = {
    "WR": [("offense_pct", "snap share", 0.08), ("target_share", "target share", 0.04),
           ("air_yards_share", "air-yard share", 0.06)],
    "TE": [("offense_pct", "snap share", 0.08), ("target_share", "target share", 0.04),
           ("air_yards_share", "air-yard share", 0.06)],
    "RB": [("offense_pct", "snap share", 0.08), ("backfield_rush_share", "backfield rush share", 0.10),
           ("target_share", "target share", 0.04)],
}
# Which defense rank describes a matchup for each position (defense_context.ranks keys; 1 = most generous).
DEFENSE_RANK_KEY = {"WR": "pass_epa_allowed", "TE": "te_target_share_allowed", "RB": "rush_epa_allowed"}
DEFENSE_RANK_LABEL = {"pass_epa_allowed": "EPA allowed per pass play",
                      "te_target_share_allowed": "target share allowed to tight ends",
                      "rush_epa_allowed": "EPA allowed per designed rush"}
EXCLUDE_STATUSES = {"Out", "Doubtful"}
# Metrics stable enough to carry a story. Air-yard share swings with a single deep ball (and goes negative on
# screens), so it only ever supports.
LEAD_METRICS = {"offense_pct", "target_share", "backfield_rush_share"}
# Matchup bands as in backend analytics/context.py (rank 1 = most generous of 32 defenses).
FAVORABLE_RANK, TOUGH_RANK = 10, 23


# --------------------------------------------------------------------------------------------- claims

def _usage_values(snap: dict, gsis_id: str, column: str, weeks: list[int]) -> list[float]:
    return [r[column] for r in snap["tables"]["player_week_usage"]
            if r["gsis_id"] == gsis_id and r["week"] in weeks and r[column] is not None]


def _team_games(snap: dict, team: str) -> dict[int, dict]:
    return {g["week"]: g for g in snap["tables"]["nfl_schedules"] if team in (g["home_team"], g["away_team"])}


def _rank_desc(rows: list[dict], key: str, team_key: str, team: str) -> int:
    """1 = highest value among rows with a value."""
    vals = sorted((r[key] for r in rows if r[key] is not None), reverse=True)
    mine = next(r[key] for r in rows if r[team_key] == team)
    return vals.index(mine) + 1


def evaluate_claim(snap: dict, src: dict):
    """Re-derive a claim's raw value from the snapshot. Raises on anything unknown."""
    kind = src["kind"]
    if kind == "usage_mean":
        return mean(_usage_values(snap, src["gsis_id"], src["column"], src["weeks"]))
    if kind == "usage_delta":
        # The change between the two percentages AS SHOWN (each rounded to a whole percent), so the
        # on-screen arithmetic always adds up: 68% -> 65% is -3 pts, never -4 from unrounded means.
        recent = mean(_usage_values(snap, src["gsis_id"], src["column"], src["recent_weeks"]))
        base = mean(_usage_values(snap, src["gsis_id"], src["column"], src["baseline_weeks"]))
        return float(round_half_up(recent * 100) - round_half_up(base * 100)) / 100
    if kind == "usage_value":
        vals = _usage_values(snap, src["gsis_id"], src["column"], [src["week"]])
        return vals[0]
    if kind == "games_count":
        return len(src["weeks"]) if _usage_values(snap, src["gsis_id"], "offense_pct", src["weeks"]) else 0
    if kind == "player_team":
        return next(r["team"] for r in snap["tables"]["player_week_usage"]
                    if r["gsis_id"] == src["gsis_id"] and r["week"] == src["week"])
    if kind == "team_context":
        row = next(r for r in snap["tables"]["team_context"] if r["team"] == src["team"])
        return row[src["column"]]
    if kind == "team_context_rank":
        return _rank_desc(snap["tables"]["team_context"], src["column"], "team", src["team"])
    if kind == "defense_rank":
        row = next(r for r in snap["tables"]["defense_context"] if r["defteam"] == src["team"])
        return row["ranks"][src["rank_key"]]
    if kind == "qb_names":
        games = _team_games(snap, src["team"])
        names = []
        for w in src["weeks"]:
            g = games[w]
            name = g["home_qb_name"] if g["home_team"] == src["team"] else g["away_qb_name"]
            if name and name not in names:
                names.append(name)
        return names
    if kind == "head_coach":
        g = _team_games(snap, src["team"])[src["week"]]
        return g["home_coach"] if g["home_team"] == src["team"] else g["away_coach"]
    if kind == "opponent":
        g = _team_games(snap, src["team"]).get(src["week"])
        if g is None:
            return "bye"
        return g["away_team"] if g["home_team"] == src["team"] else g["home_team"]
    if kind == "proe_tendency":
        # Relative to the league, not the PROE sign: when most teams sit below expectation, a negative PROE
        # can still be league-average, and calling the 16th-ranked team "run-leaning" misleads.
        rank = _rank_desc(snap["tables"]["team_context"], "proe", "team", src["team"])
        return ("one of the league's more pass-heavy offenses" if rank <= FAVORABLE_RANK
                else "one of the league's more run-heavy offenses" if rank >= TOUGH_RANK
                else "middle of the pack in pass tendency")
    if kind == "defense_band":
        rank = next(r for r in snap["tables"]["defense_context"] if r["defteam"] == src["team"])["ranks"][src["rank_key"]]
        return ("favorable" if rank <= FAVORABLE_RANK else "tough" if rank >= TOUGH_RANK
                else "middle-of-the-pack")
    if kind == "playoff_opponents":
        return [evaluate_claim(snap, {"kind": "opponent", "team": src["team"], "week": w}) for w in src["weeks"]]
    if kind == "playoff_summary":
        grades = []
        for w in src["weeks"]:
            opp = evaluate_claim(snap, {"kind": "opponent", "team": src["team"], "week": w})
            if opp != "bye":
                grades.append(evaluate_claim(snap, {"kind": "defense_band", "team": opp, "rank_key": src["rank_key"]}))
        return grade_summary(grades)
    if kind == "absent_weeks":
        played = {r["week"] for r in snap["tables"]["player_week_usage"] if r["gsis_id"] == src["gsis_id"]}
        games = _team_games(snap, src["team"])
        return [w for w in src["weeks"] if w in games and w not in played]
    if kind == "absence_reason":
        rows = [i for i in snap["tables"]["nfl_injuries"] if i["gsis_id"] == src["gsis_id"]
                and i["week"] in src["weeks"] and i["report_primary_injury"]]
        return f"a {rows[-1]['report_primary_injury'].lower()} injury" if rows else None
    if kind == "player_status":
        week = src["week"]
        if any(r["gsis_id"] == src["gsis_id"] and r["week"] == week and r["status"] == "RES"
               for r in snap["tables"]["nfl_rosters_weekly"]):
            return ["ir", week, ""]
        rep = [i for i in snap["tables"]["nfl_injuries"] if i["gsis_id"] == src["gsis_id"] and i["week"] == week]
        if rep and rep[-1]["report_status"]:
            return ["designation", week, rep[-1]["report_status"]]
        if rep and rep[-1]["practice_status"]:
            return ["practice", week, rep[-1]["practice_status"]]
        return ["none", week, ""]
    if kind == "week_value":
        return src["week"]
    if kind == "episode_week":
        return snap["week"]
    if kind == "constant_weeks":
        return src["weeks"]
    raise ValueError(f"unknown claim source kind {kind!r}")


def grade_summary(grades: list[str]) -> str:
    """['favorable', 'tough', 'favorable'] -> 'two favorable matchups and one tough one'."""
    from ..numbers import int_words
    order = ["favorable", "middle-of-the-pack", "tough"]
    counts = [(g, grades.count(g)) for g in order if grades.count(g)]
    if not counts:
        return "no games"
    if len(counts) == 1:
        g, n = counts[0]
        return f"{int_words(n) if n > 1 else 'one'} {g} matchup{'s' if n > 1 else ''}"
    parts = [f"{int_words(n)} {g} matchup{'s' if n > 1 else ''}" if i == 0 else f"{int_words(n)} {g} one{'s' if n > 1 else ''}"
             for i, (g, n) in enumerate(counts)]
    return ", ".join(parts[:-1]) + f" and {parts[-1]}" if len(parts) > 2 else f"{parts[0]} and {parts[1]}"


class ClaimBook:
    def __init__(self, snap: dict):
        self.snap = snap
        self.claims: list[dict] = []

    def add(self, label: str, fmt: str, source: dict, player: str | None = None, team: str | None = None) -> str:
        value = evaluate_claim(self.snap, source)
        display, spoken = render(fmt, value)
        cid = f"c{len(self.claims) + 1:03d}"
        self.claims.append({"id": cid, "label": label, "player": player, "team": team, "format": fmt,
                            "value": value, "display": display, "spoken": spoken, "source": source})
        return cid


# ------------------------------------------------------------------------------------------ selection

def _player_series(snap: dict) -> dict[str, list[dict]]:
    by_player: dict[str, list[dict]] = defaultdict(list)
    for r in snap["tables"]["player_week_usage"]:
        by_player[r["gsis_id"]].append(r)
    return {k: sorted(v, key=lambda r: r["week"]) for k, v in by_player.items()}


def _trends(rows: list[dict], position: str):
    trends = [compute_trend(col, [(r["week"], r[col]) for r in rows], thr) for col, _, thr in METRICS[position]]
    score, label = composite_trend(trends)
    return trends, score, label


def _is_current(rows: list[dict], snap: dict) -> bool:
    """Played in the latest data week, or the team was on bye then and played the week before."""
    last, data_week = rows[-1]["week"], snap["data_through_week"]
    if last == data_week:
        return True
    games = _team_games(snap, rows[-1]["team"])
    return last == data_week - 1 and data_week not in games


def _relevant(position: str, t: dict[str, float | None], riser: bool) -> bool:
    snap_share, tshare = t.get("offense_pct") or 0, t.get("target_share") or 0
    if position == "RB":
        return snap_share >= (0.45 if riser else 0.5) or (t.get("backfield_rush_share") or 0) >= (0.45 if riser else 0.5)
    if riser:
        return snap_share >= 0.55 or tshare >= 0.15
    return snap_share >= 0.65 and tshare >= 0.15


def _no_missed_games(rows: list[dict], snap: dict, weeks: list[int]) -> bool:
    """The player appeared in every team game from the start of the window through the latest data week."""
    played = {r["week"] for r in rows}
    team_weeks = {w for w in _team_games(snap, rows[-1]["team"]) if weeks[0] <= w <= snap["data_through_week"]}
    return team_weeks <= played


def _sustained(rows: list[dict], trend, up: bool) -> bool:
    """EVERY game in the recent window moved past the threshold versus the MEDIAN earlier game.

    One collapsed game (an in-game injury exit, an ejection, a blowout) is the commonest false "faller":
    49/52/63/56% snaps then 14% is not a lost role. Requiring each recent game to show the move, against a
    median that one odd earlier game cannot drag, keeps single-game noise out in both directions."""
    base = [r[trend.metric] for r in rows if r["week"] in trend.baseline_weeks and r[trend.metric] is not None]
    recent = [r[trend.metric] for r in rows if r["week"] in trend.recent_weeks and r[trend.metric] is not None]
    if not base or len(recent) < len(trend.recent_weeks):
        return False
    mid = median(base)
    return all((v - mid >= trend.threshold) if up else (v - mid <= -trend.threshold) for v in recent)


def select_players(snap: dict, n_risers: int, n_fallers: int, max_per_position: int,
                   recent_featured: set[str]) -> tuple[list[dict], list[dict]]:
    names = {p["gsis_id"]: p["display_name"] for p in snap["tables"]["nfl_players"]}
    week = snap["week"]
    out_now = {i["gsis_id"] for i in snap["tables"]["nfl_injuries"]
               if i["week"] == week and i["report_status"] in EXCLUDE_STATUSES}
    # A usage drop next to an injury designation is an injury story, not a role story: skip it.
    on_report = {i["gsis_id"] for i in snap["tables"]["nfl_injuries"]
                 if i["report_status"] and i["week"] >= week - 1}
    candidates = {"riser": [], "faller": []}
    for gsis_id, rows in _player_series(snap).items():
        position = rows[-1]["position"]
        if position not in METRICS or gsis_id in out_now or gsis_id not in names or not _is_current(rows, snap):
            continue
        trends, score, label = _trends(rows, position)
        if score is None or label not in ("UP", "DOWN"):
            continue
        usable = [t for t in trends if t.delta is not None]
        leads = [t for t in usable if t.metric in LEAD_METRICS]
        if not leads:
            continue
        lead = max(leads, key=lambda t: abs(t.delta / t.threshold))
        up = label == "UP"
        if (lead.delta > 0) != up or not _no_missed_games(rows, snap, lead.recent_weeks) \
                or not _sustained(rows, lead, up):
            continue
        recent = {t.metric: t.recent_avg for t in trends}
        baseline = {t.metric: t.baseline_avg for t in trends}
        entry = {"gsis_id": gsis_id, "name": names[gsis_id], "position": position, "team": rows[-1]["team"],
                 "score": score, "strength": mean(t.delta / t.threshold for t in usable), "trends": trends}
        if up and _relevant(position, recent, riser=True):
            candidates["riser"].append(entry)
        elif not up and gsis_id not in on_report and _relevant(position, baseline, riser=False):
            candidates["faller"].append(entry)

    def pick(pool: list[dict], n: int, descending: bool) -> list[dict]:
        # Recently featured players go to the back of the queue; then the clamped composite, then the
        # unclamped strength so ties at the clamp are broken by the size of the move.
        sign = -1 if descending else 1
        pool = sorted(pool, key=lambda e: (e["gsis_id"] in recent_featured, sign * e["score"],
                                           sign * e["strength"], e["gsis_id"]))
        chosen, per_pos = [], defaultdict(int)
        for e in pool:
            if per_pos[e["position"]] < max_per_position:
                chosen.append(e)
                per_pos[e["position"]] += 1
            if len(chosen) == n:
                break
        return chosen

    return pick(candidates["riser"], n_risers, True), pick(candidates["faller"], n_fallers, False)


# ------------------------------------------------------------------------------------------- facts

def player_facts(book: ClaimBook, snap: dict, entry: dict, role: str) -> dict:
    gid, team, pos = entry["gsis_id"], entry["team"], entry["position"]
    trends = entry["trends"]
    t0 = trends[0]
    recent_w, base_w = t0.recent_weeks, t0.baseline_weeks
    rows = [r for r in snap["tables"]["player_week_usage"] if r["gsis_id"] == gid]
    metrics = []
    for (col, label, _), t in zip(METRICS[pos], trends):
        if t.delta is None:
            continue
        metrics.append({
            "column": col, "label": label,
            "recent": book.add(f"{label}, recent games", "pct",
                               {"kind": "usage_mean", "gsis_id": gid, "column": col, "weeks": t.recent_weeks},
                               player=gid, team=team),
            "baseline": book.add(f"{label}, earlier games", "pct",
                                 {"kind": "usage_mean", "gsis_id": gid, "column": col, "weeks": t.baseline_weeks},
                                 player=gid, team=team),
            "change": book.add(f"{label} change", "pp",
                               {"kind": "usage_delta", "gsis_id": gid, "column": col,
                                "recent_weeks": t.recent_weeks, "baseline_weeks": t.baseline_weeks},
                               player=gid, team=team),
            "delta_over_threshold": t.delta / t.threshold,
            "series": [{"week": r["week"], "value": r[col]} for r in rows],
        })
    # Lead with the stable metric that moved most relative to its threshold (same rule as selection).
    metrics.sort(key=lambda m: (m["column"] not in LEAD_METRICS, -abs(m["delta_over_threshold"])))
    volume_col = "targets" if pos != "RB" else "designed_rushes"
    games = _team_games(snap, team)
    last_week = recent_w[-1]
    facts = {
        "gsis_id": gid, "name": entry["name"], "position": pos, "team": team, "role": role,
        "team_name": book.add("team", "team", {"kind": "player_team", "gsis_id": gid, "week": last_week},
                              player=gid, team=team),
        "composite_score": entry["score"], "recent_weeks": recent_w, "baseline_weeks": base_w,
        "recent_games": book.add("recent games in the window", "int",
                                 {"kind": "games_count", "gsis_id": gid, "weeks": recent_w}, player=gid),
        "baseline_games": book.add("earlier games in the window", "int",
                                   {"kind": "games_count", "gsis_id": gid, "weeks": base_w}, player=gid),
        "metrics": metrics,
        "volume": {"label": "targets per game" if pos != "RB" else "designed rushes per game",
                   "recent": book.add(f"{volume_col} per game, recent", "dec1",
                                      {"kind": "usage_mean", "gsis_id": gid, "column": volume_col, "weeks": recent_w},
                                      player=gid)},
        "context": {
            "qbs_recent": book.add("starting QB in recent games", "text",
                                   {"kind": "qb_names", "team": team, "weeks": recent_w}, team=team),
            "head_coach": book.add("head coach", "text", {"kind": "head_coach", "team": team, "week": last_week}, team=team),
            "proe": book.add("pass rate over expectation, season", "signed_pct1",
                             {"kind": "team_context", "team": team, "column": "proe"}, team=team),
            "proe_rank": book.add("league rank in pass rate over expectation (first = most pass-heavy)", "ordinal",
                                  {"kind": "team_context_rank", "team": team, "column": "proe"}, team=team),
            "pass_tendency": book.add("team pass tendency versus the league (from the PROE rank)", "text",
                                      {"kind": "proe_tendency", "team": team}, team=team),
            "neutral_pass_rate": book.add("neutral-situation pass rate", "pct",
                                          {"kind": "team_context", "team": team, "column": "neutral_pass_rate"}, team=team),
        },
        "schedule": [],
    }
    if pos != "RB":
        facts["volume"]["adot"] = book.add("average depth of target, recent", "dec1",
                                           {"kind": "usage_mean", "gsis_id": gid, "column": "adot", "weeks": recent_w},
                                           player=gid)
    rank_key = DEFENSE_RANK_KEY[pos]
    for week in range(snap["week"], 19):
        opp_src = {"kind": "opponent", "team": team, "week": week}
        opp = evaluate_claim(snap, opp_src)
        item = {"week": week, "playoff": week in PLAYOFF_WEEKS, "opponent_value": opp}
        if week in PLAYOFF_WEEKS:
            # Labels carry no digits: the model copies them. The week itself is a placeholder.
            wk = book.add("a fantasy-playoff week", "week", {"kind": "week_value", "week": week})
            item["week_label"] = wk
            item["opponent"] = book.add(f"opponent in {{{{{wk}}}}}", "team", opp_src, team=team)
        if opp != "bye":
            item["home"] = games[week]["home_team"] == team
            rank_src = {"kind": "defense_rank", "team": opp, "rank_key": rank_key}
            item["rank_value"] = evaluate_claim(snap, rank_src)
            if week in PLAYOFF_WEEKS:
                item["rank"] = book.add(f"that opponent's league rank in {DEFENSE_RANK_LABEL[rank_key]} so far this "
                                        "season (first = most generous)", "ordinal", rank_src, team=opp)
                item["band"] = book.add("matchup grade, an adjective: write 'a {{x}} matchup'", "text",
                                        {"kind": "defense_band", "team": opp, "rank_key": rank_key}, team=opp)
        facts["schedule"].append(item)
    facts["defense_rank_label"] = DEFENSE_RANK_LABEL[rank_key]
    facts["playoff"] = {
        "opponents": book.add("fantasy-playoff opponents in order", "opponents",
                              {"kind": "playoff_opponents", "team": team, "weeks": PLAYOFF_WEEKS}, team=team),
        "summary": book.add("fantasy-playoff matchups by defense rank so far this season", "text",
                            {"kind": "playoff_summary", "team": team, "weeks": PLAYOFF_WEEKS, "rank_key": rank_key},
                            team=team),
    }
    return facts


# Whose role a story player could be absorbing (or losing back to): same position group, same team.
TEAMMATE_GROUP = {"WR": ("WR", "TE"), "TE": ("WR", "TE"), "RB": ("RB",)}
TEAMMATE_ROLE = {"WR": ("target_share", 0.12, "targets"), "TE": ("target_share", 0.12, "targets"),
                 "RB": ("backfield_rush_share", 0.30, "running back carries")}
# Committee backs rarely clear 40% of snaps even with a real role.
TEAMMATE_MIN_SNAPS = {"WR": 0.40, "TE": 0.40, "RB": 0.30}


def teammate_event(snap: dict, p: dict) -> dict | None:
    """The injury context a role change needs.

    Riser: a teammate who held a real role in the earlier window and MISSED games in the recent window (no
    snaps while the team played) is a vacated role; the riser may only be filling in.
    Faller: a teammate who missed games in the earlier window and is back with a real role in the recent window
    took his role back.
    Missing a game is read from the usage data itself (the team played, he had no snaps); the reason and the
    current status come from the official injury report and weekly roster status."""
    col, floor, unit = TEAMMATE_ROLE[p["position"]]
    rows = defaultdict(dict)
    for r in snap["tables"]["player_week_usage"]:
        if r["team"] == p["team"] and r["position"] in TEAMMATE_GROUP[p["position"]] and r["gsis_id"] != p["gsis_id"]:
            rows[r["gsis_id"]][r["week"]] = r
    games = _team_games(snap, p["team"])
    best = None
    for gid, by_week in rows.items():
        base = [w for w in p["baseline_weeks"] if w in by_week]
        rec = [w for w in p["recent_weeks"] if w in by_week]
        missed_recent = [w for w in p["recent_weeks"] if w in games and w not in by_week]
        missed_base = [w for w in p["baseline_weeks"] if w in games and w not in by_week]
        ev = None
        if p["role"] == "riser" and base and missed_recent:
            role = median(by_week[w][col] or 0 for w in base)
            snaps = median(by_week[w]["offense_pct"] or 0 for w in base)
            if role >= floor and snaps >= TEAMMATE_MIN_SNAPS[p["position"]]:
                ev = {"kind": "vacated", "gsis_id": gid, "role_weeks": base, "missed_weeks": p["recent_weeks"],
                      "returned": p["recent_weeks"][-1] in by_week, "strength": role}
        elif p["role"] == "faller" and rec == p["recent_weeks"] and missed_base:
            role = median(by_week[w][col] or 0 for w in rec)
            if role >= floor:
                ev = {"kind": "returned", "gsis_id": gid, "role_weeks": rec, "missed_weeks": p["baseline_weeks"],
                      "returned": True, "strength": role}
        if ev is None:
            continue
        if best is None or ev["strength"] > best["strength"]:
            best = ev
    if best:
        best.update(column=col, unit=unit)
    return best


def teammate_facts(book: "ClaimBook", snap: dict, p: dict) -> dict | None:
    ev = teammate_event(snap, p)
    if not ev:
        return None
    names = {x["gsis_id"]: x["display_name"] for x in snap["tables"]["nfl_players"]}
    gid, team = ev["gsis_id"], p["team"]
    absent_src = {"kind": "absent_weeks", "team": team, "gsis_id": gid, "weeks": ev["missed_weeks"]}
    missed = evaluate_claim(snap, absent_src)
    out = {"kind": ev["kind"], "gsis_id": gid, "name": names.get(gid, gid), "returned": ev["returned"],
           "unit": ev["unit"], "column": ev["column"],
           "missed": book.add(f"weeks {names.get(gid, gid)} missed", "weeks_list", absent_src, player=gid, team=team),
           "role": book.add(f"{names.get(gid, gid)}'s {ev['column'].replace('_', ' ')} "
                            f"{'before he missed time' if ev['kind'] == 'vacated' else 'since he returned'}", "pct",
                            {"kind": "usage_mean", "gsis_id": gid, "column": ev["column"], "weeks": ev["role_weeks"]},
                            player=gid, team=team),
           "status": book.add(f"{names.get(gid, gid)}'s latest availability", "status",
                              {"kind": "player_status", "gsis_id": gid, "week": snap["week"]}, player=gid, team=team),
           # A list, not a {week: value} dict: JSON would turn the int week keys into strings.
           "series": sorted(({"week": r["week"], "value": r[ev["column"]]} for r in snap["tables"]["player_week_usage"]
                             if r["gsis_id"] == gid), key=lambda x: x["week"]),
           "missed_values": missed}
    reason_src = {"kind": "absence_reason", "gsis_id": gid, "weeks": missed}
    if evaluate_claim(snap, reason_src):
        out["reason"] = book.add(f"why {names.get(gid, gid)} missed time (injury report)", "text", reason_src,
                                 player=gid, team=team)
    if ev["kind"] == "vacated" and ev["returned"]:
        back = [w for w in p["recent_weeks"] if w not in missed]
        out["back_week"] = book.add(f"week {names.get(gid, gid)} returned", "week",
                                    {"kind": "week_value", "week": back[0]}, team=team)
    return out


def build_facts(snap: dict, cfg_selection: dict, recent_featured: set[str],
                only: str | None = None) -> dict:
    """Facts for the long-form episode, or (`only` = gsis_id) for one Short about that player."""
    risers, fallers = select_players(snap, cfg_selection["risers"], cfg_selection["fallers"],
                                     cfg_selection["max_per_position"], recent_featured)
    if only:
        risers = [e for e in risers if e["gsis_id"] == only]
        fallers = [e for e in fallers if e["gsis_id"] == only]
    book = ClaimBook(snap)
    episode = {
        "week": book.add("episode week", "week", {"kind": "episode_week"}),
        "playoff_weeks": book.add("fantasy playoff weeks", "weeks_range",
                                  {"kind": "constant_weeks", "weeks": PLAYOFF_WEEKS}),
    }
    players = ([player_facts(book, snap, e, "riser") for e in risers]
               + [player_facts(book, snap, e, "faller") for e in fallers])
    for p in players:
        p["teammate"] = teammate_facts(book, snap, p)
    return {"season": snap["season"], "week": snap["week"], "data_through_week": snap["data_through_week"],
            "snapshot_sha256": snap["content_sha256"], "episode": episode, "players": players,
            "claims": book.claims}


def recent_featured_players(history_path, window: int, before_week: int, season: int,
                            fmt: str = "long") -> set[str]:
    if not history_path.exists():
        return set()
    import json
    entries = [json.loads(l) for l in history_path.read_text().splitlines() if l.strip()]
    entries = [e for e in entries if e["season"] == season and e["week"] < before_week and e["format"] == fmt]
    weeks = sorted({e["week"] for e in entries})[-window:]
    return {p for e in entries if e["week"] in weeks for p in e["players"]}


ALL = {"risers": 99, "fallers": 99, "max_per_position": 99}


def pick_stories(snap: dict, n_risers: int, n_fallers: int, recent_featured: set[str]) -> list[dict]:
    """Shorts: the strongest sustained moves, one player per Short, at most one Short per team (two Bears
    Shorts would repeat the same QB and schedule lines), recently featured players last."""
    risers, fallers = select_players(snap, ALL["risers"], ALL["fallers"], ALL["max_per_position"], recent_featured)
    stories, teams = [], set()
    for pool, n, role in ((risers, n_risers, "riser"), (fallers, n_fallers, "faller")):
        taken = 0
        for e in pool:
            if taken == n or e["team"] in teams:
                continue
            teams.add(e["team"])
            taken += 1
            slug = re.sub(r"[^a-z0-9]+", "-", e["name"].lower()).strip("-")
            stories.append({"gsis_id": e["gsis_id"], "name": e["name"], "role": role, "team": e["team"],
                            "slug": slug, "order": len(stories)})
    return stories


def _run(ep: Episode) -> None:
    snap = read_json(ep.path("snapshot.json"))
    if ep.fmt == "short":
        story = read_json(ep.path("story.json"))
        facts = build_facts(snap, ALL, set(), only=story["gsis_id"])
        if len(facts["players"]) != 1:
            raise RuntimeError(f"{story['name']} no longer meets the selection rules")
        write_json(ep.path("facts.json"), facts)
        ep.log(f"  facts: {story['name']} ({story['role']}), {len(facts['claims'])} claims")
        return
    recent = recent_featured_players(ep.cfg.episodes_dir / "history.jsonl",
                                     ep.cfg["selection"]["recent_episode_window"], ep.week, ep.season)
    facts = build_facts(snap, ep.cfg["selection"], recent)
    if not facts["players"]:
        raise RuntimeError("no risers or fallers met the selection rules")
    write_json(ep.path("facts.json"), facts)
    ep.log(f"  facts: {sum(p['role'] == 'riser' for p in facts['players'])} risers, "
           f"{sum(p['role'] == 'faller' for p in facts['players'])} fallers, {len(facts['claims'])} claims")


def run(ep: Episode) -> None:
    recent = recent_featured_players(ep.cfg.episodes_dir / "history.jsonl",
                                     ep.cfg["selection"]["recent_episode_window"], ep.week, ep.season)
    inputs = ["snapshot.json"] + (["story.json"] if ep.fmt == "short" else [])
    run_stage(ep, "facts", VERSION, {"selection": ep.cfg["selection"], "recent_featured": sorted(recent)},
              inputs, ["facts.json"], lambda: _run(ep))
