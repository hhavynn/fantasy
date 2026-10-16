"""Stage 3 (Shorts): find the football story in the data and write it like a person would say it.

First the editorial desk (studio/editorial.py) picks a format from the data: a `mystery` or an
`opportunity` gets its own shape (stages/formats.py); everything else uses the storylines below.

Not a statistics template. Python reads the facts and picks the strongest defensible storyline:

  vanishing_targets  faller: target share fell while his snaps barely moved, and (if the data shows it)
                     which teammates' shares rose. "He's still on the field. So where did his targets go?"
  shrinking          any other faller
  fill_in            riser whose role opened up because a teammate missed the recent games
  held               riser whose teammate came back and the role stayed
  displaced          faller whose teammate came back
  takeover           riser led by backfield rush share
  unresolved         riser: snaps up, targets have not followed yet
  breakout           any other riser

Each storyline is a short sequence of beats: one or two spoken sentences, a scene for the motion renderer,
the words worth emphasising in captions, and a speaking speed. Every number, week, reason and status is a
claim placeholder ({{c012}}), so QC proves the narration matches the data; names are plain text. The
storyline ends on a question for the comments, which loops back to the hook.
"""

from __future__ import annotations

import json

from .. import editorial as E
from .. import shows as SH
from ..core import Episode, canonical_json, read_json, run_stage, sha256_bytes, write_json
from . import formats as F
from .script import PLACEHOLDER, fill

VERSION = 9
WORDS_PER_SECOND = 2.9          # am_fenrir at ~1.1x, measured on the hook line

SNAPS_BARELY_PTS = 5            # a snap-share change this small (displayed points) "barely moved"

UNIT = {"offense_pct": "snaps", "target_share": "targets", "air_yards_share": "air yards",
        "backfield_rush_share": "running back carries"}
POS_WORD = {"TE": "tight end", "WR": "receiver", "RB": "running back"}

HOOKS = {
    # The opening scene follows the hook: mystery -> player card, comparison -> split screen, warning -> lineup.
    "vanishing_targets": {
        "mystery": ("{name} is still on the field. So why did his targets dry up?", ["dry", "up"]),
        "comparison": ("Same snaps. Way fewer targets. That's {name} right now.", ["same", "fewer"]),
        "warning": ("Starting {name} this week? Look at this first.", ["starting"]),
    },
    "shrinking": {
        "proof": ("{name}'s role is shrinking. Here's the proof.", ["shrinking"]),
        "talk": ("{name} managers... we need to talk.", ["talk"]),
    },
    "fill_in": {
        "catch": ("{name}'s role just jumped. But there's a catch.", ["catch"]),
        "real": ("Is {name}'s new role real? It depends on one guy.", ["real"]),
    },
    "held": {
        "stayed": ("{tm} came back. {name} kept the role anyway.", ["kept"]),
    },
    "displaced": {
        "reason": ("{name}'s role shrank, and there's a reason.", ["reason"]),
    },
    "takeover": {
        "took_over": ("{name} just took over the {team} backfield.", ["took", "over"]),
    },
    "unresolved": {
        "yet": ("{name} is playing way more. The targets haven't followed. Yet.", ["yet"]),
        # his target share swung game to game (26%, then 5%): "haven't followed" would be false
        "swing": ("{name} is playing way more. His targets? All over the place.", ["all", "over"]),
    },
    "breakout": {
        "growing": ("{name}'s role is growing, and the numbers back it up.", ["growing"]),
    },
}

# The hook scene's on-screen headline, matched to what the narrator says (fixed text, no numbers).
HEADLINES = {
    ("vanishing_targets", "mystery"): None, ("vanishing_targets", "comparison"): None,
    ("vanishing_targets", "warning"): None,
    ("shrinking", "proof"): ["ROLE", "SHRINKING."], ("shrinking", "talk"): ["WE NEED", "TO TALK."],
    ("fill_in", "catch"): ["THERE'S A", "CATCH."], ("fill_in", "real"): ["REAL ROLE", "OR FILL-IN?"],
    ("held", "stayed"): ["HE'S BACK.", "ROLE STAYED."], ("displaced", "reason"): ["ROLE", "SHRANK."],
    ("takeover", "took_over"): ["BACKFIELD", "TAKEOVER."], ("unresolved", "yet"): ["MORE SNAPS.", "TARGETS NEXT?"],
    ("unresolved", "swing"): ["MORE SNAPS.", "TARGETS: ???"],
    ("breakout", "growing"): ["ROLE", "GROWING."],
}

TITLES = {
    "vanishing_targets": ["Why Did {name}'s Targets Dry Up?", "{name} Is Still Playing. The Targets Aren't.",
                          "Starting {name}? Look at This First"],
    "shrinking": ["{name}'s Role Is Shrinking", "The {Stat} Drop Behind {name}", "{name} Managers, Read This"],
    "fill_in": ["Is {name}'s Role Real or a Fill-In?", "{name} Is Filling In for {tm}", "The Catch With {name}"],
    "held": ["{name} Kept the Role With {tm} Back", "{name}'s Role Survived {tm}'s Return",
             "The {Stat} Change Behind {name}"],
    "displaced": ["{tm} Is Back. {name}'s Role Shrank.", "The Reason {name}'s Role Shrank",
                  "{name}'s {Stat} Just Dropped"],
    "takeover": ["{name} Took Over the Backfield", "The {Stat} Change Behind {name}", "{name}'s Role Just Changed"],
    "unresolved": ["{name} Is Playing More. Targets Next?", "One Number I'm Watching on {name}",
                   "The {Stat} Change Behind {name}"],
    "breakout": ["{name}'s Role Is Growing", "The {Stat} Change Behind {name}", "{name} Is Gaining Real Usage"],
}


def ph(cid: str) -> str:
    return "{{" + cid + "}}"


def last(name: str) -> str:
    parts = [p for p in name.split() if p not in ("Jr.", "Sr.", "II", "III", "IV")]
    return parts[-1]


def snaps_change_pts(p: dict, claims: dict) -> int | None:
    """His displayed snap-share change in points (None without a snap metric)."""
    snaps = next((m for m in p["metrics"] if m["column"] == "offense_pct"), None)
    return None if snaps is None else round(claims[snaps["change"]]["value"] * 100)


def storyline(p: dict, claims: dict) -> str:
    tm = p.get("teammate")
    if tm and tm["kind"] == "vacated":
        return "held" if tm["returned"] else "fill_in"
    if tm and tm["kind"] == "returned":
        return "displaced"
    lead = p["metrics"][0]
    if p["role"] == "faller":
        # Targets fell while his playing time held or grew: not a shrinking role, a vanishing one.
        pts = snaps_change_pts(p, claims)
        if lead["column"] == "target_share" and pts is not None and pts >= -SNAPS_BARELY_PTS:
            return "vanishing_targets"
        return "shrinking"
    if lead["column"] == "backfield_rush_share":
        return "takeover"
    if lead["column"] == "offense_pct":
        ts = next((m for m in p["metrics"] if m["column"] == "target_share"), None)
        return ("breakout" if ts and ts["delta_over_threshold"] >= 1 and ts.get("sustained", True)
                else "unresolved")
    return "breakout"


def choose(options: list[str], used: list[str], seed: str) -> str:
    """Least recently used option; ties go to the earlier-listed (preferred) option."""
    def last_use(o):
        return max((i for i, u in enumerate(used) if u == o), default=-1)
    return min(options, key=lambda o: (last_use(o), options.index(o)))


PAUSE = "‖"            # a deliberate beat inside a line: "8 targets.‖ Then 1." (the narrator lets it land)
PAUSE_SECONDS = 0.18


def beat(section: str, text: str, scene: dict, emphasis: list[str] | None = None, speed: float = 1.1,
         pause: float | None = None) -> dict:
    """One spoken line and its scene. `pause` is the silence after it (None = the config default): short when
    the next line follows straight on, longer after a reveal that needs a moment to register."""
    b = {"section": section, "text_template": text, "scene": scene, "emphasis": emphasis or [], "speed": speed}
    if pause is not None:
        b["pause"] = pause
    return b


def _then_list(values: list[dict]) -> str:
    """'{{c1}}, then {{c2}}' for the per-game values."""
    return ", then ".join(ph(v["value"]) for v in values)


def _names(gainers: list[dict]) -> str:
    """Last names keep the list quick; a same-position teammate is introduced in full, last."""
    parts = [(f"fellow {POS_WORD.get(g['position'], 'teammate')} {g['name']}" if i == len(gainers) - 1 and
              g.get("same_position") else last(g["name"])) for i, g in enumerate(gainers)]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f", and {parts[-1]}"


def compose(facts: dict, claims: dict, seed: str, used: dict, hook_override: str | None = None,
            show: str | None = None) -> tuple[list[dict], list[str], dict]:
    p = facts["players"][0]
    lead = p["metrics"][0]
    tm = p.get("teammate")
    if show in ("red_flag", "heist", "myth"):     # a show assigned by the batch editor (studio/shows.py)
        beats, titles, hook_key, variants, extra = SH.build(show, p, claims, used, hook_override)
        beats, titles, structure = finish(beats, p, lead, show, hook_key, tm, {"tm": tm["name"] if tm else ""},
                                          titles=titles, ending=extra["ending"])
        structure["hook_variants"] = variants
        return beats, titles, structure
    fmt = E.choose_format(p, claims)
    if fmt in ("mystery", "opportunity"):
        build = F.mystery if fmt == "mystery" else F.opportunity
        beats, titles, hook_key, variants = build(p, claims, used, hook_override)
        beats, titles, structure = finish(beats, p, lead, fmt, hook_key, tm, {"tm": tm["name"] if tm else ""},
                                          titles=titles)
        structure["hook_variants"] = variants
        return beats, titles, structure
    story = storyline(p, claims)
    hooks = HOOKS[story]
    snaps_up = story == "vanishing_targets" and (snaps_change_pts(p, claims) or 0) > SNAPS_BARELY_PTS
    if snaps_up:                         # "Same snaps" would be false: he's playing more
        hooks = {k: v for k, v in hooks.items() if k != "comparison"}
    if story == "unresolved":
        swing = bool(next(m for m in p["metrics"] if m["column"] == "target_share").get("weekly"))
        hooks = {k: v for k, v in hooks.items() if (k == "swing") == swing}
    hook_key = hook_override if hook_override in hooks else choose(list(hooks), used.get("hook", []), seed)
    team = claims[p["team_name"]]["display"]
    who = {"name": p["name"], "last": last(p["name"]), "team": team, "tm": tm["name"] if tm else ""}
    hook_text, hook_emph = hooks[hook_key]
    hook_scene = {"type": "hook", "story": story, "headline": HEADLINES.get((story, hook_key))}
    if story in ("vanishing_targets", "fill_in"):
        hook_scene = {"type": {"comparison": "split", "warning": "lineup"}.get(hook_key, "player_card"),
                      "story": story, "role": "hook"}
    beats = [beat("hook", hook_text.format(**who), hook_scene, hook_emph, 1.14)]
    if story == "vanishing_targets":
        beats += vanishing_beats(p, claims, who, hook_key)
        return finish(beats, p, lead, story, hook_key, tm, who)

    reveal = (f"His {lead['label']} went from {ph(lead['baseline'])} to "
              f"{'just ' if p['role'] == 'faller' else ''}{ph(lead['recent'])}.")
    # Show playing time as playing time: a snap-share move gets the on-the-field view.
    reveal_scene = {"type": "presence"} if lead["column"] == "offense_pct" else {"type": "big_compare", "metric": 0}
    beats.append(beat("reveal", reveal, reveal_scene, [], 1.08))

    values = p.get("recent_values") or []
    if story in ("vanishing_targets", "shrinking", "breakout", "takeover", "unresolved") and len(values) >= 2:
        beats.append(beat("evidence", f"{ph(p['recent_games'])} straight games: {_then_list(values)}.",
                          {"type": "bar_timeline", "metric": 0}, [], 1.1))

    if story == "vanishing_targets":
        snaps = next(m for m in p["metrics"] if m["column"] == "offense_pct")
        beats.append(beat("twist", "Here's the weird part: his snap share barely moved.",
                          {"type": "twist", "snaps": snaps["column"]}, ["barely", "moved"], 1.12))
        gainers = p.get("gainers") or []
        for g in gainers:
            g["same_position"] = g["position"] == p["position"]
        if gainers:
            gainers = sorted(gainers, key=lambda g: g.get("same_position", False))   # fellow TE said last
            line = f"The targets spread out. {_names(gainers)} are all up." if len(gainers) > 1 else \
                f"Meanwhile, {gainers[0]['name']} went from {ph(gainers[0]['baseline'])} to {ph(gainers[0]['recent'])}."
            beats.append(beat("payoff", line, {"type": "spread", "gainers": [g["gsis_id"] for g in gainers]},
                              ["spread", "out"], 1.16))
        beats.append(beat("takeaway", "He's on the field. The ball just isn't finding him like before.",
                          {"type": "statement", "lines": ["STILL ON", "THE FIELD.", "FEWER", "TARGETS."]},
                          ["isn't", "finding"], 1.12))
        beats.append(beat("question", f"Are you still starting {who['last']}?",
                          {"type": "decision", "question": ["STILL STARTING", who["last"].upper() + "?"]},
                          ["still", "starting"], 1.0))

    elif story == "shrinking":
        m2 = p["metrics"][1] if len(p["metrics"]) > 1 else None
        if m2 and m2.get("sustained", True):
            beats.append(beat("support", f"His {m2['label']}: {ph(m2['baseline'])} to {ph(m2['recent'])}.",
                              {"type": "twist", "snaps": m2["column"]}, [], 1.08))
        beats.append(beat("takeaway", "It's a small sample. But this is the trend that matters.",
                          {"type": "statement", "lines": ["SMALL SAMPLE.", "REAL TREND."]}, ["trend"], 1.08))
        beats.append(beat("question", f"Are you still starting {who['last']}?",
                          {"type": "decision", "question": ["STILL STARTING", who["last"].upper() + "?"]},
                          ["still", "starting"], 1.0))

    elif story in ("fill_in", "held", "displaced"):
        reason = f" with {ph(tm['reason'])}" if "reason" in tm else ""
        if story == "displaced":
            beats.append(beat("why", f"{tm['name']} is back. He missed {ph(tm['missed'])}{reason}.",
                              {"type": "teammate_out", "phase": 1}, ["back"], 1.08))
            beats.append(beat("teammate_role", f"Since returning, he has {ph(tm['role'])} of the {team}' "
                                               f"{tm['unit']}.", {"type": "teammate_out", "phase": 2}, [], 1.08))
            beats.append(beat("question", f"Are you still starting {who['last']}?",
                              {"type": "decision", "question": ["STILL STARTING", who["last"].upper() + "?"]},
                              ["still", "starting"], 1.0))
        else:
            beats.append(beat("why", f"{tm['name']} missed {ph(tm['missed'])}{reason}.",
                              {"type": "teammate_out", "phase": 1}, [], 1.08))
            if story == "held":
                beats.append(beat("held", f"{tm['name']} came back in {ph(tm['back_week'])}, and {who['last']} "
                                          "kept the role.", {"type": "statement", "lines": ["HE CAME BACK.",
                                                                                          "THE ROLE STAYED."]},
                                  ["kept"], 1.08))
                beats.append(beat("question", f"Are you buying {who['last']} now?",
                                  {"type": "decision", "question": ["BUYING", who["last"].upper() + "?"]},
                                  ["buying"], 1.0))
            else:
                beats.append(beat("teammate_role", f"Before that, {tm['name']} had {ph(tm['role'])} of the "
                                                   f"{team}' {tm['unit']}.", {"type": "teammate_out", "phase": 2},
                                  [], 1.08))
                code, _, detail = claims[tm["status"]]["value"]
                beats.append(beat("status", f"Right now, he's {ph(tm['status'])}.", {"type": "status_stamp"},
                                  [], 1.05))
                if code == "ir":
                    take = f"So for now, this role is {who['last']}'s."
                elif code == "none" or detail == "Full Participation in Practice":
                    take = f"If {last(tm['name'])} plays this week, this role gets tested right away."
                else:
                    take = f"It's real while {last(tm['name'])} is out. When he's back, all bets are off."
                beats.append(beat("takeaway", take, {"type": "lineup", "role": "takeaway",
                                                     "watch": f"FILL-IN ROLE · WATCH {last(tm['name']).upper()}"},
                                  ["while", "out"], 1.08))
                beats.append(beat("question", f"Would you pick up {who['last']}, or wait?",
                                  {"type": "decision", "question": ["PICK UP", who["last"].upper() + "?"],
                                   "options": ["ADD", "WAIT"]}, ["or", "wait"], 1.0))

    else:  # takeover, unresolved, breakout
        m2 = p["metrics"][1] if len(p["metrics"]) > 1 else None
        if story == "unresolved":
            ts = next(m for m in p["metrics"] if m["column"] == "target_share")
            if ts.get("weekly"):          # up and down: say the games rather than a misleading mean
                beats.append(beat("support", "But his target share hasn't settled: "
                                             + ", then ".join(ph(c) for c in ts["weekly"]) + ".",
                                  {"type": "twist", "snaps": "target_share"}, ["settled"], 1.08))
            else:
                beats.append(beat("support", f"But his target share is only {ph(ts['recent'])}.",
                                  {"type": "twist", "snaps": "target_share"}, ["only"], 1.08))
            take = ("Playing time moves first. Watch whether the targets follow.",
                    ["PLAYING TIME", "MOVES FIRST."])
        else:
            if m2 and m2.get("sustained", True):
                beats.append(beat("support", f"And his {m2['label']} went from {ph(m2['baseline'])} to "
                                             f"{ph(m2['recent'])}.", {"type": "twist", "snaps": m2["column"]}, [],
                                  1.08))
            take = (("The question now is whether it holds.", ["DOES IT", "HOLD?"]) if story == "takeover"
                    else ("The box score catches up later. The role already got bigger.",
                          ["THE ROLE", "GOT BIGGER."]))
        beats.append(beat("takeaway", take[0], {"type": "statement", "lines": take[1]}, [], 1.08))
        beats.append(beat("question", f"Is {who['last']} on your roster?",
                          {"type": "decision", "question": [f"{who['last'].upper()}", "ON YOUR ROSTER?"],
                           "options": ["YES", "NOT YET"]}, ["your", "roster"], 1.0))

    return finish(beats, p, lead, story, hook_key, tm, who)


def decline_split(values: list[int]) -> int | None:
    """Where a per-game count fell off: the earliest k>=1 such that every later game is at most half the
    average of the games before it (and at least two games follow). None when there is no clean break."""
    for k in range(1, len(values) - 1):
        before = sum(values[:k]) / k
        if before > 0 and all(v <= before / 2 for v in values[k:]):
            return k
    return None


def vanishing_beats(p: dict, claims: dict, who: dict, hook_key: str = "") -> list[dict]:
    """Targets dried up while he stayed on the field: counts, then playing time, then (when the team's own
    volume fell) the share, then a qualified takeaway. No teammate attribution: two games of other players'
    shares cannot say where his targets went."""
    lead = p["metrics"][0]
    snaps = next(m for m in p["metrics"] if m["column"] == "offense_pct")
    games = p["counts"]["games"]
    vals = [claims[g["value"]]["value"] for g in games]
    out = []
    k = decline_split(vals)
    if k is not None:
        out.append(beat("evidence", _counts_sentence(games, k), {"type": "target_balls", "split": k}, ["since"], 1.16))
    pts = round(claims[snaps["change"]]["value"] * 100)
    if pts > SNAPS_BARELY_PTS:
        out.append(beat("twist", f"His snaps? Up to {ph(snaps['recent'])}. He's on the field more than before.",
                        {"type": "split", "role": "twist", "stamp": "MORE THAN BEFORE", "stamp_word": "more"},
                        ["more"], 1.15))
    elif hook_key != "comparison":       # that hook already said "same snaps"
        out.append(beat("twist", f"His snaps? {ph(snaps['recent'])}. About the same as before.",
                        {"type": "split", "role": "twist"}, ["same"], 1.15))
    team = [g for g in games if g.get("team_targets")]
    if len(team) >= 3:
        tt = [claims[g["team_targets"]]["value"] for g in team]
        latest, earlier = tt[-1], sum(tt[:-1]) / (len(tt) - 1)
        if latest <= 0.75 * earlier:
            out.append(beat("volume", f"Part of it: just {ph(team[-1]['team_targets'])} {who['team']} targets in "
                                      f"{ph(p['recent_values'][-1]['week_claim'])}. But his share still fell, "
                                      f"{ph(lead['baseline'])} to {ph(lead['recent'])}.",
                            {"type": "volume"}, ["still", "fell"], 1.16))
        else:
            out.append(beat("volume", f"As a share of the targets, that's {ph(lead['baseline'])} down to "
                                      f"{ph(lead['recent'])}.", {"type": "volume"}, [], 1.12))
    out.append(beat("takeaway", f"{ph(p['recent_games'])} games isn't panic time. Just watch: does his share get "
                                f"back toward {ph(lead['baseline'])}?", {"type": "lineup", "role": "takeaway"},
                    ["watch"], 1.14))
    out.append(beat("question", "Riding it out, or benching him?",
                    {"type": "decision", "question": ["RIDING IT OUT", "OR BENCHING HIM?"],
                     "options": ["RIDE IT OUT", "BENCH HIM"]}, ["riding", "benching"], 1.05))
    return out


def _counts_sentence(games: list[dict], k: int) -> str:
    early = [ph(g["value"]) for g in games[:k]][-3:]
    late = [ph(g["value"]) for g in games[k:]]
    head = early[0] + " targets" + "".join(f", then {e}" for e in early[1:])
    return f"{head}. Since then: {', '.join(late)}."


def finish(beats, p, lead, story, hook_key, tm, who, titles=None, ending=None):
    titles = titles or [t.format(name=p["name"], Stat=lead["label"].title(), tm=who["tm"]) for t in TITLES[story]]
    for i, b in enumerate(beats):
        b["id"] = f"s{i + 1:02d}"
        b["player"] = p["gsis_id"]
    structure = {"angle": story, "format": story if story in ("mystery", "opportunity") else "template",
                 "hook": hook_key, "primary_metric": lead["column"], "teammate": tm["name"] if tm else None,
                 "beats": [b["section"] for b in beats], "scenes": [b["scene"]["type"] for b in beats],
                 # what a returning viewer would notice repeating: how it opens, cuts, breathes and ends
                 "opening": beats[0]["scene"]["type"],
                 "transitions": [b["scene"].get("transition", "push") for b in beats[1:]],
                 "rhythm": [b.get("pause") for b in beats],
                 "ending": beats[-1]["scene"]["type"] + ":" + str(beats[-1]["scene"].get("stage", ""))}
    # the creative fingerprint the similarity checks compare (studio/creative.py)
    family = SH.LEGACY.get(structure["format"], story if story in SH.FAMILIES else "template:" + story)
    structure["format"] = story if story in SH.FAMILIES else structure["format"]
    structure |= {"family": family, "hook_mechanism": SH.HOOK_MECHANISM.get(hook_key, hook_key),
                  "ending_mechanism": ending or SH.ENDING_OF_SCENE.get(beats[-1]["scene"]["type"], beats[-1]["scene"]["type"]),
                  "visual_device": SH.VISUAL.get(family, "template scenes"),
                  "question": SH.QUESTIONS.get(family, SH.QUESTIONS["template"]),
                  "situation": story, "player": p["gsis_id"],
                  "metrics": sorted({m["column"] for m in p["metrics"] for b in beats
                                     for c in b["scene"].get("claims", []) if c in (m["baseline"], m["recent"])})}
    return beats, titles, structure


def used_structures(ep: Episode) -> dict[str, list[str]]:
    path = ep.cfg.episodes_dir / "history.jsonl"
    if not path.exists():
        return {}
    order = read_json(ep.path("story.json")).get("order", 0)
    entries = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    prior = [e for e in entries if e["format"] == "short" and e["season"] == ep.season and e.get("structure")
             and (e["week"] < ep.week or (e["week"] == ep.week and e.get("order", 0) < order))]
    prior.sort(key=lambda e: (e["week"], e.get("order", 0)))
    return {"hook": [e["structure"]["hook"] for e in prior],
            "hook_mechanism": [e["structure"].get("hook_mechanism", SH.HOOK_MECHANISM.get(e["structure"]["hook"], ""))
                               for e in prior]}


def _run(ep: Episode, used: dict, hook: str | None) -> None:
    facts = read_json(ep.path("facts.json"))
    claims = {c["id"]: c for c in facts["claims"]}
    show = read_json(ep.path("story.json")).get("show")
    beats, titles, structure = compose(facts, claims, ep.id, used, hook, show)
    shots = []
    for b in beats:
        parts = b["text_template"].split(PAUSE)
        b["text_template"] = "".join(parts)
        if len(parts) > 1:      # TTS speaks each part and leaves PAUSE_SECONDS between them
            b["spoken_parts"] = [fill(x.strip(), claims, "spoken") for x in parts if x.strip()]
            b["part_pause"] = PAUSE_SECONDS
        b["text_display"] = fill(b["text_template"], claims, "display")
        b["text_spoken"] = fill(b["text_template"], claims, "spoken")
        b["claims_used"] = sorted(set(PLACEHOLDER.findall(b["text_template"])))
        b["target_words"] = len(b["text_spoken"].split())
        b["shot"] = f"shot_{b['id']}"
        shots.append({"id": b["shot"], "segment": b["id"], **b["scene"]})
    for b in beats:      # numbers drawn on screen are claims too: listed, so QC re-derives them
        b["claims_on_screen"] = sorted(c for c in b["scene"].get("claims", []) if c)
    used_ids = sorted({c for b in beats for c in b["claims_used"] + b["claims_on_screen"]} |
                      {c for t in titles for c in PLACEHOLDER.findall(t)})
    words = sum(len(b["text_spoken"].split()) for b in beats)
    script = {
        "episode_id": ep.id, "season": ep.season, "week": ep.week, "format": ep.fmt,
        "title_candidates": [fill(t, claims, "display") for t in titles], "title_templates": titles,
        "claims": [claims[c] for c in used_ids], "narration_segments": beats, "shots": shots,
        "structure": structure, "expected_duration_seconds": round(words / WORDS_PER_SECOND),
        "made_for_kids": False, "synthetic_voice": True,
        "cta": {"url": ep.cfg["cta"]["url"] or None, "text": beats[-1]["text_display"]},
        "facts_sha256": sha256_bytes(canonical_json(facts).encode()),
        "llm": {"provider": "none", "model": "deterministic story engine", "calls": [],
                "note": "Shorts are composed from storyline rules and claims; no language model."},
    }
    write_json(ep.path("script.json"), script)
    recent = E.history(ep.cfg.episodes_dir, ep.season, exclude=ep.id)
    p = dict(facts["players"][0], _episode_week=ep.week)
    ed = E.score(p, claims, structure["format"], recent)
    ed["show"] = structure["family"]
    ed["checks"] = E.checks(ed, words)
    if not ed["checks"]["fits_20s"]["pass"]:
        ed["verdict"], ed["reject_reasons"] = "reject", ed["reject_reasons"] + ["too long to tell in ~20 s"]
    write_json(ep.path("editorial.json"), ed)
    ep.log(f"  story: {structure['angle']} / hook {structure['hook']}, {len(beats)} beats, {words} words "
           f"(~{script['expected_duration_seconds']}s)")


def run(ep: Episode, hook: str | None = None) -> None:
    used = used_structures(ep)
    from pathlib import Path
    code = {f: sha256_bytes(Path(m.__file__).read_bytes()) for f, m in (("formats", F), ("editorial", E), ("shows", SH))}
    run_stage(ep, "script", VERSION, {"cta": ep.cfg["cta"], "used": used, "hook": hook, "code": code},
              ["facts.json", "story.json"], ["script.json", "editorial.json"], lambda: _run(ep, used, hook))
