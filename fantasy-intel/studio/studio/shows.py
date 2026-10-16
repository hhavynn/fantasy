"""Shows: the channel's content families, and the editor that decides which story gets which show.

The football question picks the show, not the other way round. Each family answers a different question,
with its own opening mechanism, beat order, dominant visual, pacing, music and ending. They are not skins over
one beat sequence:

  red_flag     Should you be worried? A concise risk report: the warning first (the player can come second), the
               strongest signal on one evolving chart, what it means, what could reverse it, and a warning
               ending (the one condition that changes the call). Slow, tense, one dominant visual.
  heist        Is this a pickup worth making before others notice? The opportunity, why it exists, the catch,
               and a direct verdict. Fast cuts, bright, an alert and a roster slot instead of charts.
  myth         Is the obvious reading wrong? A lesson: the reasonable assumption, three quick pieces of reality,
               and one rule worth remembering. Punchy stat tiles, no traditional chart.
  detective    (stages/formats.py mystery) Is this game real or an outlier? Clues, then a verdict.
  contingency  (stages/formats.py opportunity) What if the starter sits, and what if he's back? A fork.

Endings are mechanisms, not a fixed card: verdict, warning (the condition that changes the call), takeaway (a
rule), conditional (a fork), cliffhanger (the evidence that will settle it). Openings are mechanisms too:
warning, problem-first (the player is revealed second), direct recommendation, myth, question.

The batch editor (plan_batch) gives each story the show that fits it best, never the same show twice in one
batch, and drops a story that would only repeat another one this week, with the reason written down. Every
number is a claim placeholder; QC re-derives them exactly as for every other Short.
"""

from __future__ import annotations

from . import editorial as E

SNAPS_HELD_PTS = 5          # a snap share within this (displayed points) of before "held"
LOW_VOLUME = 0.75           # a team's latest-game targets this far below its lowest other game: "a low-volume week"
MYTH_SNAPS_UP_PTS = 3       # myth: playing time up at least this much (displayed points)...

FAMILIES = ("red_flag", "heist", "myth", "detective", "contingency")
LEGACY = {"mystery": "detective", "opportunity": "contingency"}      # stages/formats.py formats by show name
QUESTIONS = {
    "red_flag": "Should fantasy managers be worried about him?",
    "heist": "Is this a pickup worth making before other managers notice?",
    "myth": "Is the obvious reading of his usage wrong?",
    "detective": "Was that game real, or an outlier?",
    "contingency": "What should you do if the starter sits, and if he returns?",
    "template": "How did his usage change?",
}
MOOD = {"red_flag": "tense", "heist": "bright", "myth": "drive", "detective": "tense", "contingency": "bright"}
VISUAL = {"red_flag": "one evolving risk chart", "heist": "alert, meter and roster slot",
          "myth": "myth/reality stat tiles", "detective": "case file", "contingency": "carry grid and fork"}


def _v(claims, cid):
    return claims[cid]["value"] if cid else None


def _pts(claims, cid) -> int:
    return round(_v(claims, cid) * 100)


def _metric(p, column):
    return next((m for m in p["metrics"] if m["column"] == column), None)


def _said(name: str) -> str:
    from .stages.formats import said
    return said(name)


def _team(p, claims) -> str:
    return claims[p["team_name"]]["display"]


def _poss(team: str) -> str:
    return team + ("'" if team.endswith("s") else "'s")


# ----------------------------------------------------------------------------------------------- cases

def red_flag_case(p: dict, claims: dict) -> dict | None:
    """A falling target share (in every recent game) while his playing time held: the role is intact, the looks
    are not. Strength grows with a second signal (air yards falling too) and a big drop."""
    lead, sn = p["metrics"][0], _metric(p, "offense_pct")
    if (p["role"] != "faller" or lead["column"] != "target_share" or not lead.get("sustained", True) or not sn
            or abs(_pts(claims, sn["change"])) > SNAPS_HELD_PTS):
        return None
    fit, why = 3, ["target share fell in every recent game while his snaps held"]
    ay = _metric(p, "air_yards_share")
    if ay and ay.get("sustained") and _v(claims, ay["change"]) < 0:
        fit += 1
        why.append("air-yard share fell in every recent game too")
    if -_pts(claims, lead["change"]) >= 10:
        fit += 1
        why.append(f"a {-_pts(claims, lead['change'])}-point drop")
    games = [g for g in p.get("details", {}).get("games", []) if g.get("team_targets")]
    low = None
    if len(games) >= 1 and p.get("counts"):
        team = [_v(claims, g["team_targets"]) for g in p["counts"]["games"] if g.get("team_targets")]
        if len(team) >= 3 and team[-1] <= LOW_VOLUME * min(team[:-1]):
            g = p["counts"]["games"][-1]
            wk = next((x["week_claim"] for x in p["details"].get("games", []) if x["week"] == g["week"]), None)
            low = {**g, "week_claim": wk} if wk else None
    return {"fit": min(fit, 5), "why": why, "low_volume_game": low}


def heist_case(p: dict, claims: dict) -> dict | None:
    """A role that opened because a teammate is out, sized as a pickup: strongest when the absence is durable
    (injured reserve, or ruled out), weakest when it hinges on this week's practice report."""
    tm = p.get("teammate")
    if p["role"] != "riser" or not tm or tm["kind"] != "vacated" or tm["returned"] or "status" not in tm:
        return None
    code, _, detail = _v(claims, tm["status"])
    durable = code == "ir" or (code == "designation" and str(detail).lower() == "out")
    fit = 5 if durable else 2
    why = [f"{tm['name']} is {claims[tm['status']]['display']}" + (": a window of more than one week" if durable
                                                                    else ": the window depends on this week")]
    return {"fit": fit, "why": why, "durable": durable}


def myth_case(p: dict, claims: dict) -> dict | None:
    """More snaps and a busier passing game, yet a smaller share: the obvious reading (more playing time, more
    targets) is wrong. Needs both facts to hold on their own numbers."""
    lead, sn = p["metrics"][0], _metric(p, "offense_pct")
    if p["role"] != "faller" or lead["column"] != "target_share" or not sn or not p.get("counts"):
        return None
    if _pts(claims, sn["change"]) < MYTH_SNAPS_UP_PTS:
        return None
    games = [g for g in p["counts"]["games"] if g.get("team_targets")]
    recent = [g for g in games if g["week"] in p["recent_weeks"]]
    early = [g for g in games if g["week"] in p["baseline_weeks"]]
    if not recent or not early:
        return None
    if min(_v(claims, g["team_targets"]) for g in recent) <= max(_v(claims, g["team_targets"]) for g in early):
        return None                     # the team is not clearly throwing more: no myth to bust
    weeks = {x["week"]: x["week_claim"] for x in p.get("details", {}).get("games", [])}
    recent = [{**g, "week_claim": weeks.get(g["week"])} for g in recent]
    top = sorted(games, key=lambda g: -_v(claims, g["team_targets"]))[:len(recent)]
    busiest = {g["week"] for g in top} == {g["week"] for g in recent}
    snap_high = _v(claims, sn["recent"]) > max(s["value"] or 0 for s in sn["series"] if s["week"] in p["baseline_weeks"])
    return {"fit": 5, "why": ["snaps up and the team throwing more in every recent game, yet his share fell"],
            "recent": recent, "busiest": busiest, "snap_high": snap_high}


def cases(p: dict, claims: dict) -> dict[str, dict]:
    """Every show this story could be, with how well it fits (0-5) and why."""
    out = {}
    for name, fn in (("red_flag", red_flag_case), ("heist", heist_case), ("myth", myth_case)):
        c = fn(p, claims)
        if c:
            out[name] = c
    if E.opportunity_case(p, claims):
        out["contingency"] = {"fit": 5, "why": ["a vacated backfield and a dated status: a two-way call"]}
    m = E.mystery_case(p, claims)
    if m and E.mystery_clues(p, claims, m):
        out["detective"] = {"fit": 5, "why": ["same playing time, very different targets in comparable games"]}
    return out


# ------------------------------------------------------------------------------------------ the editor

MIN_FIT = 3


def plan_batch(rows: list[dict], recent_history: list[dict] | None = None) -> list[dict]:
    """Assign shows across one week's stories. rows: {slug, player, total, cases} (cases from cases()), strongest
    first wins. Each show is used at most once a batch; a story whose only fitting show is taken is deferred,
    and a story whose best show is weak keeps its earlier format (or the template) and says so. Returns one
    decision per row: {slug, show, verdict: make|defer, reasons}."""
    recent_history = recent_history or []
    recent_shows = [e.get("structure", {}).get("family") for e in recent_history[-3:]]
    used: dict[str, str] = {}
    out = []
    # the best-fitting stories choose first (a story that fits its show strongly beats one that fits it weakly),
    # then the editorial total breaks ties
    for r in sorted(rows, key=lambda r: (-max((c["fit"] for c in r["cases"].values()), default=0), -r["total"])):
        ranked = sorted(r["cases"].items(), key=lambda kv: (-kv[1]["fit"], FAMILIES.index(kv[0])))
        pick, notes = None, []
        for name, c in ranked:
            if name in used:
                notes.append(f"{name}: already this week's {name} ({used[name]})")
                continue
            if c["fit"] < MIN_FIT:
                notes.append(f"{name}: fit {c['fit']} < {MIN_FIT} ({'; '.join(c['why'])})")
                continue
            if recent_shows.count(name) >= 2:
                notes.append(f"{name}: two of the last three Shorts were {name}")
                continue
            pick = name
            notes.append(f"{name}: fit {c['fit']} ({'; '.join(c['why'])})")
            break
        if pick:
            used[pick] = r["player"]
            out.append({"slug": r["slug"], "player": r["player"], "show": pick, "verdict": "make", "reasons": notes})
        elif any(c["fit"] >= MIN_FIT or name in used for name, c in r["cases"].items()):
            out.append({"slug": r["slug"], "player": r["player"], "show": None, "verdict": "defer",
                        "reasons": notes + ["redundant this week: its show is taken by a stronger story; revisit "
                                            "next week if the trend holds"]})
        else:
            out.append({"slug": r["slug"], "player": r["player"], "show": None, "verdict": "make",
                        "reasons": notes + ["no show fits strongly: keeps its earlier format"]})
    return out


def board_markdown(decisions: list[dict]) -> str:
    lines = ["# Show board", "",
             "Which show each story gets, and why. One show per story, each show at most once a week; a story that "
             "would only repeat a stronger one is deferred, not forced into a different costume.", "",
             "| player | show | verdict | why |", "|---|---|---|---|"]
    for d in decisions:
        lines.append(f"| {d['player']} | {d['show'] or '(earlier format)'} | {d['verdict']} | "
                     f"{'<br>'.join(d['reasons'])} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------- openings

HOOK_MECHANISM = {   # every opening key, in every show and format, by the mechanism it uses
    "warning": "warning", "problem": "problem_first", "direct": "direct", "alert": "problem_first",
    "myth": "myth", "question": "question",
    "collide": "contradiction", "how": "question", "cliff": "contradiction",            # mystery
    "share": "number", "count": "number", "hall_out": "problem_first",                 # opportunity
    "mystery": "question", "catch": "teaser", "comparison": "contradiction",           # earlier storylines
}


def pick_mechanism(options: list[str], used_mechanisms: list[str], override: str | None = None) -> str:
    """The first-listed opening whose mechanism did not open one of the last three Shorts."""
    if override in options:
        return override
    recent = used_mechanisms[-3:]
    return next((k for k in options if HOOK_MECHANISM[k] not in recent), options[0])


# ---------------------------------------------------------------------------------------------- builders

def build(show: str, p: dict, claims: dict, used: dict, hook_override: str | None = None):
    """(beats, titles, hook_key, variants, extra) for a show. `extra` carries the ending mechanism. A show is only
    built for a story whose facts meet its rule: an --show experiment on the wrong story fails loudly."""
    rule = {"red_flag": red_flag_case, "heist": heist_case, "myth": myth_case}[show]
    if rule(p, claims) is None:
        raise ValueError(f"{p['name']}'s story does not meet the {show} rule (studio/shows.py)")
    return {"red_flag": red_flag, "heist": heist, "myth": myth}[show](p, claims, used, hook_override)


def red_flag(p, claims, used, hook_override=None):
    from .stages.story import beat, last, ph
    case = red_flag_case(p, claims)
    lead, sn = p["metrics"][0], _metric(p, "offense_pct")
    name, who, team = _said(p["name"]), last(p["name"]), _team(p, claims)
    series = [c for c in (lead["baseline"], lead["recent"]) if c]
    chart = {"type": "flag_chart", "metric": lead["column"], "camera": "still", "transition": "none", "sfx": False}
    hooks = {
        "warning": f"There's a red flag in {name}'s usage,‖ and it isn't his playing time.",
        "problem": f"He's on the field for {ph(sn['recent'])} of the snaps.‖ The ball just stopped finding him.",
        "direct": f"Starting {name} this week? Look at this first.",
    }
    key = pick_mechanism(list(hooks), used.get("hook_mechanism", []), hook_override)
    beats = [beat("warning", hooks[key],
                  {"type": "flag_open", "camera": "still", "reveal": key != "problem",
                   "claims": [sn["recent"]] if key == "problem" else []},
                  {"warning": ["red", "flag"], "problem": ["stopped"], "direct": ["first"]}[key], 1.06, pause=0.3)]
    if key == "problem":
        beats.append(beat("reveal", f"That's {name}.", {"type": "flag_open", "camera": "still", "reveal": True,
                                                       "stage": "reveal", "transition": "cut", "claims": []},
                          [who.lower()], 1.1, pause=0.2))
    beats.append(beat("signal", f"His target share: {ph(lead['baseline'])} his first {ph(p['baseline_games'])} games.‖ "
                                f"{ph(lead['recent'])} his last {ph(p['recent_games'])}.",
                      {**chart, "stage": "signal", "transition": "cut",
                       "claims": series + [p["baseline_games"], p["recent_games"]]},
                      [], 1.08, pause=0.35))
    beats.append(beat("meaning", f"He's still playing {ph(sn['recent'])} of the snaps.‖ The looks just aren't there.",
                      {**chart, "stage": "role", "claims": series + [sn["baseline"], sn["recent"]]},
                      ["looks"], 1.08, pause=0.35))
    low = case["low_volume_game"]
    if low:
        beats.append(beat("reversal", f"In his favor: the {team} had just {ph(low['team_targets'])} "
                                      f"targets in {ph(low['week_claim'])}.",
                          {**chart, "stage": "context",
                           "claims": series + [low["team_targets"], low["week_claim"]]},
                          ["favor"], 1.1, pause=0.3))
    beats.append(beat("ending", f"{ph(p['recent_games'])} games is a warning, not a verdict.‖ No rebound toward {ph(lead['baseline'])} "
                                f"this week?‖ Move on.",
                      {**chart, "stage": "call", "claims": series + [p["recent_games"]]}, ["warning"], 1.04, pause=0.7))
    full = p["name"]
    titles = [f"Red Flag: {full}'s Targets", f"Should You Worry About {full}?",
              f"{full}: The Snaps Are There. The Targets Aren't."]
    variants = {k: v.replace("‖", "") for k, v in hooks.items()}
    return beats, titles, key, variants, {"ending": "warning"}


def heist(p, claims, used, hook_override=None):
    from .stages.story import beat, last, ph
    case = heist_case(p, claims)
    tm, sn = p["teammate"], _metric(p, "offense_pct")
    lead = _metric(p, tm["column"]) or p["metrics"][0]      # his share of what the teammate left: like for like
    name, who, tml, team = _said(p["name"]), last(p["name"]), last(tm["name"]), _team(p, claims)
    unit = tm.get("unit", "targets")
    hooks = {
        "alert": f"{_said(tm['name'])} is {ph(tm['status'])}.‖ Look who's getting his {unit.split()[-1]}.",
        "direct": f"If {name}'s on your waiver wire, here's why he shouldn't be for long.",
    }
    key = pick_mechanism(list(hooks), used.get("hook_mechanism", []), hook_override)
    beats = [beat("alert", hooks[key], {"type": "heist_alert", "camera": "push", "claims": [tm["status"]]},
                  {"alert": ["look"], "direct": ["waiver"]}[key], 1.14, pause=0.15)]
    what = lead["label"] if lead["column"] != "backfield_rush_share" else f"{_poss(team)} running back carries"
    line = (f"{name}: {ph(lead['recent'])} of the {what} over the last {ph(p['recent_games'])} games.‖ "
            f"Before, {ph(lead['baseline'])}." if lead["column"] == "backfield_rush_share" else
            f"{name}: his {lead['label']} went from {ph(lead['baseline'])} to {ph(lead['recent'])}.")
    beats.append(beat("opportunity", line,
                      {"type": "heist_meter", "stage": "role", "transition": "whip", "sfx": True, "camera": "push",
                       "claims": [lead["baseline"], lead["recent"], p["recent_games"]]},
                      [who.lower()], 1.14, pause=0.15))
    beats.append(beat("why", f"{tml} had {ph(tm['role'])} of them before he got hurt.",
                      {"type": "heist_split", "transition": "whip", "sfx": True, "camera": "push",
                       "claims": [tm["role"], lead["recent"]]}, [], 1.14, pause=0.15))
    if sn and sn is not lead:
        beats.append(beat("catch", f"The catch: {ph(sn['recent'])} of the snaps.‖ A big role, not an every-down one.",
                          {"type": "heist_meter", "stage": "snaps", "transition": "whip", "camera": "push",
                           "claims": [sn["baseline"], sn["recent"]]}, ["catch"], 1.12, pause=0.2))
    if case["durable"] and _v(claims, tm["status"])[0] == "ir":       # a verdict: the window outlasts this week
        end, mech = (f"With {tml} {ph(tm['status'])}, this isn't a one-week fill-in.‖ If {who}'s on your waiver wire, "
                     "claim him."), "verdict"
    elif case["durable"]:
        end, mech = f"{tml}'s ruled out.‖ If {who}'s on your waiver wire, claim him.", "verdict"
    else:                                                             # a cliffhanger: what will settle it
        end, mech = f"It all hinges on {tml}'s status this week.‖ If he's ruled out, {who}'s worth a claim.", "cliffhanger"
    beats.append(beat("verdict", end, {"type": "heist_claim", "transition": "whip", "sfx": True, "camera": "still",
                                       "claims": [lead["recent"], tm["status"]]}, ["claim"], 1.08, pause=0.6))
    full = p["name"]
    titles = [f"Waiver Wire Heist: {full}", f"{tml} Is Out. {full} Is In.", f"Claim {full} Before Your League Does?"]
    variants = {k: v.replace("‖", "") for k, v in hooks.items()}
    return beats, titles, key, variants, {"ending": mech}


def myth(p, claims, used, hook_override=None):
    from .stages.story import beat, last, ph
    case = myth_case(p, claims)
    lead, sn = p["metrics"][0], _metric(p, "offense_pct")
    name, who, team = _said(p["name"]), last(p["name"]), _team(p, claims)
    pos = {"TE": "tight end", "WR": "receiver", "RB": "running back"}.get(p["position"], "player")
    hooks = {
        "myth": f"More snaps should mean more targets, right?‖ {name} says otherwise.",
        "question": f"How does a {pos} play more snaps and get fewer targets?",
    }
    key = pick_mechanism(list(hooks), used.get("hook_mechanism", []), hook_override)
    beats = [beat("myth", hooks[key], {"type": "myth_card", "camera": "push", "claims": []},
                  {"myth": ["otherwise"], "question": ["fewer"]}[key], 1.1, pause=0.25)]
    more = "more than ever" if case["snap_high"] else "more than before"
    beats.append(beat("reality", f"He's playing {more}: {ph(sn['recent'])} of the snaps, up from {ph(sn['baseline'])}.",
                      {"type": "myth_tile", "stage": "snaps", "transition": "cut", "camera": "push",
                       "claims": [sn["baseline"], sn["recent"]]}, [more.split()[0]], 1.14, pause=0.12))
    rec = case["recent"]
    nums = " and ".join(ph(g["team_targets"]) for g in rec)
    tail = ", their busiest games of the year." if case["busiest"] else " the last two games."
    beats.append(beat("reality", f"The {team} are throwing more, too: {nums} targets{tail}",
                      {"type": "myth_tile", "stage": "volume", "transition": "cut", "camera": "push",
                       "claims": [g["team_targets"] for g in rec] + [g["week_claim"] for g in rec if g["week_claim"]]},
                      ["more"], 1.14, pause=0.12))
    beats.append(beat("reality", f"And his share still fell: {ph(lead['baseline'])} to {ph(lead['recent'])}.",
                      {"type": "myth_tile", "stage": "share", "transition": "cut", "camera": "push",
                       "claims": [lead["baseline"], lead["recent"]]}, ["fell"], 1.12, pause=0.35))
    beats.append(beat("rule", "Snaps get you on the field. Targets get you in the lineup.‖ "
                              "Don't start him on snaps alone.",
                      {"type": "myth_rule", "transition": "cut", "camera": "still", "claims": []},
                      ["snaps", "targets"], 1.04, pause=0.6))
    full = p["name"]
    titles = [f"More Snaps, Fewer Targets: {full}", f"The {full} Myth", f"Why {full}'s Snaps Are Misleading"]
    variants = {k: v.replace("‖", "") for k, v in hooks.items()}
    return beats, titles, key, variants, {"ending": "takeaway"}


ENDING_OF_SCENE = {"decision": "buttons", "fork": "conditional", "lineup": "one_number", "zoom_out": "verdict",
                   "tip": "verdict", "lock": "verdict"}
