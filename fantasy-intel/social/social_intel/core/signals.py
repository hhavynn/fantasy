"""Sport-agnostic text signals: rumour language, sarcasm, debate/contrarian framing, questions.

Deliberately simple and inspectable (regex cues with counts) so every flag on a trend can be
explained in one line. They are cues for ranking and editorial caution, not truth judgements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RUMOR = re.compile(
    r"\brumou?r|\breportedly\b|\bhearing\b|\bsources? (say|tell|indicate)|\bper sources\b|\bunconfirmed\b|"
    r"\bcould be\b|\bmight be\b|\bspeculat|\bbuzz\b|\bwhispers?\b|\bi'?ve heard\b|\bif true\b|\bnot confirmed\b",
    re.I)
CONFIRMED = re.compile(r"\bofficially\b|\bannounced\b|\bconfirmed\b|\bhas been placed\b|\bruled out\b|\bsigns?\b", re.I)
SARCASM = re.compile(
    r"/s\b|\byeah right\b|\bsure,? (jan|buddy)\b|\btotally\b.*\bnot\b|\bwhat could go wrong\b|"
    r"\bsurely\b|\bclearly the (best|goat)\b|\bnever been better\b|🙄|🤡|\blmao\b|\bclown\b",
    re.I)
DEBATE = re.compile(
    r"\boverrated\b|\bunderrated\b|\bhot take\b|\bunpopular opinion\b|\bchange my mind\b|\bfight me\b|"
    r"\bam i crazy\b|\bthoughts\?|\bdebate\b|\bvs\.?\b|\bor\b.*\?|\bwrong about\b|\bnobody is talking\b|"
    r"\beveryone is (sleeping|wrong|overreacting)\b|\bfraud\b|\bbust\b|\bleague[- ]winner\b|\bsell\b|\bbuy\b|"
    r"\bpanic\b|\boverreact|\bis (he|she|it) (real|for real|legit)\b|\bshould i\b",
    re.I)
CONTRARIAN = re.compile(
    r"\beveryone\b|\bnobody\b|\bpeople are\b|\bthe internet\b|\btwitter is\b|\breddit is\b|\boverreact|"
    r"\bunpopular\b|\bhot take\b|\bactually\b|\bslept on\b|\bsleeping on\b",
    re.I)
QUESTION = re.compile(r"\?\s*$|\?\s", re.M)
HYPE = re.compile(r"\bbreaking\b|\bjust in\b|\bhuge\b|\bmassive\b|\bwow\b|🚨|🔥|‼️|\bholy\b", re.I)


@dataclass
class TextSignals:
    rumor: bool
    confirmed: bool
    sarcasm: bool
    debate: bool
    contrarian: bool
    question: bool
    hype: bool


def text_signals(text: str) -> TextSignals:
    return TextSignals(
        rumor=bool(RUMOR.search(text)),
        confirmed=bool(CONFIRMED.search(text)),
        sarcasm=bool(SARCASM.search(text)),
        debate=bool(DEBATE.search(text)),
        contrarian=bool(CONTRARIAN.search(text)),
        question=bool(QUESTION.search(text)),
        hype=bool(HYPE.search(text)),
    )
