"""Labelled cartoon parody and parallel-universe scripts.

The rules are the account's, and ``soccer/qc.py`` enforces them: only the original cast, a parody label on
screen and aloud, real names only in the narrator's framing, invented lines only from fictional characters, and
every real number a sourced claim. The stock templates are one-offs, because the originality gate rejects a repeat:
for a new event, write beats by hand and pass them with ``make-soccer --beats``.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from ..numbers import int_words

LABELS = {"parody": "PARODY", "parallel_universe": "WHAT IF: NOT REAL"}
OPEN = {"parody": "This is a cartoon parody. None of it happened.",
        "parallel_universe": "What if? This is made up, and none of it happened."}
CLOSE = {"parody": "Still a parody. Follow for the next one.",
         "parallel_universe": "That universe is made up. Follow for the next one."}

TEMPLATES: dict[str, list[tuple[str, dict]]] = {
    "heist": [
        ("The <subject> debate got so loud that a talking trophy decided to leave.",
         {"type": "toon", "set": "vault", "cast": [{"who": "trophy", "x": 540, "from": -500}],
          "say": {"who": "trophy", "text": "I'M OUT", "at": "leave"}}),
        ("The Boss, a cartoon manager from nowhere, calls an emergency meeting.",
         {"type": "toon", "set": "press", "cast": [{"who": "boss", "x": 540, "sweat": True}],
          "say": {"who": "boss", "text": "WHERE IS IT?", "at": "emergency"}}),
        ("Check the vault, check the kit bag, check the group chat.",
         {"type": "toon", "set": "vault", "cast": [{"who": "boss", "x": 330}, {"who": "fan", "x": 750}],
          "say": {"who": "fan", "text": "NOT IN THE CHAT", "at": "group"}}),
        ("The pundit blames the voters. The voters blame the pundit.",
         {"type": "toon", "set": "studio", "cast": [{"who": "pundit", "x": 540}],
          "say": {"who": "pundit", "text": "IT WAS RIGGED, PROBABLY", "at": "blames"}}),
        ("The trophy turns up at a service station, eating a sausage roll, finally at peace.",
         {"type": "toon", "set": "wild", "cast": [{"who": "trophy", "x": 540}],
          "say": {"who": "trophy", "text": "NEVER AGAIN", "at": "peace"}}),
    ],
    "nature": [
        ("Here, in the wild, we find the transfer rumour.",
         {"type": "toon", "set": "wild", "cast": [{"who": "rumour", "x": 540, "from": 500}]}),
        ("Spotted near <subject>, it feeds on anonymous sources and airport photos.",
         {"type": "toon", "set": "wild", "cast": [{"who": "rumour", "x": 540, "s": 1.2}],
          "say": {"who": "rumour", "text": "SOURCES SAY", "at": "feeds"}}),
        ("The Boss approaches slowly. One wrong word and it doubles in size.",
         {"type": "toon", "set": "wild", "cast": [{"who": "boss", "x": 280, "sweat": True}, {"who": "rumour", "x": 760, "s": 1.5}],
          "say": {"who": "boss", "text": "NO COMMENT", "at": "wrong"}}),
        ("By deadline day it has eaten a fee, a medical and somebody's weekend.",
         {"type": "toon", "set": "wild", "cast": [{"who": "rumour", "x": 540, "s": 1.8}],
          "say": {"who": "rumour", "text": "STILL HUNGRY", "at": "eaten"}}),
        ("Experts say it will return next window. It always returns.",
         {"type": "toon", "set": "studio", "cast": [{"who": "pundit", "x": 540}],
          "say": {"who": "pundit", "text": "IT ALWAYS RETURNS", "at": "always"}}),
    ],
    "press": [
        ("A cartoon press conference, after all the <subject> chaos.",
         {"type": "toon", "set": "press", "cast": [{"who": "boss", "x": 540, "sweat": True}], "title": "PRESS CONFERENCE"}),
        ("Are you worried? Never worried, says the Boss, sweating through a coat.",
         {"type": "toon", "set": "press", "cast": [{"who": "boss", "x": 540, "sweat": True}],
          "say": {"who": "boss", "text": "NEVER WORRIED", "at": "never"}}),
        ("Is your job safe? The Boss stares at a water bottle for a very long time.",
         {"type": "toon", "set": "press", "cast": [{"who": "boss", "x": 540, "sweat": True}],
          "say": {"who": "boss", "text": "...", "at": "stares"}}),
        ("What's the plan? The plan, says the Boss, is to have a plan.",
         {"type": "toon", "set": "press", "cast": [{"who": "boss", "x": 540}],
          "say": {"who": "boss", "text": "THE PLAN IS A PLAN", "at": "plan"}}),
        ("Someone asks about the referee. The microphone quietly leaves the room.",
         {"type": "toon", "set": "press", "cast": [{"who": "ref", "x": 760}, {"who": "boss", "x": 300}],
          "say": {"who": "ref", "text": "DON'T LOOK AT ME", "at": "referee"}}),
    ],
    "what_if": [
        ("What if the whole <subject> story went the other way?",
         {"type": "split", "ours": {"text": "EVERYONE'S ARGUING", "cast": [{"who": "pundit", "x": 540}]},
          "other": {"text": "NOBODY NOTICED", "cast": [{"who": "fan", "x": 540}]}}),
        ("In the other universe, the group chats are silent.",
         {"type": "split", "ours": {"text": "PHONES MELTING", "cast": [{"who": "fan", "x": 540}]},
          "other": {"text": "TOTAL SILENCE", "cast": [{"who": "fan", "x": 540}]}}),
        ("The pundits have been forced to discuss the weather.",
         {"type": "split", "ours": {"text": "HOT TAKES", "cast": [{"who": "pundit", "x": 540}]},
          "other": {"text": "LIGHT DRIZZLE", "cast": [{"who": "pundit", "x": 540}]}}),
        ("Radio phone-ins are down to one caller, and it's about a lost dog.",
         {"type": "split", "ours": {"text": "LINES JAMMED", "cast": [{"who": "fan", "x": 540}]},
          "other": {"text": "LOST DOG", "cast": [{"who": "fan", "x": 540}]}}),
        ("Somebody's uncle has no opinion on it at all, and the family is worried.",
         {"type": "split", "ours": {"text": "STRONG OPINIONS", "cast": [{"who": "pundit", "x": 540}]},
          "other": {"text": "NO OPINION", "cast": [{"who": "fan", "x": 540}]}}),
        ("And the Boss has, somehow, slept through the night.",
         {"type": "split", "ours": {"text": "NO SLEEP", "cast": [{"who": "boss", "x": 540, "sweat": True}]},
          "other": {"text": "FAST ASLEEP", "cast": [{"who": "boss", "x": 540}]}}),
    ],
}
BY_TOPIC = (("award", "heist"), ("transfer", "nature"), ("manager", "press"), ("crisis", "press"), ("rivalry", "press"))
UNITS = {"goals": "goals", "assists": "assists", "minutes": "minutes"}


def subject_of(package: dict[str, Any]) -> str:
    return re.split(r"\s+&\s+|:", package["cluster"]["label"])[0].strip()


def real_names(package: dict[str, Any]) -> list[str]:
    """Everyone real in the story: the gate keeps invented lines away from all of them, not just the lead."""
    cluster = package["cluster"]
    label = re.split(r":", cluster["label"])[0]
    return sorted({*re.split(r"\s+&\s+", label), *(cluster.get("entity_names") or {}).values()})


def template_for(package: dict[str, Any]) -> str:
    topics = package["cluster"].get("topics") or []
    return next((name for topic, name in BY_TOPIC if topic in topics), "press")


def real_stat(package: dict[str, Any]) -> dict[str, Any] | None:
    """The first whole-number season stat, so the reality beat can be checked against the package."""
    stats = (package["cluster"].get("enrichment") or {}).get("stats") or []
    for unit in UNITS:
        for s in stats:
            if s["label"].endswith(f"{unit} this season") and isinstance(s["value"], int) and s["value"] > 0:
                return {**s, "unit": unit}
    return None


def stat_claim(s: dict[str, Any]) -> dict[str, Any]:
    return {"id": "c001", "label": s["label"], "value": s["value"], "display": str(s["value"]),
            "spoken": int_words(s["value"]), "format": "int", "source": s["source"], "as_of": s.get("as_of")}


def beats_for(fmt: str, package: dict[str, Any], custom: list | None = None) -> tuple[list, list[dict], str]:
    subject = subject_of(package)
    name = "custom" if custom else "what_if" if fmt == "parallel_universe" else template_for(package)
    body = custom or [(t.replace("<subject>", subject), copy.deepcopy(s)) for t, s in TEMPLATES[name]]
    label = LABELS[fmt]
    title = package["idea"]["working_title"]
    opening = {"type": "toon", "set": "studio", "title": label, "cast": [{"who": "fan", "x": 540}]}
    beats = [(OPEN[fmt], opening), *body]
    claims: list[dict] = []
    stat = real_stat(package)
    if stat:
        claims = [stat_claim(stat)]
        who = stat["label"].rsplit(f" {stat['unit']} this season", 1)[0]
        beats.append((f"Back in reality: {{{{c001}}}} {stat['unit']} this season for {who}.",
                      {"type": "fact", "value": claims[0]["display"], "label": stat["unit"].upper() + " THIS SEASON", "at": "reality"}))
    beats.append((CLOSE[fmt], {"type": "cta", "text": "FOLLOW", "sub": "STILL NOT REAL"}))
    return [(t, {**s, "badge": label}, []) for t, s in beats], claims, title
