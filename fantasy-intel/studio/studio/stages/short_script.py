"""Stage 3 (Shorts): a ~30-40 second script for one player, composed deterministically from claims.

One statistic is the protagonist. Python decides the story's ANGLE from the data, and every sentence is a
fixed phrasing whose numbers, names and teams are claim placeholders, so a Short can only say what the data
says. No LLM.

  angle        when                                                        verdict
  expansion    riser led by target share, or snaps up and targets followed "the role got bigger"
  unresolved   riser led by snap share, targets have not followed yet      "does the opportunity follow?"
  takeover     riser led by backfield rush share                           "does this role hold?"
  contraction  any faller                                                  "the role is shrinking"

Variety (so the channel does not look mass-produced): hook families rotate least-recently-used across the
channel's Shorts history, and the evidence/verdict phrasings rotate with them. Beats follow the shot rhythm
stat -> trend chart -> one context card -> takeaway -> follow. From `playoff_runway_from_week` a schedule
beat joins: role first, schedule second.
"""

from __future__ import annotations

import hashlib
import json

from ..core import Episode, canonical_json, read_json, run_stage, sha256_bytes, write_json
from .script import PLACEHOLDER, WORDS_PER_SECOND, fill

VERSION = 6

UNIT = {"offense_pct": "snaps", "target_share": "targets", "air_yards_share": "air yards",
        "backfield_rush_share": "running back carries"}

# Hook families valid for each angle. {stat} is the protagonist statistic.
HOOKS = {
    "number_first": "One number changed for {name}: {stat}.",
    "question_up": "Is {name}'s role actually growing? Look at his {stat}.",
    "question_down": "Is {name}'s role slipping? Look at his {stat}.",
    "contrarian": "Don't judge {name} by the box score. Judge him by his {stat}.",
    "warning_targets": "Don't wait for targets to notice what happened to {name}.",
    "backfield": "This backfield changed more than the box score shows.",
    "warning_down": "The role is shrinking for {name}.",
    "fill_in_question": "Is {name}'s new role here to stay?",
    "fill_in_why": "{name}'s role jumped. The reason matters more than the number.",
    "displaced": "{name}'s role shrank, and there's a reason.",
}
ANGLE_HOOKS = {
    "expansion": ["number_first", "question_up", "contrarian"],
    "unresolved": ["warning_targets", "number_first", "question_up"],
    "takeover": ["backfield", "number_first", "contrarian"],
    "contraction": ["warning_down", "number_first", "question_down"],
    "fill_in": ["fill_in_question", "number_first", "fill_in_why"],
    "held": ["number_first", "question_up", "contrarian"],
    "displaced": ["displaced", "number_first", "question_down"],
}
# Hooks that never name the player: the reveal must.
NAMELESS = {"backfield"}

REVEAL = "{who} went from {before} of the {team}' {unit} to {now}, comparing his last {recent} games with the {earlier} before."
# True by construction: the selection rule requires the move in every recent game (facts._sustained).
EVIDENCE = ["And it showed up in each of those games, not just one big week.",
            "It wasn't one big week, either. It showed up in every one of those games."]
QB_CHANGE = "One more thing: {qbs} have both started in that stretch."
VOLUME = {"targets per game": "He's averaging {v} targets a game in that stretch.",
          "designed rushes per game": "He's averaging {v} designed runs a game in that stretch."}

VERDICTS = {
    "expansion": ["The takeaway isn't the box score. The role got bigger.",
                  "Box scores catch up later. The role already got bigger."],
    "unresolved": ["That's the signal I'm tracking next week: does the opportunity follow the role?",
                   "The playing time moved first. Next week tells us if the targets follow."],
    "takeover": ["I don't need a projection for this one. I just want to know whether this role holds.",
                 "One backfield, one role. The question now is whether it holds."],
    "contraction": ["Roles shrink before box scores do. This one already has.",
                    "Next week tells us whether he wins that role back."],
    "fill_in": ["The role is real while {tm} is out. The question is what happens when he's back.",
                "Use the role while it lasts, and check {tm}'s status before you count on it."],
    # When the teammate is practicing fully or off the report, he may play this week: say so.
    "fill_in_ir": ["With {tm} on injured reserve, this role is his for now.",
                   "{tm} is on injured reserve. Until he's back, this is the role to use."],
    "fill_in_returning": ["If {tm} plays this week, this role gets tested right away.",
                          "Check {tm}'s status before you count on this role."],
    "held": ["That's a role change, not a fill-in.", "{tm} came back, and the role stayed."],
    "displaced": ["That's what a returning teammate does to a role. The box score won't show it.",
                  "The role shrank when {tm} came back. Expect that while he's on the field."],
}
# Injury context, only ever from the usage data (who missed games) and the official injury report / roster
# status (why, and where he stands now). Reasons and statuses are claims, never typed by hand.
TEAMMATE = {
    "fill_in": ["Here's why: {tm} missed {missed}{with_reason}.",
                "Before that, {tm} had {tm_role} of the {team}' {tm_unit}.",
                "Right now, {tm} is {status}."],
    "held": ["{tm} missed {missed}{with_reason}, then came back in {back_week}.",
             "And {name}'s role held with him back."],
    "displaced": ["{tm} is back. He missed {missed}{with_reason}.",
                  "Since returning, he has {tm_role} of the {team}' {tm_unit}."],
}
PLAYOFF = "Role first, schedule second: in {weeks} he gets {opponents}. So far this season, that's {summary}."
CTA_NO_SITE = ["Follow for the next usage signal.",
               "The full usage dashboard is coming. Follow to see it first."]
CTA_SITE = "The full chart is in the first link on my channel."

TITLES = {
    "expansion": ["{name}'s Role Just Changed", "The {Stat} Change Behind {name}", "{name} Is Gaining Real Usage"],
    "unresolved": ["One Number I'm Watching on {name}", "{name}'s Role Just Changed",
                   "The {Stat} Change Behind {name}"],
    "takeover": ["{name}'s Role Just Changed", "The {Stat} Change Behind {name}",
                 "Don't Evaluate {name} by the Box Score"],
    "contraction": ["The Role Is Shrinking for {name}", "The {Stat} Change Behind {name}",
                    "One Number I'm Watching on {name}"],
    "fill_in": ["Is {name}'s Role Real or a Fill-In?", "{name} Is Filling In for {tm}",
                "The {Stat} Change Behind {name}"],
    "held": ["{name}'s Role Survived {tm}'s Return", "{name}'s Role Just Changed", "The {Stat} Change Behind {name}"],
    "displaced": ["{tm} Is Back. {name}'s Role Shrank.", "The Role Is Shrinking for {name}",
                  "The {Stat} Change Behind {name}"],
}


def ph(cid: str) -> str:
    return "{{" + cid + "}}"


def angle_for(p: dict) -> str:
    tm = p.get("teammate")
    if tm and tm["kind"] == "vacated":
        return "held" if tm["returned"] else "fill_in"
    if tm and tm["kind"] == "returned":
        return "displaced"
    if p["role"] == "faller":
        return "contraction"
    lead = p["metrics"][0]["column"]
    if lead == "backfield_rush_share":
        return "takeover"
    if lead == "offense_pct":
        ts = next((m for m in p["metrics"] if m["column"] == "target_share"), None)
        return "expansion" if ts and ts["delta_over_threshold"] >= 1 else "unresolved"
    return "expansion"


def choose(options: list[str], used: list[str], seed: str) -> str:
    """Least recently used option (never used beats used long ago); ties broken by a stable hash."""
    def last_use(o: str) -> int:
        return max((i for i, u in enumerate(used) if u == o), default=-1)

    def h(o: str) -> str:
        return hashlib.sha256(f"{seed}:{o}".encode()).hexdigest()
    return min(options, key=lambda o: (last_use(o), h(o)))


def context_sentence(p: dict, angle: str, snap_recent: float | None) -> str:
    """The one context card's line, chosen by what the data actually shows."""
    lead, rest = p["metrics"][0], {m["column"]: m for m in p["metrics"][1:]}
    if angle == "expansion" and lead["column"] == "target_share" and "offense_pct" in rest:
        m = rest["offense_pct"]
        if snap_recent is not None and snap_recent >= 0.6:
            return f"And he played {ph(m['recent'])} of the snaps, so these weren't a handful of random targets."
        return f"His snap share moved from {ph(m['baseline'])} to {ph(m['recent'])}."
    if angle in ("expansion", "unresolved") and "target_share" in rest:
        m = rest["target_share"]
        if angle == "expansion":
            return f"His target share followed, from {ph(m['baseline'])} to {ph(m['recent'])}."
        return f"His target share is {ph(m['recent'])}, so the targets haven't fully followed the playing time yet."
    if angle == "takeover" and "offense_pct" in rest:
        m = rest["offense_pct"]
        if m["delta_over_threshold"] >= 1:
            return f"His snap share moved with it, from {ph(m['baseline'])} to {ph(m['recent'])}."
        return f"His snap share sits at {ph(m['recent'])}."
    if angle == "contraction" and lead["column"] == "target_share" and "offense_pct" in rest:
        m = rest["offense_pct"]
        if abs(m["delta_over_threshold"]) < 1:
            return (f"He's still on the field for {ph(m['recent'])} of the snaps, so those targets are going to "
                    "someone else.")
        return f"His snap share fell too, from {ph(m['baseline'])} to {ph(m['recent'])}."
    m = p["metrics"][1] if len(p["metrics"]) > 1 else None
    return f"His {m['label']} moved from {ph(m['baseline'])} to {ph(m['recent'])}." if m else ""


def compose(facts: dict, claims: dict, seed: str, used: dict[str, list[str]], cta_url: str = "",
            runway: bool = False) -> tuple[list[dict], list[str], dict]:
    """Return (segments, title templates, structure) for the single player in facts.json.

    One sentence per segment, each with its own visual, so the picture changes every few seconds."""
    p = facts["players"][0]
    lead = p["metrics"][0]
    tm = p.get("teammate")
    angle = angle_for(p)
    hook_family = choose(ANGLE_HOOKS[angle], used.get("hook", []), seed)
    evidence_i = choose([str(i) for i in range(len(EVIDENCE))], used.get("evidence", []), seed)
    verdict_key = angle
    if angle == "fill_in":
        code, _, detail = claims[tm["status"]]["value"]
        if code == "ir":
            verdict_key = "fill_in_ir"
        elif code == "none" or detail == "Full Participation in Practice":
            verdict_key = "fill_in_returning"
    verdicts = [str(i) for i, v in enumerate(VERDICTS[verdict_key])
                if not (hook_family == "warning_down" and "shrink" in v)]     # never say it twice
    verdict_i = choose(verdicts, used.get("verdict", []), seed)
    cta_i = choose([str(i) for i in range(len(CTA_NO_SITE))], used.get("cta", []), seed)
    team = ph(p["team_name"])
    pid = p["gsis_id"]

    segs: list[tuple[str, str, dict]] = [
        ("hook", HOOKS[hook_family].format(name=p["name"], stat=lead["label"]),
         {"type": "hero_stat", "player": pid, "metric": 0}),
        ("reveal", REVEAL.format(who=p["name"] if hook_family in NAMELESS else "He", before=ph(lead["baseline"]),
                                 now=ph(lead["recent"]), team=team, unit=UNIT[lead["column"]],
                                 recent=ph(p["recent_games"]), earlier=ph(p["baseline_games"])),
         {"type": "usage_bars", "player": pid, "metric": 0, "checks": False}),
    ]
    volume = ("volume", VOLUME[p["volume"]["label"]].format(v=ph(p["volume"]["recent"])),
              {"type": "volume_card", "player": pid})
    if tm:
        fmt = {"tm": tm["name"], "name": p["name"], "missed": ph(tm["missed"]), "team": team,
               "with_reason": f" with {ph(tm['reason'])}" if "reason" in tm else "",
               "tm_role": ph(tm["role"]), "tm_unit": tm["unit"], "status": ph(tm["status"]),
               "back_week": ph(tm["back_week"]) if "back_week" in tm else ""}
        lines = [t.format(**fmt) for t in TEAMMATE[angle]]
        if angle == "fill_in":
            segs += [("why", lines[0], {"type": "teammate_timeline", "player": pid, "phase": 1}),
                     ("teammate_role", lines[1], {"type": "teammate_timeline", "player": pid, "phase": 2}),
                     ("status", lines[2], {"type": "status_card", "player": pid})]   # no volume: time is short
        elif angle == "held":
            segs += [("why", lines[0], {"type": "teammate_timeline", "player": pid, "phase": 1}),
                     ("held", lines[1], {"type": "usage_bars", "player": pid, "metric": 0, "checks": True}), volume]
        else:
            segs += [("why", lines[0], {"type": "teammate_timeline", "player": pid, "phase": 1}),
                     ("teammate_role", lines[1], {"type": "teammate_timeline", "player": pid, "phase": 2})]
    else:
        snaps = next((m for m in p["metrics"] if m["column"] == "offense_pct"), None)
        snap_recent = claims[snaps["recent"]]["value"] if snaps else None
        segs.append(("evidence", EVIDENCE[int(evidence_i)],
                     {"type": "usage_bars", "player": pid, "metric": 0, "checks": True}))
        ctx = context_sentence(p, angle, snap_recent)
        if ctx:
            segs.append(("context", ctx, {"type": "stat_compare", "player": pid}))
        segs.append(volume)
    if len(claims[p["context"]["qbs_recent"]]["value"]) == 2:
        segs.append(("qb", QB_CHANGE.format(qbs=ph(p["context"]["qbs_recent"])), {"type": "qb_card", "player": pid}))
    if runway:
        segs.append(("playoff", PLAYOFF.format(weeks=ph(facts["episode"]["playoff_weeks"]),
                                               opponents=ph(p["playoff"]["opponents"]),
                                               summary=ph(p["playoff"]["summary"])),
                     {"type": "playoff_tiles", "player": pid}))
    segs.append(("verdict", VERDICTS[verdict_key][int(verdict_i)].format(tm=tm["name"] if tm else ""),
                 {"type": "takeaway_card", "angle": angle}))
    segs.append(("cta", CTA_SITE if cta_url else CTA_NO_SITE[int(cta_i)], {"type": "follow_card"}))

    stat_cap = lead["label"].title()
    titles = [t.format(name=p["name"], Stat=stat_cap, tm=tm["name"] if tm else "") for t in TITLES[angle]]
    out = [{"id": f"s{i + 1:02d}", "section": sec, "player": pid if sec != "cta" else None,
            "text_template": text, "shot_spec": shot} for i, (sec, text, shot) in enumerate(segs)]
    structure = {"angle": angle, "verdict_set": verdict_key, "hook": hook_family, "evidence": evidence_i, "verdict": verdict_i,
                 "cta": "site" if cta_url else cta_i, "primary_metric": lead["column"], "runway": runway,
                 "teammate": tm["name"] if tm else None}
    return out, titles, structure


def used_structures(ep: Episode) -> dict[str, list[str]]:
    """Structure choices of the Shorts that come before this one (earlier weeks, then earlier stories of this
    week), oldest first, so the rotation is stable when a Short is rebuilt."""
    path = ep.cfg.episodes_dir / "history.jsonl"
    if not path.exists():
        return {}
    order = read_json(ep.path("story.json")).get("order", 0)
    entries = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    prior = [e for e in entries if e["format"] == "short" and e["season"] == ep.season and e.get("structure")
             and (e["week"] < ep.week or (e["week"] == ep.week and e.get("order", 0) < order))]
    prior.sort(key=lambda e: (e["week"], e.get("order", 0)))
    return {k: [e["structure"][k] for e in prior] for k in ("hook", "evidence", "verdict", "cta")}


def _run(ep: Episode, used: dict) -> None:
    facts = read_json(ep.path("facts.json"))
    claims = {c["id"]: c for c in facts["claims"]}
    runway = ep.week >= ep.cfg["shorts"]["playoff_runway_from_week"]
    segs, titles, structure = compose(facts, claims, ep.id, used, ep.cfg["cta"]["url"], runway)
    shots = []
    for seg in segs:
        seg["text_display"] = fill(seg["text_template"], claims, "display")
        seg["text_spoken"] = fill(seg["text_template"], claims, "spoken")
        seg["claims_used"] = sorted(set(PLACEHOLDER.findall(seg["text_template"])))
        seg["target_words"] = len(seg["text_spoken"].split())
        seg["shot"] = f"shot_{seg['id']}"
        shots.append({"id": seg["shot"], "segment": seg["id"], **seg.pop("shot_spec")})
    used_ids = sorted({c for s in segs for c in s["claims_used"]} | {c for t in titles for c in PLACEHOLDER.findall(t)})
    words = sum(len(s["text_spoken"].split()) for s in segs)
    script = {
        "episode_id": ep.id, "season": ep.season, "week": ep.week, "format": ep.fmt,
        "title_candidates": [fill(t, claims, "display") for t in titles], "title_templates": titles,
        "claims": [claims[c] for c in used_ids], "narration_segments": segs, "shots": shots,
        "structure": structure,
        "expected_duration_seconds": round(words / WORDS_PER_SECOND),
        "made_for_kids": False, "synthetic_voice": True,
        "cta": {"url": ep.cfg["cta"]["url"] or None, "text": segs[-1]["text_display"]},
        "facts_sha256": sha256_bytes(canonical_json(facts).encode()),
        "llm": {"provider": "none", "model": "deterministic composer", "calls": [],
                "note": "Shorts are composed from fixed phrasings and claims; no language model."},
    }
    write_json(ep.path("script.json"), script)
    ep.log(f"  script: {structure['angle']} / hook {structure['hook']}, {len(segs)} beats, {words} words "
           f"(~{script['expected_duration_seconds']}s)")


def run(ep: Episode) -> None:
    used = used_structures(ep)
    run_stage(ep, "script", VERSION, {"cta": ep.cfg["cta"], "shorts": ep.cfg["shorts"], "used": used},
              ["facts.json", "story.json"], ["script.json"], lambda: _run(ep, used))
