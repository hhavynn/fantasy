"""Showrunner for the flame / praise series: NFL entertainment, powered by real football data.

stages/flame.py finds the game (an extreme line against the position and against the player's own season). This
module decides what makes it worth watching for someone who has never played fantasy football, and how to tell it:

  1. ANGLES   the football premise, from verified data only. Each angle carries its own claims and says
              whether a casual NFL fan gets it without a spreadsheet (`broad`):
                short_throws  a QB's passes went nowhere: his average throw against the league's, and (when true)
                              the shortest average throw of any QB with real volume this season
                rough_day     a QB's afternoon fell apart: sacks, interceptions, points, the worst-of-season stat
                target_hog    a receiver drew more targets than an entire NFL offense did that day
                monster_line  a big box score: catches, yards, touchdowns (or a QB's yards and touchdowns)
                stat_outlier  the selection stat alone (fantasy-flavoured; usually fails the entertainment test)
  2. FORMATS  genuinely different shows, each with its own opening, joke construction, rhythm, scenes, look,
              music, ending and length (FORMATS). Not skins over one beat sequence:
                roast_monologue  deadpan, no music, one observation built to an abrupt punchline (14-22 s)
                courtroom        a parody trial: case called, exhibits stamped, a verdict and a gavel (24-36 s)
                comparison       one verified benchmark beside his number, drawn as a picture, then the callback
                main_character   a cinematic edit: title cards, stat slams, a movie-poster finish (20-30 s)
                fan_meltdown     fake calm that escalates into all-caps celebration, then a hard cut (14-22 s)
  3. BRIEF    the angle and format are chosen by editorial fit, minus what the recent series videos already did
              (format, look, opening, joke mechanism, ending). The brief, with its scores and the reason for each,
              is stored as brief.json, so the script is reproducible from brief + claims. No language model.
  4. TEST     "If this viewer did not play fantasy football, would it still be worth watching?" A brief whose
              angle is not broad, or whose subject is neither recognizable nor a season extreme, fails and is
              not made for this series.

Jokes are about the stat line and the football, never the person. Every number is a claim placeholder; a joke
never states a fact the claims do not hold, and parody formats say so on screen.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core import Episode
from . import flame as FL

SERIES = "flame_praise"
SHORT_RATIO = 0.65             # short_throws: his average throw at most this share of the league's
ROUGH_SACKS, ROUGH_INTS, ROUGH_POINTS = 4, 2, 10
SHOOTOUT_POINTS = 27         # "a shootout": both teams scored at least this many
HOG_TARGETS = 15               # target_hog: at least this many targets...
MONSTER_YARDS = {"QB": 350, "WR": 140, "TE": 110, "RB": 140}
# Long pause inside a line (the beat before a punchline); `‖` is the short one.
BEAT = "⏸"
PART_PAUSE, BEAT_PAUSE = 0.2, 0.55
# Endings this series never uses: fantasy strategy questions.
FANTASY_ENDING = ("start", "bench", "sit ", "lineup", "waiver", "roster", "trade", "fantasy", "add him", "drop him")

FORMATS = {
    "roast_monologue": {"kinds": ("flame",), "style": "deadpan", "mood": None, "length": (12, 26),
                        "opening": "joke_first", "joke": "deadpan_escalation", "ending": "abrupt_punchline",
                        "humor": "deadpan", "exaggeration": "high"},
    "courtroom": {"kinds": ("flame",), "style": "court", "mood": "court", "length": (22, 38), "parody": True,
                  "opening": "case_called", "joke": "evidence_to_absurd_verdict", "ending": "verdict",
                  "humor": "mock_serious", "exaggeration": "medium"},
    "comparison": {"kinds": ("flame", "praise"), "style": "versus", "mood": "drive", "length": (15, 32),
                   "opening": "benchmark_first", "joke": "absurd_comparison", "ending": "callback",
                   "humor": "observational", "exaggeration": "low"},
    "main_character": {"kinds": ("praise",), "style": "cinematic", "mood": "hype", "length": (18, 32),
                       "opening": "title_card", "joke": "mythmaking", "ending": "poster",
                       "humor": "dramatic", "exaggeration": "high"},
    "fan_meltdown": {"kinds": ("praise",), "style": "meme", "mood": "frantic", "length": (12, 24),
                     "opening": "fake_calm", "joke": "escalation", "ending": "scream_cut",
                     "humor": "absurd", "exaggeration": "very_high"},
}
# Editorial fit of a format to an angle (0 = cannot tell it). A heuristic, not a prediction of views.
FIT = {
    ("roast_monologue", "short_throws"): 4, ("roast_monologue", "rough_day"): 4,
    ("courtroom", "rough_day"): 5, ("courtroom", "short_throws"): 3,
    ("comparison", "short_throws"): 5, ("comparison", "target_hog"): 4,
    ("main_character", "target_hog"): 5, ("main_character", "monster_line"): 5,
    ("fan_meltdown", "target_hog"): 4, ("fan_meltdown", "monster_line"): 4,
}
POS_WORD = FL.POS_WORD


# ------------------------------------------------------------------------------------------- angles

def _rows(snap: dict, table: str, gid: str) -> list[dict]:
    return sorted((r for r in FL._table(snap, table) if r["gsis_id"] == gid), key=lambda r: r["week"])


def recognizable(snap: dict, p: dict) -> tuple[bool, str]:
    """A public proxy for "people know who this is": a first-round pick, a 5th-year veteran, a QB with a starter's
    volume, or a top-24 volume player at his position this season."""
    if p.get("draft_round") == 1:
        return True, "first-round pick"
    if (p.get("years_exp") or 0) >= 5:
        return True, "veteran"
    pos, gid = p["position"], p["gsis_id"]
    if pos == "QB":
        att = sum(r["attempts"] for r in _rows(snap, "qb", gid))
        return att >= FL.CONSTANTS["season_min_attempts"], f"{att} attempts this season"
    col = "carries" if pos == "RB" else "targets"
    tot: dict[str, float] = {}
    for r in FL._pos_rows(snap, pos):
        tot[r["gsis_id"]] = tot.get(r["gsis_id"], 0) + (r.get(col) or 0)
    rank = 1 + sum(1 for v in tot.values() if v > tot.get(gid, 0))
    return rank <= 24, f"#{rank} at his position in season {col}"


def enrich(book, snap: dict, p: dict) -> None:
    """Game, box score and story angles for a flame / praise player, every number a claim."""
    gid, pos, week, team, kind = p["gsis_id"], p["position"], p["week"], p["team"], p["role"]
    add = book.add
    game = {"points": add(f"{team} points, week {week}", "int", {"kind": "fp_points", "team": team, "week": week},
                          team=team),
            "opp_points": add(f"opponent points, week {week}", "int",
                              {"kind": "fp_points", "team": team, "week": week, "side": "opponent"}, team=team),
            "result": add(f"{team} result, week {week}", "text", {"kind": "fp_result", "team": team, "week": week},
                          team=team),
            "venue": add(f"{team} home or away, week {week}", "text", {"kind": "venue", "team": team, "week": week},
                         team=team)}
    game["result_value"] = book.claims[-2]["value"]
    game["home"] = book.claims[-1]["value"] == "vs"
    p["game"] = game
    val = lambda table, col, label, fmt="int": add(f"{label}, week {week}", fmt, {"kind": "fp_value", "metric": col,
                                                                                    "gsis_id": gid, "week": week,
                                                                                    "table": table}, player=gid)
    raw = lambda table, col: FL._row(snap, table, gid, week).get(col) or 0
    box = {}
    if pos == "QB":
        for col, label in (("completions", "completions"), ("attempts", "attempts"), ("passing_yards", "passing yards"),
                           ("passing_tds", "passing touchdowns"), ("passing_interceptions", "interceptions"),
                           ("sacks_suffered", "sacks")):
            box[col] = val("qb", col, label)
            box[col + "_value"] = raw("qb", col)
    else:
        box["targets"] = val("usage", "targets", "targets")
        box["targets_value"] = raw("usage", "targets")
        has_box = any(r["gsis_id"] == gid and r["week"] == week for r in FL._table(snap, "box"))
        cols = (("receptions", "catches"), ("receiving_yards", "receiving yards"), ("receiving_tds", "receiving touchdowns"))
        if pos == "RB":
            cols = (("carries", "carries"), ("rushing_yards", "rushing yards"), ("rushing_tds", "rushing touchdowns")) + cols
        for col, label in cols if has_box else ():
            box[col] = val("box", col, label)
            box[col + "_value"] = raw("box", col)
    p["box"] = box
    rec, why = recognizable(snap, p)
    p["recognizable"], p["recognition"] = rec, why
    p["angles"] = angles(book, snap, p)


def angles(book, snap: dict, p: dict) -> list[dict]:
    gid, pos, week, team, kind = p["gsis_id"], p["position"], p["week"], p["team"], p["role"]
    add, lead, box, game = book.add, p["lead"], p["box"], p["game"]
    out = []
    if pos == "QB" and kind == "flame":
        att = box["attempts_value"]
        rows = _rows(snap, "qb", gid)
        weeks = [r["week"] for r in rows if r["attempts"]]
        adot = FL._row(snap, "qb", gid, week)["passing_air_yards"] / att
        league = FL.evaluate(snap, {"kind": "fp_league_rate", "rate": "adot", "position": "QB", "through": week})
        if adot <= SHORT_RATIO * league:
            src = lambda k, **kw: {"kind": k, "rate": "adot", "position": "QB", "through": week, **kw}
            a = {"id": "short_throws", "broad": True, "flavor": "distance",
                 "x": add(f"air yards per attempt, week {week}", "dec1", {"kind": "fp_rate", "rate": "adot",
                                                                          "gsis_id": gid, "weeks": [week]}, player=gid),
                 "league": add(f"league air yards per attempt, QB games weeks 1-{week}", "dec1", src("fp_league_rate")),
                 "season": add("his air yards per attempt this season", "dec1",
                               {"kind": "fp_rate", "rate": "adot", "gsis_id": gid, "weeks": weeks}, player=gid),
                 "season_rank": add("rank of his season air yards per attempt, QBs (1 = shortest)", "ordinal",
                                    src("fp_season_rate_rank", gsis_id=gid, direction="flame",
                                        min_den=FL.CONSTANTS["season_min_attempts"]), player=gid),
                 "season_count": add("QBs with the minimum attempts this season", "int",
                                     src("fp_season_rate_count", min_den=FL.CONSTANTS["season_min_attempts"])),
                 "min_attempts": add("season attempts floor", "int", {"kind": "fp_constant",
                                                                      "name": "season_min_attempts"}),
                 "yac_share": add(f"share of his passing yards after the catch, week {week}", "pct",
                                  {"kind": "fp_rate", "rate": "yac_share", "gsis_id": gid, "weeks": [week]},
                                  player=gid)}
            a["season_rank_value"] = book.claims[-4]["value"]
            a["yac_share_value"] = book.claims[-1]["value"]
            a["strength"] = 5 if a["season_rank_value"] == 1 else 4
            out.append(a)
        rough = (box["sacks_suffered_value"] >= ROUGH_SACKS) + (box["passing_interceptions_value"] >= ROUGH_INTS) \
            + (FL.evaluate(snap, {"kind": "fp_points", "team": team, "week": week}) <= ROUGH_POINTS) \
            + (lead["rank_value"] <= 3)
        if rough >= 2:
            out.append({"id": "rough_day", "broad": True, "flavor": "collapse", "strength": 2 + rough})
    if kind == "praise" and pos in ("WR", "TE", "RB") and box["targets_value"] >= HOG_TARGETS:
        opp = book.claims[[c["id"] for c in book.claims].index(p["opponent"])]["value"]
        a = {"id": "target_hog", "broad": True, "flavor": "volume",
             "opp_targets": add(f"{opp} team targets, week {week}", "int", {"kind": "team_targets", "team": opp,
                                                                            "week": week}, team=opp),
             "teams_fewer": add(f"NFL teams with fewer targets than him, week {week}", "int",
                                {"kind": "fp_teams_fewer_targets", "gsis_id": gid, "week": week}, player=gid),
             "rank": add(f"rank of his targets among {pos} games this season", "ordinal",
                         {"kind": "fp_rank", "metric": "targets", "position": pos, "gsis_id": gid, "week": week,
                          "through": week, "direction": "praise"}, player=gid),
             "games": add(f"qualifying {pos} games this season", "int",
                          {"kind": "fp_games", "metric": "targets", "position": pos, "through": week})}
        a["opp_targets_value"] = book.claims[-4]["value"]
        a["teams_fewer_value"] = book.claims[-3]["value"]
        a["rank_value"] = book.claims[-2]["value"]
        a["beats_opponent"] = a["opp_targets_value"] < box["targets_value"]
        a["strength"] = 5 if a["beats_opponent"] else 4 if a["teams_fewer_value"] else 3
        out.append(a)
    if kind == "praise":
        yards = box.get("passing_yards_value") if pos == "QB" else box.get("receiving_yards_value", 0) + (
            box.get("rushing_yards_value", 0) if pos == "RB" else 0)
        tds = box.get("passing_tds_value", 0) + box.get("receiving_tds_value", 0) + box.get("rushing_tds_value", 0)
        if yards and (yards >= MONSTER_YARDS[pos] or tds >= 3 or lead["rank_value"] == 1):
            out.append({"id": "monster_line", "broad": True, "flavor": "box", "strength": 3 + (tds >= 2)
                        + (yards >= MONSTER_YARDS[pos])})
    out.append({"id": "stat_outlier", "broad": lead["metric"] in ("air_yards", "targets", "ypa", "rec_air_yards"),
                "flavor": "stat", "strength": 2})
    return out


# -------------------------------------------------------------------------------------------- briefs

def series_history(episodes_dir: Path, season: int) -> list[dict]:
    """Earlier series videos' structures, oldest first (history.jsonl rows with structure.series)."""
    path = episodes_dir / "history.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rows = [r for r in rows if r.get("season") == season and (r.get("structure") or {}).get("series") == SERIES]
    rows.sort(key=lambda r: (r["week"], r.get("order", 0)))
    return [r["structure"] for r in rows]


def repetition(fmt: str, recent: list[dict]) -> tuple[float, list[str]]:
    """How much a format would repeat the last two series videos: the format itself, its look, its opening, its joke
    mechanism and its ending each count. Changing the opening sentence alone never makes a video different."""
    spec, last2 = FORMATS[fmt], recent[-2:]
    pen, why = 0.0, []
    for key, weight in (("format_family", 2.5), ("visual_style", 1.0), ("opening", 0.75), ("joke", 0.75),
                        ("ending", 1.0)):
        mine = fmt if key == "format_family" else spec[{"visual_style": "style"}.get(key, key)]
        hits = sum(1 for r in last2 if r.get(key) == mine)
        if hits:
            pen += weight * hits
            why.append(f"{key} '{mine}' in {hits} of the last two")
    return pen, why


def entertainment_test(p: dict, angle: dict) -> tuple[bool, str]:
    """Would someone who doesn't play fantasy football still watch this?"""
    if not angle["broad"]:
        return False, f"the premise ({angle['id']}) only means something to fantasy players"
    if not (p["recognizable"] or p["lead"]["rank_value"] == 1):
        return False, f"neither a name people know ({p['recognition']}) nor a season extreme"
    return True, (f"{angle['id']}: a football premise anyone gets; "
                  + ("a name people know (" + p["recognition"] + ")" if p["recognizable"] else "a season extreme"))


def choose(p: dict, recent: list[dict], treatment: str | None = None) -> dict:
    """The creative brief: angle, format, look, music, length, opening, joke mechanism, ending, and why."""
    kind = p["role"]
    options = []
    for a in p["angles"]:
        for fmt, spec in FORMATS.items():
            fit = FIT.get((fmt, a["id"]), 0)
            if kind not in spec["kinds"] or not fit or not eligible(fmt, a, p):
                continue
            pen, why = repetition(fmt, recent)
            options.append({"format": fmt, "angle": a, "fit": fit, "penalty": pen, "why_penalty": why,
                            "score": round(a["strength"] + fit - pen, 2)})
    if treatment:
        options = [o for o in options if o["format"] == treatment] or options
    if not options:
        a = next(a for a in p["angles"] if a["id"] == "stat_outlier")
        ok, why = entertainment_test(p, a)
        return {"series": SERIES, "kind": kind, "angle": a["id"], "format": None, "entertainment_test":
                {"pass": False, "why": "no format can tell this story: " + why}, "options": []}
    options.sort(key=lambda o: (-o["score"], o["format"]))
    best = options[0]
    spec = FORMATS[best["format"]]
    ok, why = entertainment_test(p, best["angle"])
    return {"series": SERIES, "kind": kind, "player": p["name"], "angle": best["angle"]["id"],
            "premise": premise(p, best["angle"]), "format": best["format"], "visual_style": spec["style"],
            "music_mood": spec["mood"], "length_range": list(spec["length"]), "opening": spec["opening"],
            "joke": spec["joke"], "ending": spec["ending"], "humor": spec["humor"],
            "exaggeration": spec["exaggeration"], "parody": spec.get("parody", False),
            "scores": {"angle_strength": best["angle"]["strength"], "format_fit": best["fit"],
                       "repetition_penalty": best["penalty"], "total": best["score"],
                       "evidence_rank": f"{p['lead']['rank_value']} of {p['lead']['games_value']}",
                       "recognizable": p["recognizable"], "recognition": p["recognition"]},
            "why_not_repeated": best["why_penalty"] or ["nothing in the last two series videos matches"],
            "entertainment_test": {"pass": ok, "why": why},
            "options": [{k: o[k] for k in ("format", "fit", "penalty", "score")} | {"angle": o["angle"]["id"]}
                        for o in options]}


def eligible(fmt: str, a: dict, p: dict) -> bool:
    """A format needs the facts its joke stands on."""
    if fmt == "comparison" and a["id"] == "target_hog":
        return a["beats_opponent"] or a["teams_fewer_value"] > 0
    if fmt == "courtroom":
        return p["position"] == "QB"
    if fmt in ("main_character", "fan_meltdown"):
        return len([k for k in p["box"] if not k.endswith("_value")]) >= 3
    return True


def premise(p: dict, a: dict) -> str:
    return {"short_throws": "his passes barely left the line of scrimmage",
            "rough_day": "a passing afternoon that came apart",
            "target_hog": "one receiver drew more targets than an NFL offense",
            "monster_line": "a monster box score",
            "stat_outlier": f"an extreme {p['lead']['label']}"}[a["id"]]


# ---------------------------------------------------------------------------------------------- scripts

def compose(facts: dict, claims: dict, brief: dict):
    """Beats, titles and structure for the brief's format. Each builder is its own show."""
    from .formats import said
    from .story import beat, last, ph
    p = facts["players"][0]
    a = next(x for x in p["angles"] if x["id"] == brief["angle"])
    fmt = brief["format"]
    ctx = {"p": p, "a": a, "claims": claims, "name": said(p["name"]), "who": last(p["name"]),
           "team": claims[p["team_name"]]["display"], "opp": ph(p["opponent"]), "ph": ph, "beat": beat}
    beats, titles = BUILDERS[fmt](ctx)
    for b in beats:
        b["scene"].update(fp=p["role"], style=FORMATS[fmt]["style"])
        b["scene"].setdefault("sfx", False)
    return beats, titles


def _about_half(a: dict) -> bool:
    """'About half' is only said when the share shown on screen is 45-55%."""
    return 0.45 <= a["yac_share_value"] <= 0.55


def _won(ctx) -> bool:
    return ctx["p"]["game"]["result_value"] == "won"


def build_roast_monologue(ctx):
    """One observation, said deadpan, escalated twice, then cut. No music; silences do the work."""
    p, a, ph, beat, name, who = ctx["p"], ctx["a"], ctx["ph"], ctx["beat"], ctx["name"], ctx["who"]
    box, g = p["box"], p["game"]
    still = {"camera": "still"}
    if a["id"] == "short_throws":
        lines = [
            ("open", f"{name} played the {ctx['opp']} with his passing game on airplane mode.",
             {"type": "dp_toggle", "label": "AIRPLANE MODE", "claims": [p["opponent"]]}, 0.98, 0.35),
            ("stat", f"His average throw traveled {ph(a['x'])} yards.{BEAT} The league average is {ph(a['league'])}.",
             {"type": "dp_stat", "big": a["x"], "unit": "YARDS PER THROW", "small": a["league"],
              "small_label": "LEAGUE AVERAGE", "claims": [a["x"], a["league"]]}, 1.0, 0.3),
        ]
        if _about_half(a) and a["season_rank_value"] != 1:       # one escalation: the season fact beats this one
            lines.append(("escalate", f"About half his passing yards happened after the catch.{BEAT} The ball "
                          "showed up.‖ The receivers did the rest.",
                          {"type": "dp_line", "lines": ["{yac} OF HIS YARDS:", "AFTER THE CATCH"], "fill": {"yac": a["yac_share"]},
                           "claims": [a["yac_share"]]}, 1.0, 0.3))
        if a["season_rank_value"] == 1:
            lines.append(("escalate2", f"And it's not a one-week thing.{BEAT} Nobody with {ph(a['min_attempts'])}-plus "
                          f"throws this season throws it shorter.",
                          {"type": "dp_line", "lines": ["SHORTEST AVERAGE THROW", "IN THE NFL THIS SEASON"],
                           "foot": "MIN. {min} ATTEMPTS · {season} YARDS", "fill": {"min": a["min_attempts"],
                                                                                    "season": a["season"]},
                           "claims": [a["min_attempts"], a["season"], a["season_rank"], a["season_count"]]}, 1.0, 0.35))
        end = (f"The {ctx['team']} {ph(g['result'])} anyway.{BEAT} The deep safety would like his afternoon back."
               if _won(ctx) else f"The {ctx['team']} {ph(g['result'])}.{BEAT} The deep safety would like his "
               "afternoon back.")
        lines.append(("punchline", end, {"type": "dp_end", "lines": ["THE DEEP SAFETY", "WOULD LIKE HIS", "AFTERNOON BACK."],
                                         "claims": [g["result"], g["points"], g["opp_points"]]}, 0.96, 0.7))
        titles = [f"{p['name']} Played the {ph(p['opponent'])} on Airplane Mode",
                  f"What Was {p['name']} Doing Against the {ph(p['opponent'])}?"]
    else:                                   # rough_day
        lines = [
            ("open", f"{name}'s afternoon against the {ctx['opp']} had the energy of a group project nobody started.",
             {"type": "dp_line", "lines": ["GROUP PROJECT.", "NOBODY STARTED."], "claims": [p["opponent"]]}, 0.98, 0.35),
            ("stat", f"{ph(box['sacks_suffered'])} sacks.{BEAT} {ph(box['passing_interceptions'])} "
                     f"interception{'s' if box['passing_interceptions_value'] != 1 else ''}.{BEAT} "
                     f"{ph(g['points'])} points.",
             {"type": "dp_stat", "big": g["points"], "unit": "POINTS", "small": box["sacks_suffered"],
              "small_label": "SACKS", "claims": [box["sacks_suffered"], box["passing_interceptions"], g["points"]]},
             1.0, 0.3),
            ("punchline", f"{'At home.' if g['home'] else 'On the road, at least.'}{BEAT} The film room is going to "
                          "need snacks.",
             {"type": "dp_end", "lines": ["THE FILM ROOM", "IS GOING TO", "NEED SNACKS."],
              "claims": [g["venue"]]}, 0.96, 0.7),
        ]
        titles = [f"This {p['name']} Performance Needs an Explanation",
                  f"{p['name']} vs the {ph(p['opponent'])} Was a Group Project"]
    beats = [beat(sec, text, {**still, **sc, "transition": "none" if i else "none"}, [], speed, pause=pause)
             for i, (sec, text, sc, speed, pause) in enumerate(lines)]
    beats[-1]["scene"]["music_drop"] = True
    beats[-1]["scene"]["sfx_cue"] = "scratch"
    return beats, titles


def build_courtroom(ctx):
    """Case called, exhibits entered one by one (each a verified stat, stamped), an absurd verdict, a gavel."""
    p, a, ph, beat, name, who = ctx["p"], ctx["a"], ctx["ph"], ctx["beat"], ctx["name"], ctx["who"]
    box, g, lead = p["box"], p["game"], p["lead"]
    exhibits = []
    if a["id"] == "short_throws":
        exhibits.append((f"his average throw went {ph(a['x'])} yards.‖ League average:‖ {ph(a['league'])}.",
                         a["x"], "YARDS PER THROW", [a["x"], a["league"]]))
        if a["season_rank_value"] == 1:
            exhibits.append((f"the shortest average throw in the league this season,‖ minimum "
                             f"{ph(a['min_attempts'])} attempts.", a["season"], "SEASON AIR YARDS / ATT",
                             [a["season"], a["season_rank"], a["season_count"], a["min_attempts"]]))
        if _about_half(a):
            exhibits.append(("about half his passing yards came after the catch.", a["yac_share"],
                             "OF HIS YARDS: AFTER THE CATCH", [a["yac_share"]]))
        verdict = ("The court finds the passing game guilty of loitering near the line of scrimmage.", "GUILTY",
                   "LOITERING NEAR THE LINE OF SCRIMMAGE")
    else:
        exhibits.append((f"{ph(box['sacks_suffered'])} sacks.", box["sacks_suffered"], "SACKS", [box["sacks_suffered"]]))
        worst = {"lowest": "worst", "fewest": "fewest"}.get(lead["sup"], lead["sup"])
        how = "by expected points added" if lead["metric"] == "epa" else f"by {lead['label']}"
        exhibits.append((f"{how},‖ the {ph(lead['rank'])}-{worst} of {ph(lead['games'])} quarterback games this "
                         "season." if lead["rank_value"] > 1 else
                         f"{how},‖ the {worst} of {ph(lead['games'])} quarterback games this season.",
                         lead["value"], lead["screen"], [lead["value"], lead["rank"], lead["games"]]))
        exhibits.append((f"the {ctx['team']} scored {ph(g['points'])} points.{BEAT} "
                         + ("At home." if g["home"] else "Total."), g["points"], "POINTS SCORED",
                         [g["points"], g["venue"]]))
        verdict = ("The court finds this passing game guilty of loitering in its own backfield.", "GUILTY",
                   "LOITERING IN ITS OWN BACKFIELD")
    beats = [beat("case", f"Order in the court.‖ The NFL versus {name},‖ {ph(p['week_claim'])}, against the "
                          f"{ctx['opp']}.",
                  {"type": "court_docket", "camera": "still", "claims": [p["week_claim"], p["opponent"]],
                   "sfx_cue": "gavel"}, [], 1.0, pause=0.3)]
    for i, (text, big, label, cl) in enumerate(exhibits[:3]):
        letter = "ABC"[i]
        beats.append(beat(f"exhibit_{letter.lower()}", f"Exhibit {letter}:‖ {text}",
                          {"type": "court_exhibit", "letter": letter, "big": big, "label": label, "camera": "still",
                           "transition": "cut", "claims": cl, "index": i}, [], 1.04, pause=0.25))
    beats.append(beat("verdict", f"{verdict[0]}{BEAT} Case closed.",
                      {"type": "court_verdict", "stamp": verdict[1], "charge": verdict[2], "camera": "still",
                       "transition": "zoom", "music_drop": True, "claims": [], "sfx_cue": "gavel"},
                      ["guilty"], 1.0, pause=0.6))
    titles = [f"The NFL v. {p['name']}, {ph(p['week_claim'])}", f"{p['name']} Has Been Charged (Parody)"]
    return beats, titles


def build_comparison(ctx):
    """One verified benchmark next to his number, drawn as a football picture, then the callback."""
    p, a, ph, beat, name, who = ctx["p"], ctx["a"], ctx["ph"], ctx["beat"], ctx["name"], ctx["who"]
    box, g = p["box"], p["game"]
    if a["id"] == "short_throws":
        field = {"type": "cmp_field", "camera": "still", "x": a["x"], "league": a["league"], "season": a["season"],
                 "min": a["min_attempts"]}
        beats = [
            beat("tease", f"We tried to measure {name}'s passes against the {ctx['opp']}.{BEAT} We needed a smaller "
                          "ruler.", {**field, "stage": "tease", "claims": [p["opponent"]]}, ["smaller"], 1.0, pause=0.2),
            beat("benchmark", f"The average NFL pass this season travels {ph(a['league'])} yards.‖ His average in that "
                              f"game?{BEAT} {ph(a['x'])}.",
                 {**field, "stage": "him", "transition": "none", "music_drop": True,
                  "claims": [a["league"], a["x"], p["opponent"]]}, [], 1.02, pause=0.35),
        ]
        if a["season_rank_value"] == 1:
            beats.append(beat("season", f"And for the whole season,‖ at {ph(a['season'])} yards a throw,‖ nobody "
                                        f"with {ph(a['min_attempts'])}-plus attempts throws it shorter.",
                              {**field, "stage": "season", "transition": "none",
                               "claims": [a["league"], a["x"], a["season"], a["min_attempts"], a["season_rank"],
                                          a["season_count"]]}, ["nobody"], 1.06, pause=0.3))
        hi_lo = (g["points"], g["opp_points"]) if _won(ctx) else (g["opp_points"], g["points"])
        result = (f"The {ctx['team']} {ph(g['result'])} anyway, {ph(hi_lo[0])} to {ph(hi_lo[1])}." if _won(ctx) else
                  f"The {ctx['team']} {ph(g['result'])}, {ph(hi_lo[0])} to {ph(hi_lo[1])}.")
        beats.append(beat("callback", f"{result}{BEAT} Somebody tell the deep safety he can go home.",
                          {**field, "stage": "safety", "transition": "none",
                           "claims": [a["league"], a["x"], g["points"], g["opp_points"], g["result"]]},
                          ["home"], 1.0, pause=0.6))
        titles = [f"{p['name']}'s Passes Didn't Go Anywhere", f"The Shortest Throws in the NFL Belong to {p['name']}"]
        return beats, titles
    # target_hog
    opp = ctx["opp"]
    tally = {"type": "cmp_tally", "camera": "still", "him": p["box"]["targets"], "them": a["opp_targets"]}
    beats = [beat("setup", f"{name} was targeted {ph(box['targets'])} times against the {opp}.",
                  {**tally, "stage": "him", "claims": [box["targets"], p["opponent"]]}, [], 1.04, pause=0.2)]
    if a["beats_opponent"]:
        beats.append(beat("versus", f"The entire {opp} offense?{BEAT} {ph(a['opp_targets'])}.",
                          {**tally, "stage": "them", "transition": "none", "music_drop": True,
                           "claims": [box["targets"], a["opp_targets"], p["opponent"]]}, ["entire"], 1.0, pause=0.35))
        punch = "One receiver out-targeted a whole NFL offense.‖ In the same game."
    else:
        beats.append(beat("versus", f"{ph(a['teams_fewer'])} entire NFL offenses got fewer than that this week.",
                          {**tally, "stage": "teams", "transition": "none", "claims": [box["targets"],
                                                                                         a["teams_fewer"]]},
                          ["entire"], 1.04, pause=0.35))
        punch = "Whole offenses.‖ Fewer targets than one receiver."
    hi_lo = (g["points"], g["opp_points"]) if _won(ctx) else (g["opp_points"], g["points"])
    beats.append(beat("callback", f"{punch}{BEAT} The {ctx['team']} {ph(g['result'])}, {ph(hi_lo[0])} to "
                                  f"{ph(hi_lo[1])},‖ but that stat line won the group chat.",
                      {**tally, "stage": "end", "transition": "none",
                       "claims": [box["targets"], a["opp_targets"], g["result"], g["points"], g["opp_points"]]},
                      ["group"], 1.04, pause=0.6))
    titles = [f"{p['name']} Out-Targeted the Entire {ph(p['opponent'])} Offense",
              f"{ph(box['targets'])} Targets: {p['name']} Is Actually Ridiculous"]
    return beats, titles


def _slams(p: dict) -> list[tuple[str, str]]:
    """(claim, word) for the box-score slam, in the order a broadcast would say them."""
    b = p["box"]
    if p["position"] == "QB":
        order = [("completions", "completions"), ("passing_yards", "yards"), ("passing_tds", "touchdowns")]
    else:
        order = [("targets", "targets"), ("receptions", "catches"), ("receiving_yards", "yards")]
        if b.get("receiving_tds_value"):
            order.append(("receiving_tds", "touchdown" if b["receiving_tds_value"] == 1 else "touchdowns"))
    return [(b[k], w) for k, w in order if k in b]


def build_main_character(ctx):
    """A cinematic edit: letterboxed title cards, stat slams on the beat, the contrast, a movie-poster finish."""
    p, a, ph, beat, name, who = ctx["p"], ctx["a"], ctx["ph"], ctx["beat"], ctx["name"], ctx["who"]
    g = p["game"]
    slams = _slams(p)
    beats = [beat("title", f"{ph(p['week_claim'])}.‖ The {ctx['team']} and the {ctx['opp']}.{BEAT} And one "
                           f"{POS_WORD[p['position']]} who decided everybody else was optional.",
                  {"type": "cine_title", "camera": "push", "claims": [p["week_claim"], p["opponent"]],
                   "sfx_cue": "boom"}, ["optional"], 0.98, pause=0.25),
             beat("slams", "‖ ".join(f"{ph(c)} {w}." for c, w in slams),
                  {"type": "cine_slam", "camera": "push", "transition": "cut", "slams": [c for c, _ in slams],
                   "words": [w.upper() for _, w in slams], "claims": [c for c, _ in slams]}, [], 1.08, pause=0.2)]
    if a["id"] == "target_hog":
        rank_line = (f"Out of {ph(a['games'])} {POS_WORD[p['position']]} games this season,‖ nobody's seen more."
                     if a["rank_value"] == 1 else
                     f"That's the {ph(a['rank'])}-most of {ph(a['games'])} {POS_WORD[p['position']]} games this season.")
        if a["beats_opponent"]:
            line = f"The whole {ctx['opp']} offense got {ph(a['opp_targets'])}.‖ {rank_line}"
            cl = [a["opp_targets"], p["opponent"], a["rank"], a["games"]]
        else:
            line = rank_line
            cl = [a["rank"], a["games"]]
        beats.append(beat("contrast", line, {"type": "cine_contrast", "camera": "push", "transition": "zoom",
                                             "them": a.get("opp_targets") if a["beats_opponent"] else None,
                                             "games": a["games"], "rank": a["rank"], "rank1": a["rank_value"] == 1,
                                             "claims": cl}, ["whole", "nobody"], 1.04, pause=0.25))
    pts = lambda cid: ctx["claims"][cid]["value"]
    shootout = " a shootout" if min(pts(g["points"]), pts(g["opp_points"])) >= SHOOTOUT_POINTS else ""
    if g["result_value"] == "lost":
        end = (f"The {ctx['team']} {ph(g['result'])}{shootout}, {ph(g['opp_points'])} to {ph(g['points'])}.{BEAT} "
               f"{who} did not.")
        tag = f"THE {ctx['team'].upper()} LOST. HE DIDN'T."
    else:
        end = (f"The {ctx['team']} {ph(g['result'])}{shootout}, {ph(g['points'])} to {ph(g['opp_points'])}.{BEAT} "
               f"{who} was the whole movie.")
        tag = "STARRING. DIRECTED BY. PRODUCED BY."
    abbr = {"targets": "TGT", "catches": "REC", "yards": "YDS", "touchdown": "TD", "touchdowns": "TD",
            "completions": "CMP"}
    beats.append(beat("poster", end, {"type": "cine_poster", "camera": "push", "transition": "zoom", "tagline": tag,
                                      "music_drop": True, "sfx_cue": "boom",
                                      "credits": [[c, abbr.get(w, w.upper())] for c, w in slams],
                                      "claims": [g["points"], g["opp_points"], g["result"]] + [c for c, _ in slams]},
                      ["did", "whole"], 0.98, pause=0.7))
    titles = [f"{p['name']} Was the Main Character Against the {ph(p['opponent'])}",
              f"Nobody Was Stopping {p['name']}"]
    return beats, titles


def build_fan_meltdown(ctx):
    """Fake calm, then each stat louder than the last, then a scream and a hard cut."""
    p, a, ph, beat, name, who = ctx["p"], ctx["a"], ctx["ph"], ctx["beat"], ctx["name"], ctx["who"]
    slams = _slams(p)
    beats = [beat("calm", f"Okay.‖ Everybody relax.‖ We're going to talk about {name} calmly.",
                  {"type": "melt_calm", "camera": "still", "claims": []}, ["calmly"], 0.96, pause=0.2)]
    for i, (c, w) in enumerate(slams[:3]):
        beats.append(beat(f"stat_{i + 1}", f"{ph(c)} {w}.", {"type": "melt_stat", "camera": "still", "level": i + 1,
                                                               "big": c, "word": w.upper(), "transition": "cut",
                                                               "claims": [c]}, [], 1.04 + 0.05 * i, pause=0.12))
    if a["id"] == "target_hog" and a["beats_opponent"]:
        beats.append(beat("detail", f"The {ctx['opp']} as a team got {ph(a['opp_targets'])}!",
                          {"type": "melt_stat", "camera": "still", "level": 4, "big": a["opp_targets"],
                           "word": "THE WHOLE OTHER TEAM", "transition": "cut",
                           "claims": [a["opp_targets"], p["opponent"]]}, [], 1.15, pause=0.12))
    beats.append(beat("scream", "We are not calm.‖ We will never be calm again.",
                      {"type": "melt_scream", "camera": "still", "transition": "cut", "claims": [],
                       "sfx_cue": "swell"}, ["never"], 1.12, pause=0.5))
    titles = [f"Stay Calm About {p['name']}? Impossible", f"{p['name']} Is Actually Ridiculous"]
    return beats, titles


BUILDERS = {"roast_monologue": build_roast_monologue, "courtroom": build_courtroom, "comparison": build_comparison,
            "main_character": build_main_character, "fan_meltdown": build_fan_meltdown}


def structure(brief: dict, beats: list[dict]) -> dict:
    """What history.jsonl records for the series, so the next brief can avoid repeating it."""
    return {"series": SERIES, "angle": brief["kind"], "format": brief["kind"], "format_family": brief["format"],
            "story_angle": brief["angle"], "hook": f"{brief['format']}:{brief['opening']}",
            "opening": brief["opening"], "joke": brief["joke"], "ending": brief["ending"],
            "visual_style": brief["visual_style"], "music_mood": brief["music_mood"] or "none",
            "music": bool(brief["music_mood"]), "duration_range": brief["length_range"],
            "beats": [b["section"] for b in beats], "scenes": [b["scene"]["type"] for b in beats],
            "transitions": [b["scene"].get("transition", "push") for b in beats[1:]],
            "rhythm": [b.get("pause") for b in beats], "primary_metric": brief["angle"], "teammate": None}


def fantasy_ending(text: str) -> bool:
    low = f" {text.lower()} "
    return any(w in low for w in FANTASY_ENDING)


def describe(ep: Episode, brief: dict) -> str:
    """brief.md: the creative brief in words, for the review before rendering."""
    lines = [f"# Creative brief: {brief.get('player', '')} ({brief['kind']})", "",
             f"- Premise: {brief.get('premise', '—')}", f"- Angle: {brief['angle']}",
             f"- Format: **{brief['format']}** ({brief.get('humor', '')} humor, {brief.get('exaggeration', '')} "
             "exaggeration)",
             f"- Look: {brief.get('visual_style')}; music: {brief.get('music_mood') or 'none'}; "
             f"length {brief.get('length_range')} s",
             f"- Opening: {brief.get('opening')}; joke: {brief.get('joke')}; ending: {brief.get('ending')}",
             f"- Entertainment test: {'PASS' if brief['entertainment_test']['pass'] else 'FAIL'} "
             f"({brief['entertainment_test']['why']})",
             f"- Not a repeat because: {'; '.join(brief.get('why_not_repeated', []))}", "",
             "Options considered (score = angle strength + format fit - repetition):", ""]
    lines += [f"- {o['format']} on {o['angle']}: fit {o['fit']}, repetition -{o['penalty']}, score {o['score']}"
              for o in brief.get("options", [])]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------- editorial, thumbnail, metadata

def editorial_row(p: dict, brief: dict, words: int) -> dict:
    """The brief's scores in the editorial board's shape, so review.md shows them like every other Short's."""
    from .. import editorial as E
    sc = brief["scores"]
    s = {"relevance": 5 if p["recognizable"] else 3, "hook": min(5, sc["format_fit"]),
         "surprise": 5 if p["lead"]["rank_value"] == 1 else 4, "evidence": 4,
         "actionability": 3, "timeliness": 4, "visual": 5,
         "distinctiveness": max(1, 5 - round(sc["repetition_penalty"]))}
    why = {"relevance": p["recognition"], "hook": f"{brief['format']} fit {sc['format_fit']} for {brief['angle']}",
           "surprise": f"rank {sc['evidence_rank']}", "evidence": "verified game and season claims",
           "actionability": "entertainment: a take to share or argue with, not a lineup call",
           "timeliness": "the latest game", "visual": f"{brief['visual_style']} look, format-specific scenes",
           "distinctiveness": "; ".join(brief["why_not_repeated"])}
    row = {"player": p["name"], "format": brief["format"], "scores": s, "reasons": why, "total": sum(s.values()),
           "recognizable": p["recognizable"], "verdict": "make" if brief["entertainment_test"]["pass"] else "reject",
           "reject_reasons": [] if brief["entertainment_test"]["pass"] else [brief["entertainment_test"]["why"]]}
    lo, hi = brief["length_range"]
    row["checks"] = {k: v for k, v in E.checks(row).items() if k != "consequence"}
    row["checks"]["entertainment"] = {"question": "If this viewer did not play fantasy football, would it still be "
                                                  "worth watching?", "pass": brief["entertainment_test"]["pass"],
                                      "why": brief["entertainment_test"]["why"]}
    row["checks"]["length"] = {"question": f"Does it fit the format's {lo}-{hi} s?", "pass": True,
                               "why": f"{words} words; checked against the render by QC"}
    return row


def thumbnail(p: dict, claims: dict, structure: dict) -> tuple[str, str, str]:
    """Kicker, big text, label: the story, not the stat sheet."""
    d = lambda cid: claims[cid]["display"]
    a = next(x for x in p["angles"] if x["id"] == structure["story_angle"])
    last = p["name"].split()[-1].upper()
    fam = structure["format_family"]
    if a["id"] == "short_throws":
        return ("AIRPLANE MODE" if fam == "roast_monologue" else "THE NFL v. " + last if fam == "courtroom"
                else "SHORTEST THROWS IN THE NFL"), f"{d(a['x'])} YDS", "NFL AVERAGE: " + d(a["league"])
    if a["id"] == "rough_day":
        return ("THE NFL v. " + last if fam == "courtroom" else "WHAT HAPPENED?"), \
            f"{d(p['game']['points'])} POINTS", f"{d(p['box']['sacks_suffered'])} SACKS"
    if a["id"] == "target_hog":
        them = f"THE {d(p['opponent']).upper()}: {d(a['opp_targets'])}" if a["beats_opponent"] else "ONE RECEIVER"
        return ("MAIN CHARACTER" if fam == "main_character" else "WE ARE NOT CALM" if fam == "fan_meltdown"
                else "ONE MAN. ONE OFFENSE."), f"{d(p['box']['targets'])} TARGETS", them
    slams = [(c, w) for c, w in [(p["box"].get("receiving_yards"), "YARDS"), (p["box"].get("passing_yards"), "YARDS")]
             if c]
    big = f"{d(slams[0][0])} {slams[0][1]}" if slams else d(p["lead"]["value"])
    return "NOBODY WAS STOPPING HIM" if p["role"] == "praise" else "WHAT HAPPENED?", big, p["lead"]["screen"]


def description(p: dict, claims: dict, structure: dict) -> list[str]:
    d = lambda cid: claims[cid]["display"]
    g = p["game"]
    lines = [f"{p['name']} vs the {d(p['opponent'])}, {d(p['week_claim'])}: {premise(p, next(a for a in p['angles'] if a['id'] == structure['story_angle']))}. "
             f"Final: {d(g['points'])}-{d(g['opp_points'])} ({d(g['result'])}).",
             "Every stat is from nflverse game data; the jokes are jokes."]
    if FORMATS[structure["format_family"]].get("parody"):
        lines.append("Parody: no real court, ruling or statement is depicted.")
    return lines


def tags(p: dict, claims: dict) -> list[str]:
    d = lambda cid: claims[cid]["display"]
    return ["shorts", "nfl", "football", "nfl shorts", "nfl memes", p["name"], d(p["team_name"]), d(p["opponent"]),
            f"{d(p['team_name'])} vs {d(p['opponent'])}"]


def storyboard(ep: Episode) -> Path:
    """storyboard.png: each beat's scene in its settled state, with its line beneath. No narration, no render: a
    cheap preview to judge a brief before spending a render on it."""
    from PIL import Image, ImageDraw

    from .. import headshots
    from .. import motion as M
    from ..core import read_json
    from . import render_motion as R
    script, facts = read_json(ep.path("script.json")), read_json(ep.path("facts.json"))
    claims = {c["id"]: c for c in facts["claims"]}
    st = script["structure"]
    from .. import flame_scenes as FPS
    ctx = R.Ctx(facts, claims, FPS.ACCENT.get(st["angle"], M.GOLD), st["angle"])
    ctx.headshot, ctx.headshots_on = headshots.for_player(ep.cfg, facts["players"][0]), headshots.enabled(ep.cfg)
    tw, th, pad = 324, 576, 18
    segs = script["narration_segments"]
    sheet = Image.new("RGB", (len(segs) * (tw + pad) + pad, th + 300), (16, 16, 20))
    d = ImageDraw.Draw(sheet)
    font = M.font("bold", 22)
    for i, (seg, shot) in enumerate(zip(segs, script["shots"])):
        dur = max(1.5, len(seg["text_spoken"].split()) / 2.9)
        ctx.tokens = []
        frame = R.draw_scene({**shot, "_start": 0.0}, dur * 0.95, dur, ctx).resize((tw, th))
        x = pad + i * (tw + pad)
        sheet.paste(frame, (x, pad))
        d.text((x, th + pad + 8), f"{i + 1}. {shot['type']}", font=M.font("black", 22), fill=(255, 205, 64))
        line, y = "", th + pad + 40
        for w in seg["text_display"].split():
            if font.getlength(f"{line} {w}") > tw:
                d.text((x, y), line, font=font, fill=(235, 235, 240))
                line, y = w, y + 28
            else:
                line = f"{line} {w}".strip()
        d.text((x, y), line, font=font, fill=(235, 235, 240))
    out = ep.path("storyboard.png")
    sheet.save(out)
    return out
