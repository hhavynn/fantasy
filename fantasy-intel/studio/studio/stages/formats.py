"""Editorial formats for Shorts: a story shape chosen from the data (studio/editorial.py), not a template.

Each format owns its opening, the order and voice of its beats, its scenes, transitions, music and ending.
Narration interprets, in the voice of a fan telling a friend something surprising: short lines, contractions,
a question where a person would ask one, numbers only where the number is the point. The screen carries the
exact figures and their denominators. Every number, week, reason and status is a claim placeholder
({{c012}}); every number drawn on screen is a claim the scene spec lists; QC re-derives both.

Rhythm is written into the lines: `‖` is a deliberate pause inside a line (story.PAUSE), and each beat's
`pause` is the silence after it: short when the next line follows straight on, longer after a reveal.

  mystery      Opens on the contradiction itself (same snaps, very different targets), asks which game was
               real, shows what the data can (the team's volume, a teammate who came back), then zooms out
               to his whole season for the answer. Clues are shown side by side, never claimed as causes.
  opportunity  Opens on the workload number, then who and why, the split with the starter, the injury and
               its dated status, and a conditional call: what to do if the starter sits, and if he returns.
"""

from __future__ import annotations

from .. import editorial as E

SUFFIXES = ("Jr.", "Sr.", "II", "III", "IV")


def said(name: str) -> str:
    """The name as the narrator says it: 'Oronde Gadsden', not 'Oronde Gadsden two'."""
    return " ".join(w for w in name.split() if w not in SUFFIXES)


POS_WORD = {"TE": "tight end", "WR": "receiver", "RB": "running back"}

# Opening candidates, strongest first (scored in docs/creative-pass-4/README.md). The first one not used
# by this format in its last two videos opens the video; the rest stay available for A/B tests (--hook).
MYSTERY_HOOKS = {
    "collide": "Same snaps both weeks. {t1} targets, then {t2}.",
    "how": "How do you play the exact same snaps and go from {t1} targets to {t2}?",
    "cliff": "{name}'s targets just fell off a cliff. His snaps didn't move.",
}
OPPORTUNITY_HOOKS = {
    "share": "{share} of the backfield?",
    "count": "{own} of {all} running back carries. One guy.",
    "hall_out": "{tm}'s out, and {name}'s getting basically every carry.",
}
TITLES = {
    "mystery": ["{name}: Same Snaps, {t1} Targets, Then {t2}", "Which {name} Is Real?",
                "The {name} Target Mystery"],
    "opportunity": ["{share} of the Backfield: {name} With {tm} Out", "{own} of {all}: {name}'s Window",
                    "Start {name} While {tm} Sits?"],
}


def pick_hook(options: dict, used: list[str], override: str | None) -> tuple[str, list[str]]:
    """The strongest opening unless it opened one of the last two Shorts: vary only when repetition would
    show, never at random."""
    order = list(options)
    if override in options:
        return override, order
    recent = used[-2:]
    hook = next((k for k in order if k not in recent), order[0])
    return hook, order


def outlier_game(claims: dict, season: list[dict]) -> dict | None:
    """The one game far above the rest (at least double every other game's targets), if there is one."""
    games = [g for g in season if g.get("targets")]
    if len(games) < 4:
        return None
    vals = [claims[g["targets"]]["value"] for g in games]
    top = max(range(len(vals)), key=lambda i: vals[i])
    others = vals[:top] + vals[top + 1:]
    return games[top] if vals[top] >= 2 * max(others) and vals[top] >= 5 else None


def mystery(p: dict, claims: dict, used: dict, hook_override: str | None = None):
    from .story import beat, last, ph
    case = E.mystery_case(p, claims)
    clues = E.mystery_clues(p, claims, case)
    a, b = case["games"]
    who = last(p["name"])
    pos = POS_WORD.get(p["position"], "player")
    hook, order = pick_hook(MYSTERY_HOOKS, used.get("hook", []), hook_override)
    fields = {"name": said(p["name"]), "t1": ph(a["targets"]), "t2": ph(b["targets"])}
    mirror = {"type": "mirror", "games": [a["week"], b["week"]], "stage": "both", "camera": "still",
              "claims": [a["snaps"], b["snaps"], a["snap_share"], b["snap_share"], a["targets"], b["targets"],
                         a["week_claim"], b["week_claim"]]}
    beats = [beat("hook", MYSTERY_HOOKS[hook].format(**fields), mirror,
                  {"collide": ["same"], "how": ["exact", "same"], "cliff": ["cliff"]}[hook], 1.1, pause=0.25)]
    beats.append(beat("question", f"So which one's the real {said(p['name'])}?",
                      {"type": "which", "games": [a["week"], b["week"]], "transition": "zoom", "camera": "still",
                       "claims": [a["targets"], b["targets"], a["week_claim"], b["week_claim"]]},
                      ["real"], 1.14, pause=0.2))
    # One clue, the one that matters going forward: a teammate who came back beats the team's one-week volume.
    clues = sorted(clues, key=lambda c_: c_["kind"] != "returned")[:1]
    for clue in clues:
        if clue["kind"] == "volume":
            beats.append(beat("clue", f"Part of it? The {_team(p, claims)} just threw way less.",
                              {"type": "volume_balls", "games": [a["week"], b["week"]], "transition": "cut",
                               "sfx": True, "camera": "still", "exhibit": "A",
                               "claims": [a["team_targets"], b["team_targets"], a["targets"], b["targets"]]},
                              ["less"], 1.16, pause=0.15))
        elif clue["kind"] == "returned":
            r = clue["who"]
            beats.append(beat("clue", f"Part of it? {said(r['name'])} was back, out-snapping him and drawing "
                                      f"{ph(r['targets'])} targets.",
                              {"type": "rival", "rival": r["gsis_id"], "transition": "cut", "sfx": True,
                               "camera": "still", "exhibit": "B",
                               "claims": [r["missed"], r["snaps"], r["targets"], b["snaps"], b["targets"]]},
                              ["back"], 1.16, pause=0.25))
    season = p["details"].get("season", [])
    spike = outlier_game(claims, season)
    zoom = {"type": "zoom_out", "transition": "cut", "camera": "still",
            "claims": [c for g in season for c in (g["targets"], g["snaps"], g["week_claim"]) if c]}
    if spike and case["falling"] and spike["week"] == a["week"]:
        before = [g for g in season if g["week"] < spike["week"] and g.get("targets")]
        steady = [g for g in season if g["week"] >= a["week"] - 1 and g.get("snaps")]
        locked = len({claims[g["snaps"]]["value"] for g in steady}) == 1
        beats.append(beat("zoom", f"But zoom out. Before that {ph(spike['targets'])}-target game?‖ "
                                  + ", ".join(ph(g["targets"]) for g in before[-3:]) + ".",
                          {**zoom, "stage": "zoom", "spike": spike["week"]}, ["zoom", "out"], 1.12, pause=0.3))
        snaps_line = "His snaps are locked in. The targets aren't." if locked else \
            "He's on the field plenty. The targets just aren't there."
        beats.append(beat("verdict", f"That {ph(spike['targets'])} was the outlier. {snaps_line}‖ "
                                     "Don't chase the big week.",
                          {**zoom, "stage": "verdict", "spike": spike["week"], "transition": "none",
                           "locked": locked, "stamp": "DON'T CHASE THE SPIKE"},
                          ["outlier", "chase"], 1.08, pause=0.6))
    else:
        beats.append(beat("verdict", f"His playing time held.‖ The targets didn't.‖ Don't start him off one big "
                                     "week,‖ and don't cut him off one bad one.",
                          {**zoom, "stage": "verdict", "spike": None, "transition": "cut", "locked": False,
                           "stamp": "HOLD, DON'T START"}, ["held", "didn't"], 1.08, pause=0.6))
    t = {"name": p["name"], "t1": ph(a["targets"]), "t2": ph(b["targets"])}
    titles = [x.format(**t) for x in TITLES["mystery"]]
    variants = {k: MYSTERY_HOOKS[k].format(**fields).replace("‖", "") for k in order}
    return beats, titles, hook, variants


def opportunity(p: dict, claims: dict, used: dict, hook_override: str | None = None):
    from .story import beat, last, ph
    tm = p["teammate"]
    d = p["details"]
    car = d["carries"]
    who, tml = last(p["name"]), last(tm["name"])
    team = _team(p, claims)
    hook, order = pick_hook(OPPORTUNITY_HOOKS, used.get("hook", []), hook_override)
    fields = {"name": said(p["name"]), "team": team, "tm": said(tm["name"]), "share": ph(car["own_recent_share"]),
              "own": ph(car["own_recent"]), "all": ph(car["team_recent"])}
    beats = [beat("hook", OPPORTUNITY_HOOKS[hook].format(**fields),
                  {"type": "giant_number", "camera": "drift", "reveal_player": hook == "hall_out",
                   "claims": [car["own_recent_share"], car["recent_weeks"], car["own_recent"], car["team_recent"]]},
                  {"share": ["backfield"], "count": ["one", "guy"], "hall_out": ["every"]}[hook], 1.08, pause=0.2)]
    reveal = (f"That's {said(p['name'])}, with {said(tm['name'])} out." if hook != "hall_out"
              else f"{ph(car['own_recent'])} of {ph(car['team_recent'])}, to be exact.")
    beats.append(beat("reveal", reveal,
                      {"type": "carry_grid", "stage": "recent", "identify": True, "transition": "whip", "sfx": True,
                       "camera": "drift", "claims": [car["own_recent"], car["team_recent"], car["recent_weeks"]]},
                      [who.lower()], 1.14, pause=0.2))
    beats.append(beat("before", f"With {tml} playing? {who} got {ph(car['own_base_share'])} of the carries.",
                      {"type": "carry_grid", "stage": "before", "transition": "rewind", "sfx": True, "camera": "drift",
                       "claims": [car["own_base_share"], car["tm_base_share"], car["tm_base"], car["own_base"],
                                  car["team_base"], car["base_weeks"]]},
                      [tml.lower()], 1.14, pause=0.25))
    reason = f" with {ph(tm['reason'])}" if tm.get("reason") else ""
    status_bits = ([f"as of {ph(d['as_of'])},‖ he was {ph(d['status_brief'])}"]
                   if "as_of" in d and "status_brief" in d else [])
    line = f"{tml}'s missed {ph(tm['missed'])}{reason}" + (",‖ and " + status_bits[0].replace("‖", "")
                                                           if status_bits else "") + "."
    beats.append(beat("status", line,
                      {"type": "report", "transition": "cut", "camera": "drift",
                       "claims": [c for c in (tm["status"], tm.get("reason"), tm["missed"], d.get("as_of"),
                                              d.get("status_brief")) if c]},
                      [], 1.16, pause=0.3))
    code, _, detail = claims[tm["status"]]["value"]
    nxt = d.get("next_game")
    fork = {"type": "fork", "transition": "whip", "sfx": True, "camera": "still",
            "claims": [car["own_recent_share"], car["own_base_share"], car["recent_weeks"], car["base_weeks"]]
            + ([nxt["opponent"], nxt["venue"]] if nxt else [])}
    if code == "none" or detail == "Full Participation in Practice":
        call = f"{tml}'s practicing again, so this could flip right away.‖ If he's back, expect the old split."
    else:
        call = f"If {tml} sits again, {who}'s an easy start.‖ If he's back, expect the old split."
    beats.append(beat("verdict", call, fork, ["start", "split"], 1.08, pause=0.6))
    t = {"name": p["name"], "team": team, "tm": tml, "own": ph(car["own_recent"]), "all": ph(car["team_recent"]),
         "share": ph(car["own_recent_share"])}
    titles = [x.format(**t) for x in TITLES["opportunity"]]
    variants = {k: OPPORTUNITY_HOOKS[k].format(**fields).replace("‖", "") for k in order}
    return beats, titles, hook, variants


def _team(p: dict, claims: dict) -> str:
    return claims[p["team_name"]]["display"]
