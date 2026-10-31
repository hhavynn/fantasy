"""Soccer topic tagging and how much each topic matters to a short-form soccer audience."""

from __future__ import annotations

import re

TOPIC_PATTERNS: dict[str, str] = {
    "transfer": r"\btransfers?\b|\bsign(ed|ing|s)?\b|\bfee\b|\bhere we go\b|\bloan(ed)?\b|\bbid\b|\bmedical\b|"
                r"\brelease clause\b|\bdeadline day\b",
    "injury": r"\binjur|\bhamstring|\bacl\b|\bruled out\b|\bsidelined\b|\bmuscle problem\b|\bknock\b",
    "tactics": r"\bformation|\bpress(ing)?\b|\btactic|\bxg\b|\bback (three|four|five)\b|\bfalse nine\b|\binverted\b",
    "form": r"\bform\b|\bunbeaten|\bwinless|\bstreak|\bin a row\b|\bwinning run\b",
    "refereeing": r"\bvar\b|\breferee|\bpenalty|\bred card|\boffside|\bhandball",
    "rules": r"\boffside\b|\bhandball\b|\bextra time\b|\bpenalty shootout\b|\baway goals?\b|\bdouble jeopardy\b|"
             r"\bfive subs\b|\brule change\b",
    "title_race": r"\btitle race\b|\btitle challenge\b|\bleague leaders\b|\bchampions? elect\b|\btop of the (table|league)\b",
    "relegation": r"\brelegat|\bdrop zone\b|\bbottom of the (table|league)\b|\bgreat escape\b",
    "award": r"\bballon d.?or\b|\bgolden boot\b|\bgolden ball\b|\bplayer of the (year|season)\b|\bpuskas\b|\bbest fifa\b",
    "rivalry": r"\bclasico\b|\bclásico\b|\bderby\b|\brivalry\b|\bder klassiker\b|\bsuperclasico\b",
    "manager": r"\bsacked\b|\bfired\b|\bnew manager\b|\bhead coach\b|\bmanagerial\b|\bresign(s|ed)?\b",
    "crisis": r"\bcrisis\b|\bdisaster\b|\bembarrass|\bcollapse|\bhumiliat|\bfinished\b|\bwashed\b|\bbottled?\b",
    "rise": r"\bworld class\b|\bmasterclass\b|\bbest in the world\b|\bgenerational\b|\bwonderkid\b|\bhat[- ]trick\b",
    "serious": r"\bdied\b|\bdeath\b|\bpasses away\b|\bpassed away\b|\btragedy\b|\bracis|\babuse|\bassault|\bhospital|"
               r"\bmental health\b|\bsuicide\b|\bcrash\b|\barrested\b|\bcharged with\b",
    "meme": r"\blmao|\blol\b|\bmeme|\bcooked\b|\bbro\b|\bgoat\b|\bcope\b|\bbanter\b|\bratio\b",
}
_COMPILED = {k: re.compile(v, re.I) for k, v in TOPIC_PATTERNS.items()}

TOPIC_WEIGHTS: dict[str, float] = {
    "award": 1.0, "rivalry": 1.0, "transfer": 0.95, "crisis": 0.85, "title_race": 0.8, "manager": 0.8,
    "rise": 0.75, "relegation": 0.7, "refereeing": 0.7, "injury": 0.7, "form": 0.65, "rules": 0.65,
    "tactics": 0.6, "meme": 0.6,
}

SOCCER_CONTEXT = re.compile(
    r"\bsoccer\b|\bfootball\b|\bfutbol\b|\bfútbol\b|\bpremier league\b|\bchampions league\b|\bucl\b|\beuropa league\b|"
    r"\bla liga\b|\bbundesliga\b|\bserie a\b|\bligue 1\b|\bmls\b|\bfa cup\b|\btransfer|\bmanager\b|\bstriker\b|"
    r"\bmidfielder\b|\bdefender\b|\bgoalkeeper\b|\bkeeper\b|\bwinger\b|\bderby\b|\bpitch\b|\bxg\b|\bvar\b|"
    r"\bballon d.?or\b|\bmatchday\b|\bgoals?\b|\bclean sheet\b|\bkit\b|\bfc\b",
    re.I)


def tag_topics(text: str) -> list[str]:
    return [k for k, rx in _COMPILED.items() if rx.search(text)]
