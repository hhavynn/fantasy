"""Editorial formats for Shorts: a story shape chosen from the data (studio/editorial.py), not a template.

Each format owns its opening, the order and voice of its beats, its scenes, transitions, music and ending.
Narration interprets; the screen carries the exact numbers. Every number, week, reason and status is still a
claim placeholder ({{c012}}), and every number drawn on screen is a claim the scene spec lists, so QC checks
both against the data.

  mystery      "Same snaps, two weeks straight. Eight targets, then one." Clues from the data (the team threw
               less; a teammate came back) are shown side by side, never claimed as the cause. Ends on a verdict
               the evidence supports: what is stable, what is not, and the decision that follows.
  opportunity  "He just got almost every running back carry." The old split, the absence (official report),
               the status as of a date, and the window: start him while it is open, and what the only
               evidence says happens when the starter returns.
"""

from __future__ import annotations

from .. import editorial as E

SUFFIXES = ("Jr.", "Sr.", "II", "III", "IV")


def said(name: str) -> str:
    """The name as the narrator says it: 'Oronde Gadsden', not 'Oronde Gadsden two'."""
    return " ".join(w for w in name.split() if w not in SUFFIXES)


POS_WORD = {"TE": "tight end", "WR": "receiver", "RB": "running back"}
ORDINAL_CLUE = ["First clue", "Second clue", "Third clue"]

MYSTERY_HOOKS = {
    # opening line(s); the mirror scene's staging follows: snaps collide first, or both rows land at once
    "same_snaps": "{name} played {same} snaps in back-to-back weeks.",
    "question": "Same snaps, back-to-back weeks. So why did {name} go from {t1} targets to {t2}?",
    "rhythm": "{s1} snaps, {t1} targets. {s2} snaps, {t2}. Same {pos}.",
}
OPPORTUNITY_HOOKS = {
    "giant": "{name} just got almost every {team} running back carry.",
    "count": "{own} of {all}. That's how many {team} running back carries went to {name}.",
    "changed_hands": "The {team} backfield just changed hands. And it might not be for long.",
}
TITLES = {
    "mystery": ["{name}: Same Snaps, {t1} Targets, Then {t2}", "What Happened to {name}'s Targets?",
                "The {name} Mystery"],
    "opportunity": ["{name} Just Took Over the {team} Backfield", "{own} of {all}: {name}'s Window",
                    "Start {name} While {tm} Sits?"],
}


def _hook_order(options: dict, used: list[str]) -> list[str]:
    from .story import choose
    first = choose(list(options), used, "")
    return [first] + [k for k in options if k != first]


def mystery(p: dict, claims: dict, used: dict, hook_override: str | None = None):
    from .story import beat, last, ph
    case = E.mystery_case(p, claims)
    clues = E.mystery_clues(p, claims, case)
    a, b = case["games"]
    who = last(p["name"])
    pos = POS_WORD.get(p["position"], "player")
    same_count = claims[a["snaps"]]["value"] == claims[b["snaps"]]["value"]
    order = _hook_order(MYSTERY_HOOKS, used.get("hook", []))
    hook = hook_override if hook_override in MYSTERY_HOOKS else order[0]
    fields = {"name": said(p["name"]), "pos": pos, "same": "the exact same" if same_count else "the same",
              "t1": ph(a["targets"]), "t2": ph(b["targets"]), "s1": ph(a["snaps"]), "s2": ph(b["snaps"])}
    mirror = {"type": "mirror", "games": [a["week"], b["week"]], "transition": "cut", "camera": "still",
              "claims": [a["snaps"], b["snaps"], a["snap_share"], b["snap_share"], a["targets"], b["targets"],
                         a["week_claim"], b["week_claim"]]}
    beats = [beat("hook", MYSTERY_HOOKS[hook].format(**fields),
                  {**mirror, "stage": "collide" if hook == "same_snaps" else "both"},
                  ["exact", "same"] if hook == "same_snaps" else ["why"] if hook == "question" else ["same"], 1.12)]
    if hook == "same_snaps":
        beats.append(beat("contradiction", f"{ph(a['targets'])} targets. Then {ph(b['targets'])}.",
                          {**mirror, "stage": "targets", "transition": "none"}, [], 0.98))
    returned = None
    for i, clue in enumerate(clues):
        lead_in = ORDINAL_CLUE[i]
        if clue["kind"] == "volume":
            beats.append(beat("clue", f"{lead_in}: the {_team(p, claims)} threw way less.",
                              {"type": "volume_balls", "games": [a["week"], b["week"]], "transition": "cut",
                               "camera": "still", "exhibit": "A" if i == 0 else "B",
                               "claims": [a["team_targets"], b["team_targets"], a["targets"], b["targets"]]},
                              ["way", "less"], 1.1))
        elif clue["kind"] == "returned":
            returned = r = clue["who"]
            beats.append(beat("clue", f"{lead_in}: another {pos}, {said(r['name'])}, was back. More snaps than {who}, "
                                      f"and {ph(r['targets'])} targets.",
                              {"type": "rival", "rival": r["gsis_id"], "transition": "cut", "camera": "still",
                               "exhibit": "A" if i == 0 else "B",
                               "claims": [r["missed"], r["snaps"], r["targets"], b["snaps"], b["targets"]]},
                              ["back", "more"], 1.1))
    if case["falling"]:
        steady = (f"But even with {last(returned['name'])} back, {who}'s snaps didn't budge." if returned and same_count
                  else f"But {who}'s playing time didn't budge.")
        verdict = "Don't start him off one big week. Don't cut him off one bad one."
        stamp = ["HOLD HIM.", "DON'T START HIM YET."]
    else:
        steady = f"And {who}'s playing time held steady."
        verdict = "One big week isn't a role yet. But the snaps say he's worth a bench spot."
        stamp = ["STASH HIM.", "DON'T START HIM YET."]
    board = {"type": "verdict", "games": [a["week"], b["week"]], "transition": "cut", "camera": "still",
             "stamp": stamp, "claims": [a["snaps"], b["snaps"], a["targets"], b["targets"]]}
    beats.append(beat("verdict", steady, {**board, "stage": "board"}, ["didn't", "budge"], 1.06))
    beats.append(beat("takeaway", verdict, {**board, "stage": "call", "transition": "none"}, ["start", "cut"], 1.02))
    t = {"name": p["name"], "t1": ph(a["targets"]), "t2": ph(b["targets"])}
    titles = [x.format(**t) for x in TITLES["mystery"]]
    variants = {k: MYSTERY_HOOKS[k].format(**fields) for k in order}
    return beats, titles, hook, variants


def opportunity(p: dict, claims: dict, used: dict, hook_override: str | None = None):
    from .story import beat, last, ph
    tm = p["teammate"]
    car = p["details"]["carries"]
    lead = p["metrics"][0]
    who, tml = last(p["name"]), last(tm["name"])
    team = _team(p, claims)
    order = _hook_order(OPPORTUNITY_HOOKS, used.get("hook", []))
    hook = hook_override if hook_override in OPPORTUNITY_HOOKS else order[0]
    fields = {"name": said(p["name"]), "team": team, "own": ph(car["own_recent"]), "all": ph(car["team_recent"])}
    beats = [beat("hook", OPPORTUNITY_HOOKS[hook].format(**fields),
                  {"type": "giant_number" if hook != "count" else "carry_grid", "stage": "recent",
                   "identify": hook == "count",
                   "transition": "cut", "camera": "drift",
                   "claims": [car["own_recent_share"], car["recent_weeks"], car["own_recent"], car["team_recent"]]},
                  ["almost", "every"] if hook == "giant" else ["changed", "hands"], 1.08)]
    if hook != "count":
        beats.append(beat("count", f"{ph(car['own_recent'])} of {ph(car['team_recent'])} since {tml} went out.",
                          {"type": "carry_grid", "stage": "recent", "transition": "whip", "camera": "drift",
                           "claims": [car["own_recent"], car["team_recent"], car["recent_weeks"]]}, [], 1.04))
    beats.append(beat("before", f"Before that? This was {said(tm['name'])}'s backfield. {ph(car['tm_base'])} carries to "
                                f"{ph(car['own_base'])}.",
                      {"type": "carry_grid", "stage": "before", "transition": "rewind", "camera": "drift",
                       "claims": [car["tm_base"], car["own_base"], car["team_base"], car["base_weeks"]]},
                      [tml.lower() + "'s"], 1.1))
    reason = ph(tm["reason"]) if tm.get("reason") else "an absence"
    beats.append(beat("absence", f"Then {reason} kept {tml} out.",
                      {"type": "handoff", "transition": "whip", "camera": "drift",
                       "claims": [c for c in (tm.get("reason"), tm["missed"]) if c]}, ["out"], 1.04))
    status = claims[tm["status"]]["value"]
    if "as_of" in p["details"]:
        beats.append(beat("status", f"As of {ph(p['details']['as_of'])}, {tml} was {ph(tm['status'])}.",
                          {"type": "report", "transition": "whip", "camera": "drift",
                           "claims": [tm["status"], p["details"]["as_of"]]}, [], 1.12))
    code, _, detail = status
    nxt = p["details"].get("next_game")
    window = {"type": "window", "transition": "whip", "camera": "drift",
              "claims": [car["own_base_share"], car["tm_base_share"], car["base_weeks"]]
              + ([nxt["opponent"], nxt["venue"]] if nxt else [])}
    if code == "none" or detail == "Full Participation in Practice":
        call = f"If {tml} plays this week, {who}'s window could close right away."
    else:
        call = f"So while {tml} sits, {who}'s a starter."
    beats.append(beat("verdict", call, {**window, "stage": "open"}, ["sits", "starter"], 1.04))
    beats.append(beat("caveat", f"When {tml}'s back, the only split we've seen is this one.",
                      {**window, "stage": "closing", "transition": "none"}, ["only"], 1.06))
    t = {"name": p["name"], "team": team, "tm": tml, "own": ph(car["own_recent"]), "all": ph(car["team_recent"])}
    titles = [x.format(**t) for x in TITLES["opportunity"]]
    variants = {k: OPPORTUNITY_HOOKS[k].format(**fields) for k in order}
    return beats, titles, hook, variants


def _team(p: dict, claims: dict) -> str:
    return claims[p["team_name"]]["display"]
