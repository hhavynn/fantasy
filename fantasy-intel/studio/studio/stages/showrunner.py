"""Showrunner for the flame / praise series: NFL entertainment, powered by real football data.

stages/flame.py finds the game (an extreme line against the position and against the player's own season). This
module decides what makes it worth watching for someone who has never played fantasy football, and how to tell it:

  1. ANGLES   the football premise, from verified data only. Each angle carries its own claims and says
              whether a casual NFL fan gets it without a spreadsheet (`broad`):
                short_throws  a QB's passes went nowhere: his average throw against the league's, and (when true)
                              the shortest average throw of any QB with real volume this season
                rough_day     a QB's afternoon fell apart: sacks, interceptions, points, the worst-of-season stat
                target_hog    a receiver's pile of targets: more than the other offense, or than whole NFL offenses,
                              only when the week's team totals say so
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
AIRLESS_SHARE = 0.30          # "only 20 of his 107 passing yards came in the air": at most this share flew
SHOOTOUT_POINTS = 27         # "a shootout": both teams scored at least this many
HOG_TARGETS = 15               # target_hog: at least this many targets...
MIN_TEAMS_FEWER = 2           # ..."N entire NFL offenses got fewer": one offense is a coincidence, not a joke
MONSTER_YARDS = {"QB": 350, "WR": 140, "TE": 110, "RB": 140}
# Pauses inside a line, for the short-form pace: `‖` a breath (barely any), `⏸` the comic beat before a punchline.
# Kokoro pads each spoken part by about 0.13 s on its own, so these are what is added on top.
BEAT = "⏸"
PART_PAUSE, BEAT_PAUSE = 0.06, 0.28
BEAT_GAP, LAST_GAP = 0.08, 0.32        # silence after a beat, and after the last one (the cut lands on the joke)
SERIES_MIN_S = 10              # QC's floor for any series video; a format's own floor is its length minus 2 s
WORD_S = 0.38                  # seconds per spoken word at speed 1.22, comic-beat parts included (Week 7 renders: 0.34-0.41)
# Endings this series never uses: fantasy strategy questions.
FANTASY_ENDING = ("start", "bench", "sit ", "lineup", "waiver", "roster", "trade", "fantasy", "add him", "drop him")

# speed: Kokoro rate (the channel default is 1.12); length: the format's target seconds; pacing: the audit's
# format-aware limits (first spoken hook, first payoff, longest silence), see studio/pacing.py.
FORMATS = {
    "roast_monologue": {"kinds": ("flame",), "style": "deadpan", "mood": None, "length": (11, 16), "speed": 1.16,
                        "opening": "joke_first", "joke": "deadpan_escalation", "ending": "abrupt_punchline",
                        "humor": "deadpan", "exaggeration": "high",
                        "pacing": {"hook_s": 3.0, "payoff_s": 4.0, "gap_s": 0.6}},
    "courtroom": {"kinds": ("flame",), "style": "court", "mood": "court", "length": (17, 23), "speed": 1.24,
                  "parody": True, "opening": "case_called", "joke": "evidence_to_absurd_verdict", "ending": "verdict",
                  "humor": "mock_serious", "exaggeration": "medium",
                  "pacing": {"hook_s": 2.0, "payoff_s": 4.0, "gap_s": 0.6}},
    "comparison": {"kinds": ("flame", "praise"), "style": "versus", "mood": "drive", "length": (14, 19),
                   "speed": 1.28, "opening": "tease_first", "joke": "absurd_comparison", "ending": "callback",
                   "humor": "observational", "exaggeration": "low",
                   "pacing": {"hook_s": 3.0, "payoff_s": 4.0, "gap_s": 0.6}},
    "main_character": {"kinds": ("praise",), "style": "cinematic", "mood": "hype", "length": (13, 19),
                       "speed": 1.22, "opening": "stat_first", "joke": "mythmaking", "ending": "poster",
                       "humor": "dramatic", "exaggeration": "high",
                       "pacing": {"hook_s": 1.5, "payoff_s": 4.0, "gap_s": 0.6}},
    "fan_meltdown": {"kinds": ("praise",), "style": "meme", "mood": "frantic", "length": (10, 17), "speed": 1.28,
                     "opening": "fake_calm", "joke": "escalation", "ending": "scream_cut",
                     "humor": "absurd", "exaggeration": "very_high",
                     "pacing": {"hook_s": 2.0, "payoff_s": 3.5, "gap_s": 0.5}},
    # Its own show (stages/parody.py), never chosen for a flame or a praise here: `kinds` is empty on purpose, because
    # it uses generated pictures. It sits here so the series gates, length and pacing read it like any other format.
    "ai_parody": {"kinds": (), "style": "ai_parody", "mood": "court", "length": (13, 21), "speed": 1.22,
                  "parody": True, "show": True,
                  "note": "AI parody: the video is AI-generated and fictional. The stats are real.",
                  "opening": "joke_first", "joke": "literal_visual_gag", "ending": "literal_visual_gag",
                  "humor": "deadpan", "exaggeration": "high",
                  "pacing": {"hook_s": 3.0, "payoff_s": 4.0, "gap_s": 0.6}},
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
        season_att = sum(r["attempts"] for r in rows)
        if adot <= SHORT_RATIO * league and season_att >= FL.CONSTANTS["season_min_attempts"]:   # the angle ranks his season
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
            a["air_yards_completed"] = add(f"passing yards in the air (yards minus yards after catch), week {week}",
                                           "int", {"kind": "fp_completed_air", "gsis_id": gid, "week": week}, player=gid)
            a["air_yards_completed_value"] = book.claims[-1]["value"]
            # "airless": most of his passing yards were run after the catch (only said when at most 30% flew)
            a["airless"] = a["air_yards_completed_value"] <= AIRLESS_SHARE * box["passing_yards_value"]
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
        a["strength"] = 5 if a["beats_opponent"] else 4 if a["teams_fewer_value"] >= MIN_TEAMS_FEWER else 3
        out.append(a)
    if kind == "praise":
        yards = (box.get("passing_yards_value") or 0) if pos == "QB" else box.get("receiving_yards_value", 0) + (
            box.get("rushing_yards_value", 0) if pos == "RB" else 0)
        tds = box.get("passing_tds_value", 0) + box.get("receiving_tds_value", 0) + box.get("rushing_tds_value", 0)
        if yards >= MONSTER_YARDS[pos] or tds >= 3:      # the box score itself: a rank-1 share is not a monster line
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


def choose(p: dict, recent: list[dict], treatment: str | None = None, fits=None) -> dict:
    """The creative brief: angle, format, look, music, length, opening, joke mechanism, ending, and why.
    `fits(format, angle_id) -> (ok, why)` drops options whose script cannot reach the format's minimum length
    (length_check); a forced treatment that cannot tell the story says why in `treatment_note`."""
    kind = p["role"]
    options, rejected = [], {}
    for a in p["angles"]:
        for fmt, spec in FORMATS.items():
            fit = FIT.get((fmt, a["id"]), 0)
            if kind not in spec["kinds"]:
                continue
            if not fit or not eligible(fmt, a, p):
                rejected.setdefault(fmt, []).append(f"{a['id']}: " + ("not a story it tells" if not fit
                                                                      else "missing the facts its joke needs"))
                continue
            if fits:
                ok, why = fits(fmt, a["id"])
                if not ok:
                    rejected.setdefault(fmt, []).append(f"{a['id']}: {why}")
                    continue
            pen, why = repetition(fmt, recent)
            options.append({"format": fmt, "angle": a, "fit": fit, "penalty": pen, "why_penalty": why,
                            "score": round(a["strength"] + fit - pen, 2)})
    note = None
    if treatment and options:
        forced = [o for o in options if o["format"] == treatment]
        if not forced:
            why = "; ".join(rejected.get(treatment) or [f"not a {kind} format"])
            note = f"treatment {treatment} cannot tell this story ({why}); chose among the other formats"
        options = forced or options
    if not options:
        a = next(a for a in p["angles"] if a["id"] == "stat_outlier")
        ok, why = entertainment_test(p, a)
        short = sorted({r for rs in rejected.values() for r in rs if "minimum" in r})
        why = "; ".join(short) if short else why          # a story existed, its scripts ran short
        return {"series": SERIES, "kind": kind, "player": p["name"], "angle": a["id"], "format": None,
                "entertainment_test": {"pass": False, "why": "no format can tell this story: " + why},
                "options": [], "rejected": rejected}
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
                        for o in options], "rejected": rejected, **({"treatment_note": note} if note else {})}


def min_seconds(fmt: str) -> float:
    """The shortest video QC passes for a format (qc.run_gates: its length minus 2 s, never under SERIES_MIN_S)."""
    return max(SERIES_MIN_S, FORMATS[fmt]["length"][0] - 2)


def estimate_seconds(beats: list[dict], claims: dict, fmt: str) -> float:
    """The narration's length before any audio: spoken words at the format's speed, the comic beats, the gaps."""
    from .script import fill
    from .story import PAUSE
    text = [fill(b["text_template"].replace(PAUSE, " ").replace(BEAT, " "), claims, "spoken") for b in beats]
    words = sum(len(t.split()) for t in text)
    return (words * WORD_S * 1.22 / FORMATS[fmt]["speed"] + BEAT_PAUSE * sum(b["text_template"].count(BEAT)
                                                                             for b in beats)
            + sum(b.get("pause") or 0 for b in beats))


def length_check(facts: dict, claims: dict):
    """choose()'s `fits`: compose the option's script and keep it only if it can reach the format's floor. A format
    whose facts run out before its minimum length is not padded; it is not chosen."""
    def fits(fmt: str, angle: str) -> tuple[bool, str]:
        beats, _ = compose(facts, claims, {"format": fmt, "angle": angle})
        est, floor = estimate_seconds(beats, claims, fmt), min_seconds(fmt)
        return est >= floor, f"script ~{est:.1f}s, under {fmt}'s {floor:.0f}s minimum"
    return fits


def eligible(fmt: str, a: dict, p: dict) -> bool:
    """A format needs the facts its joke stands on."""
    if fmt == "comparison" and a["id"] == "target_hog":
        return a["beats_opponent"] or a["teams_fewer_value"] >= MIN_TEAMS_FEWER
    if fmt == "courtroom":
        return p["position"] == "QB"
    if fmt in ("main_character", "fan_meltdown"):
        return len([k for k in p["box"] if not k.endswith("_value")]) >= 3
    return True


def premise(p: dict, a: dict, val=None) -> str:
    """The story in one line (brief.md, the upload description). A comparison is only stated when the claims hold
    it: `val` reads a claim's value (the description and QC pass theirs); without it, the values cached on the angle."""
    if a["id"] == "target_hog":
        get = (lambda cid, cached: val(cid)) if val else (lambda cid, cached: cached)
        tgt = get(p["box"]["targets"], p["box"]["targets_value"])
        who = f"one {POS_WORD[p['position']]}"
        if get(a["opp_targets"], a["opp_targets_value"]) < tgt:
            return f"{who} drew more targets than the entire opposing offense"
        fewer = get(a["teams_fewer"], a["teams_fewer_value"])
        if fewer >= 1:
            return f"{who} drew more targets than {'an entire NFL offense' if fewer == 1 else f'{fewer} entire NFL offenses'}"
        if get(a["rank"], a["rank_value"]) == 1:
            return f"{tgt} targets, the most in any {POS_WORD[p['position']]} game this season"
        return f"{tgt} targets in one game"
    return {"short_throws": "his passes barely left the line of scrimmage",
            "rough_day": "a passing afternoon that came apart",
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
           "team": claims[p["team_name"]]["display"], "opp": ph(p["opponent"]), "ph": ph, "beat": beat, "fmt": fmt}
    beats, titles = BUILDERS[fmt](ctx)
    for b in beats:
        b["scene"].update(fp=p["role"], style=FORMATS[fmt]["style"], series=True)
        b["scene"].setdefault("sfx", False)
    return beats, titles


def _about_half(a: dict) -> bool:
    """'About half' is only said when the share shown on screen is 45-55%."""
    return 0.45 <= a["yac_share_value"] <= 0.55


def _won(ctx) -> bool:
    return ctx["p"]["game"]["result_value"] == "won"


def _cue(event: str, word: str | None = None, claim: str | None = None, at: str = "start", prefer=(), **kw) -> dict:
    """A comedic or dramatic beat the sound should land on: a spoken word (its start or end) or a number as it is
    said. The effect itself is chosen later (sfx_palette.select), from the format's palette and recent history."""
    return {"event": event, "word": word, "claim": claim, "at": at, "prefer": list(prefer), **kw}


def _beat(ctx, section, text, scene, cues=(), emphasis=(), pause=BEAT_GAP):
    fmt = ctx["fmt"]
    b = ctx["beat"](section, text, {**scene, "cues": list(cues)}, list(emphasis), FORMATS[fmt]["speed"], pause=pause)
    return b


def build_roast_monologue(ctx):
    """One observation, deadpan, escalated once, then cut on the punchline. No music: the silences are short and
    placed (the beat before the reveal, the beat before the punchline), never just dead air."""
    p, a, ph, name = ctx["p"], ctx["a"], ctx["ph"], ctx["name"]
    box, g = p["box"], p["game"]
    still = {"camera": "still", "transition": "cut"}           # deadpan: hard cuts, nothing slides
    if a["id"] == "short_throws":
        beats = [
            _beat(ctx, "open", f"{name} played the {ctx['opp']} on airplane mode.",
                  {**still, "type": "dp_toggle", "label": "AIRPLANE MODE", "claims": [p["opponent"]]},
                  [_cue("ui", word="airplane", prefer=["toggle_click"])], ["airplane"]),
            _beat(ctx, "stat", f"Average throw:‖ {ph(a['x'])} yards.{BEAT} League average:‖ {ph(a['league'])}.",
                  {**still, "type": "dp_stat", "big": a["x"], "unit": "YARDS PER THROW", "small": a["league"],
                   "small_label": "LEAGUE AVERAGE", "claims": [a["x"], a["league"]]},
                  [_cue("awkward_pause", claim=a["league"], at="end", context="dead_silence")]),
        ]
        if a["airless"]:
            beats.append(_beat(ctx, "escalate", f"{ph(a['air_yards_completed'])} of his {ph(box['passing_yards'])} "
                                                "passing yards happened in the air.‖ The receivers did the rest.",
                               {**still, "type": "dp_line", "lines": ["{air} OF {yds} YARDS", "IN THE AIR."],
                                "fill": {"air": a["air_yards_completed"], "yds": box["passing_yards"]},
                                "claims": [a["air_yards_completed"], box["passing_yards"]]}))
        elif a["season_rank_value"] == 1:
            beats.append(_beat(ctx, "escalate", "Shortest in the league this season,‖ by the way.",
                               {**still, "type": "dp_line", "lines": ["SHORTEST AVERAGE THROW", "IN THE NFL THIS SEASON"],
                                "foot": "MIN. {min} ATTEMPTS · {season} YARDS",
                                "fill": {"min": a["min_attempts"], "season": a["season"]},
                                "claims": [a["min_attempts"], a["season"], a["season_rank"], a["season_count"]]}))
        beats.append(_beat(ctx, "punchline", f"The {ctx['team']} {ph(g['result'])} anyway.{BEAT} The deep safety "
                                             "wants his afternoon back.",
                           {**still, "type": "dp_end", "lines": ["THE DEEP SAFETY", "WANTS HIS", "AFTERNOON BACK."],
                            "claims": [g["result"], g["points"], g["opp_points"]]},
                           [_cue("final_payoff", word="back", at="end", context="deadpan_hit")], pause=LAST_GAP))
        titles = [f"{p['name']} Played the {ph(p['opponent'])} on Airplane Mode",
                  f"What Was {p['name']} Doing Against the {ph(p['opponent'])}?"]
        return beats, titles
    s = "s" if box["passing_interceptions_value"] != 1 else ""
    sack, sacks_label = _sacks(box)
    beats = [
        _beat(ctx, "open", f"{name} against the {ctx['opp']}?{BEAT} Group project.‖ Nobody started it.",
              {**still, "type": "dp_line", "lines": ["GROUP PROJECT.", "NOBODY STARTED IT."], "claims": [p["opponent"]]},
              [_cue("punchline", word="started", at="end", context="setup_aside")]),
        _beat(ctx, "stat", f"{ph(box['sacks_suffered'])} {sack}.‖ {ph(box['passing_interceptions'])} interception{s}."
                           f"‖ {ph(g['points'])} points.",
              {**still, "type": "dp_stat", "big": g["points"], "unit": "POINTS", "small": box["sacks_suffered"],
               "small_label": sacks_label, "claims": [box["sacks_suffered"], box["passing_interceptions"], g["points"]]}),
        _beat(ctx, "punchline", f"{'At home.' if g['home'] else 'On the road.'}{BEAT} The film room needs snacks.",
              {**still, "type": "dp_end", "lines": ["THE FILM ROOM", "NEEDS SNACKS."], "claims": [g["venue"]]},
              [_cue("final_payoff", word="snacks", at="end", context="deadpan_hit")], pause=LAST_GAP),
    ]
    titles = [f"This {p['name']} Performance Needs an Explanation",
              f"{p['name']} vs the {ph(p['opponent'])} Was a Group Project"]
    return beats, titles


def build_courtroom(ctx):
    """Rapid-fire: case called on a gavel, three exhibits stamped as each number is said, a verdict on a gavel."""
    p, a, ph, name = ctx["p"], ctx["a"], ctx["ph"], ctx["name"]
    box, g, lead = p["box"], p["game"], p["lead"]
    court = {"camera": "still", "transition": "cut"}
    exhibits = []
    # what the conspiracy board pins up beside the number: lines of (prefix, claim or None, suffix)
    notes = {}
    if a["id"] == "short_throws":
        notes[a["season"]] = {"note": [["", a["season_rank"], " SHORTEST"], ["OF ", a["season_count"], " QBS"]]}
        notes[a["air_yards_completed"]] = {"note": [["OUT OF ", box["passing_yards"], ""], ["", None, "PASSING YARDS"]]}
        exhibits.append((f"his average throw:‖ {ph(a['x'])} yards.", a["x"], "YARDS PER THROW", [a["x"], a["league"]],
                         a["x"]))
        if a["season_rank_value"] == 1:
            exhibits.append((f"the shortest average throw in the league,‖ minimum {ph(a['min_attempts'])} attempts.",
                             a["season"], "SEASON AIR YARDS / ATT", [a["season"], a["season_rank"], a["season_count"],
                                                                     a["min_attempts"]], a["min_attempts"]))
        if a["airless"]:
            exhibits.insert(1, (f"{ph(a['air_yards_completed'])} of his {ph(box['passing_yards'])} yards came in the air.",
                                a["air_yards_completed"], "PASSING YARDS IN THE AIR",
                                [a["air_yards_completed"], box["passing_yards"]], a["air_yards_completed"]))
        if _about_half(a):
            exhibits.append(("about half his yards came after the catch.", a["yac_share"],
                             "OF HIS YARDS: AFTER THE CATCH", [a["yac_share"]], None))
        verdict, charge = "loitering near the line of scrimmage", "LOITERING NEAR THE LINE OF SCRIMMAGE"
    if a["id"] != "short_throws":
        worst = {"lowest": "worst", "fewest": "fewest"}.get(lead["sup"], lead["sup"])
        notes[lead["value"]] = {"rank": lead["rank"], "rank_value": lead["rank_value"], "games": lead["games"],
                                "worst": worst.upper()}
        how = "by expected points" if lead["metric"] == "epa" else f"by {lead['label']}"
        sack, sacks_label = _sacks(box)
        exhibits.append((f"{ph(box['sacks_suffered'])} {sack}.", box["sacks_suffered"], sacks_label,
                         [box["sacks_suffered"]], box["sacks_suffered"]))
        exhibits.append((f"{how},‖ the {ph(lead['rank'])}-{worst} of {ph(lead['games'])} quarterback games this season."
                         if lead["rank_value"] > 1 else
                         f"{how},‖ the {worst} of {ph(lead['games'])} quarterback games this season.",
                         lead["value"], lead["screen"], [lead["value"], lead["rank"], lead["games"]],
                         lead["rank"] if lead["rank_value"] > 1 else lead["games"]))
        exhibits.append((f"{ph(g['points'])} points.{BEAT} " + ("At home." if g["home"] else "Total."), g["points"],
                         "POINTS SCORED", [g["points"], g["venue"]], g["points"]))
        verdict, charge = "loitering in his own backfield", "LOITERING IN HIS OWN BACKFIELD"
    beats = [_beat(ctx, "case", f"Order!‖ The NFL versus {name}.",
                   {**court, "type": "cx_order", "transition": "none",
                    "claims": [p["week_claim"], p["opponent"]]},
                   [_cue("joke_setup", word="order", prefer=["gavel_single"])], ["order"])]
    for i, (text, big, label, cl, stamp_on) in enumerate(exhibits[:3]):
        letter = "ABC"[i]
        cues = [_cue("roast_impact", claim=stamp_on, at="end", context="stat_ruins_it")] if i == 1 and stamp_on \
            else []
        last = i == len(exhibits[:3]) - 1
        if last and "At home." in text:
            cues.append(_cue("roast_impact", word="home", at="end", context="fans_hurt"))
        beats.append(_beat(ctx, f"exhibit_{letter.lower()}", f"Exhibit {letter}:‖ {text}",
                           {**court, "type": "cx_exhibit", "letter": letter, "big": big, "label": label,
                            "claims": cl, "index": i, "stamp_on": stamp_on, **notes.get(big, {})}, cues))
    beats.append(_beat(ctx, "verdict", f"Verdict:{BEAT} guilty of {verdict}.",
                       {**court, "type": "cx_verdict", "stamp": "GUILTY", "charge": charge, "transition": "zoom",
                        "music_drop": True, "claims": []},
                       [_cue("verdict", word="guilty", prefer=["gavel"]), _cue("hard_stop", word="guilty")],
                       ["guilty"], pause=LAST_GAP))
    titles = [f"The NFL v. {p['name']}, {ph(p['week_claim'])}", f"{p['name']} Has Been Charged (Parody)"]
    return beats, titles


def build_comparison(ctx):
    """The tease, the picture (his number beside a verified benchmark), the escalation, the callback."""
    p, a, ph, name = ctx["p"], ctx["a"], ctx["ph"], ctx["name"]
    box, g = p["box"], p["game"]
    if a["id"] == "short_throws":
        field = {"camera": "still", "x": a["x"], "league": a["league"], "season": a["season"],
                 "min": a["min_attempts"], "transition": "none"}
        beats = [
            _beat(ctx, "tease", f"Measuring {name}'s passes against the {ctx['opp']}?{BEAT} Grab a smaller ruler.",
                  {**field, "type": "vx_hook", "stage": "tease", "claims": [p["opponent"]]},
                  [_cue("punchline", word="ruler", at="end", context="setup_aside")], ["smaller"]),
            _beat(ctx, "reveal", f"Average NFL throw:‖ {ph(a['league'])} yards.‖ His?{BEAT} {ph(a['x'])}.",
                  {**field, "type": "vx_lanes", "stage": "him", "music_drop": True, "transition": "cut",
                   "claims": [a["league"], a["x"], p["opponent"]]},
                  [] if a["airless"] else [_cue("stat_reveal", claim=a["x"], at="end", context="stat_ruins_it")]),
        ]
        if a["airless"]:
            beats.append(_beat(ctx, "air", f"Of his {ph(box['passing_yards'])} passing yards?{BEAT} "
                                           f"{ph(a['air_yards_completed'])} came in the air.",
                               {**field, "type": "vx_air", "stage": "air", "transition": "cut", "air": a["air_yards_completed"], "yds": box["passing_yards"],
                                "claims": [a["league"], a["x"], a["air_yards_completed"], box["passing_yards"]]},
                               [_cue("roast_impact", word="air", at="end", context="stat_ruins_it")], ["air"]))
        elif a["season_rank_value"] == 1:
            beats.append(_beat(ctx, "season", f"All season,‖ nobody with {ph(a['min_attempts'])}-plus attempts throws "
                                              "it shorter.",
                               {**field, "type": "vx_season", "stage": "season", "transition": "cut", "claims": [a["league"], a["x"], a["season"],
                                                                       a["min_attempts"], a["season_rank"],
                                                                       a["season_count"]]},
                               [], ["nobody"]))
        hi_lo = (g["points"], g["opp_points"]) if _won(ctx) else (g["opp_points"], g["points"])
        beats.append(_beat(ctx, "callback", f"The {ctx['team']} {ph(g['result'])} anyway,‖ {ph(hi_lo[0])} to "
                                            f"{ph(hi_lo[1])}.{BEAT} Tell the deep safety he can go home.",
                           {**field, "type": "vx_scoreboard", "stage": "safety", "transition": "cut",
                            "claims": [a["league"], a["x"], g["points"], g["opp_points"], g["result"], p["team_name"],
                                       p["opponent"]]},
                           [_cue("final_payoff", word="home", at="end", context="throw_out"),
                            _cue("hard_stop", word="home", at="end")], ["home"], pause=LAST_GAP))
        titles = [f"{p['name']}'s Passes Didn't Go Anywhere", f"The Shortest Throws in the NFL Belong to {p['name']}"]
        return beats, titles
    opp = ctx["opp"]
    tally = {"type": "cmp_tally", "camera": "still", "him": box["targets"], "them": a["opp_targets"],
             "transition": "none"}
    beats = [_beat(ctx, "setup", f"{name}:‖ {ph(box['targets'])} targets against the {opp}.",
                   {**tally, "stage": "him", "claims": [box["targets"], p["opponent"]]},
                   [])]
    if a["beats_opponent"]:
        beats.append(_beat(ctx, "versus", f"The entire {opp} offense?{BEAT} {ph(a['opp_targets'])}.",
                           {**tally, "stage": "them", "music_drop": True,
                            "claims": [box["targets"], a["opp_targets"], p["opponent"]]},
                           [_cue("punchline", claim=a["opp_targets"], at="end", context="absurd_comparison")],
                           ["entire"]))
        punch = "One receiver out-targeted a whole NFL offense."
    else:
        offenses = "offense" if a["teams_fewer_value"] == 1 else "offenses"
        beats.append(_beat(ctx, "versus", f"{ph(a['teams_fewer'])} entire NFL {offenses} got fewer that week.",
                           {**tally, "stage": "teams", "claims": [box["targets"], a["teams_fewer"]]},
                           [_cue("punchline", claim=a["teams_fewer"], at="end", context="absurd_comparison")],
                           ["entire"]))
        punch = "Whole offenses, fewer targets than one receiver."
    hi_lo = (g["points"], g["opp_points"]) if _won(ctx) else (g["opp_points"], g["points"])
    beats.append(_beat(ctx, "callback", f"{punch}{BEAT} The {ctx['team']} {ph(g['result'])}, {ph(hi_lo[0])} to "
                                        f"{ph(hi_lo[1])}.‖ That stat line still won the group chat.",
                       {**tally, "stage": "end", "claims": [box["targets"], a["opp_targets"], g["result"], g["points"],
                                                            g["opp_points"]]},
                       [_cue("final_payoff", word="chat", at="end", context="tiny_celebration"),
                        _cue("hard_stop", word="chat", at="end")], ["group"],
                       pause=LAST_GAP))
    titles = [f"{p['name']} Out-Targeted the Entire {ph(p['opponent'])} Offense",
              f"{ph(box['targets'])} Targets: {p['name']} Is Actually Ridiculous"]
    return beats, titles


def _sacks(box: dict) -> tuple[str, str]:
    """The spoken word and the on-screen label for his sack count: "1 sack", "4 sacks"."""
    return ("sack", "SACK") if box["sacks_suffered_value"] == 1 else ("sacks", "SACKS")


RB_CATCHES = 3                 # a running back's receiving line joins the slam from this many catches


def _slams(p: dict) -> list[tuple[str, str]]:
    """(claim, word) for the box-score slam, in the order a broadcast would say them. A running back's line is his
    rushing (his targets lead only on a target_hog day); zero touchdowns are never slammed; one is singular."""
    b = p["box"]
    if p["position"] == "QB":
        order = [("completions", "completions"), ("passing_yards", "yards"), ("passing_tds", "touchdowns")]
    elif p["position"] == "RB":
        order = [("carries", "carries"), ("rushing_yards", "yards"), ("rushing_tds", "touchdowns")]
        if b.get("receptions_value", 0) >= RB_CATCHES:
            order += [("receptions", "catches"), ("receiving_yards", "receiving yards"),
                      ("receiving_tds", "receiving touchdowns")]
        if b.get("targets_value", 0) >= HOG_TARGETS:
            order.insert(0, ("targets", "targets"))
    else:
        order = [("targets", "targets"), ("receptions", "catches"), ("receiving_yards", "yards"),
                 ("receiving_tds", "touchdowns")]
    out = []
    for k, w in order:
        if k not in b or (k.endswith("_tds") and not b[k + "_value"]):
            continue
        if b[k + "_value"] == 1:
            w = {"carries": "carry", "catches": "catch"}.get(w, w.removesuffix("s"))
        out.append((b[k], w))
    return out


def _lead_rank(ctx) -> dict | None:
    """The selection stat's season rank, said when it is top three: the claim-backed line a monster box score
    stands on beside its numbers. None when it is not top three or its label would put a number in the script."""
    p, ph = ctx["p"], ctx["ph"]
    lead = p["lead"]
    if lead["rank_value"] > 3 or lead["sup"] not in ("most", "highest") or any(ch.isdigit() for ch in lead["label"]):
        return None
    pos, tied = POS_WORD[p["position"]], bool(lead.get("tied"))
    if lead["rank_value"] == 1:
        said = f"{'Tied for the' if tied else 'The'} {lead['sup']} {lead['label']} in any {pos} game this season"
    else:
        said = f"The {ph(lead['rank'])}-{lead['sup']} {lead['label']} of {ph(lead['games'])} {pos} games this season"
    return {"said": said, "rank1": lead["rank_value"] == 1, "tied": tied, "sup": lead["sup"].upper(),
            "claims": [lead["value"], lead["rank"], lead["games"]]}


def build_main_character(ctx):
    """A hype edit: the absurd number on frame 0, the comparison that makes it absurd, the rest of the line on the
    beat, the season context, a poster. No title card before the entertainment."""
    p, a, ph, name, who = ctx["p"], ctx["a"], ctx["ph"], ctx["name"], ctx["who"]
    g = p["game"]
    slams = _slams(p)
    lead_c, lead_w = slams[0]
    beats = [_beat(ctx, "open", f"{ph(lead_c)} {lead_w}.‖ {name} against the {ctx['opp']}.",
                   {"type": "hx_open", "camera": "push", "transition": "none", "slams": [lead_c],
                    "words": [lead_w.upper()], "open": True, "claims": [lead_c, p["opponent"], p["week_claim"]]},
                   [_cue("hype_peak", claim=lead_c, at="end", context="arrival")])]
    if a["id"] == "target_hog" and a["beats_opponent"]:
        beats.append(_beat(ctx, "contrast", f"The entire {ctx['opp']} offense?{BEAT} {ph(a['opp_targets'])}.",
                           {"type": "hx_vs", "camera": "push", "transition": "cut", "them": a["opp_targets"],
                            "him": p["box"]["targets"], "games": a["games"], "rank": a["rank"],
                            "rank1": a["rank_value"] == 1, "stage": "them", "music_drop": True,
                            "claims": [a["opp_targets"], p["opponent"], p["box"]["targets"]]},
                           [_cue("punchline", claim=a["opp_targets"], at="end", context="absurd_comparison")],
                           ["entire"]))
    rest = slams[1:]
    if rest:
        beats.append(_beat(ctx, "slams", ",‖ ".join(f"{ph(c)} {w}" for c, w in rest) + ".",
                           {"type": "hx_slams", "camera": "push", "transition": "cut", "slams": [c for c, _ in rest],
                            "words": [w.upper() for _, w in rest], "claims": [c for c, _ in rest]}))
    if a["id"] == "target_hog":
        line = (f"Out of {ph(a['games'])} {POS_WORD[p['position']]} games this season,‖ nobody's seen more."
                if a["rank_value"] == 1 else
                f"That's the {ph(a['rank'])}-most of {ph(a['games'])} {POS_WORD[p['position']]} games this season.")
        beats.append(_beat(ctx, "rank", line, {"type": "hx_rank", "camera": "push", "transition": "cut",
                                               "games": a["games"], "rank": a["rank"], "rank1": a["rank_value"] == 1,
                                               "stage": "rank", "claims": [a["rank"], a["games"]]},
                           [_cue("hype_peak", word="more", at="end", context="glaze_peak")] if a["rank_value"] == 1 else [],
                           ["nobody"]))
    elif a["id"] == "monster_line" and (lr := _lead_rank(ctx)):
        lead = p["lead"]
        beats.append(_beat(ctx, "rank", lr["said"] + ".",
                           {"type": "hx_rank", "camera": "push", "transition": "cut", "games": lead["games"],
                            "rank": lead["rank"], "rank1": lr["rank1"], "sup": lr["sup"], "tied": lr["tied"],
                            "stage": "rank", "claims": lr["claims"]}))
    pts = lambda cid: ctx["claims"][cid]["value"]
    shootout = " a shootout" if min(pts(g["points"]), pts(g["opp_points"])) >= SHOOTOUT_POINTS else ""
    if g["result_value"] == "lost":
        end, tag = f"The {ctx['team']} {ph(g['result'])}{shootout}.{BEAT} {who} did not.", \
            f"THE {ctx['team'].upper()} LOST. HE DIDN'T."
    else:
        end, tag = f"The {ctx['team']} {ph(g['result'])}{shootout}.{BEAT} {who} was the whole movie.", \
            "STARRING. DIRECTED BY. PRODUCED BY."
    abbr = {"targets": "TGT", "catches": "REC", "catch": "REC", "yards": "YDS", "touchdown": "TD",
            "touchdowns": "TD", "completions": "CMP", "carries": "CAR", "carry": "CAR", "receiving yards": "REC YDS",
            "receiving touchdown": "REC TD", "receiving touchdowns": "REC TD"}
    beats.append(_beat(ctx, "poster", end, {"type": "hx_poster", "camera": "push", "transition": "zoom",
                                            "tagline": tag, "credits": [[c, abbr.get(w, w.upper())] for c, w in slams],
                                            "claims": [g["points"], g["opp_points"], g["result"], p["team_name"],
                                                       p["opponent"]]
                                            + [c for c, _ in slams]},
                       [_cue("hard_stop", word=("not" if g["result_value"] == "lost" else "movie"), at="end")],
                       ["did", "whole"], pause=LAST_GAP))
    titles = [f"{p['name']} Was the Main Character Against the {ph(p['opponent'])}",
              f"Nobody Was Stopping {p['name']}"]
    return beats, titles


def build_fan_meltdown(ctx):
    """Fake calm, each stat louder than the last, a scream, a hard cut."""
    p, a, ph, name = ctx["p"], ctx["a"], ctx["ph"], ctx["name"]
    slams = _slams(p)
    beats = [_beat(ctx, "calm", f"Okay.‖ Stay calm.‖ It's just {name}.",
                   {"type": "melt_calm", "camera": "still", "transition": "none", "claims": []}, [], ["calm"])]
    for i, (c, w) in enumerate(slams[:3]):
        cues = [_cue("stat_reveal", claim=c, at="end", context="absurd_comparison")]
        beats.append(_beat(ctx, f"stat_{i + 1}", f"{ph(c)} {w}!", {"type": "melt_stat", "camera": "still", "level": i + 1,
                                                                  "big": c, "word": w.upper(), "transition": "cut",
                                                                  "claims": [c]}, cues if i == 0 else []))
    if a["id"] == "target_hog" and a["beats_opponent"]:
        beats.append(_beat(ctx, "detail", f"The whole {ctx['opp']} team got {ph(a['opp_targets'])}!",
                           {"type": "melt_stat", "camera": "still", "level": 4, "big": a["opp_targets"],
                            "word": "THE WHOLE OTHER TEAM", "transition": "cut",
                            "claims": [a["opp_targets"], p["opponent"]]},
                           [_cue("hype_peak", claim=a["opp_targets"], at="end", context="meltdown")]))
    elif a["id"] == "monster_line" and (lr := _lead_rank(ctx)) and lr["rank1"]:
        lead = p["lead"]
        beats.append(_beat(ctx, "detail", lr["said"] + "!",
                           {"type": "melt_stat", "camera": "still", "level": 4, "big": lead["value"],
                            "word": lead["screen"], "transition": "cut", "claims": lr["claims"]}))
    beats.append(_beat(ctx, "scream", "We are not calm.‖ We will never be calm again.",
                       {"type": "melt_scream", "camera": "still", "transition": "cut", "claims": []},
                       [_cue("hard_stop", word="again", at="end")], ["never"],
                       pause=LAST_GAP))
    titles = [f"Stay Calm About {p['name']}? Impossible", f"{p['name']} Is Actually Ridiculous"]
    return beats, titles


BUILDERS = {"roast_monologue": build_roast_monologue, "courtroom": build_courtroom, "comparison": build_comparison,
            "main_character": build_main_character, "fan_meltdown": build_fan_meltdown}


def assign_sfx(beats: list[dict], brief: dict, recent: list[dict], seed: str) -> None:
    """Choose the effect for every cue (sfx_palette.select): the beat's purpose, the format's sound personality,
    this video's earlier choices and the last two series videos' sounds."""
    from .. import sfx_palette as P
    cues = [c for b in beats for c in b["scene"].get("cues", [])]
    chosen = iter(P.select(cues, brief["format"], brief["kind"], [r.get("sfx", []) for r in recent], seed))
    for b in beats:
        b["scene"]["cues"] = [next(chosen) for _ in b["scene"].get("cues", [])]
    # the last beat leaves room for its payoff sound to finish (a scream, a crowd), then cuts
    tail = {"crowd": 1.1, "bed": 1.2, "sting": 0.9, "impact": 0.75, "cartoon": 0.45, "ui": 0.3, "cut": 0.5,
            "stop": 0.45, "reaction": 0.9}
    need = []
    for c in beats[-1]["scene"]["cues"]:
        if not c.get("sfx"):
            continue
        spec = P.PALETTE[c["sfx"]]
        if spec.get("registry") and P.available(c["sfx"]):   # a reaction plays out in full, capped
            need.append(min(1.8, len(P.render(c["sfx"])) / P.SR - spec["attack"] + 0.1))
        else:
            need.append(tail[spec["class"]])
    if need:
        beats[-1]["pause"] = max(beats[-1].get("pause") or 0, max(need))


def structure(brief: dict, beats: list[dict]) -> dict:
    """What history.jsonl records for the series, so the next brief can avoid repeating it."""
    return {"series": SERIES, "angle": brief["kind"], "format": brief["kind"], "format_family": brief["format"],
            "story_angle": brief["angle"], "hook": f"{brief['format']}:{brief['opening']}",
            "opening": brief["opening"], "joke": brief["joke"], "ending": brief["ending"],
            "visual_style": brief["visual_style"], "music_mood": brief["music_mood"] or "none",
            "music": bool(brief["music_mood"]), "duration_range": brief["length_range"],
            "beats": [b["section"] for b in beats], "scenes": [b["scene"]["type"] for b in beats],
            "transitions": [b["scene"].get("transition", "push") for b in beats[1:]],
            "rhythm": [b.get("pause") for b in beats], "primary_metric": brief["angle"], "teammate": None,
            "sfx": [c["sfx"] for b in beats for c in b["scene"].get("cues", []) if c.get("sfx")],
            "pacing": FORMATS[brief["format"]]["pacing"]}


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
    if brief.get("rejected"):
        lines += ["", "Formats not considered:", ""]
        lines += [f"- {fmt}: {'; '.join(why)}" for fmt, why in brief["rejected"].items()]
    if brief.get("treatment_note"):
        lines += ["", f"Note: {brief['treatment_note']}"]
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
    if fam == "ai_parody":
        return "AI PARODY", f"{d(p['box']['passing_yards'])} YARDS", p["name"].upper()
    if a["id"] == "short_throws":
        return ("AIRPLANE MODE" if fam == "roast_monologue" else "THE NFL v. " + last if fam == "courtroom"
                else "SHORTEST THROWS IN THE NFL"), f"{d(a['x'])} YDS", "NFL AVERAGE: " + d(a["league"])
    if a["id"] == "rough_day":
        return ("THE NFL v. " + last if fam == "courtroom" else "WHAT HAPPENED?"), \
            f"{d(p['game']['points'])} POINTS", f"{d(p['box']['sacks_suffered'])} {_sacks(p['box'])[1]}"
    if a["id"] == "target_hog":
        them = f"THE {d(p['opponent']).upper()}: {d(a['opp_targets'])}" if a["beats_opponent"] else "ONE RECEIVER"
        return ("MAIN CHARACTER" if fam == "main_character" else "WE ARE NOT CALM" if fam == "fan_meltdown"
                else "ONE MAN. ONE OFFENSE."), f"{d(p['box']['targets'])} TARGETS", them
    slams = [(c, w) for c, w in [(p["box"].get("receiving_yards"), "YARDS"), (p["box"].get("passing_yards"), "YARDS")]
             if c]
    big = f"{d(slams[0][0])} {slams[0][1]}" if slams else d(p["lead"]["value"])
    return "NOBODY WAS STOPPING HIM" if p["role"] == "praise" else "WHAT HAPPENED?", big, p["lead"]["screen"]


def description(p: dict, claims: dict, structure: dict) -> list[str]:
    """The upload description's story lines: every number and comparison read from the claims (QC re-derives
    them from the snapshot and checks the description still says the same)."""
    d = lambda cid: claims[cid]["display"]
    g = p["game"]
    a = next(a for a in p["angles"] if a["id"] == structure["story_angle"])
    lines = [f"{p['name']} vs the {d(p['opponent'])}, {d(p['week_claim'])}: "
             f"{premise(p, a, lambda cid: claims[cid]['value'])}. "
             f"Final: {d(g['points'])}-{d(g['opp_points'])} ({d(g['result'])}).",
             "Every stat is from nflverse game data; the jokes are jokes."]
    if FORMATS[structure["format_family"]].get("parody"):
        lines.append(FORMATS[structure["format_family"]].get("note",
                                                             "Parody: no real court, ruling or statement is depicted."))
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
