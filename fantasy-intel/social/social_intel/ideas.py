"""Turn ranked trend clusters into video ideas.

Deterministic, like the studio's Shorts scripts: every number in a title, hook or argument comes
from a sourced stat (our analytics or a measured social count), so nothing is invented. The
creative variety comes from (a) choosing formats from what the data and the crowd disagree or agree
about, and (b) rotating hook families least-recently-used across all ideas, so the openings never
read like one template.

Social opinion is always attributed ("r/fantasyfootball thinks...", "Sleeper managers are adding
the player...") and never stated as fact.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from social_intel.contract import SourcedStat, VideoIdea, utcnow

BANNED = re.compile(
    r"\blet'?s dive in\b|\bgame[- ]changer\b|\bin the world of\b|\bbuckle up\b|\bwithout further ado\b|"
    r"\bunleash|\bdelve\b|\btapestry\b|\bit'?s important to note\b|\bstay tuned\b|\bsmash that like\b|"
    r"\bin today'?s video\b|\bhey guys\b|\bnavigat(e|ing) the\b",
    re.I)

FORMAT_LABEL = {
    "internet_vs_data": "The internet thinks X, our data says Y",
    "contrarian_breakdown": "Contrarian statistical breakdown",
    "opinion_vs_numbers": "Popular opinion vs. the actual numbers",
    "buy_low_sell_high": "Buy-low / sell-high argument",
    "player_vs_player": "Player-vs-player debate",
    "matchup_warning": "Fantasy matchup warning",
    "breakout_prediction": "Breakout player prediction",
    "meme_argument": "Meme-driven NFL argument",
    "overrated_underrated": "Overrated / underrated",
    "injury_ripple": "Injury ripple-effect breakdown",
}
SHORT_TOPICS = {"start_sit", "injury", "matchup", "waivers", "breakout", "usage"}


def pct(x: float | None) -> str:
    return "?" if x is None else f"{x * 100:.0f}%"


def num(x: float | None, d: int = 1) -> str:
    return "?" if x is None else f"{x:.{d}f}".rstrip("0").rstrip(".") if d else f"{x:.0f}"


def is_share(metric: str) -> bool:
    return metric in ("offense_pct", "target_share", "backfield_rush_share", "air_yards_share")


def fmt_metric(metric: str, v: float | None) -> str:
    return pct(v) if is_share(metric) else num(v)


PLURAL_METRICS = {"fantasy points", "expected fantasy points", "targets", "carries"}
METRIC_WORD = {"offense_pct": "snap share", "target_share": "target share", "backfield_rush_share": "share of the RB carries",
               "fantasy_points_ppr": "fantasy points", "xfp": "expected fantasy points", "targets": "targets",
               "carries": "carries"}


@dataclass
class Lead:
    """The lead player's facts, with helpers that always carry a source."""

    p: dict[str, Any]

    @property
    def name(self) -> str:
        return self.p["name"]

    @property
    def last(self) -> str:
        parts = self.name.split()
        return parts[-1] if parts[-1].lower().rstrip(".") not in {"jr", "sr", "ii", "iii", "iv"} else parts[-2]

    def trend(self, metric: str | None = None) -> tuple[str, dict] | None:
        tr = self.p.get("trend") or {}
        m = metric or self.p.get("lead_metric") or "offense_pct"
        if m in tr:
            return m, tr[m]
        for alt in ("offense_pct", "target_share", "backfield_rush_share", "xfp"):
            if alt in tr:
                return alt, tr[alt]
        return None

    def stats(self) -> list[dict[str, Any]]:
        return self.p.get("stats") or []

    def stat(self, label_start: str) -> dict[str, Any] | None:
        return next((s for s in self.stats() if s["label"].startswith(label_start)), None)


def _weeks(ws: list[int]) -> str:
    return f"Week {ws[0]}" if len(ws) == 1 else f"Weeks {ws[0]}-{ws[-1]}"


class HookRotation:
    """Least-recently-used hook families across the whole batch, never the same sentence twice."""

    def __init__(self, history: Counter[str] | None = None):
        self.used: Counter[str] = Counter(history or {})
        self.texts: set[str] = set()

    def pick(self, candidates: dict[str, str], n: int = 3, lead: str | None = None) -> list[str]:
        clean = {k: v for k, v in candidates.items() if v and not BANNED.search(v) and v not in self.texts}
        chosen: list[str] = []
        if lead and lead in clean:  # the format's own opening goes first
            chosen.append(lead)
        chosen += [k for k in sorted(clean, key=lambda k: (self.used[k], k)) if k not in chosen][: n - len(chosen)]
        for k in chosen:
            self.used[k] += 1
            self.texts.add(clean[k])
        return [clean[k] for k in chosen]


def availability(p: dict[str, Any], upcoming_week: int | None, cues: dict[str, Any] | None = None) -> str:
    """out | uncertain | active, from the official report for the upcoming week and roster status. When
    reporters say the player is back and our tables still say out, the answer is 'uncertain' (verify)."""
    reported_back = bool(cues) and cues.get("return", 0) > cues.get("out", 0)
    if p.get("roster_status") == "RES":
        return "uncertain" if reported_back else "out"
    inj = p.get("injury") or {}
    if upcoming_week and inj.get("week") != upcoming_week:
        return "active"  # last week's designation says nothing about this week
    st = (inj.get("report_status") or "").lower()
    if st == "out":
        return "uncertain" if reported_back else "out"
    return "uncertain" if st in ("doubtful", "questionable") else "active"


POS_GROUP = {"QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE", "K": "K"}


def rival(c: dict[str, Any], allow_out_lead: bool = False) -> dict[str, Any] | None:
    """A second player worth a head-to-head: same position group, available, with usage data. With the
    lead out, the candidate is a same-team replacement instead (the backup everyone is adding)."""
    enr = c.get("enrichment") or {}
    players = enr.get("players") or []
    if len(players) < 2:
        return None
    a, week = players[0], enr.get("upcoming_week")
    for b in players[1:]:
        if POS_GROUP.get(b.get("position")) != POS_GROUP.get(a.get("position")) or availability(b, week) == "out":
            continue
        if allow_out_lead:
            if b.get("team") == a.get("team"):
                return b
        elif a.get("trend") and b.get("trend") and availability(a, week) != "out":
            return b
    return None


def replacement(c: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """(name, evidence, source) of who takes the lead's work: a same-team, same-position player the
    crowd is discussing (and Sleeper adding), else the teammate with the biggest recent share."""
    enr = c.get("enrichment") or {}
    players = enr.get("players") or []
    if not players:
        return "", None, None
    a = players[0]
    b = rival(c, allow_out_lead=True)
    if b:
        adds = next((tp for tp in c.get("top_posts") or [] if tp.get("platform") == "sleeper"
                     and tp.get("subject") == b["name"] and tp.get("direction") == "add"), None)
        if adds:
            return b["name"], f"added in {adds['engagement']['likes']:,} Sleeper leagues in 24 hours", adds["url"]
        return b["name"], None, None
    mates = [m for m in a.get("teammates") or [] if (m.get("recent") or 0) >= 0.05]
    if mates:
        m = mates[0]
        return m["name"], f"{pct(m['recent'])} {m['metric']} over the last two weeks", m["source"]
    return "", None, None


def choose_formats(c: dict[str, Any]) -> list[str]:
    enr = c.get("enrichment") or {}
    topics = set(c.get("topics") or [])
    verdicts = enr.get("verdicts") or []
    rel = verdicts[0]["relation"] if verdicts else None
    comps = {x["name"]: x["value"] for x in (c.get("score") or {}).get("components", [])}
    players = enr.get("players") or []
    has_trend = bool(players and players[0].get("trend"))
    lead_status = availability(players[0], enr.get("upcoming_week"), enr.get("status_cues")) if players else None
    if lead_status == "out":
        # nobody needs a take on a player who isn't playing: the story is who replaces him
        return ["injury_ripple"]
    cues = enr.get("status_cues") or {}
    reported_back = cues.get("return", 0) > cues.get("out", 0)
    returning = bool(players) and ((bool(players[0].get("out_weeks")) and lead_status == "active")
                                   or (reported_back and lead_status in ("uncertain", "active")))
    out: list[str] = []
    if rel == "contradicts":
        out += ["internet_vs_data", "contrarian_breakdown"]
    if "injury" in topics and players and (returning or lead_status == "uncertain"):
        out.append("injury_ripple")
    if topics & {"trade", "buy_low", "sell_high"} and has_trend:
        out.append("buy_low_sell_high")
    if rel == "supports" and enr.get("social_direction") == "positive":
        out.append("breakout_prediction")
    if rival(c):
        out.append("player_vs_player")
    if "matchup" in topics and players and players[0].get("next_game"):
        out.append("matchup_warning")
    if "bust" in topics and has_trend:
        out.append("overrated_underrated")
    if comps.get("meme_potential", 0) >= 0.45 or "meme" in topics:
        out.append("meme_argument")
    lead_dir = players[0].get("data_direction") if players else None
    if (has_trend and lead_dir == "up" and enr.get("social_direction") != "negative"
            and availability(players[0], enr.get("upcoming_week")) == "active"):
        out.append("breakout_prediction")  # the role is growing and nobody is arguing against it
    if has_trend or not out:
        out.append("opinion_vs_numbers")  # without a measured trend this angle has no numbers to show
    return list(dict.fromkeys(out))[:3]


def social_stats(c: dict[str, Any]) -> list[SourcedStat]:
    out = []
    for tp in c.get("top_posts") or []:
        if tp.get("platform") == "sleeper" and tp["engagement"].get("likes"):
            verb = "added" if tp.get("direction") == "add" else "dropped"
            out.append(SourcedStat(f"{tp.get('subject') or 'player'} {verb} in Sleeper leagues (24h)", tp["engagement"]["likes"],
                                   tp["url"], tp.get("created_at")))
        elif tp.get("comment_rate_per_h"):
            out.append(SourcedStat(f"fresh comments per hour on the {tp.get('community') or tp['platform']} thread",
                                   tp["comment_rate_per_h"], tp["url"] + " (counted via Arctic Shift archive)",
                                   tp.get("created_at")))
        elif tp["engagement"] and not tp.get("attention_only"):
            e = tp["engagement"]
            key = "views" if "views" in e and tp["platform"] in ("youtube", "tiktok") else "likes" if "likes" in e else next(iter(e))
            who = f"{tp['author']}'s" if tp.get("author") else "the top"
            out.append(SourcedStat(f"{key} on {who} {tp['platform']} post", e[key], tp["url"], tp.get("created_at")))
    return out[:4]


def why_trending(c: dict[str, Any]) -> str:
    parts = []
    comps = {x["name"]: x for x in (c.get("score") or {}).get("components", [])}
    for tp in (c.get("top_posts") or [])[:4]:
        if tp.get("platform") == "sleeper" and tp["engagement"].get("likes"):
            verb = "added" if tp.get("direction") == "add" else "dropped"
            parts.append(f"{tp.get('subject') or 'player'} {verb} in {tp['engagement']['likes']:,} Sleeper leagues (24h)")
        elif tp.get("comment_rate_per_h"):
            parts.append(f"{tp['community']} thread drawing {tp['comment_rate_per_h']:.0f} comments/hour: \"{tp['text'][:80]}\"")
    if "cross_platform" in comps:
        parts.append(comps["cross_platform"]["detail"])
    if "velocity" in comps:
        parts.append("momentum " + comps["velocity"]["detail"])
    return "; ".join(parts[:4]) or (c.get("score") or {}).get("explanation", "")


def build_idea(c: dict[str, Any], fmt: str, rot: HookRotation, window, meme_bank: dict[str, list[str]],
               now: datetime) -> VideoIdea:
    enr = c.get("enrichment") or {}
    players = enr.get("players") or []
    topics = c.get("topics") or []
    names = c.get("entity_names") or {}
    lead = Lead(players[0]) if players else None
    label = c["label"]
    stance = c.get("stance") or {}
    claims = stance.get("claims") or []
    crowd = _crowd(c)
    verdict = (enr.get("verdicts") or [{}])[0]
    key_stats: list[SourcedStat] = []
    supporting, opposing, graphics, notes = [], [], [], []
    hooks: dict[str, str] = {}
    title, angle, question = label, "", "Where do you land on this?"
    duration = 40

    if lead:
        t = lead.trend()
        key_stats += [SourcedStat(s["label"], s["value"], s["source"], s.get("as_of")) for s in lead.stats()
                      if s["value"] not in (0, 0.0)][:8]  # a QB's 0% target share is a true number and useless
        inj = lead.p.get("injury")
        proj = lead.p.get("projection")
        nxt = lead.p.get("next_game")
        m, tr = t if t else (None, None)
        mword = METRIC_WORD.get(m or "", (m or "").replace("_", " ")) if m else "usage"
        says = "say" if mword in PLURAL_METRICS else "says"
        status = availability(lead.p, enr.get("upcoming_week"), enr.get("status_cues"))
        cues = enr.get("status_cues") or {}
        if status == "uncertain" and cues.get("return", 0) > cues.get("out", 0) and (
                lead.p.get("roster_status") == "RES" or ((lead.p.get("injury") or {}).get("report_status") or "").lower() == "out"):
            notes.append("Reports say " + lead.name + " is back (" + ", ".join(cues.get("return_links", [])[:2]) +
                         ") but our roster/injury tables still list him out: confirm the status before scripting."
                         .replace(" him ", " the player as "))
        if tr:
            up = tr["delta"] > 0
            r, b = fmt_metric(m, tr["recent"]), fmt_metric(m, tr["baseline"])
            move = f"{b} to {r}"
            hooks["number"] = f"{r}. That's {lead.name}'s {mword} lately. It was {b}."
            hooks["receipt"] = f"{_weeks(tr['baseline_weeks'])}: {b} {mword}. {_weeks(tr['recent_weeks'])}: {r}."
            supporting.append(f"{lead.name}'s {mword} went from {move} ({_weeks(tr['baseline_weeks'])} vs "
                              f"{_weeks(tr['recent_weeks'])}).")
            graphics.append(f"weekly {mword} bars, {_weeks(tr['baseline_weeks'] + tr['recent_weeks'])}, recent weeks highlighted")
            if len(tr["recent_weeks"]) < 3:
                opposing.append(f"Two games is a small sample: the {move} move rests on {_weeks(tr['recent_weeks'])}.")
        else:
            up, move = None, ""
        if proj and proj.get("points") is not None:
            rng = f" (80% range {num(proj['low'])}-{num(proj['high'])})" if proj.get("low") is not None else ""
            supporting.append(f"Our Week {proj['week']} projection: {num(proj['points'])} PPR points{rng}.")
            graphics.append("projection range bar with the median marked")
            if proj.get("low") is not None and proj.get("high") is not None and proj["high"] - proj["low"] > 12:
                opposing.append(f"The range is wide ({num(proj['low'])}-{num(proj['high'])}): this is a boom/bust call, not a lock.")
        if inj and inj.get("report_status") and inj.get("week") == enr.get("upcoming_week"):
            opposing.append(f"Official Week {inj['week']} report: {inj['report_status']} ({inj.get('injury') or 'injury'}), "
                            f"practice: {inj.get('practice_status') or 'n/a'}.")
        elif lead.p.get("out_weeks"):
            notes.append(f"Missed time earlier: out/doubtful on the official report in Week(s) "
                         f"{', '.join(map(str, lead.p['out_weeks']))}; the trend weeks may include that absence.")
        sc = lead.p.get("scores") or {}
        if sc.get("value_gap_label") and sc["value_gap_label"] != "ALIGNED":
            s = lead.stat("PPR points vs expected")
            if s:
                supporting.append(f"Scoring vs expected: {num(s['value'])} PPR points per game "
                                  f"{'above' if s['value'] > 0 else 'below'} what the usage predicts ({sc['value_gap_label'].lower().replace('_', ' ')}).")
        owned = lead.stat("ESPN rostered %")
        if owned and up is not None:
            hooks["stakes"] = f"{lead.name} is on {num(owned['value'], 0)}% of ESPN rosters. " + (
                "The other managers are about to find out why." if up else "Some of them should be nervous.")
        if crowd:
            hooks["internet"] = f"{crowd}. Let's check." if not BANNED.search(crowd) else ""
        hooks["question"] = f"Is {lead.name} {'for real' if up else 'done'}, or is everyone reading this wrong?"
        if cues.get("return", 0) > cues.get("out", 0):
            hooks.pop("question", None)  # "is he done?" is the wrong question for a player coming back
        if status == "out":
            for k in ("number", "receipt", "question", "contrarian", "internet"):
                hooks.pop(k, None)  # takes on an absent player's form are noise; the story is the replacement
        elif verdict.get("relation") == "contradicts":
            hooks["contrarian"] = (f"Everyone's {'panicking about' if up else 'hyping'} {lead.last}. "
                                   f"The {mword} {says} the opposite.")
        elif up is not None:
            verb = "are" if says == "say" else "is"
            hooks["contrarian"] = f"Nobody wants to say it about {lead.last}, so I will: the {mword} {verb} {'real' if up else 'gone'}."
        if nxt:
            hooks["clock"] = f"You have until Sunday to decide on {lead.last}. Here's the one number that matters."
        if claims:
            notes.append(f"From the discussion, verbatim: \"{claims[-1]['text'][:160]}\" ({claims[-1]['url']})")

        if fmt == "internet_vs_data":
            hooks["fmt"] = f"The internet's {lead.last} take vs. the actual {mword}. Only one of them is right."
            title = f"The Internet Is {'Wrong' if verdict else 'Split'} About {lead.name}"
            angle = (f"The crowd leans {verdict.get('social', 'one way')}; our usage data says {verdict.get('data', '?')}. "
                     f"Show the take, then the {mword} ({move}).")
            question = f"Who's right about {lead.last}: the timeline or the snap counts?"
            graphics = ["screenshot-style card of the top post (text + link, no usernames)"] + graphics
        elif fmt == "contrarian_breakdown":
            hooks["fmt"] = (f"{lead.last}'s {mword} went {move}. Somehow that's not the conversation."
                            if move else f"There's one {lead.last} number nobody is talking about.")
            title = f"Everyone's Missing This About {lead.name}"
            angle = f"Lead with the number the takes ignore: {mword} {move}. Then the counter-argument, then the call."
            question = f"Am I crazy, or is {lead.last} being misread?"
        elif fmt == "breakout_prediction":
            hooks["fmt"] = f"{lead.last}'s breakout isn't coming. It's already in the {mword}."
            title = f"{lead.name}: The Breakout Is Already in the Numbers"
            angle = f"Social buzz agrees with the usage ({mword} {move}); the video's job is to say how far it goes."
            question = f"Is {lead.last} a weekly starter from here?"
        elif fmt == "injury_ripple" and (status == "active" or (status == "uncertain" and (
                lead.p.get("out_weeks") or cues.get("return", 0) > cues.get("out", 0)))):
            # the injury talk is about a return: how much of the old role comes back
            back = "is back" if status == "active" else "is trending toward a return"
            hooks["fmt"] = f"{lead.last} {back}. The question is how much of the old role comes with it."
            title = (f"{lead.name} Is Back. How Much of the Old Role Returns?" if status == "active"
                     else f"{lead.name}'s Return: How Much Work Comes Back?")
            missed = lead.p.get("out_weeks") or []
            if missed:
                supporting.append(f"Out or doubtful on the official report in {_weeks(missed) if len(missed) == 1 else 'Weeks ' + ', '.join(map(str, missed))}.")
            angle = ("Official status first (report/roster only), then the role before the injury vs. who filled it, "
                     "then a start/sit call with the ramp-up risk.")
            question = f"Starting {lead.last} right away, or one week of patience?"
            mates = lead.p.get("teammates") or []
            if mates:
                mate = mates[0]
                supporting.append(f"Who held the role meanwhile: {mate['name']}, {pct(mate['recent'])} {mate['metric']} over the "
                                  f"last two weeks ({mate['source']}).")
                key_stats.append(SourcedStat(f"{mate['name']} recent {mate['metric']}", mate["recent"], mate["source"],
                                             enr.get("data_cut")))
            graphics.append(f"role before the injury vs. the fill-in's share while {lead.last} was out")
        elif fmt == "injury_ripple":
            who, evidence, src = replacement(c)
            inj = lead.p.get("injury") or {}
            if who and evidence:
                supporting.append(f"Who takes the work: {who}, {evidence} ({src}).")
                hooks["fmt"] = f"{lead.last} {'is out' if status == 'out' else 'might not play'}. {who} was just {evidence}." \
                    if "Sleeper" in evidence else f"With {lead.last} {'out' if status == 'out' else 'iffy'}, {who} is the name: {evidence}."
            elif who:
                hooks["fmt"] = f"{lead.last} {'is out' if status == 'out' else 'might not play'}. Everyone's asking about {who}."
            else:
                hooks["fmt"] = f"{lead.last} {'is out' if status == 'out' else 'might not play'}. Here's who the work actually goes to."
            if inj.get("report_status"):
                hooks["status"] = (f"{lead.last}: {inj['report_status'].lower()} on the official Week {inj['week']} report. "
                                   f"Here's your lineup move.")
            title = (f"{lead.name} {'Is Out' if status == 'out' else 'Is Questionable'}: "
                     + (f"Is {who} the Answer?" if who else "Who Actually Gets the Work?"))
            angle = ("Status first (official report or roster move only), then who absorbs the role and how much of it, "
                     "then the pickup/start call.")
            question = f"Who are you starting in {lead.last}'s place?"
            graphics.append(f"opportunity split: {lead.last}'s role vs. {who or 'the fill-in'}")
        elif fmt == "buy_low_sell_high":
            buy = bool(up) if verdict.get("relation") != "contradicts" else not up
            title = f"{'Buy' if buy else 'Sell'} {lead.name} Now?"
            angle = f"Trade framing: the market (crowd + roster %) vs. the role ({mword} {move})."
            question = f"Would you trade for {lead.last} at today's price?"
            hooks["fmt"] = f"Trade offer: you get {lead.last}. Before you laugh, look at the {mword}."
        elif fmt == "player_vs_player" and rival(c):
            other = Lead(rival(c))
            ot = other.trend()
            title = f"{lead.name} or {other.name}?"
            if ot:
                om, otr = ot
                supporting.append(f"{other.name}: {METRIC_WORD.get(om, om)} {fmt_metric(om, otr['baseline'])} -> "
                                  f"{fmt_metric(om, otr['recent'])}.")
                key_stats += [SourcedStat(s["label"] + f" ({other.name})", s["value"], s["source"], s.get("as_of"))
                              for s in other.stats()[:4]]
            angle = "Side-by-side on role, projection range and matchup; end on one pick."
            question = f"{lead.last} or {other.last}: who's in your lineup?"
            hooks["fmt"] = f"{lead.last} or {other.last}. One of them is a trap this week."
            graphics.append("split-screen stat cards, same scale for both players")
        elif fmt == "matchup_warning" and nxt:
            ranks = lead.p.get("opponent_defense_ranks") or {}
            key = "rush_epa_allowed" if lead.p.get("position") == "RB" else "pass_epa_allowed"
            rank = ranks.get(key)
            hooks["fmt"] = f"{lead.last} against {nxt['opponent']} this week. Here's the problem."
            title = f"Week {nxt['week']} Warning: {lead.name} vs. {nxt['opponent']}"
            if rank:
                supporting.append(f"{nxt['opponent']} ranks {rank} of 32 in {key.replace('_', ' ')} (1 = allows the most).")
            if nxt.get("implied_total") is not None:
                supporting.append(f"{lead.p['team']} implied total: {num(nxt['implied_total'])} points.")
            angle = "Role is the floor, matchup is the swing: say which one wins this week."
            question = f"Starting {lead.last} against {nxt['opponent']}?"
        elif fmt == "overrated_underrated":
            hooks["fmt"] = (f"Overrated? {lead.last}'s {mword} went {move}." if move else f"Is {lead.last} overrated? Let's be honest.")
            title = f"Is {lead.name} Overrated?"
            angle = f"Put the 'bust' takes next to the role ({mword} {move}) and scoring vs. expected."
            question = f"Overrated, underrated, or exactly right on {lead.last}?"
        elif fmt == "meme_argument":
            hooks["fmt"] = f"The {lead.last} discourse has gone feral. One chart settles it."
            title = f"The {lead.name} Discourse, Explained With Numbers"
            angle = "Open on the funniest real take (attributed), then let the numbers settle it."
            question = f"Best {lead.last} take you've seen this week?"
            duration = 35
        else:  # opinion_vs_numbers
            hooks["fmt"] = f"What people say about {lead.last}, and what the {mword} {says}."
            title = f"What People Say About {lead.name} vs. What the Numbers Say"
            angle = f"Popular take first, then {mword} {move}, then whether the take survives."
            question = f"Does the {lead.last} take survive the numbers?"
    else:
        # team-level or news-led cluster without player analytics: still a lead, flagged for research
        hooks["question"] = f"{label.split(':')[0]}: overreaction or the real story?"
        hooks["internet"] = f"{crowd}. Here's what we actually know." if crowd else ""
        hooks["clock"] = "This story moved fast today. Here's what's confirmed and what isn't."
        title = f"{label.split(':')[0]}: What's Real?"
        angle = "Separate the confirmed report from the speculation; no projections until we pull the numbers."
        notes.append("No player analytics attached: research before scripting.")
        duration = 35

    if lead and status == "out":
        # a player who isn't playing: recent form and projection are not the story
        supporting = [a for a in supporting if not a.startswith((f"{lead.name}'s", "Our Week"))]
        opposing = [a for a in opposing if not a.startswith("Two games is a small sample")]
        graphics = [g for g in graphics if not g.startswith(("weekly ", "projection range"))]
    key_stats = social_stats(c) + key_stats
    picks = rot.pick(hooks, 3, lead="fmt")
    fillers = [f"{lead.last if lead else label.split(':')[0]}: here's the part nobody's putting on screen.",
               f"Thirty seconds on {lead.name if lead else label.split(':')[0]}. You'll want them before kickoff.",
               f"One chart on {lead.last if lead else label.split(':')[0]} and the comments can fight about it."]
    for f in fillers:
        if len(picks) >= 3:
            break
        if f not in rot.texts:
            rot.texts.add(f)
            picks.append(f)
    links = [tp["url"] for tp in (c.get("top_posts") or []) if tp.get("url")][:6]
    flags = (c.get("score") or {}).get("flags") or []
    # research confidence: how much of the angle our own sourced data can actually settle
    conf = 0.25
    if lead and lead.trend():
        conf += 0.25  # a measured role trend for the lead player
    if verdict.get("relation") in ("supports", "contradicts"):
        conf += 0.15  # the data speaks directly to the take
    if any(tp.get("platform") == "news" for tp in c.get("top_posts") or []):
        conf += 0.1  # a reported, linkable fact anchors the story
    if (c.get("independent_sources") or 0) >= 3:
        conf += 0.1
    if lead and lead.p.get("projection"):
        conf += 0.05
    if "unconfirmed_rumor" in flags:
        conf *= 0.6
        notes.append("Rumour language without confirmation: verify with an official source before scripting.")
    if not lead:
        conf = min(conf, 0.35)
    expires = window.expires
    if set(topics) & {"trade", "dynasty", "bust", "buy_low", "sell_high"} and not set(topics) & SHORT_TOPICS:
        expires = now + timedelta(days=7)
    rel_window = (f"until {window.label}" if expires == window.expires else f"about a week (until {expires:%b %d})")
    memes = _memes(fmt, topics, meme_bank)
    idea_key = f"{c.get('topic_id', c['cluster_id'])}|{fmt}"
    return VideoIdea(
        idea_id=hashlib.sha1(f"{idea_key}|{now:%Y-%m-%d}".encode()).hexdigest()[:10],
        cluster_id=c["cluster_id"], sport=c["sport"], format=fmt, working_title=title, hooks=picks,
        why_trending=why_trending(c), source_links=links, key_stats=key_stats[:12], supporting_args=supporting[:5],
        opposing_args=opposing[:5], editorial_angle=angle, entities=c.get("entities") or {},
        duration_s=duration if lead and set(topics) & SHORT_TOPICS else max(duration, 45),
        graphics=list(dict.fromkeys(graphics))[:5], memes=memes, engagement_question=question,
        trend_score=(c.get("score") or {}).get("total", 0.0), research_confidence=round(min(conf, 0.9), 2),
        relevance_window=rel_window, relevance_expires=expires,
        notes=notes + [f"names: {', '.join(names.values())}"] if names else notes,
    )


def _crowd(c: dict[str, Any]) -> str:
    """One attributed sentence about what people are doing/saying, built from measured counts."""
    for tp in c.get("top_posts") or []:
        if tp.get("platform") == "sleeper" and tp["engagement"].get("likes") and tp.get("subject"):
            verb = "adding" if tp.get("direction") == "add" else "dropping"
            return f"{tp['engagement']['likes']:,} Sleeper leagues are {verb} {tp['subject']} right now"
    for tp in c.get("top_posts") or []:
        if tp.get("platform") == "reddit" and tp.get("comment_rate_per_h", 0) and tp["comment_rate_per_h"] >= 10:
            return f"{tp['community']} is arguing about this at {tp['comment_rate_per_h']:.0f} comments an hour"
    return ""


def _memes(fmt: str, topics: list[str], bank: dict[str, list[str]]) -> list[str]:
    key = {"internet_vs_data": "contrarian", "contrarian_breakdown": "contrarian", "injury_ripple": "injury",
           "buy_low_sell_high": "trade", "breakout_prediction": "breakout", "overrated_underrated": "bust",
           "matchup_warning": "matchup", "meme_argument": "overreaction"}.get(fmt)
    if not key:
        key = "overreaction" if "bust" in topics else "breakout"
    picks = (bank.get(key) or [])[:2] + (bank.get("sfx") or [])[:2]
    return picks


def generate(ranking: list[dict[str, Any]], adapter, top: int = 8, per_cluster: int = 3, now: datetime | None = None,
             hook_history: Counter[str] | None = None, min_score: float = 0.0) -> list[VideoIdea]:
    now = now or utcnow()
    window = adapter.event_window(now)
    rot = HookRotation(hook_history)
    formats_for = getattr(adapter, "choose_formats", choose_formats)
    build = getattr(adapter, "build_idea", build_idea)
    ideas: list[VideoIdea] = []
    seen_lead: set[str] = set()
    for c in ranking:
        if len({i.cluster_id for i in ideas}) >= top:
            break
        if (c.get("score") or {}).get("total", 0) < min_score:
            continue
        if "already_covered" in ((c.get("score") or {}).get("flags") or []):
            continue
        lead = ((c.get("entities") or {}).get("players") or [c.get("topic_id")])[0]
        if lead in seen_lead:
            continue  # one cluster per lead player per batch: variety beats repetition
        seen_lead.add(lead)
        for fmt in formats_for(c)[:per_cluster]:
            ideas.append(build(c, fmt, rot, window, adapter.meme_bank(), now))
    return ideas
