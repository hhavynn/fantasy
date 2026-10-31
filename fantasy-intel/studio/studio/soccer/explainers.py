"""The "every X explained" library: deterministic scripts, each checked against the IFAB Laws of the Game.

No LLM: the wording here is what gets narrated. Numbers are claims (``{{c001}}``), so the narration gate
applies unchanged. Pitch coordinates run 0-1: x across the pitch, y from the goal line (0) to halfway (1).
Every pitch figure is an anonymous dot: no kits, crests or real players.
"""

from __future__ import annotations

from typing import Any

from ..numbers import int_words

CTA = "Follow for every rule, explained."
LAW_11 = "IFAB Laws of the Game, Law 11 (Offside)"
LAW_12 = "IFAB Laws of the Game, Law 12 (Fouls and Misconduct)"
VAR_PROTOCOL = "IFAB Laws of the Game, Video Assistant Referee protocol"


def count(cid: str, label: str, value: int, source: str) -> dict[str, Any]:
    return {"id": cid, "label": label, "value": value, "display": str(value), "spoken": int_words(value),
            "format": "int", "source": source}


def word(cid: str, label: str, value: str, source: str) -> dict[str, Any]:
    return {"id": cid, "label": label, "value": value, "display": value, "spoken": value, "format": "text",
            "source": source}


ATTACK = [{"id": "passer", "team": "a", "x": 0.42, "y": 0.62}, {"id": "runner", "team": "a", "x": 0.68, "y": 0.26}]
DEFEND = [{"id": "keeper", "team": "k", "x": 0.5, "y": 0.05}, {"id": "last", "team": "d", "x": 0.34, "y": 0.34},
          {"id": "next", "team": "d", "x": 0.6, "y": 0.42}]
OFFSIDE_PITCH = {"type": "pitch", "players": ATTACK + DEFEND, "ball": [0.42, 0.62], "line": 0.34}
LEVEL = [{**p, "y": 0.34} if p["id"] == "runner" else p for p in ATTACK] + DEFEND

EXPLAINERS: dict[str, dict[str, Any]] = {
    "offside": {
        "title": "Every part of the offside rule explained",
        "kicker": "OFFSIDE", "topics": ("offside",), "sources": [LAW_11], "hook": "argument", "ending": "follow",
        "claims": [word("c001", "defender counted for the offside line", "second", LAW_11)],
        "beats": [
            ("Offside starts more arguments than any other rule. Here's all of it.",
             {"type": "title", "text": "OFFSIDE", "sub": "EVERY RULE EXPLAINED"}, []),
            ("You're offside if you're closer to goal than the ball and the {{c001}}-last defender when a teammate plays it.",
             {**OFFSIDE_PITCH, "line_at": "defender", "mark": {"id": "runner", "at": "closer"}}, ["offside"]),
            ("The keeper usually counts as one of those defenders.",
             {**OFFSIDE_PITCH, "mark": {"id": "keeper", "at": "keeper"}}, ["keeper"]),
            ("It's judged at the moment of the pass, not when you receive it.",
             {**OFFSIDE_PITCH, "ball_to": {"at": "pass", "to": [0.68, 0.26]}}, ["pass"]),
            ("Head, body and feet count. Hands and arms don't.",
             {"type": "columns", "left": "COUNT", "left_items": ["HEAD", "BODY", "FEET"], "left_at": "head",
              "right": "DON'T", "right_items": ["HANDS", "ARMS"], "right_at": "hands"}, ["count"]),
            ("Level with that defender is onside.",
             {"type": "pitch", "players": LEVEL, "ball": [0.42, 0.62], "line": 0.34,
              "verdict": {"at": "onside", "text": "ONSIDE", "good": True}}, ["onside"]),
            ("You can't be offside in your own half, or straight from a goal kick, throw-in or corner.",
             {"type": "stack", "items": ["OWN HALF", "GOAL KICK", "THROW-IN", "CORNER"],
              "at": ["half", "goal", "throw", "corner"], "head": "NEVER OFFSIDE"}, []),
            ("And standing offside isn't an offence until you get involved in the play.",
             {**OFFSIDE_PITCH, "verdict": {"at": "involved", "text": "INVOLVED?", "good": False}}, ["involved"]),
        ],
    },
    "cards": {
        "title": "Every card in football explained",
        "kicker": "CARDS", "topics": ("red card", "yellow card", "sent off"), "sources": [LAW_12], "hook": "forgotten",
        "ending": "follow",
        "claims": [count("c001", "yellow cards in one match that make a red", 2, LAW_12)],
        "beats": [
            ("Yellow, red, and the bit people forget about both.",
             {"type": "title", "text": "CARDS", "sub": "EVERY RULE EXPLAINED"}, []),
            ("A yellow card is a caution. You stay on, but the referee has noted it.",
             {"type": "card", "colors": ["yellow"], "label": "CAUTION"}, ["caution"]),
            ("Get {{c001}} yellows in one match and that's a red.",
             {"type": "card", "colors": ["yellow", "yellow", "red"], "at": ["yellows", "match", "red"], "label": "SENT OFF"},
             ["red"]),
            ("A red card means you're sent off, and your team can't replace you.",
             {"type": "card", "colors": ["red"], "label": "NO REPLACEMENT", "label_at": "replace"}, ["replace"]),
            ("Reds are for things like serious foul play, violent conduct or stopping an obvious goal-scoring chance.",
             {"type": "stack", "items": ["SERIOUS FOUL PLAY", "VIOLENT CONDUCT", "OBVIOUS CHANCE STOPPED"],
              "at": ["serious", "violent", "stopping"], "head": "STRAIGHT RED"}, []),
            ("Managers and coaching staff can be shown cards too.",
             {"type": "card", "colors": ["yellow", "red"], "label": "STAFF TOO", "label_at": "staff"}, ["staff"]),
        ],
    },
    "var": {
        "title": "What VAR can actually do, explained",
        "kicker": "VAR", "topics": ("var", "video review", "referee"), "sources": [VAR_PROTOCOL], "hook": "limit",
        "ending": "follow",
        "claims": [count("c001", "match-changing situations VAR reviews", 4, VAR_PROTOCOL)],
        "beats": [
            ("VAR can't check everything. Here's what it's actually allowed to touch.",
             {"type": "title", "text": "VAR", "sub": "EVERY RULE EXPLAINED"}, []),
            ("It only steps in for {{c001}} things: goals, penalty decisions, direct red cards and mistaken identity.",
             {"type": "stack", "items": ["GOALS", "PENALTIES", "DIRECT REDS", "MISTAKEN IDENTITY"],
              "at": ["goals", "penalty", "direct", "mistaken"], "head": "ONLY THESE"}, []),
            ("And only for a clear and obvious error, or a serious missed incident.",
             {"type": "columns", "left": "CLEAR", "left_items": ["OBVIOUS ERROR"], "left_at": "clear",
              "right": "MISSED", "right_items": ["SERIOUS INCIDENT"], "right_at": "serious"}, ["clear"]),
            ("The video team can't overrule anyone. They advise the referee.",
             {"type": "stamp", "text": "ADVICE ONLY", "at": "advise"}, ["advise"]),
            ("The referee makes the final call, often after checking the screen at the side of the pitch.",
             {"type": "stamp", "text": "REFEREE DECIDES", "at": "final"}, ["referee"]),
        ],
    },
}


def for_text(text: str) -> str | None:
    """The explainer a trend's text is about, by its topic words."""
    low = text.lower()
    return next((k for k, e in EXPLAINERS.items() if any(t in low for t in e["topics"])), None)
