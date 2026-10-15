"""Stage 2: pick the week's usage risers and fallers and turn every number into a sourced claim.

Selection is deterministic Python; the LLM never picks players or computes a stat. Each claim records
how to re-derive its value from snapshot.json (`source`), and `evaluate_claim` does exactly that, so
the QC gate can prove every number in the narration matches the data.

Usage only: snap share, target share, air-yard share, backfield rush share, team context (PROE, QB)
and the schedule. No fantasy points, xFP, PPG or projections.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from statistics import mean, median

from ..core import STUDIO_ROOT, Episode, read_json, run_stage, write_json
from ..numbers import render

sys.path.insert(0, str(STUDIO_ROOT.parent / "backend"))
from app.analytics.trends import compute_trend, composite_trend  # noqa: E402  (pure stdlib module)

VERSION = 1
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
# Matchup bands as in backend analytics/context.py (rank 1 = most generous of 32 defenses).
FAVORABLE_RANK, TOUGH_RANK = 10, 23
# PROE is in percentage points; within ±2 we call a team close to neutral.
PROE_LEAN = 2.0


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
        return (mean(_usage_values(snap, src["gsis_id"], src["column"], src["recent_weeks"]))
                - mean(_usage_values(snap, src["gsis_id"], src["column"], src["baseline_weeks"])))
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
        proe = next(r for r in snap["tables"]["team_context"] if r["team"] == src["team"])["proe"]
        return ("pass-leaning" if proe >= PROE_LEAN else "run-leaning" if proe <= -PROE_LEAN
                else "close to neutral")
    if kind == "defense_band":
        rank = next(r for r in snap["tables"]["defense_context"] if r["defteam"] == src["team"])["ranks"][src["rank_key"]]
        return ("favorable" if rank <= FAVORABLE_RANK else "tough" if rank >= TOUGH_RANK
                else "middle-of-the-pack")
    if kind == "week_value":
        return src["week"]
    if kind == "episode_week":
        return snap["week"]
    if kind == "constant_weeks":
        return src["weeks"]
    raise ValueError(f"unknown claim source kind {kind!r}")


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


def _latest_game_confirms(rows: list[dict], trend, up: bool) -> bool:
    """The most recent game on its own moved past the threshold versus the MEDIAN earlier game, so a single
    injury-shortened game (in either window) cannot read as a role change."""
    latest = next((r[trend.metric] for r in reversed(rows) if r[trend.metric] is not None), None)
    base = [r[trend.metric] for r in rows if r["week"] in trend.baseline_weeks and r[trend.metric] is not None]
    if latest is None or not base:
        return False
    change = latest - median(base)
    return change >= trend.threshold if up else change <= -trend.threshold


def select_players(snap: dict, n_risers: int, n_fallers: int, max_per_position: int,
                   recent_featured: set[str]) -> tuple[list[dict], list[dict]]:
    names = {p["gsis_id"]: p["display_name"] for p in snap["tables"]["nfl_players"]}
    week = snap["week"]
    out_now = {i["gsis_id"] for i in snap["tables"]["nfl_injuries"]
               if i["week"] == week and i["report_status"] in EXCLUDE_STATUSES}
    # A usage drop next to an injury designation is an injury story, not a role story: skip it.
    on_report = {i["gsis_id"] for i in snap["tables"]["nfl_injuries"] if i["report_status"]}
    candidates = {"riser": [], "faller": []}
    for gsis_id, rows in _player_series(snap).items():
        position = rows[-1]["position"]
        if position not in METRICS or gsis_id in out_now or gsis_id not in names or not _is_current(rows, snap):
            continue
        trends, score, label = _trends(rows, position)
        if score is None or label not in ("UP", "DOWN"):
            continue
        usable = [t for t in trends if t.delta is not None]
        lead = max(usable, key=lambda t: abs(t.delta / t.threshold))
        up = label == "UP"
        if not _no_missed_games(rows, snap, lead.recent_weeks) or not _latest_game_confirms(rows, lead, up):
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
    # Lead with the metric that moved most relative to its threshold.
    metrics.sort(key=lambda m: -abs(m["delta_over_threshold"]))
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
            "pass_tendency": book.add("team tendency versus expectation (describes the PROE sign)", "text",
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
    return facts


def build_facts(snap: dict, cfg_selection: dict, recent_featured: set[str]) -> dict:
    risers, fallers = select_players(snap, cfg_selection["risers"], cfg_selection["fallers"],
                                     cfg_selection["max_per_position"], recent_featured)
    book = ClaimBook(snap)
    episode = {
        "week": book.add("episode week", "week", {"kind": "episode_week"}),
        "playoff_weeks": book.add("fantasy playoff weeks", "weeks_range",
                                  {"kind": "constant_weeks", "weeks": PLAYOFF_WEEKS}),
    }
    players = ([player_facts(book, snap, e, "riser") for e in risers]
               + [player_facts(book, snap, e, "faller") for e in fallers])
    return {"season": snap["season"], "week": snap["week"], "data_through_week": snap["data_through_week"],
            "snapshot_sha256": snap["content_sha256"], "episode": episode, "players": players,
            "claims": book.claims}


def recent_featured_players(history_path, window: int, before_week: int, season: int) -> set[str]:
    if not history_path.exists():
        return set()
    import json
    entries = [json.loads(l) for l in history_path.read_text().splitlines() if l.strip()]
    entries = [e for e in entries if e["season"] == season and e["week"] < before_week and e["format"] == "long"]
    entries.sort(key=lambda e: e["week"])
    return {p for e in entries[-window:] for p in e["players"]}


def _run(ep: Episode) -> None:
    snap = read_json(ep.path("snapshot.json"))
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
    run_stage(ep, "facts", VERSION, {"selection": ep.cfg["selection"], "recent_featured": sorted(recent)},
              ["snapshot.json"], ["facts.json"], lambda: _run(ep))
