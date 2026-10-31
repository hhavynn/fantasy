"""Soccer video formats and idea assembly.

Three formats, none of which needs a player's stat line: a labelled cartoon parody of the week's event,
a parallel-universe what-if, and an "every X explained" explainer. Parody ideas carry the content rules
in their notes so they reach the script stage. Scripts are written later, never here.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from social_intel.contract import SourcedStat, VideoIdea
from social_intel.ideas import HookRotation, social_stats, why_trending

FORMAT_LABEL = {
    "parody": "Labelled cartoon parody of the week's event",
    "parallel_universe": "Parallel-universe what-if",
    "explainer": "Every X explained",
}
PARODY_TOPICS = {"award", "rivalry", "transfer", "crisis", "title_race", "manager", "meme"}
WHAT_IF_TOPICS = {"transfer", "rivalry", "title_race", "relegation"}
EXPLAINER_TOPICS = {"rules", "refereeing", "tactics"}
TIMELY_TOPICS = {"award", "rivalry", "manager", "crisis"}
CONTENT_RULES = ("Original cartoon characters only: no face-swaps, cloned voices, match footage or club crests. "
                 "Label it as parody on screen and aloud. Invented lines must be obviously absurd, never a real quote.")


def choose_formats(c: dict[str, Any]) -> list[str]:
    topics = set(c.get("topics") or [])
    out = []
    if topics & PARODY_TOPICS:
        out.append("parody")
    if topics & WHAT_IF_TOPICS:
        out.append("parallel_universe")
    if topics & EXPLAINER_TOPICS:
        out.append("explainer")
    return out or ["parody"]


def _subject(c: dict[str, Any]) -> str:
    return c["label"].split(":")[0]


def build_idea(c: dict[str, Any], fmt: str, rot: HookRotation, window, meme_bank: dict[str, list[str]],
               now: datetime) -> VideoIdea:
    who = _subject(c)
    topic = (c.get("topics") or ["this"])[0].replace("_", " ")
    if fmt == "parallel_universe":
        title, angle = f"The universe where {who} went differently", "Change one real decision and follow it through."
        hooks = {"fmt": f"In another universe, {who} went the other way.",
                 "turn": f"One decision changes all of this for {who}.",
                 "ask": f"What if {who} never happened?"}
        graphics, duration = ["split-screen timeline: what happened vs what could have"], 45
    elif fmt == "explainer":
        title, angle = f"Every part of the {topic} row explained", "Teach the one rule or idea behind this week's argument."
        hooks = {"fmt": f"Everyone's arguing about {who}. Here's the rule behind it.",
                 "turn": f"{topic.capitalize()}, explained before the next match.",
                 "ask": f"Think you understand the {who} call?"}
        graphics, duration = ["step-by-step diagram on a plain pitch"], 40
    else:
        title, angle = f"What really happened with {who} (parody)", "Absurd cartoon reenactment of the week's moment."
        hooks = {"fmt": f"A totally fictional cartoon about {who}.",
                 "turn": f"Nobody asked for the {who} cartoon. Here it is.",
                 "ask": f"This is a parody. The {who} story isn't."}
        graphics, duration = ["cartoon scene with a visible parody label"], 35
    picks = rot.pick(hooks, 3, lead="fmt")
    fillers = [f"{who}: {topic}, and the part nobody's drawn yet.", f"The {topic} story around {who}, in under a minute.",
               f"{who}. {topic.capitalize()}. Here's the short version."]
    picks += [f for f in fillers if f not in picks][: 3 - len(picks)]  # rotation is spent when a subject repeats
    links = [tp["url"] for tp in (c.get("top_posts") or []) if tp.get("url")][:6]
    enr = c.get("enrichment") or {}
    stats = [SourcedStat(s["label"], s["value"], s["source"], s.get("as_of")) for s in enr.get("stats") or []]
    expires = window.expires if set(c.get("topics") or []) & TIMELY_TOPICS else now + timedelta(days=7)
    rel = f"until {window.label}" if expires == window.expires else f"about a week (until {expires:%b %d})"
    flags = (c.get("score") or {}).get("flags") or []
    notes = [CONTENT_RULES] if fmt == "parody" else []
    if "unconfirmed_rumor" in flags:
        notes.append("Rumour language without confirmation: verify with an official source before scripting.")
    conf = 0.25 + 0.25 * bool(stats) + 0.2 * any(tp.get("platform") == "news" for tp in c.get("top_posts") or [])
    key = f"{c.get('topic_id', c['cluster_id'])}|{fmt}|{now:%Y-%m-%d}"
    return VideoIdea(
        idea_id=hashlib.sha1(key.encode()).hexdigest()[:10], cluster_id=c["cluster_id"], sport=c["sport"], format=fmt,
        working_title=title, hooks=picks, why_trending=why_trending(c), source_links=links,
        key_stats=(social_stats(c) + stats)[:12], supporting_args=[], opposing_args=[], editorial_angle=angle,
        entities=c.get("entities") or {}, duration_s=duration, graphics=graphics,
        memes=(meme_bank.get(fmt) or [])[:2] + (meme_bank.get("sfx") or [])[:2],
        engagement_question="Where do you land on this?", trend_score=(c.get("score") or {}).get("total", 0.0),
        research_confidence=round(conf * (0.6 if "unconfirmed_rumor" in flags else 1), 2), relevance_window=rel,
        relevance_expires=expires, notes=notes)
