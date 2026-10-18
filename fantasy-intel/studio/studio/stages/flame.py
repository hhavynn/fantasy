"""Flame / praise Shorts: one player, one game, roasted for a historically bad usage or efficiency line (flame) or
hyped for a monster one (praise). Playful about the stat line, never about the person.

How a candidate is chosen (deterministic, no model; every threshold is a constant below):

  1. The game is the latest week in the data. Each position has a handful of usage/efficiency metrics (METRICS):
     quarterbacks' passing air yards, air yards per attempt, passing EPA and yards per attempt (nflverse weekly
     stats); receivers' and tight ends' target share, air yards and targets; backs' share of the running-back
     carries and snap share (player_week_usage). Never fantasy points, projections, routes or first reads.
  2. Volume floors keep it to real roles and full games: a game only counts with 20+ attempts (QB), 50%+ of the
     snaps (WR/TE) or 30%+ (RB) (QUALIFY). A flame also needs an established role before it (FLAME_ROLE): a
     player is not roasted for a game he was never going to have.
  3. The game is compared with his position's distribution: every qualifying game at his position this season,
     latest week included. It must rank in the extreme 10% (EXTREME_SHARE): the 9th-lowest of 160 QB games, say.
  4. And with his own season: his games before it (at least MIN_PRIOR), pooled for rates (air yards over
     attempts) and averaged for per-game counts. A flame must be below his own norm, a praise above it.
  5. Strength = how far the game sits from the position (z-score) + half his own z-score (capped) + a bonus for
     the season's single most extreme game. The strongest metric leads; others in the same direction that are in
     the extreme third of the position are the "receipts". The strongest flame and the strongest praise, from
     different teams, are the week's two Shorts.

Every number is a claim whose source kind starts with `fp_`; `evaluate` re-derives it from snapshot.json, so the
QC data-claims gate checks these Shorts exactly as it checks the others.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from statistics import mean, pstdev

from .. import editorial as E
from ..core import Episode, canonical_json, read_json, run_stage, sha256_bytes, write_json
from ..numbers import render

VERSION = 1
KINDS = ("flame", "praise")
EXTREME_SHARE = 0.10           # the game must rank in this extreme share of the position's games this season...
EXTREME_MIN_RANK = 3           # ...or at worst this rank (small samples)
RECEIPT_SHARE = 1 / 3          # a supporting metric must sit in this extreme share to be shown as a receipt
MIN_PRIOR = 2                  # qualifying games before this one, for a baseline of his own
OWN_Z_CAP = 3.0

# A game counts (for him and for the position's distribution) only at real volume.
QUALIFY = {"QB": ("attempts", 20), "WR": ("offense_pct", 0.5), "TE": ("offense_pct", 0.5), "RB": ("offense_pct", 0.3)}
# A flame also needs an established role in his earlier games (mean of this column at least this).
FLAME_ROLE = {"QB": ("attempts", 20), "WR": ("target_share", 0.15), "TE": ("target_share", 0.12),
              "RB": ("backfield_rush_share", 0.45)}
POS_WORD = {"QB": "quarterback", "WR": "receiver", "TE": "tight end", "RB": "running back"}
POS_SCREEN = {"QB": "QB", "WR": "WR", "TE": "TE", "RB": "RB"}

# metric -> spec. table: qb (snapshot qb_week_passing) or usage (player_week_usage). num/den: per-game value is
# num/den (a rate), and his baseline pools them (sum/sum); without den the baseline is the per-game mean.
# kinds: which Shorts it may lead. sup: superlative words (flame, praise). floor: smallest spread used for his
# own z-score, so a player who did the same thing every week does not divide by ~0.
METRICS = {
    "air_yards": {"pos": ("QB",), "table": "qb", "num": "passing_air_yards", "den": None, "fmt": "int",
                  "label": "passing air yards", "screen": "PASSING AIR YARDS", "kinds": KINDS,
                  "sup": ("fewest", "most"), "floor": 30},
    "adot": {"pos": ("QB",), "table": "qb", "num": "passing_air_yards", "den": "attempts", "fmt": "dec1",
             "label": "air yards per attempt", "screen": "AIR YARDS PER ATTEMPT", "kinds": ("flame",),
             "sup": ("lowest", "highest"), "floor": 0.8},
    "epa": {"pos": ("QB",), "table": "qb", "num": "passing_epa", "den": None, "fmt": "dec1",
            "label": "passing EPA", "screen": "PASSING EPA", "kinds": KINDS, "sup": ("lowest", "highest"),
            "floor": 5},
    "ypa": {"pos": ("QB",), "table": "qb", "num": "passing_yards", "den": "attempts", "fmt": "dec1",
            "label": "yards per attempt", "screen": "YARDS PER ATTEMPT", "kinds": KINDS,
            "sup": ("lowest", "highest"), "floor": 0.8},
    "target_share": {"pos": ("WR", "TE"), "table": "usage", "num": "target_share", "den": None, "fmt": "pct",
                     "label": "target share", "screen": "TARGET SHARE", "kinds": KINDS,
                     "sup": ("lowest", "highest"), "floor": 0.04},
    "rec_air_yards": {"pos": ("WR", "TE"), "table": "usage", "num": "air_yards", "den": None, "fmt": "int",
                      "label": "air yards", "screen": "AIR YARDS", "kinds": KINDS, "sup": ("fewest", "most"),
                      "floor": 20},
    "targets": {"pos": ("WR", "TE", "RB"), "table": "usage", "num": "targets", "den": None, "fmt": "int",
                "label": "targets", "screen": "TARGETS", "kinds": ("praise",), "sup": ("fewest", "most"),
                "floor": 1.5},
    "backfield_share": {"pos": ("RB",), "table": "usage", "num": "backfield_rush_share", "den": None, "fmt": "pct",
                        "label": "share of the running back carries", "screen": "SHARE OF RB CARRIES",
                        "kinds": KINDS, "sup": ("lowest", "highest"), "floor": 0.08},
    "snap_share": {"pos": ("RB",), "table": "usage", "num": "offense_pct", "den": None, "fmt": "pct",
                   "label": "snap share", "screen": "SNAP SHARE", "kinds": ("praise",), "sup": ("lowest", "highest"),
                   "floor": 0.06},
}
# Context counts shown with the lead (the volume it happened on), by position.
VOLUME = {"QB": ("attempts", "throws", "ATTEMPTS"), "WR": ("targets", "targets", "TARGETS"),
          "TE": ("targets", "targets", "TARGETS"), "RB": ("carries", "carries", "CARRIES")}
SNAPS = ("offense_pct", "snap share", "SNAP SHARE")


# --------------------------------------------------------------------------------------- data access

TABLES = {"qb": "qb_week_passing", "usage": "player_week_usage", "box": "player_week_box"}
# Rates told as one number (num / den over the rows): a game, his season pooled, or the league's pooled.
RATES = {"adot": ("qb", "passing_air_yards", "attempts"), "ypa": ("qb", "passing_yards", "attempts"),
         "yac_share": ("qb", "passing_yards_after_catch", "passing_yards")}
CONSTANTS = {"season_min_attempts": 100}     # "quarterbacks with 100+ attempts this season"


def _table(snap: dict, name: str) -> list[dict]:
    return snap["tables"].get(TABLES[name], [])


def _pos_rows(snap: dict, pos: str) -> list[dict]:
    if pos == "QB":
        return list(_table(snap, "qb"))
    return [r for r in _table(snap, "usage") if r["position"] == pos]


def qualifies(pos: str, row: dict) -> bool:
    col, floor = QUALIFY[pos]
    return (row.get(col) or 0) >= floor


def game_value(metric: str, row: dict) -> float | None:
    spec = METRICS[metric]
    num = row.get(spec["num"])
    if num is None:
        return None
    if spec["den"]:
        den = row.get(spec["den"]) or 0
        return num / den if den else None
    return num


def own_baseline(metric: str, rows: list[dict]) -> float:
    """His norm before the game: pooled for a rate (total air yards / total attempts), else the per-game mean."""
    spec = METRICS[metric]
    if spec["den"]:
        return sum(r[spec["num"]] for r in rows) / sum(r[spec["den"]] for r in rows)
    return mean(game_value(metric, r) for r in rows)


def distribution(snap: dict, metric: str, pos: str, through: int) -> list[tuple[str, int, float]]:
    """(gsis_id, week, value) for every qualifying game at the position this season, through `through`."""
    out = []
    for r in _pos_rows(snap, pos):
        if r["week"] <= through and qualifies(pos, r):
            v = game_value(metric, r)
            if v is not None:
                out.append((r["gsis_id"], r["week"], v))
    return out


def _row(snap: dict, table: str, gsis_id: str, week: int) -> dict:
    return next(r for r in _table(snap, table) if r["gsis_id"] == gsis_id and r["week"] == week)


def _beyond(values: list[float], x: float, kind: str) -> int:
    """How many values are strictly more extreme than x (lower for a flame, higher for a praise)."""
    return sum(1 for v in values if (v < x if kind == "flame" else v > x))


def evaluate(snap: dict, src: dict):
    """Re-derive an fp_ claim from the snapshot (called by facts.evaluate_claim, so QC re-checks it)."""
    k = src["kind"]
    if k == "fp_value":
        spec = METRICS.get(src["metric"])
        table = src.get("table") or spec["table"]
        row = _row(snap, table, src["gsis_id"], src["week"])
        return game_value(src["metric"], row) if spec else row[src["metric"]]
    if k == "fp_own":
        spec = METRICS[src["metric"]]
        rows = [_row(snap, spec["table"], src["gsis_id"], w) for w in src["weeks"]]
        return own_baseline(src["metric"], rows)
    if k in ("fp_rank", "fp_games"):
        vals = [v for _, w, v in distribution(snap, src["metric"], src["position"], src["through"])
                if w >= src.get("from_week", 0)]
        if k == "fp_games":
            return len(vals)
        x = game_value(src["metric"], _row(snap, METRICS[src["metric"]]["table"], src["gsis_id"], src["week"]))
        return 1 + _beyond(vals, x, src["direction"])
    if k == "fp_qualifier":
        return QUALIFY[src["position"]][1]
    if k == "fp_team":
        return _row(snap, src["table"], src["gsis_id"], src["week"])["team"]
    if k == "fp_rate":                       # a rate over named weeks of his games, pooled (one week = that game)
        table, num, den = RATES[src["rate"]]
        rows = [_row(snap, table, src["gsis_id"], w) for w in src["weeks"]]
        return sum(r[num] for r in rows) / sum(r[den] for r in rows)
    if k == "fp_league_rate":                # every qualifying game at the position through the week, pooled
        table, num, den = RATES[src["rate"]]
        rows = [r for r in _pos_rows(snap, src["position"]) if r["week"] <= src["through"]
                and qualifies(src["position"], r)]
        return sum(r[num] for r in rows) / sum(r[den] for r in rows)
    if k in ("fp_season_rate_rank", "fp_season_rate_count"):
        # his season-pooled rate against every player at the position with at least min_den of the denominator
        table, num, den = RATES[src["rate"]]
        tot: dict[str, list[float]] = {}
        for r in _pos_rows(snap, src["position"]):
            if r["week"] <= src["through"]:
                t = tot.setdefault(r["gsis_id"], [0.0, 0.0])
                t[0] += r[num] or 0
                t[1] += r[den] or 0
        rates = {g: n / d for g, (n, d) in tot.items() if d >= src["min_den"]}
        if k == "fp_season_rate_count":
            return len(rates)
        mine = rates[src["gsis_id"]]
        return 1 + _beyond(list(rates.values()), mine, src["direction"])
    if k == "fp_constant":
        return CONSTANTS[src["name"]]
    if k == "fp_completed_air":             # passing yards that happened in the air: yards minus yards after catch
        r = _row(snap, "qb", src["gsis_id"], src["week"])
        return r["passing_yards"] - r["passing_yards_after_catch"]
    if k in ("fp_points", "fp_result"):
        g = next(g for g in snap["tables"]["nfl_schedules"] if g["week"] == src["week"]
                 and src["team"] in (g["home_team"], g["away_team"]))
        home = g["home_team"] == src["team"]
        mine, theirs = (g["home_score"], g["away_score"]) if home else (g["away_score"], g["home_score"])
        if k == "fp_points":
            return theirs if src.get("side") == "opponent" else mine
        return "won" if mine > theirs else "lost" if mine < theirs else "tied"
    if k == "fp_teams_fewer_targets":       # NFL teams whose whole offense drew fewer targets that week than he did
        mine = _row(snap, "usage", src["gsis_id"], src["week"])["targets"]
        return sum(1 for r in snap["tables"]["nfl_team_week"] if r["week"] == src["week"] and r["team_targets"] < mine)
    raise ValueError(f"unknown flame claim kind {k!r}")


def describe(src: dict) -> str:
    """For review.md: where an fp_ claim comes from."""
    k = src["kind"]
    if k == "fp_value":
        spec = METRICS.get(src["metric"])
        table = "qb_week_passing (nfl_weekly_stats)" if (src.get("table") or (spec or {}).get("table")) == "qb" \
            else "player_week_usage"
        col = (f"{spec['num']}/{spec['den']}" if spec and spec["den"] else spec["num"]) if spec else src["metric"]
        return f"{table}.{col}, week {src['week']} only"
    if k == "fp_own":
        spec = METRICS[src["metric"]]
        how = f"sum {spec['num']} / sum {spec['den']}" if spec["den"] else f"mean of {spec['num']}"
        return f"{how}, his weeks {src['weeks']} (before the game)"
    if k == "fp_rank":
        return (f"rank of his week {src['week']} {src['metric']} among qualifying {src['position']} games, weeks "
                f"{src.get('from_week', 1)}-{src['through']} (1 = {'lowest' if src['direction'] == 'flame' else 'highest'})")
    if k == "fp_games":
        return f"qualifying {src['position']} games, weeks {src.get('from_week', 1)}-{src['through']}"
    if k == "fp_qualifier":
        return f"volume floor for a {src['position']} game to count ({QUALIFY[src['position']][0]})"
    if k == "fp_team":
        return f"{src['table']} team, week {src['week']}"
    if k == "fp_rate":
        _, num, den = RATES[src["rate"]]
        return f"sum {num} / sum {den}, his weeks {src['weeks']}"
    if k == "fp_league_rate":
        _, num, den = RATES[src["rate"]]
        return f"sum {num} / sum {den} over every qualifying {src['position']} game, weeks 1-{src['through']}"
    if k in ("fp_season_rate_rank", "fp_season_rate_count"):
        _, num, den = RATES[src["rate"]]
        what = "rank (1 = lowest)" if src.get("direction") == "flame" else \
            "rank (1 = highest)" if k.endswith("rank") else "number of players"
        return f"season {num}/{den} per {src['position']} with {src['min_den']}+ {den}, weeks 1-{src['through']}: {what}"
    if k == "fp_constant":
        return f"selection constant {src['name']}"
    if k == "fp_completed_air":
        return f"qb_week_passing passing_yards - passing_yards_after_catch, week {src['week']}"
    if k in ("fp_points", "fp_result"):
        return f"nfl_schedules final score ({src['team']}, week {src['week']})"
    if k == "fp_teams_fewer_targets":
        return f"nfl_team_week.team_targets below his week {src['week']} targets"
    return k


# ------------------------------------------------------------------------------------------ selection

def _z(x: float, mu: float, sd: float) -> float:
    return (x - mu) / sd if sd > 0 else 0.0


def candidates(snap: dict) -> list[dict]:
    """Every flame and praise case in the latest week, strongest first. Each carries its numbers and the reason
    it qualified (or the metrics that came close), so the board can show why."""
    week = snap["data_through_week"]
    names = {p["gsis_id"]: p for p in snap["tables"]["nfl_players"]}
    out = []
    for pos in QUALIFY:
        rows = _pos_rows(snap, pos)
        by_player: dict[str, list[dict]] = {}
        for r in rows:
            by_player.setdefault(r["gsis_id"], []).append(r)
        dists = {m: distribution(snap, m, pos, week) for m, s in METRICS.items() if pos in s["pos"]}
        for gid, games in by_player.items():
            games.sort(key=lambda r: r["week"])
            latest = next((r for r in games if r["week"] == week), None)
            if latest is None or not qualifies(pos, latest):
                continue
            prior = [r for r in games if r["week"] < week and qualifies(pos, r)]
            if len(prior) < MIN_PRIOR:
                continue
            role_col, role_min = FLAME_ROLE[pos]
            established = mean((r.get(role_col) or 0) for r in prior) >= role_min
            for kind in KINDS:
                if kind == "flame" and not established:
                    continue
                cases = []
                for metric, dist in dists.items():
                    spec = METRICS[metric]
                    x = game_value(metric, latest)
                    if x is None or any(game_value(metric, r) is None for r in prior):
                        continue
                    vals = [v for _, _, v in dist]
                    n = len(vals)
                    rank = 1 + _beyond(vals, x, kind)
                    sign = -1 if kind == "flame" else 1
                    own = own_baseline(metric, prior)
                    per_game = [game_value(metric, r) for r in prior]
                    z_pos = sign * _z(x, mean(vals), pstdev(vals))
                    z_own = sign * _z(x, own, max(pstdev(per_game), spec["floor"]))
                    season_extreme = all(sign * (x - v) > 0 for v in per_game)
                    case = {"metric": metric, "value": x, "rank": rank, "games": n, "own": own,
                            "z_pos": round(z_pos, 3), "z_own": round(z_own, 3), "season_extreme": season_extreme,
                            "tied": sum(1 for v in vals if v == x) > 1,
                            "strength": round(z_pos + 0.5 * min(z_own, OWN_Z_CAP) + (0.5 if rank == 1 else 0)
                                              + (0.25 if season_extreme else 0), 3)}
                    case["extreme"] = rank <= max(EXTREME_MIN_RANK, math.ceil(EXTREME_SHARE * n))
                    case["leads"] = case["extreme"] and z_own > 0 and kind in spec["kinds"]
                    case["receipt"] = rank <= math.ceil(RECEIPT_SHARE * n) and z_own > 0
                    cases.append(case)
                leads = sorted((c for c in cases if c["leads"]), key=lambda c: -c["strength"])
                if not leads:
                    continue
                lead = leads[0]
                receipts = [c for c in sorted(cases, key=lambda c: -c["strength"])
                            if c is not lead and c["receipt"] and not _same_idea(lead["metric"], c["metric"])][:2]
                meta = names.get(gid, {})
                out.append({"kind": kind, "gsis_id": gid, "name": meta.get("display_name") or gid, "position": pos,
                            "team": latest["team"], "week": week, "lead": lead, "receipts": receipts,
                            "prior_weeks": [r["week"] for r in prior], "strength": lead["strength"],
                            "slug": f"{kind}-" + re.sub(r"[^a-z0-9]+", "-", (meta.get("display_name") or gid).lower()
                                                        ).strip("-")})
    out.sort(key=lambda c: (-c["strength"], c["name"]))
    return out


def _same_idea(a: str, b: str) -> bool:
    """Air yards and air yards per attempt say the same thing twice on a receipt."""
    return {a, b} == {"air_yards", "adot"}


def pick(snap: dict, only: str | None = None) -> list[dict]:
    """The week's two Shorts: the strongest flame and the strongest praise, from different teams. `only` (a player
    slug, e.g. bo-nix) returns that player's strongest case(s) instead."""
    cands = candidates(snap)
    if only:
        mine = [c for c in cands if c["slug"].split("-", 1)[1] == only or c["slug"] == only]
        return [{**c, "order": 90 + KINDS.index(c["kind"])} for c in mine[:1]]
    chosen, teams = [], set()
    for kind in KINDS:
        best = next((c for c in cands if c["kind"] == kind and c["team"] not in teams), None)
        if best:
            chosen.append(best)
            teams.add(best["team"])
    # after the week's usage Shorts, flame before praise, so --player runs sort the same way
    return [{**c, "order": 90 + KINDS.index(c["kind"])} for c in chosen]


def board_markdown(snap: dict, cands: list[dict], chosen: list[dict]) -> str:
    fmt = lambda m, v: render(METRICS[m]["fmt"], v)[0]
    lines = ["# Flame / praise board", "",
             f"Latest game week {snap['data_through_week']}. A game qualifies at "
             + ", ".join(f"{p} {c} >= {v}" for p, (c, v) in QUALIFY.items())
             + f"; it must rank in the extreme {EXTREME_SHARE:.0%} of its position's qualifying games this season "
             f"(at worst rank {EXTREME_MIN_RANK}) and be past his own average of at least {MIN_PRIOR} earlier games. "
             "Strength = position z + half his own z (capped at 3) + 0.5 for the season's most extreme game + 0.25 "
             "for his own season extreme.", "",
             "| | kind | player | pos | team | lead | game | own avg | rank | z pos | z own | strength |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    picked = {(c["kind"], c["gsis_id"]) for c in chosen}
    for c in cands[:40]:
        ld = c["lead"]
        mark = "**picked**" if (c["kind"], c["gsis_id"]) in picked else ""
        lines.append(f"| {mark} | {c['kind']} | {c['name']} | {c['position']} | {c['team']} | {ld['metric']} | "
                     f"{fmt(ld['metric'], ld['value'])} | {fmt(ld['metric'], ld['own'])} | {ld['rank']}/{ld['games']} | "
                     f"{ld['z_pos']:.2f} | {ld['z_own']:.2f} | {c['strength']:.2f} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------- facts

def week_complete(snap: dict, week: int) -> bool:
    """Every regular-season game of the week has a final score (not just Thursday night's)."""
    games = [g for g in snap["tables"]["nfl_schedules"] if g["week"] == week and g.get("game_type", "REG") == "REG"]
    return bool(games) and all(g["home_score"] is not None and g["away_score"] is not None for g in games)


def week_best(lead: dict) -> bool:
    """'And the most of anyone in Week N': only when it isn't already the season's best, and only once every game
    of that week has been played (with only Thursday's game in, 'anyone' would be two teams)."""
    return lead["week_rank_value"] == 1 and lead["rank_value"] != 1 and lead.get("week_complete", False)


def build_facts(snap: dict, story: dict) -> dict:
    """The claims one flame or praise Short is told with."""
    from .facts import ClaimBook
    case = next((c for c in candidates(snap) if c["gsis_id"] == story["gsis_id"] and c["kind"] == story["role"]),
                None)
    if case is None:
        raise RuntimeError(f"{story['name']} is no longer a {story['role']} case in this data")
    book = ClaimBook(snap)
    gid, pos, week, team = case["gsis_id"], case["position"], case["week"], case["team"]
    lead = case["lead"]
    table = METRICS[lead["metric"]]["table"]
    meta = next((x for x in snap["tables"]["nfl_players"] if x["gsis_id"] == gid), {})
    jerseys = {j["gsis_id"]: j["jersey_number"] for j in snap["tables"].get("jerseys", [])}
    val = lambda m, w, label, fmt=None: book.add(label, fmt or (METRICS[m]["fmt"] if m in METRICS else "int"),
                                                 {"kind": "fp_value", "metric": m, "gsis_id": gid, "week": w,
                                                  "table": table}, player=gid)
    season = [r for r in _table(snap, table) if r["gsis_id"] == gid and qualifies(pos, r)]
    season.sort(key=lambda r: r["week"])
    vol_col, vol_word, vol_screen = VOLUME[pos]

    def case_claims(c: dict) -> dict:
        m = c["metric"]
        spec = METRICS[m]
        return {"metric": m, "label": spec["label"], "screen": spec["screen"],
                "sup": spec["sup"][KINDS.index(case["kind"])],
                "value": val(m, week, f"{spec['label']}, week {week}"),
                "rank": book.add(f"rank of his {spec['label']} among {pos} games this season", "ordinal",
                                 {"kind": "fp_rank", "metric": m, "position": pos, "gsis_id": gid, "week": week,
                                  "through": week, "direction": case["kind"]}, player=gid),
                "rank_value": c["rank"], "games_value": c["games"], "tied": c["tied"],
                "season_extreme": c["season_extreme"], "z_pos": c["z_pos"], "z_own": c["z_own"]}

    lead_c = case_claims(lead)
    m = lead["metric"]
    lead_c |= {
        "games": book.add(f"qualifying {pos} games this season", "int",
                          {"kind": "fp_games", "metric": m, "position": pos, "through": week}),
        "week_rank": book.add(f"rank of his {METRICS[m]['label']} among {pos} games in week {week}", "ordinal",
                              {"kind": "fp_rank", "metric": m, "position": pos, "gsis_id": gid, "week": week,
                               "through": week, "from_week": week, "direction": case["kind"]}, player=gid),
        "week_games": book.add(f"qualifying {pos} games in week {week}", "int",
                               {"kind": "fp_games", "metric": m, "position": pos, "through": week, "from_week": week}),
        "own": book.add(f"his {METRICS[m]['label']} before week {week}"
                        + (" (pooled)" if METRICS[m]["den"] else " (per game)"), METRICS[m]["fmt"],
                        {"kind": "fp_own", "metric": m, "gsis_id": gid, "weeks": case["prior_weeks"]}, player=gid),
        "own_weeks": book.add("his earlier games", "weeks_list", {"kind": "constant_weeks", "weeks": case["prior_weeks"]}),
        "per_game": not METRICS[m]["den"],
        "week_rank_value": evaluate_rank(snap, m, pos, gid, week, case["kind"], from_week=week),
        "week_complete": week_complete(snap, week),
        "series": [{"week": r["week"],
                    "week_claim": book.add("game week", "week", {"kind": "week_value", "week": r["week"]}),
                    "value": val(m, r["week"], f"{METRICS[m]['label']}, week {r['week']}")} for r in season],
        # every qualifying game at his position this season, for the strip on screen (dots, no numbers drawn)
        "dist": sorted(v for _, _, v in distribution(snap, m, pos, week)),
    }
    p = {"gsis_id": gid, "name": case["name"], "position": pos, "team": team, "role": case["kind"],
         "flame": True, "week": week, "espn_id": meta.get("espn_id"), "jersey": jerseys.get(gid),
         "draft_round": meta.get("draft_round"), "years_exp": meta.get("years_of_experience"),
         "team_name": book.add("team", "team", {"kind": "fp_team", "table": table, "gsis_id": gid, "week": week},
                               team=team),
         "week_claim": book.add("game week", "week", {"kind": "week_value", "week": week}),
         "opponent": book.add(f"opponent, week {week}", "team", {"kind": "opponent", "team": team, "week": week},
                              team=team),
         "qualifier": book.add(f"{pos} volume floor ({QUALIFY[pos][0]})",
                               "pct" if QUALIFY[pos][0] == "offense_pct" else "int",
                               {"kind": "fp_qualifier", "position": pos}),
         "volume": {"word": vol_word, "screen": vol_screen,
                    "value": val(vol_col, week, f"{vol_word}, week {week}")},
         "lead": lead_c, "receipts": [case_claims(c) for c in case["receipts"]],
         "selection": {k: case[k] for k in ("strength", "prior_weeks")} | {"lead": lead}}
    if pos != "QB":
        p["snaps"] = val("offense_pct", week, f"snap share, week {week}", "pct")
    from .showrunner import enrich
    enrich(book, snap, p)                  # the game, the box score and the story angles (showrunner.py)
    return {"season": snap["season"], "week": snap["week"], "data_through_week": snap["data_through_week"],
            "snapshot_sha256": snap["content_sha256"],
            "episode": {"week": book.add("episode week", "week", {"kind": "episode_week"})},
            "players": [p], "claims": book.claims}


def evaluate_rank(snap, metric, pos, gid, week, kind, from_week=0) -> int:
    return evaluate(snap, {"kind": "fp_rank", "metric": metric, "position": pos, "gsis_id": gid, "week": week,
                           "through": week, "from_week": from_week, "direction": kind})


def _facts_run(ep: Episode) -> None:
    snap = read_json(ep.path("snapshot.json"))
    story = read_json(ep.path("story.json"))
    facts = build_facts(snap, story)
    write_json(ep.path("facts.json"), facts)
    ep.log(f"  facts: {story['name']} ({story['role']}: {facts['players'][0]['lead']['metric']}), "
           f"{len(facts['claims'])} claims")


def facts_run(ep: Episode) -> None:
    code = sha256_bytes(Path(__file__).read_bytes())
    run_stage(ep, "facts", VERSION, {"code": code}, ["snapshot.json", "story.json"], ["facts.json"],
              lambda: _facts_run(ep))


# ---------------------------------------------------------------------------------------- the script
# Roast or hype the stat line, never the person. Openings per metric and kind; the first not used by the last two
# Shorts opens the video (formats.pick_hook). {x} the lead number, {n} the volume, {opp} the opponent.

HOOKS = {
    ("flame", "air_yards"): {
        "total": "{n} throws.‖ {x} air yards.‖ For the whole game.",
        "against": "Against the {opp}, {name} threw it {n} times for {x} air yards.‖ Total.",
    },
    ("flame", "adot"): {
        "distance": "{name}'s average pass against the {opp}?‖ {x} yards downfield.",
        "per_throw": "{n} throws, and they traveled {x} yards each.‖ On average.",
    },
    ("flame", "epa"): {
        "epa": "{name} put up {x} passing EPA against the {opp}.‖ Those are points going the wrong way.",
    },
    ("flame", "ypa"): {
        "ypa": "{x} yards per attempt.‖ {name} threw it {n} times against the {opp}.",
    },
    ("flame", "target_share"): {
        "ghost": "{name} played {snaps} of the snaps against the {opp}.‖ His target share?‖ {x}.",
        "decoy": "{x} of the targets.‖ {name} was out there for {snaps} of the snaps.",
    },
    ("flame", "rec_air_yards"): {
        "air": "{x} air yards on {n} targets.‖ {name}, what was that?",
    },
    ("flame", "backfield_share"): {
        "share": "{name} got {x} of the running back carries against the {opp}.‖ Just {x}.",
    },
    ("praise", "air_yards"): {
        "let_fly": "{x} air yards.‖ {name} was letting it fly against the {opp}.",
        "total": "{n} throws.‖ {x} air yards.‖ That's a quarterback with intentions.",
    },
    ("praise", "epa"): {
        "epa": "{x} passing EPA.‖ {name} just had a monster game against the {opp}.",
    },
    ("praise", "ypa"): {
        "ypa": "{x} yards per attempt.‖ {name} was carving up the {opp}.",
    },
    ("praise", "target_share"): {
        "whole_offense": "{x} of the targets.‖ {name} was the passing game against the {opp}.",
        "magnet": "{name} saw {x} of the targets against the {opp}.‖ Ball magnet.",
    },
    ("praise", "rec_air_yards"): {
        "air": "{x} air yards.‖ {name} was where the ball was going all day.",
    },
    ("praise", "targets"): {
        "count": "{x} targets.‖ {name} was plan A, plan B and plan C against the {opp}.",
    },
    ("praise", "backfield_share"): {
        "workhorse": "{x} of the running back carries.‖ {name} was the whole backfield against the {opp}.",
    },
    ("praise", "snap_share"): {
        "never_left": "{x} of the snaps.‖ {name} never left the field against the {opp}.",
    },
}
# The closing roast or hype line: about the numbers, never the person.
PUNCH = {
    ("flame", "QB"): ["That's not a downfield passing game.‖ That's a handoff with extra steps.",
                      "The defense didn't need a safety.‖ It needed a crossing guard."],
    ("flame", "WR"): ["He was on the field.‖ The ball just never got the memo."],
    ("flame", "TE"): ["He was on the field.‖ The ball just never got the memo."],
    ("flame", "RB"): ["The backfield had a party.‖ He wasn't on the list."],
    ("praise", "QB"): ["That's a quarterback playing with the cheat codes on."],
    ("praise", "WR"): ["When the ball goes up, it's going to him.‖ That's a role."],
    ("praise", "TE"): ["When the ball goes up, it's going to him.‖ That's a role."],
    ("praise", "RB"): ["That's not a committee.‖ That's one guy and some spectators."],
}
TITLES = {
    "flame": ["{name}: {x} {Label} vs the {opp}", "{name} Needs to Explain This Stat Line"],
    "praise": ["{name}: {x} {Label} vs the {opp}", "{name}'s Monster Game, by the Numbers"],
}
# Only when it is literally true: his game is the season's single most extreme at his position.
TITLE_EXTREME = "The {Sup} {Label} by Any {Pos} This Season"


def _sup_phrase(c: dict, kind_word: str) -> str:
    from .story import ph
    if c["rank_value"] == 1:
        return ("tied for the " if c["tied"] else "the ") + c["sup"]
    return f"the {ph(c['rank'])}-{c['sup']}"


def compose(facts: dict, claims: dict, used: dict, hook_override: str | None = None):
    from .formats import pick_hook, said
    from .story import beat, finish, last, ph
    p = facts["players"][0]
    kind, pos, lead = p["role"], p["position"], p["lead"]
    who = last(p["name"])
    team = claims[p["team_name"]]["display"]
    hooks = HOOKS[(kind, lead["metric"])]
    hook, order = pick_hook(hooks, used.get("hook", []), hook_override)
    fields = {"name": said(p["name"]), "x": ph(lead["value"]), "n": ph(p["volume"]["value"]),
              "opp": ph(p["opponent"]), "snaps": ph(p["snaps"]) if p.get("snaps") else ""}
    flame = kind == "flame"
    base = {"camera": "push", "fp": kind}
    hook_claims = [lead["value"], p["volume"]["value"], p["opponent"], p["week_claim"]] + (
        [p["snaps"]] if p.get("snaps") else [])
    beats = [beat("hook", hooks[hook].format(**fields),
                  {**base, "type": "fp_hook", "stamp": "FLAME" if flame else "PRAISE", "claims": hook_claims},
                  [], 1.1, pause=0.2)]
    posw = POS_WORD[pos]
    rank_line = f"That's {_sup_phrase(lead, kind)} of {ph(lead['games'])} {posw} games this season"
    if week_best(lead):
        rank_line += f",‖ and the {lead['sup']} of anyone in {ph(p['week_claim'])}."
    else:
        rank_line += "."
    beats.append(beat("rank", rank_line,
                      {**base, "type": "fp_strip", "transition": "whip", "sfx": True,
                       "claims": [lead["value"], lead["rank"], lead["games"], p["qualifier"], p["week_claim"]]
                       + ([lead["week_rank"]] if week_best(lead) else [])},
                      [lead["sup"]], 1.1, pause=0.25))
    if flame:
        tail = "A new low for him this season." if lead["season_extreme"] else "Below his own normal, too."
    else:
        tail = "A new high for him this season." if lead["season_extreme"] else "Above his own normal, too."
    beats.append(beat("own", f"His {lead['label']} before this?‖ {ph(lead['own'])}" +
                      (" a game." if lead["per_game"] else ".") + f"‖ {tail}",
                      {**base, "type": "fp_own", "transition": "push",
                       "claims": [s["value"] for s in lead["series"]] + [s["week_claim"] for s in lead["series"]]
                       + [lead["own"], lead["own_weeks"]]},
                      ["low" if flame else "high"], 1.08, pause=0.2))
    if p["receipts"]:
        items = [f"{ph(r['value'])} {r['label']}" for r in p["receipts"]]
        lead_in = "The rest of the receipt:" if flame else "And it wasn't just that:"
        line = f"{lead_in} {items[0]}" + (f",‖ and {items[1]}." if len(items) > 1 else ".")
        beats.append(beat("receipts", line,
                          {**base, "type": "fp_receipt", "transition": "push", "sfx": True,
                           "claims": [r["value"] for r in p["receipts"]] + [r["rank"] for r in p["receipts"]]
                           + [p["volume"]["value"]] + ([p["snaps"]] if p.get("snaps") else [])},
                          ["receipt"] if flame else [], 1.1, pause=0.2))
    punch = PUNCH[(kind, pos)]
    line = punch[int(sha256_bytes(p["gsis_id"].encode())[:8], 16) % len(punch)]
    question = (f"Still starting {who}?" if flame else f"Is {who} in your lineup?")
    beats.append(beat("verdict", f"{line}‖ {question}",
                      {"type": "decision", "transition": "push", "fp": kind,
                       "question": ["STILL STARTING", f"{who.upper()}?"] if flame else ["IS HE IN", "YOUR LINEUP?"],
                       "options": ["START", "BENCH"] if flame else ["YES", "NOT YET"], "claims": []},
                      ["starting"] if flame else ["lineup"], 1.04, pause=0.6))
    t = {"name": p["name"], "x": ph(lead["value"]), "Label": lead["label"].title().replace("Epa", "EPA"),
         "opp": ph(p["opponent"]), "Sup": lead["sup"].title(), "Pos": posw.title()}
    titles = [x.format(**t) for x in TITLES[kind]]
    if lead["rank_value"] == 1 and not lead["tied"]:
        titles.insert(1, TITLE_EXTREME.format(**t))
    beats, titles, structure = finish(beats, p, {"column": lead["metric"]}, kind, hook, None, {"tm": ""},
                                      titles=titles)
    structure["format"] = kind
    structure["hook_variants"] = {k: hooks[k].format(**fields).replace("‖", "") for k in order}
    return beats, titles, structure


def editorial_row(p: dict, words: int) -> dict:
    """The editorial board's eight scores for a flame/praise Short, with reasons (heuristic, as in editorial.py)."""
    lead = p["lead"]
    rank, n = lead["rank_value"], lead["games_value"]
    s = {"relevance": 5 if p["position"] == "QB" or E.recognizable(p) else 4,
         "hook": 5 if rank == 1 else 4, "surprise": 5 if lead["z_pos"] >= 2 else 4,
         "evidence": 4, "actionability": 3, "timeliness": 4, "visual": 5,
         "distinctiveness": 5}
    why = {"relevance": f"{p['position']} with an established role", "hook": f"rank {rank} of {n} position games",
           "surprise": f"{lead['z_pos']:.1f} SD from the position's games", "evidence":
           "one game, measured against the season distribution and his own games",
           "actionability": "a start/bench conversation, not a waiver call", "timeliness": "the latest game",
           "visual": "format-specific scenes (strip, own bars, receipt)", "distinctiveness": "new format"}
    row = {"player": p["name"], "format": p["role"], "scores": s, "reasons": why, "total": sum(s.values()),
           "recognizable": E.recognizable(p), "verdict": "make", "reject_reasons": []}
    row["checks"] = E.checks(row, words)
    return row


def _finish_beats(beats: list[dict], claims: dict) -> list[dict]:
    """Pause markers to spoken parts: `‖` a short breath, `⏸` the longer beat before a punchline."""
    from .script import PLACEHOLDER, fill
    from .showrunner import BEAT, BEAT_PAUSE, PART_PAUSE
    from .story import PAUSE
    shots = []
    for b in beats:
        # `‖` is a breath Kokoro takes from the punctuation itself (one call, natural prosody); only the comic
        # beat `⏸` splits the line into separately spoken parts with a measured silence between them
        text = b["text_template"].replace(PAUSE, "") if b["scene"].get("series") else b["text_template"]
        chunks = re.split(f"({PAUSE}|{BEAT})", text)
        parts, gaps = chunks[0::2], [BEAT_PAUSE if m == BEAT else PART_PAUSE for m in chunks[1::2]]
        b["text_template"] = "".join(parts)
        if len(parts) > 1:
            keep = [i for i, x in enumerate(parts) if x.strip()]
            b["spoken_parts"] = [fill(parts[i].strip(), claims, "spoken") for i in keep]
            b["part_pauses"] = [gaps[i] if i < len(gaps) else PART_PAUSE for i in keep]
            b["part_pause"] = PART_PAUSE
        b["text_display"] = fill(b["text_template"], claims, "display")
        b["text_spoken"] = fill(b["text_template"], claims, "spoken")
        b["claims_used"] = sorted(set(PLACEHOLDER.findall(b["text_template"])))
        b["claims_on_screen"] = sorted(c for c in b["scene"].get("claims", []) if c)
        b["target_words"] = len(b["text_spoken"].split())
        b["shot"] = f"shot_{b['id']}"
        shots.append({"id": b["shot"], "segment": b["id"], **b["scene"]})
    return shots


def _write_script(ep: Episode, facts: dict, claims: dict, beats: list[dict], titles: list[str], structure: dict,
                  note: str) -> int:
    from .script import PLACEHOLDER, fill
    from .story import WORDS_PER_SECOND
    shots = _finish_beats(beats, claims)
    used_ids = sorted({c for b in beats for c in b["claims_used"] + b["claims_on_screen"]} |
                      {c for t in titles for c in PLACEHOLDER.findall(t)})
    words = sum(len(b["text_spoken"].split()) for b in beats)
    write_json(ep.path("script.json"), {
        "episode_id": ep.id, "season": ep.season, "week": ep.week, "format": ep.fmt,
        "title_candidates": [fill(t, claims, "display") for t in titles], "title_templates": titles,
        "claims": [claims[c] for c in used_ids], "narration_segments": beats, "shots": shots,
        "structure": structure, "expected_duration_seconds": round(words / WORDS_PER_SECOND),
        "made_for_kids": False, "synthetic_voice": True,
        "cta": {"url": ep.cfg["cta"]["url"] or None, "text": beats[-1]["text_display"]},
        "facts_sha256": sha256_bytes(canonical_json(facts).encode()),
        "llm": {"provider": "none", "model": "deterministic story engine", "calls": [], "note": note},
    })
    return words


class NotEntertaining(RuntimeError):
    """The brief failed the entertainment test: not made for this series."""


def _script_run(ep: Episode, used: dict, hook: str | None) -> None:
    from . import showrunner as SR
    facts = read_json(ep.path("facts.json"))
    claims = {c["id"]: c for c in facts["claims"]}
    story = read_json(ep.path("story.json"))
    p = facts["players"][0]
    if story.get("treatment") == "classic":            # the first flame / praise template, kept for comparison
        beats, titles, structure = compose(facts, claims, used, hook)
        words = _write_script(ep, facts, claims, beats, titles, structure,
                              "Flame / praise Shorts are composed from selection rules and claims; no language model.")
        write_json(ep.path("editorial.json"), editorial_row(p, words))
        write_json(ep.path("brief.json"), {"series": None, "treatment": "classic"})
        ep.log(f"  story: {structure['angle']} (classic) / hook {structure['hook']}, {len(beats)} beats, {words} words")
        return
    recent = SR.series_history(ep.cfg.episodes_dir, ep.season) + story.get("recent_series", [])
    brief = SR.choose(p, recent, story.get("treatment"))
    write_json(ep.path("brief.json"), brief)
    ep.path("brief.md").write_text(SR.describe(ep, brief))
    if not brief["entertainment_test"]["pass"] and not story.get("force"):
        raise NotEntertaining(f"{p['name']}: {brief['entertainment_test']['why']}")
    beats, titles = SR.compose(facts, claims, brief)
    SR.assign_sfx(beats, brief, recent, ep.id)
    scale = ep.cfg["shorts"].get("speed_scale", 1.0)       # tts multiplies every line by it; the formats' speeds
    for b in beats:                                         # are already the delivered pace, so divide it back out
        b["speed"] = round(b["speed"] / scale, 4)
    from .story import finish
    beats, titles, _ = finish(beats, p, {"column": brief["angle"]}, brief["kind"], brief["format"], None, {"tm": ""},
                              titles=titles)
    structure = SR.structure(brief, beats)
    words = _write_script(ep, facts, claims, beats, titles, structure,
                          "Composed from the stored creative brief (brief.json) and resolved claims; no language model.")
    write_json(ep.path("editorial.json"), SR.editorial_row(p, brief, words))
    ep.log(f"  story: {brief['kind']} / {brief['format']} on {brief['angle']}, {len(beats)} beats, {words} words")


def script_run(ep: Episode, hook: str | None = None) -> None:
    from . import showrunner as SR
    from .story import used_structures
    used = used_structures(ep)
    code = {"flame": sha256_bytes(Path(__file__).read_bytes()), "showrunner": sha256_bytes(Path(SR.__file__).read_bytes())}
    run_stage(ep, "script", VERSION, {"cta": ep.cfg["cta"], "used": used, "hook": hook, "code": code,
                                      "series": SR.series_history(ep.cfg.episodes_dir, ep.season)},
              ["facts.json", "story.json"], ["script.json", "editorial.json", "brief.json"],
              lambda: _script_run(ep, used, hook))


# ----------------------------------------------------------------------------- thumbnail and metadata

def thumbnail_text(p: dict, claims: dict, structure: dict | None = None) -> tuple[str, str, str]:
    d = lambda cid: claims[cid]["display"]
    if (structure or {}).get("series"):
        from .showrunner import thumbnail
        return thumbnail(p, claims, structure)
    lead = p["lead"]
    rank = "THE " + lead["sup"].upper() if lead["rank_value"] == 1 else f"{d(lead['rank']).upper()}-{lead['sup'].upper()}"
    kicker = f"{rank} OF {d(lead['games'])} {POS_SCREEN[p['position']]} GAMES"
    return kicker, d(lead["value"]), lead["screen"]


def description_lines(p: dict, claims: dict, structure: dict | None = None) -> list[str]:
    if (structure or {}).get("series"):
        from .showrunner import description
        return description(p, claims, structure)
    lead = p["lead"]
    d = lambda cid: claims[cid]["display"]
    rank = "the " + lead["sup"] if lead["rank_value"] == 1 else f"the {d(lead['rank'])}-{lead['sup']}"
    return [f"{p['name']}: {d(lead['value'])} {lead['label']} vs the {d(p['opponent'])} in {d(p['week_claim'])}, "
            f"{rank} of {d(lead['games'])} {POS_WORD[p['position']]} games this season "
            f"(his earlier average: {d(lead['own'])}).",
            "Usage and efficiency only: no fantasy points, no projections."]
