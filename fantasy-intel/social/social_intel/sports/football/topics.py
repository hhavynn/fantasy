"""Fantasy-football topic tagging and topic weights (how much a topic matters to our audience)."""

from __future__ import annotations

import re

TOPIC_PATTERNS: dict[str, str] = {
    "waivers": r"\bwaiver|\bwire\b|\bpick ?up\b|\bfaab\b|\bstash\b|\bdrop(ped|ping)?\b|\badd(ed)?\b",
    "start_sit": r"\bstart\b|\bsit\b|\bflex\b|\bbench\b|\blineup\b|\bwho do i start\b|\bwdis\b|\bstart/sit\b",
    "trade": r"\btrade[sd]?\b|\bbuy[- ]low\b|\bsell[- ]high\b|\bfleec|\brobbed\b|\bveto\b|\baccept\b",
    "injury": r"\binjur|\bhamstring|\bankle|\bknee|\bconcussion|\bacl\b|\bir\b|\bquestionable\b|\bdoubtful\b|"
              r"\bruled out\b|\bout for\b|\blimited\b|\bdnp\b|\bmri\b|\bsprain|\bstrain|\btorn\b|\bprotocol\b",
    "breakout": r"\bbreakout|\bbreak out\b|\bleague[- ]winner|\bsmash|\bemerg|\btakeover|\bbell ?cow|\bwr1\b|\brb1\b|"
                r"\bthe guy\b|\bhype\b",
    "bust": r"\bbust|\boverrated|\bdisaster|\bdud\b|\bunstartable|\bdone\b|\bfraud|\bwashed",
    "buy_low": r"\bbuy[- ]low|\bbuy him\b|\bpanic\b|\boverreact",
    "sell_high": r"\bsell[- ]high|\bsell now\b|\bcash in\b",
    "usage": r"\bsnap[s]?\b|\bsnap share|\btarget share|\btargets?\b|\broutes?\b|\bcarries\b|\btouches\b|\bred ?zone|"
             r"\bair yards|\bopportunit|\bworkload|\brole\b|\bcommittee|\brbbc\b",
    "projection": r"\bprojection|\bprojected|\branking|\bros\b|\brest of season\b|\becr\b",
    "matchup": r"\bmatchup|\bdefense\b|\bvs\.? |\bshadow|\bcoverage\b|\bfunnel\b|\bshootout\b|\bvegas\b|\bimplied\b",
    "dynasty": r"\bdynasty|\brookie pick|\bdevy\b|\bsuperflex\b|\bkeeper\b|\bktc\b",
    "rookie": r"\brookie",
    "qb_play": r"\bqb\b|\bquarterback|\bbenched\b|\bstarter\b|\bstarting qb\b",
    "coaching": r"\bcoach|\boc\b|\bplay ?call|\bscheme\b|\bfired\b",
    "transaction": r"\bsigned\b|\breleased\b|\bwaived\b|\btraded to\b|\bacquire|\bdeadline\b|\bcontract|\bholdout|"
                   r"\bsuspend",
    "meme": r"\blmao|\blol\b|\bmeme|\bcooked\b|\bbro\b|\bgoat\b|\bwashed\b|\bdiff\b|\bcope\b|\bbozo\b|\bratio\b",
}
_COMPILED = {k: re.compile(v, re.I) for k, v in TOPIC_PATTERNS.items()}

# 0-1 fantasy relevance of each topic for a fantasy-football Shorts channel.
TOPIC_WEIGHTS: dict[str, float] = {
    "waivers": 1.0, "start_sit": 1.0, "trade": 0.95, "injury": 0.95, "breakout": 0.9, "buy_low": 0.9,
    "sell_high": 0.9, "usage": 0.9, "bust": 0.8, "projection": 0.75, "matchup": 0.75, "rookie": 0.7,
    "qb_play": 0.65, "dynasty": 0.6, "transaction": 0.6, "coaching": 0.45, "meme": 0.35,
}

NFL_CONTEXT = re.compile(
    r"\bnfl\b|\bfantasy\b|\bfootball\b|\btouchdown|\btd\b|\bqb\b|\brb\b|\bwr\b|\bte\b|\bppr\b|\bsnap|\btargets?\b|"
    r"\bweek \d+\b|\bsunday\b|\binterception|\byards\b|\bplayoffs?\b|\bsuper bowl\b|\bgridiron\b|\bendzone|"
    r"\bend zone\b|\bdraft\b",
    re.I)


def tag_topics(text: str) -> list[str]:
    return [k for k, rx in _COMPILED.items() if rx.search(text)]
