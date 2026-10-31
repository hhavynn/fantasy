"""The AI parody show: the week's worst stat lines, told with generated pictures of everything except the player.

Selection starts from stages/flame.py: the same historically bad games, measured the same way. The social trend engine
only nudges the order, and a person in real trouble is never picked. Each pick gets a joke from JOKES, least recently
used first, and the show's own pictures from ART.

No real person is ever depicted: no face, body, uniform, photo or team mark goes into any generated picture. A prompt
describes objects and an empty stadium, and check_prompt refuses anything else. The stat line is the character. The
narrator speaks as itself and never quotes anyone, and the claims chain from stages/flame.py checks every number.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core import Episode, read_json, run_stage, sha256_bytes, write_json
from ..numbers import TEAM_NAMES
from . import flame as FL
from . import showrunner as SR
from .facts import kickoff

VERSION = 1
SERIES = "ai_parody"
FORMAT = "ai_parody"
TREND_BOOST = 1.5              # a top-scoring trend is worth this much selection strength
# Never a joke, whatever the stat line says: a player's injury, or real trouble anywhere near him.
TROUBLE = re.compile(r"\b(arrest\w*|charged|assault\w*|domestic|death|died|dies|passed away|hospital\w*|crash\w*|"
                     r"shooting|funeral|suicide|overdose\w*|custody)\b", re.I)
NO_PEOPLE = "vertical photo, no people, no faces, no hands, no text, no letters, no numbers, no logos, no uniforms"
BALL = "brown leather American football"    # plain "football" comes back round and black-and-white, or as a rugby ball
# Words that would point a picture at a person, a team or a mark. check_prompt also refuses player and team names.
FORBIDDEN = re.compile(r"\b(nfl|logo|jersey|uniform|helmet|quarterback|receiver|player|coach|man|woman|person|people|"
                       r"fan|fans|crowd|face|portrait|celebrity|number \d+)\b", re.I)
MOVES = ("push", "drift_left", "rise", "pull")

ART = {
    "chair": "A cheap striped beach chair alone in the middle of an empty stadium field at dusk, unmarked grass, stadium lights on",
    "snail": "A small garden snail beside a {ball} on short grass, macro photo, stadium lights blurred behind",
    "parking": "A {ball} parked neatly inside an unmarked painted parking space on empty asphalt, a stadium in the distance",
    "tiny_ball": "A comically tiny toy {ball} beside a full-size {ball} on bright green grass, macro photo",
    "paper_plane": "A crumpled paper airplane nose-down in green grass under stadium lights, macro photo",
    "cone": "One orange traffic cone alone on a huge empty unmarked grass field at night, long shadows",
    "binoculars": "A pair of binoculars resting on a stadium railing, pointed at an empty grass field, dusk light",
    "lost_box": "A cardboard box holding one deflated {ball} on a bare concrete floor against a plain wall, one bright light",
    "flood": "An empty stadium with its field flooded by still water, one {ball} floating, overcast dusk",
    "smoke": "A {ball} smoking gently on a charcoal grill beside an empty stadium tunnel, dramatic light",
    "deflated": "A deflated {ball} lying in a muddy puddle on grass, rain falling, stadium lights behind",
    "dark_board": "A huge blank stadium screen switched off above empty seats at night",
    "seats": "Rows of empty stadium seats at night with one plain paper popcorn cup on a seat",
    "tumbleweed": "A tumbleweed rolling across an empty unmarked grass field at sunset, wide shot",
    "rowboat": "A small wooden rowboat resting on a flooded stadium field at dawn, calm water",
}

# Each joke: the opening line, the closing line, the word each lands on, its pictures (hook, stat, extra, punch), the
# title's middle. About the stat line and the football. Never the person.
JOKES = {
    "short_throws": [
        {"id": "beach_chair", "hook": "{name} against the {opp}?{B} The deep safety brought a beach chair.",
         "hook_word": "chair", "punch": "That chair had the best view in the building.", "punch_word": "building",
         "art": ("chair", "tiny_ball", "binoculars", "chair"), "title": "The Beach Chair Game"},
        {"id": "snail", "hook": "{name}'s passes against the {opp}?{B} A garden snail filed for more targets.",
         "hook_word": "targets", "punch": "The snail is asking for a raise.", "punch_word": "raise",
         "art": ("snail", "paper_plane", "cone", "snail"), "title": "Outrun by a Snail"},
        {"id": "parking", "hook": "{name} against the {opp}?{B} Every throw found short-term parking.",
         "hook_word": "parking", "punch": "Validation is at the line of scrimmage.", "punch_word": "scrimmage",
         "art": ("parking", "tiny_ball", "cone", "parking"), "title": "Short-Term Parking"},
    ],
    "rough_day": [
        {"id": "lost_and_found", "hook": "{name} against the {opp}?{B} The passing game is in the lost and found.",
         "hook_word": "found", "punch": "Nobody has claimed it yet.", "punch_word": "yet",
         "art": ("lost_box", "deflated", "seats", "lost_box"), "title": "Lost and Found"},
        {"id": "flood_warning", "hook": "{name} against the {opp}?{B} Flood warning for the pocket.",
         "hook_word": "pocket", "punch": "Bring a boat next week.", "punch_word": "boat",
         "art": ("flood", "deflated", "tumbleweed", "rowboat"), "title": "Flood Warning"},
        {"id": "well_done", "hook": "{name} against the {opp}?{B} That offense came off the grill well done.",
         "hook_word": "done", "punch": "Somebody open a window.", "punch_word": "window",
         "art": ("smoke", "dark_board", "seats", "smoke"), "title": "Well Done"},
    ],
}


# ------------------------------------------------------------------------------------------------ selection

def _trouble(t: dict, name: str, player_trend: bool) -> bool:
    text = " ".join([t.get("label") or "", *(t.get("keywords") or [])])
    return bool(TROUBLE.search(text)) or (player_trend and "injury" in (t.get("topics") or []))


def load_trends(path: Path | None) -> list[dict]:
    """Clusters from `trends export` (fantasy-intel/social). No file: selection stands on the stats alone."""
    if not path:
        return []
    return json.loads(Path(path).read_text()).get("clusters", [])


def rank(cands: list[dict], trends: list[dict]) -> list[dict]:
    """Strongest first, with a nudge for what people are talking about. Social numbers never reach the script."""
    out = []
    for c in cands:
        mine = [t for t in trends if c["gsis_id"] in (t["entities"].get("players") or [])]
        team = [t for t in trends if c["team"] in (t["entities"].get("teams") or [])]
        if any(_trouble(t, c["name"], True) for t in mine) or any(_trouble(t, c["name"], False) for t in team):
            continue
        heat = max(((t.get("score") or {}).get("total", 0) for t in mine), default=0)
        out.append({**c, "parody_score": round(c["strength"] + TREND_BOOST * heat / 100, 3)})
    return sorted(out, key=lambda c: (-c["parody_score"], c["name"]))


def limit(cands: list[dict], count: int) -> list[dict]:
    out, teams = [], set()
    for c in cands:
        if c["team"] not in teams and len(out) < count:
            out.append(c)
            teams.add(c["team"])
    return out


def pick(snap: dict, trends: list[dict], count: int, only: str | None = None) -> list[dict]:
    """The week's Shorts: quarterback flames with a joke to tell, one per team, a case that passes the show's test."""
    flames = [c for c in FL.candidates(snap) if c["kind"] == "flame" and c["position"] == "QB"]
    if only:
        flames = [c for c in flames if c["slug"] == f"flame-{only}" or c["slug"] == only]
    keep = []
    for c in rank(flames, trends):
        p = FL.build_facts(snap, {"gsis_id": c["gsis_id"], "name": c["name"], "role": c["kind"]})["players"][0]
        if brief(p, [])["entertainment_test"]["pass"]:
            keep.append(c)
    return limit(keep, count)


def post_by(snap: dict, team: str, week: int) -> str | None:
    """48 hours after kickoff, which is the safe side of 48 hours after the final whistle."""
    when = kickoff(snap, team, week)
    if not when:
        return None
    start = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/New_York"))
    return (start + timedelta(hours=48)).isoformat()


# -------------------------------------------------------------------------------------------------- prompts

def prompt(key: str) -> str:
    return f"{ART[key].format(ball=BALL)}. {NO_PEOPLE}"


def check_prompt(text: str, names: list[str]) -> list[str]:
    """Everything that could aim a picture at a real person or a team. Empty means clean."""
    out = sorted({m.group(0).lower() for m in FORBIDDEN.finditer(text.replace("no people", "").replace("no faces", ""))})
    words = {w.lower() for n in names for w in n.split() if len(w) > 2}
    out += sorted(words & {w.lower() for w in re.findall(r"[A-Za-z']+", text)})
    out += sorted(t for t in TEAM_NAMES.values() if re.search(rf"\b{t}\b", text, re.I))
    return out


# ------------------------------------------------------------------------------------------------- the brief

def brief(p: dict, recent: list[dict]) -> dict:
    """The strongest angle the show has jokes for, and the joke used least recently. No language model."""
    usable = sorted((a for a in p["angles"] if a["id"] in JOKES), key=lambda a: -a["strength"])
    if not usable:
        return {"series": SERIES, "kind": p["role"], "player": p["name"], "angle": None, "format": None,
                "entertainment_test": {"pass": False, "why": "no angle this show has a joke for"}}
    a = usable[0]
    used = [r.get("joke") for r in recent]
    joke = min(JOKES[a["id"]], key=lambda j: (len(used) - used[::-1].index(j["id"]) if j["id"] in used else -1))
    ok, why = SR.entertainment_test(p, a)
    spec = SR.FORMATS[FORMAT]
    return {"series": SERIES, "kind": p["role"], "player": p["name"], "angle": a["id"], "premise": SR.premise(p, a),
            "format": FORMAT, "visual_style": spec["style"], "music_mood": spec["mood"],
            "length_range": list(spec["length"]), "opening": spec["opening"], "joke": joke["id"],
            "ending": joke["id"], "humor": spec["humor"], "exaggeration": spec["exaggeration"], "parody": True,
            "scores": {"angle_strength": a["strength"], "format_fit": 5, "repetition_penalty": 0.0,
                       "total": a["strength"], "evidence_rank": f"{p['lead']['rank_value']} of {p['lead']['games_value']}",
                       "recognizable": p["recognizable"], "recognition": p["recognition"]},
            "why_not_repeated": [f"joke '{joke['id']}' is the least recently used for {a['id']}"],
            "entertainment_test": {"pass": ok, "why": why}, "options": [], "rejected": {}}


# ----------------------------------------------------------------------------------------------- the script

def _scene(i: int, art: str, stats=(), claims=(), hit_word: str | None = None) -> dict:
    return {"type": "ai_still", "art": art, "move": MOVES[i % len(MOVES)], "camera": "still",
            "transition": "none" if i == 0 else "cut", "hit_word": hit_word,
            "stats": [{"claim": c, "label": label} for c, label in stats], "claims": list(claims)}


def _short_throws(ctx: dict) -> list[tuple]:
    a, box, ph = ctx["a"], ctx["p"]["box"], ctx["ph"]
    stat = (f"Average throw:‖ {ph(a['x'])} yards.{SR.BEAT} League average:‖ {ph(a['league'])}.",
            [(a["x"], "YARDS PER THROW"), (a["league"], "LEAGUE AVERAGE")], [a["x"], a["league"]], a["league"])
    if a["airless"]:
        return [stat, (f"{ph(a['air_yards_completed'])} of his {ph(box['passing_yards'])} passing yards happened in "
                       "the air.", [(a["air_yards_completed"], "YARDS IN THE AIR")],
                       [a["air_yards_completed"], box["passing_yards"]], a["air_yards_completed"])]
    if a["season_rank_value"] == 1:
        return [stat, (f"Shortest average throw in the league,‖ minimum {ph(a['min_attempts'])} attempts.",
                       [(a["season"], "SEASON AVERAGE")],
                       [a["season"], a["season_rank"], a["season_count"], a["min_attempts"]], a["min_attempts"])]
    return [stat, (f"And his season average?‖ {ph(a['season'])}.", [(a["season"], "SEASON AVERAGE")], [a["season"]],
                   a["season"])]


def _rough_day(ctx: dict) -> list[tuple]:
    p, ph = ctx["p"], ctx["ph"]
    box, g, lead = p["box"], p["game"], p["lead"]
    sack, label = SR._sacks(box)
    s = "s" if box["passing_interceptions_value"] != 1 else ""
    worst = {"lowest": "worst", "fewest": "fewest"}.get(lead["sup"], lead["sup"])
    how = "By expected points" if lead["metric"] == "epa" else f"By {lead['label']}"
    place = f"the {ph(lead['rank'])}-{worst} of" if lead["rank_value"] > 1 else f"the {worst} of"
    claims = [box["sacks_suffered"], box["passing_interceptions"], g["points"]]
    return [(f"{ph(box['sacks_suffered'])} {sack}.‖ {ph(box['passing_interceptions'])} interception{s}.‖ "
             f"{ph(g['points'])} points.", [(box["sacks_suffered"], label), (g["points"], "POINTS")], claims, g["points"]),
            (f"{how},‖ {place} {ph(lead['games'])} quarterback games this season.", [(lead["value"], lead["screen"])],
             [lead["value"], lead["rank"], lead["games"]], lead["games"])]


STATS = {"short_throws": _short_throws, "rough_day": _rough_day}


def compose(facts: dict, claims: dict, b: dict) -> tuple[list[dict], list[str]]:
    from .formats import said
    from .story import beat, ph
    p = facts["players"][0]
    a = next(x for x in p["angles"] if x["id"] == b["angle"])
    joke = next(j for j in JOKES[b["angle"]] if j["id"] == b["joke"])
    ctx = {"p": p, "a": a, "claims": claims, "ph": ph, "beat": beat, "fmt": FORMAT}
    g, art = p["game"], joke["art"]
    fill = {"name": said(p["name"]), "opp": ph(p["opponent"]), "B": SR.BEAT}
    team = claims[p["team_name"]]["display"]
    beats = [SR._beat(ctx, "hook", joke["hook"].format(**fill),
                      _scene(0, art[0], claims=[p["opponent"], p["week_claim"]], hit_word=joke["hook_word"]),
                      [SR._cue("punchline", word=joke["hook_word"], at="end", context="setup_aside")])]
    for i, (text, stats, shown, sting) in enumerate(STATS[b["angle"]](ctx), start=1):
        cues = [SR._cue("stat_reveal", claim=sting, at="end", context="stat_ruins_it")] if i == 1 else []
        beats.append(SR._beat(ctx, f"stat_{i}", text, _scene(i, art[i], stats, shown), cues))
    beats.append(SR._beat(ctx, "punch", f"The {team} {ph(g['result'])}.{SR.BEAT} {joke['punch']}",
                          _scene(3, art[3], claims=[g["result"], p["team_name"]], hit_word=joke["punch_word"]),
                          [SR._cue("final_payoff", word=joke["punch_word"], at="end", context="deadpan_hit"),
                           SR._cue("hard_stop", word=joke["punch_word"], at="end")], pause=SR.LAST_GAP))
    for x in beats:
        x["scene"].update(fp=p["role"], style=SR.FORMATS[FORMAT]["style"], series=True)
        x["scene"].setdefault("sfx", False)
    titles = [f"{p['name']} vs the {ph(p['opponent'])}: {joke['title']} (AI Parody)",
              f"{p['name']}'s Stat Line, {joke['title']} (AI Parody)"]
    return beats, titles


def history(episodes_dir: Path, season: int, skip: str = "") -> list[dict]:
    """Earlier parody Shorts' structures, oldest first, so a joke rests before it comes back."""
    path = episodes_dir / "history.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
    rows = [r for r in rows if r.get("season") == season and r["episode_id"] != skip
            and (r.get("structure") or {}).get("series") == SERIES]
    rows.sort(key=lambda r: (r["week"], r.get("order", 0)))
    return [r["structure"] for r in rows]


def hero_beats(ep: Episode) -> dict[str, str]:
    """The Wan clips this Short can use: picture key to clip path, for clips that exist on disk."""
    return {p.stem: f"clips/{p.name}" for p in sorted(ep.path("clips").glob("*.mp4"))}


def _script_run(ep: Episode, hero: bool) -> None:
    from .story import finish
    facts = read_json(ep.path("facts.json"))
    claims = {c["id"]: c for c in facts["claims"]}
    story = read_json(ep.path("story.json"))
    p = facts["players"][0]
    recent = history(ep.cfg.episodes_dir, ep.season, ep.id) + story.get("recent_series", [])
    b = brief(p, recent)
    write_json(ep.path("brief.json"), b)
    ep.path("brief.md").write_text(SR.describe(ep, b) if b["format"] else f"# No brief\n\n{b['entertainment_test']['why']}\n")
    if b["format"] is None or not b["entertainment_test"]["pass"]:
        raise FL.NotEntertaining(f"{p['name']}: {b['entertainment_test']['why']}")
    beats, titles = compose(facts, claims, b)
    clips = hero_beats(ep) if hero else {}
    for x in beats:
        if x["scene"]["art"] in clips:
            x["scene"].update(type="ai_hero", clip=clips[x["scene"]["art"]])
    SR.assign_sfx(beats, b, recent, ep.id)
    scale = ep.cfg["shorts"].get("speed_scale", 1.0)
    for x in beats:
        x["speed"] = round(x["speed"] / scale, 4)
    beats, titles, _ = finish(beats, p, {"column": b["angle"]}, b["kind"], b["format"], None, {"tm": ""}, titles=titles)
    structure = SR.structure(b, beats)
    # one show, many jokes: what repeats is the joke and its pictures, so that is what the originality gate compares
    pictures = [x["scene"]["art"] for x in beats]
    structure |= {"series": SERIES, "hook": f"{FORMAT}:{b['joke']}", "opening": b["joke"], "scenes": pictures}
    words = FL._write_script(ep, facts, claims, beats, titles, structure,
                             "Composed from the show's joke bank and resolved claims; no language model.")
    write_json(ep.path("editorial.json"), SR.editorial_row(p, b, words))
    ep.log(f"  story: {FORMAT} on {b['angle']}, joke {b['joke']}, {len(beats)} beats, {words} words")


def script_run(ep: Episode, hero: bool = False) -> None:
    code = {"parody": sha256_bytes(Path(__file__).read_bytes()), "showrunner": sha256_bytes(Path(SR.__file__).read_bytes())}
    from ..core import sha256_path
    clips = sha256_path(ep.path("clips")) if hero and ep.path("clips").exists() else None
    run_stage(ep, "script", VERSION, {"cta": ep.cfg["cta"], "code": code, "clips": clips,
                                      "history": history(ep.cfg.episodes_dir, ep.season, ep.id)},
              ["facts.json", "story.json"], ["script.json", "editorial.json", "brief.json"],
              lambda: _script_run(ep, hero))
