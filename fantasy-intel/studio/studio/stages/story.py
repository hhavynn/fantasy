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
from ..numbers import round_half_up
from ..core import Episode, canonical_json, read_json, run_stage, sha256_bytes, write_json
from . import formats as F
from .script import PLACEHOLDER, fill

VERSION = 10
WORDS_PER_SECOND = 2.9          # am_fenrir at ~1.1x, measured on the hook line

SNAPS_BARELY_PTS = 5            # a snap-share change this small (displayed points) "barely moved"
SWING_PTS = 5                   # weekly target shares further apart than this (displayed points) "haven't settled"
HELD_PTS = 5                    # with the teammate back, a role within this many points of without him "stayed"

UNIT = {"offense_pct": "snaps", "target_share": "targets", "air_yards_share": "air yards",
        "backfield_rush_share": "running back carries"}
COUNT_NOUN = {"targets": ("target", "targets"), "designed_rushes": ("carry", "carries")}
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
        # the role shrank in the games he was back: "kept the role" would be false
        "tested": ("{tm} came back. Did {name} keep the role?", ["keep"]),
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
    ("held", "stayed"): ["HE'S BACK.", "ROLE STAYED."], ("held", "tested"): ["HE'S BACK.", "ROLE KEPT?"],
    ("displaced", "reason"): ["ROLE", "SHRANK."],
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
    "held_tested": ["{tm} Is Back. Did {name} Keep the Role?", "The {Stat} Change Behind {name}",
                    "{name}'s Role With {tm} Back"],
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


def _shown_pts(share: float) -> int:
    """A share in the points the viewer sees: the claim formatter's half-up rounding (0.105 -> 11), not Python's
    ties-to-even round (0.105 -> 10), so every threshold reads the number on screen."""
    return int(round_half_up(share * 100))


def snaps_change_pts(p: dict, claims: dict) -> int | None:
    """His displayed snap-share change in points (None without a snap metric)."""
    snaps = next((m for m in p["metrics"] if m["column"] == "offense_pct"), None)
    return None if snaps is None else _shown_pts(claims[snaps["change"]]["value"])


def _pts(cid: str, claims: dict) -> int:
    """A share claim in displayed points (0.215 -> 22)."""
    return _shown_pts(claims[cid]["value"])


def targets_swing(ts: dict, claims: dict) -> bool:
    """His game-by-game target shares are far apart (26%, then 5%), not just short of the mean's threshold."""
    pts = [_pts(c, claims) for c in ts.get("weekly") or []]
    return len(pts) >= 2 and max(pts) - min(pts) > SWING_PTS


def held_split(p: dict) -> tuple[list[dict], list[dict]]:
    """His recent lead-metric games without the teammate, and with him back."""
    missed = set(p["teammate"]["missed_values"])
    values = p.get("recent_values") or []
    return [v for v in values if v["week"] in missed], [v for v in values if v["week"] not in missed]


def role_held(p: dict, claims: dict) -> bool:
    """With the teammate back, every game stayed within HELD_PTS of his average without him."""
    out, back = held_split(p)
    if not out or not back:
        return True
    without = sum(_pts(v["value"], claims) for v in out) / len(out)
    return all(_pts(v["value"], claims) >= without - HELD_PTS for v in back)


def _possessive(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


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
    # a planned detective or contingency is made as planned, not re-chosen (the board and the video must agree)
    fmt = {"detective": "mystery", "contingency": "opportunity"}.get(show) or E.choose_format(p, claims)
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
        swing = targets_swing(next(m for m in p["metrics"] if m["column"] == "target_share"), claims)
        hooks = {k: v for k, v in hooks.items() if (k == "swing") == swing}
    kept = story != "held" or role_held(p, claims)
    ending = None        # the takeaway is the ending; the decision buttons after it are only the call to action
    if story == "held":
        hooks = {k: v for k, v in hooks.items() if (k == "stayed") == kept}
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
        counts_at = len(beats)
        m2 = support_metric(p)
        if m2:
            beats.append(beat("support", f"His {m2['label']}: {ph(m2['baseline'])} to {ph(m2['recent'])}.",
                              {"type": "twist", "snaps": m2["column"]}, [], 1.08))
        beats.append(beat("takeaway", "It's a small sample. But this is the trend that matters.",
                          {"type": "statement", "lines": ["SMALL SAMPLE.", "REAL TREND."]}, ["trend"], 1.08))
        ending = "warning"
        beats.append(beat("question", f"Are you still starting {who['last']}?",
                          {"type": "decision", "question": ["STILL STARTING", who["last"].upper() + "?"]},
                          ["still", "starting"], 1.0))
        _fit(beats, counts_at, counts_beats(p, claims, team), claims)

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
                out, back = held_split(p)
                if out and back:      # the games that back up the hook: without him, then with him back
                    said = lambda vs: " and ".join(ph(v["value"]) for v in vs)
                    him = tm["name"] if last(tm["name"]) == who["last"] else last(tm["name"])
                    beats.append(beat("held", f"{him} came back in {ph(tm['back_week'])}. "
                                              f"{_possessive(who['last'])} {lead['label']}: {said(out)} without him, "
                                              f"{said(back)} with him back.", {"type": "bar_timeline", "metric": 0},
                                      ["back"], 1.08))
                else:
                    beats.append(beat("held", f"{tm['name']} came back in {ph(tm['back_week'])}.",
                                      {"type": "teammate_out", "phase": 2}, ["back"], 1.08))
                ending = "verdict"
                if kept:
                    take = ("The role survived his return. That's what makes it real.",
                            ["HE CAME BACK.", "THE ROLE STAYED."], ["survived"])
                elif back and all(_pts(v["value"], claims) > _pts(lead["baseline"], claims) for v in back):
                    take = ("Smaller with him back, but still bigger than before.", ["SMALLER.", "STILL BIGGER."],
                            ["still", "bigger"])
                else:
                    take, ending = (("With him back, the role shrank.", ["HE CAME BACK.", "THE ROLE SHRANK."],
                                     ["shrank"]), "warning")
                beats.append(beat("takeaway", take[0], {"type": "statement", "lines": take[1]}, take[2], 1.08))
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
                ending = "conditional"
                if code == "ir":
                    take, ending = f"So for now, this role is {who['last']}'s.", "verdict"
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
        m2 = support_metric(p)
        counts_at = len(beats)
        extra = []
        if story == "unresolved":
            ts = next(m for m in p["metrics"] if m["column"] == "target_share")
            games = ", then ".join(ph(c) for c in ts.get("weekly") or [])
            if targets_swing(ts, claims):          # up and down: say the games rather than a misleading mean
                beats.append(beat("support", f"But his target share hasn't settled: {games}.",
                                  {"type": "twist", "snaps": "target_share"}, ["settled"], 1.08))
            elif ts.get("weekly") and abs(_pts(ts["change"], claims)) <= SNAPS_BARELY_PTS:
                beats.append(beat("support", f"But his target share barely moved: {ph(ts['baseline'])} before, "
                                             f"{' and '.join(ph(c) for c in ts['weekly'])} now.",
                                  {"type": "twist", "snaps": "target_share"}, ["barely", "moved"], 1.08))
            elif ts.get("weekly"):                 # steady game to game, just short of a sustained rise
                beats.append(beat("support", f"But his target share is only {games}.",
                                  {"type": "twist", "snaps": "target_share"}, ["only"], 1.08))
            else:
                beats.append(beat("support", f"But his target share is only {ph(ts['recent'])}.",
                                  {"type": "twist", "snaps": "target_share"}, ["only"], 1.08))
            extra = raw_targets_beats(p)
            take = ("Playing time moves first. Watch whether the targets follow.",
                    ["PLAYING TIME", "MOVES FIRST."])
            ending = "cliffhanger"
        else:
            if m2:
                beats.append(beat("support", f"And his {m2['label']} went from {ph(m2['baseline'])} to "
                                             f"{ph(m2['recent'])}.", {"type": "twist", "snaps": m2["column"]}, [],
                                  1.08))
            elif story == "breakout" and lead["column"] != "offense_pct" and snaps_steady(p, claims):
                # more of the ball without more playing time: the share rise is the role, not the snaps
                snaps = next(m for m in p["metrics"] if m["column"] == "offense_pct")
                beats.append(beat("support", f"His snaps? {ph(snaps['recent'])}. About the same as before. "
                                             "The ball just finds him more.", {"type": "twist", "snaps": "offense_pct"},
                                  ["same", "more"], 1.12))
            extra = counts_beats(p, claims, team)
            take = (("The question now is whether it holds.", ["DOES IT", "HOLD?"]) if story == "takeover"
                    else ("The box score catches up later. The role already got bigger.",
                          ["THE ROLE", "GOT BIGGER."]))
            ending = "cliffhanger" if story == "takeover" else "verdict"
        beats.append(beat("takeaway", take[0], {"type": "statement", "lines": take[1]}, [], 1.08))
        beats.append(beat("question", f"Is {who['last']} on your roster?",
                          {"type": "decision", "question": [f"{who['last'].upper()}", "ON YOUR ROSTER?"],
                           "options": ["YES", "NOT YET"]}, ["your", "roster"], 1.0))
        _fit(beats, counts_at + (story == "unresolved"), extra, claims)

    titles = None
    if not kept:
        titles = [t.format(name=p["name"], Stat=lead["label"].title(), tm=who["tm"]) for t in TITLES["held_tested"]]
    return finish(beats, p, lead, story, hook_key, tm, who, titles=titles, ending=ending)


def snaps_steady(p: dict, claims: dict) -> bool:
    """Every recent game's snap share within SNAPS_BARELY_PTS of his earlier average (38% then 81% averages out
    the same, but it is not the same playing time)."""
    snaps = next((m for m in p["metrics"] if m["column"] == "offense_pct"), None)
    if snaps is None:
        return False
    base = _pts(snaps["baseline"], claims)
    recent = [round((s["value"] or 0) * 100) for s in snaps["series"] if s["week"] in p["recent_weeks"]]
    return bool(recent) and all(abs(v - base) <= SNAPS_BARELY_PTS for v in recent)


def support_metric(p: dict) -> dict | None:
    """The first supporting metric that moved the lead's way in every recent game."""
    up = p["metrics"][0]["delta_over_threshold"] > 0
    return next((m for m in p["metrics"][1:] if m.get("sustained", True) and (m["delta_over_threshold"] > 0) == up),
                None)


def _fit(beats: list[dict], at: int, options: list[dict], claims: dict) -> None:
    """Insert the first (longest) option that keeps the Short inside the ~20 s word budget, if any does."""
    fits = next((b for b in options if _words(beats + [b], claims) <= E.MAX_WORDS), None)
    if fits:
        beats.insert(at, fits)


def _words(beats: list[dict], claims: dict) -> int:
    """Spoken words, as story.run and the editorial budget count them."""
    return sum(len(fill(b["text_template"].replace(PAUSE, ""), claims, "spoken").split()) for b in beats)


def raw_targets_beats(p: dict) -> list[dict]:
    """His recent games in raw targets ("In raw targets: 7, then 2."): what "hasn't settled" or "only" looks like
    on the stat sheet. A back's per-game counts are carries, so his targets come from the game details."""
    counts = p.get("counts") or {}
    if counts.get("column") == "targets":
        games = [g for g in counts["games"] if g["week"] in p["recent_weeks"]]
        k = next((i for i, g in enumerate(counts["games"]) if g["week"] in p["recent_weeks"]), 0)
        scene = {"type": "target_balls", "split": k}
    else:
        games = [{"value": g.get("targets")} for g in (p.get("details") or {}).get("games", [])]
        scene = {"type": "twist", "snaps": "target_share"}
    if len(games) < 2 or not all(g["value"] for g in games):
        return []
    return [beat("counts", f"In raw targets: {_then_list(games)}.", scene, [], 1.12)]


def counts_beats(p: dict, claims: dict, team: str) -> list[dict]:
    """The share move in raw per-game volume ("4 targets, then 1, then 0. Since then: 3, 7."), longest first: for
    a riser, with "no one on the team got more" when his latest game matched or beat every teammate, then without
    it. Empty when the counts did not move his way by at least one a game (a share can rise because the team
    threw less)."""
    counts = p.get("counts") or {}
    games = counts.get("games") or []
    k = next((i for i, g in enumerate(games) if g["week"] in p["recent_weeks"]), None)
    early = [g for g in games[:k or 0] if g["week"] in p["baseline_weeks"]]
    if not k or not early:
        return []
    vals = lambda gs: [claims[g["value"]]["value"] for g in gs]
    late = games[k:]
    move = sum(vals(late)) / len(late) - sum(vals(early)) / len(early)
    if (move if p["role"] == "riser" else -move) < 1:
        return []
    noun = COUNT_NOUN.get(counts["column"], (counts["column"].replace("_", " "),) * 2)
    text = _counts_sentence(early + late, len(early), noun[vals(early[-3:])[0] != 1])
    # the scene draws the same games the sentence reads (baseline window, then recent), split between the two
    scene = {"type": "target_balls", "weeks": [g["week"] for g in early + late], "split": len(early)}
    out = []
    spread = (p.get("details") or {}).get("spread")
    if (p["role"] == "riser" and counts["column"] == "targets" and spread and spread["week"] == late[-1]["week"]
            and vals(late)[-1] >= claims[spread["most_by_teammate"]]["value"]):
        out.append(beat("counts", f"{text} No one on the {team} got more in {ph(spread['week_claim'])}.", scene,
                        ["more"], 1.12))
    out.append(beat("counts", text, scene, ["since"], 1.12))
    return out


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
        out.append(beat("evidence", _counts_sentence(games, k, "targets" if vals[:k][-3:][0] != 1 else "target"),
                        {"type": "target_balls", "split": k}, ["since"], 1.16))
    pts = _shown_pts(claims[snaps["change"]]["value"])
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


def _counts_sentence(games: list[dict], k: int, noun: str = "targets") -> str:
    early = [ph(g["value"]) for g in games[:k]][-3:]
    late = [ph(g["value"]) for g in games[k:]]
    head = f"{early[0]} {noun}" + "".join(f", then {e}" for e in early[1:])
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
                  # flame / praise players carry game stat lines, not usage metrics: none to fingerprint
                  "metrics": sorted({m["column"] for m in p.get("metrics", []) for b in beats
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
