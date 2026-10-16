"""Narration-aware shot selection: what is on screen while each phrase is spoken, and why.

The planner reads an episode's script, claims and word timings and cuts each narration beat into clauses (at
sentence ends, and at commas when both halves are long enough to hold a shot). Each clause gets an intent from
its words and claims, and the intent picks the media:

  intent             first choice                                    otherwise
  dominance / hype   the player's best cleared game photo, stat slam  practice / interview photo, headshot fallback
  identity           a real photo of the player named                 headshot fallback (labelled), missing slate
  stat_hook          photo + the number as an overlay (hype, roast)   the beat's own graphic (analysis)
  stat_run           one photo per number, a hard cut on each         the beat's graphic
  comparison         the beat's graphic (the ruler, the lanes)        inset over the player's photo
  data_story         the beat's graphic                               -
  teammate_absence   a verified photo of THAT teammate                the beat's graphic
  injury_status      the availability report (source evidence)        never generic injury footage
  team_result        a team photo (labelled FILE PHOTO) + the score   the beat's graphic
  punchline          context B-roll matching the joke word (ILLUSTRATIVE), snapped on the hit; else the graphic
  verdict            the beat's graphic                               -

There is no fixed sequence: treatments weight photos against graphics differently, ties are broken by a seed
from the episode id, every asset is spent sparingly (a re-used photo gets a different crop), and a plan whose
shape matches a recent episode's is re-drawn. Every pick carries a reason; every gap (wanted vs used) is listed
so the storyboard says exactly what media is missing. Nothing is rendered here.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field

from . import media_library as ML
from .numbers import TEAM_NAMES

# ------------------------------------------------------------------------------------------ treatments

TREATMENTS = {
    # photo_bias: how strongly a photo beats the graphic when both fit; stat_overlay: numbers slam over photos;
    # max_shot: longest single shot (s) before a photo shot is split with a new crop; grade: colour look
    "commentary": {"photo_bias": 0.55, "stat_overlay": True, "max_shot": 3.2, "min_shot": 0.9, "grade": "neutral",
                   "motion": "drift", "transitions": ("cut", "dissolve"), "inset": 0.3, "presenter": 1,
                   "split_stat_runs": False, "caption": "minimal"},
    "player_hype": {"photo_bias": 0.85, "stat_overlay": True, "max_shot": 2.4, "min_shot": 0.7, "grade": "punch",
                    "motion": "push", "transitions": ("cut", "flash", "whip"), "inset": 0.5, "presenter": 0,
                    "split_stat_runs": True, "caption": "minimal"},
    "roast": {"photo_bias": 0.7, "stat_overlay": True, "max_shot": 2.8, "min_shot": 0.7, "grade": "cold",
              "motion": "snap", "transitions": ("cut", "flash"), "inset": 0.2, "presenter": 0,
              "split_stat_runs": False, "caption": "minimal"},
    "fantasy_analysis": {"photo_bias": 0.4, "stat_overlay": False, "max_shot": 3.6, "min_shot": 1.0,
                         "grade": "neutral", "motion": "drift", "transitions": ("cut", "dissolve"), "inset": 0.35,
                         "presenter": 1, "split_stat_runs": False, "caption": "minimal"},
    "injury_opportunity": {"photo_bias": 0.45, "stat_overlay": False, "max_shot": 3.4, "min_shot": 1.0,
                           "grade": "muted", "motion": "drift", "transitions": ("cut", "dissolve"), "inset": 0.3,
                           "presenter": 1, "split_stat_runs": False, "caption": "minimal"},
    "trade_debate": {"photo_bias": 0.6, "stat_overlay": True, "max_shot": 3.0, "min_shot": 0.9, "grade": "neutral",
                     "motion": "push", "transitions": ("cut", "whip"), "inset": 0.45, "presenter": 1,
                     "split_stat_runs": False, "caption": "minimal"},
}

ANGLE_TREATMENT = {"praise": "player_hype", "flame": "roast", "opportunity": "injury_opportunity",
                   "fill_in": "injury_opportunity", "vanishing_targets": "fantasy_analysis", "mystery": "commentary",
                   "trade": "trade_debate"}


def choose_treatment(angle: str, recent: list[str] | None = None) -> str:
    """The treatment that fits the story, rotated off the last two episodes' when another one also fits."""
    want = ANGLE_TREATMENT.get(angle, "fantasy_analysis")
    recent = recent or []
    if recent[-2:].count(want) == 2:
        alt = {"player_hype": "commentary", "roast": "commentary", "fantasy_analysis": "commentary",
               "injury_opportunity": "fantasy_analysis", "commentary": "fantasy_analysis",
               "trade_debate": "commentary"}[want]
        return alt
    return want


# ------------------------------------------------------------------------------------------- clauses

@dataclass
class Clause:
    segment: str
    seg_index: int
    t0: float                   # seconds within the beat
    t1: float
    tokens: list[dict]
    text: str
    claims: list[str]
    cue_words: list[str] = field(default_factory=list)   # sound-cue words that land in this clause
    cue_events: list[str] = field(default_factory=list)
    index: int = 0                      # position within its beat

    @property
    def dur(self) -> float:
        return self.t1 - self.t0


def _word(tok) -> str:
    return re.sub(r"[^a-z0-9']", "", tok["text"].lower())


COMMA_MIN = 1.2        # a comma only cuts a shot when both sides last this long ("17 catches, 138 yards" stays whole)


def clauses(seg: dict, tokens: list[dict], seg_index: int, seg_dur: float, min_len: float = 0.55) -> list[Clause]:
    """Cut a beat at sentence ends (and at commas when both halves can hold a shot). The first clause starts at 0
    and the last ends at the beat's end, so clauses tile the beat."""
    if not tokens:
        return [Clause(seg["id"], seg_index, 0.0, seg_dur, [], seg.get("text_display", ""), [])]
    groups, cur = [], []
    for i, tok in enumerate(tokens):
        cur.append(tok)
        end = re.search(r"[.?!]$", tok["text"])
        comma = re.search(r"[,:;]$", tok["text"])
        if (end or comma) and i + 1 < len(tokens):
            groups.append((cur, "end" if end else "comma"))
            cur = []
    if cur:
        groups.append((cur, "end"))
    # merge: a comma split only stands when both sides are long enough; anything shorter than min_len joins on
    merged: list[list[dict]] = []
    prev_kind = None                      # how the clause before this one ended: the boundary being judged
    for g, kind in groups:
        if merged:
            prev = merged[-1]
            short_prev = prev[-1]["end"] - prev[0]["start"] < min_len
            short_cur = g[-1]["end"] - g[0]["start"] < min_len
            comma_short = prev_kind == "comma" and min(g[-1]["end"] - g[0]["start"],
                                                       prev[-1]["end"] - prev[0]["start"]) < COMMA_MIN
            if short_cur or short_prev or comma_short:
                merged[-1] = prev + g
                prev_kind = kind
                continue
        merged.append(g)
        prev_kind = kind
    cues = (seg.get("scene") or {}).get("cues", [])
    out = []
    for gi, g in enumerate(merged):
        t0 = 0.0 if gi == 0 else round((merged[gi - 1][-1]["end"] + g[0]["start"]) / 2, 3)
        t1 = seg_dur if gi == len(merged) - 1 else round((g[-1]["end"] + merged[gi + 1][0]["start"]) / 2, 3)
        words = {_word(t) for t in g}
        cw, ce = [], []
        claims_here = {t["claim"] for t in g if t["claim"]}
        for q in cues:                      # where the mix puts it (series_scenes.anchor_time): its word or claim
            w = (q.get("word") or "").lower()
            if (w and any(x.startswith(w) for x in words)) or (q.get("claim") and q["claim"] in claims_here):
                cw.append(w)
                ce.append(q.get("event", ""))
        out.append(Clause(seg["id"], seg_index, t0, t1, g, " ".join(t["text"] for t in g),
                          sorted({t["claim"] for t in g if t["claim"]}), cw, ce))
    return out


def split_by_names(cl: Clause, facts: dict) -> list[Clause]:
    """'That's Braelon Allen, with Breece Hall out.': two people, two shots. A clause that names the player and his
    teammate on either side of a comma is cut there, however short the halves are (>= 0.5 s each)."""
    p = facts["players"][0]
    mate = (p.get("teammate") or {}).get("name") or ""
    if not mate:
        return [cl]
    who = lambda toks: ({w.lower() for w in p["name"].split()[-1:]} & {_word(t).replace("'s", "") for t in toks},
                        {w.lower() for w in mate.split()[-1:]} & {_word(t).replace("'s", "") for t in toks})
    for i, tok in enumerate(cl.tokens[:-1]):
        if not re.search(r"[,:;]$", tok["text"]):
            continue
        a, b = cl.tokens[:i + 1], cl.tokens[i + 1:]
        (pa, ma), (pb, mb) = who(a), who(b)
        if ((pa and mb and not ma) or (ma and pb and not mb)) and a[-1]["end"] - cl.t0 >= 0.5 and cl.t1 - b[0]["start"] >= 0.5:
            cut = round((a[-1]["end"] + b[0]["start"]) / 2, 3)
            mk = lambda toks, t0, t1: Clause(cl.segment, cl.seg_index, t0, t1, toks, " ".join(t["text"] for t in toks),
                                             sorted({t["claim"] for t in toks if t["claim"]}),
                                             cl.cue_words, cl.cue_events)
            return [mk(a, cl.t0, cut), mk(b, cut, cl.t1)]
    return [cl]


# -------------------------------------------------------------------------------------------- intents

HYPE = {"dominated", "monster", "nobody", "nobody's", "best", "most", "career", "unstoppable", "record", "historic",
        "elite", "did", "didn't", "not"}
COMPARE = {"average", "league", "entire", "than", "versus", "vs", "compared", "only", "every", "offense"}
DATA = {"share", "percent", "carries", "targets", "snaps", "snap", "routes", "rank", "games", "season", "story"}
INJURY = {"injury", "injured", "questionable", "doubtful", "practicing", "practice", "reserve", "ir", "hamstring",
          "ankle", "knee", "quadricep", "concussion", "status", "report"}
ABSENT = {"out", "missed", "sits", "sat", "playing", "without", "back", "inactive"}
RESULT = {"won", "lost", "win", "loss", "beat", "shootout", "score", "anyway"}
VERDICT = {"start", "sit", "bench", "expect", "trade", "add", "drop", "fade", "if"}
PUNCH_EVENTS = {"punchline", "roast_impact", "final_payoff", "hype_peak"}


def classify(cl: Clause, facts: dict, claims: dict) -> str:
    p = facts["players"][0]
    words = {_word(t) for t in cl.tokens}
    first, last = p["name"].split()[0].lower(), p["name"].split()[-1].lower()
    mate = (p.get("teammate") or {}).get("name") or ""
    mate_words = {w.lower() for w in mate.split() if len(w) > 2}
    team_words = ({w.lower() for w in (p.get("team_name") or "").split()} | {p["team"].lower()}
                  | {TEAM_NAMES.get(p["team"], "").lower()})
    nums = [c for c in cl.claims if re.search(r"\d", claims.get(c, {}).get("display", ""))]
    names_player = bool({first, last, last + "'s"} & words)
    names_mate = bool(mate_words & words) or bool({w + "'s" for w in mate_words} & words)
    injury_claim = any(claims.get(c, {}).get("source", {}).get("kind", "").startswith(("injury", "status", "practice"))
                       for c in cl.claims)
    if any(e in PUNCH_EVENTS for e in cl.cue_events) and p.get("role") in ("flame",) and not nums:
        return "punchline"
    if (INJURY & words) or injury_claim:
        return "injury_status"
    if words & VERDICT and ("if" in words or words & {"start", "sit", "bench", "trade"}):
        return "verdict"
    if names_mate:
        return "teammate_absence"
    if RESULT & words and (team_words & words or "anyway" in words):
        return "team_result"
    if len(nums) >= 3:
        return "stat_run"
    if nums and cl.seg_index == 0 and cl.t0 == 0.0:
        return "stat_hook"                                 # the opening number is the hook
    if any(e in PUNCH_EVENTS for e in cl.cue_events) and not nums:
        return "punchline"
    if COMPARE & words and (nums or "entire" in words or "average" in words):
        return "comparison"
    if DATA & words and (nums or words & {"story", "share"}):
        return "data_story"
    if nums and cl.seg_index == 0:
        return "stat_hook"
    if names_player and HYPE & words:
        return "dominance"
    if names_player:
        return "identity"
    if HYPE & words:
        return "dominance"
    if nums:
        return "data_story" if not (words & {"his", "he", "him"}) else "stat_hook"
    return "identity" if words & {"he", "his", "him"} else "data_story"


def subject_of(intent: str, cl: Clause, facts: dict) -> tuple[str | None, str | None, str | None]:
    """(gsis id, name, team) of who should be on screen for this clause."""
    p = facts["players"][0]
    mate = p.get("teammate") or {}
    words = {_word(t) for t in cl.tokens}
    mate_words = {w.lower() for w in (mate.get("name") or "").split() if len(w) > 2}
    if mate and (mate_words & words or {w + "'s" for w in mate_words} & words) and intent in (
            "teammate_absence", "injury_status"):
        return mate.get("gsis_id"), mate.get("name"), p["team"]
    if intent == "team_result":
        return None, p.get("team_name") or p["team"], p["team"]
    return p["gsis_id"], p["name"], p["team"]


# -------------------------------------------------------------------------------------------- the plan

GAME_FIRST = ("action_photo", "catch_photo", "rush_photo", "celebration_photo", "sideline_photo", "practice_photo",
              "interview_still", "headshot_fallback")
IDENTITY_FIRST = ("action_photo", "catch_photo", "rush_photo", "practice_photo", "sideline_photo", "interview_still",
                  "celebration_photo", "headshot_fallback")
FRAMINGS = ("medium", "tight", "wide")
RELEVANCE = {"dominance": {"game", "catch", "celebration", "runout"}, "stat_hook": {"game", "throwing", "catch"},
             "stat_run": {"game", "catch", "throwing"}, "identity": {"name_on_jersey", "portrait", "game"},
             "punchline": set()}


@dataclass
class Plan:
    shots: list[dict]
    gaps: list[dict]
    treatment: str
    seed: int
    signature: str


def _seed(episode_id: str, treatment: str, attempt: int) -> int:
    return int(hashlib.sha256(f"{episode_id}|{treatment}|{attempt}".encode()).hexdigest()[:8], 16)


def _photos(lib: ML.Library, pid: str | None, team: str | None, types, season: int, cfg_headshots: bool,
            intent: str, labels_ok: bool = True) -> list[ML.Entry]:
    """Cleared, available photos of one player in the current team's uniform, best first for this intent."""
    if not pid:
        return []
    out = []
    for e in lib.query(player_id=pid, types=types):
        if not lib.available(e) or not ML.cleared(e, cfg_headshots):
            continue
        if team and e.team != team:          # another uniform (college, old team): never for current analysis
            continue
        out.append(e)
    want = RELEVANCE.get(intent, set())
    rank = {t: i for i, t in enumerate(types)}
    return sorted(out, key=lambda e: (rank.get(e.type, 99), -len(want & set(e.relevance)),
                                      -(e.season or 0), -min(e.width, e.height)))


def _broll_for(lib: ML.Library, cl: Clause) -> ML.Entry | None:
    words = {_word(t) for t in cl.tokens} | {w for w in cl.cue_words if w}
    best = None
    for e in lib.query(types=("context_broll", "licensed_video")):
        if not lib.available(e):
            continue
        hit = len(words & set(e.relevance))
        if hit and (best is None or hit > best[0]):
            best = (hit, e)
    return best[1] if best else None


def plan(script: dict, facts: dict, claims: dict, tokens_by_seg: dict[str, list[dict]], durations: dict[str, float],
         lib: ML.Library, treatment: str, *, season: int, cfg_headshots: bool, episode_id: str,
         recent_signatures: list[str] | None = None, presenter_segments: set[str] | None = None) -> Plan:
    recent = set(recent_signatures or [])
    for attempt in range(6):
        p = _plan_once(script, facts, claims, tokens_by_seg, durations, lib, treatment, season=season,
                       cfg_headshots=cfg_headshots, seed=_seed(episode_id, treatment, attempt),
                       presenter_segments=presenter_segments or set())
        if p.signature not in recent:
            return p
    return p


def _plan_once(script, facts, claims, tokens_by_seg, durations, lib, treatment, *, season, cfg_headshots, seed,
               presenter_segments) -> Plan:
    T = TREATMENTS[treatment]
    rng = random.Random(seed)
    uses: dict[str, int] = {}
    framings: dict[str, int] = {}
    shots, gaps = [], []
    offset = 0.0
    last_kind, run = None, 0
    presenter_left = T["presenter"]
    for si, (seg, scene) in enumerate(zip(script["narration_segments"], script["shots"])):
        dur = durations[seg["id"]]
        cls = [part for cl in clauses(seg, tokens_by_seg[seg["id"]], si, dur) for part in split_by_names(cl, facts)]
        beat_mate = None                    # "Hall's missed two weeks ... he was not practicing": still Hall
        for ci, cl in enumerate(cls):
            cl.index = ci
            intent = classify(cl, facts, claims)
            sid, sname, steam = subject_of(intent, cl, facts)
            if sid and sid != facts["players"][0]["gsis_id"]:
                beat_mate = (sid, sname, steam)
            elif beat_mate and intent == "injury_status":
                sid, sname, steam = beat_mate
            picks = _choose(cl, intent, sid, sname, steam, scene, lib, T, rng, uses, last_kind, run, season,
                            cfg_headshots, facts, presenter_left if seg["id"] in presenter_segments else 0)
            for pk in picks:
                pk.update(segment=seg["id"], intent=intent, subject_id=sid, subject_name=sname, subject_team=steam,
                          narration=" ".join(t["text"] for t in pk.pop("_tokens")), seg_index=si, claims=cl.claims,
                          start=round(offset + pk["t0"], 3), end=round(offset + pk["t1"], 3),
                          dur=round(pk["t1"] - pk["t0"], 3), names_player=sid == facts["players"][0]["gsis_id"]
                          and intent in ("identity", "dominance", "stat_hook", "stat_run"))
                if pk["kind"] == "presenter":
                    presenter_left -= 1
                if pk.get("asset"):
                    e = lib.get(pk["asset"])
                    uses[e.id] = uses.get(e.id, 0) + 1
                    n = framings.get(e.id, 0)
                    framings[e.id] = n + 1
                    pk["crop"] = {"framing": pk.get("framing") or ("wide" if e.type == "team_photo" else FRAMINGS[n % len(FRAMINGS)]),
                                  "motion": _motion(T, intent, rng), "subject": e.subject}
                    pk["labels"] = ML.labels_for(e, season)
                    pk["credit"] = ML.short_credit(e)
                kind = pk["kind"]
                run = run + 1 if kind == last_kind else 1
                last_kind = kind
                shots.append(pk)
        offset += dur
    _merge_graphics(shots)
    for i, s in enumerate(shots):
        s["index"] = i + 1
        if s.get("wanted"):
            gaps.append({"shot": s["index"], "segment": s["segment"], "narration": s["narration"], "wanted": s["wanted"],
                         "used": s["kind"] + (f" ({s['asset']})" if s.get("asset") else "")})
        s["transition"] = "none" if i == 0 else _transition(T, shots[i - 1], s, rng)
        s["caption_pos"] = "auto" if s["kind"] in ("photo", "broll", "inset") else "low"
    sig = "-".join(s["kind"][0] + (s.get("asset") or s.get("scene_type") or "")[:6] for s in shots)
    return Plan(shots, gaps, treatment, seed, hashlib.sha1(sig.encode()).hexdigest()[:12])


def _motion(T, intent, rng) -> str:
    if T["motion"] == "snap":
        return "snap" if intent == "punchline" else rng.choice(("push", "drift"))
    if T["motion"] == "push":
        return rng.choice(("push", "push", "pull"))
    return rng.choice(("drift", "drift_left", "push"))


def _transition(T, prev, cur, rng) -> str:
    if cur["kind"] == "graphic" and prev["kind"] == "graphic" and prev.get("scene_type") == cur.get("scene_type"):
        return "none"
    if cur.get("intent") == "punchline":
        return "cut"
    return rng.choice(T["transitions"])


def _merge_graphics(shots: list[dict]) -> None:
    """Two consecutive clauses showing the same beat graphic are one shot: the graphic plays on uninterrupted."""
    i = 0
    while i + 1 < len(shots):
        a, b = shots[i], shots[i + 1]
        if a["kind"] == b["kind"] == "graphic" and a["segment"] == b["segment"] and a.get("mode") == b.get("mode"):
            a["t1"], a["end"] = b["t1"], b["end"]
            a["dur"] = round(a["end"] - a["start"], 3)
            a["narration"] += " " + b["narration"]
            a["why"] += f"; continues through '{b['narration']}'"
            shots.pop(i + 1)
        else:
            i += 1


def _shot(kind, cl, t0=None, t1=None, toks=None, **kw) -> dict:
    return {"kind": kind, "t0": cl.t0 if t0 is None else t0, "t1": cl.t1 if t1 is None else t1,
            "_tokens": cl.tokens if toks is None else toks, **kw}


def _choose(cl, intent, sid, sname, steam, scene, lib, T, rng, uses, last_kind, run, season, cfg_headshots, facts,
            presenter_left) -> list[dict]:
    scene_type = scene["type"]
    graphic = lambda why, mode="full", **kw: _shot("graphic", cl, scene_type=scene_type, mode=mode, why=why, **kw)
    types = IDENTITY_FIRST if intent == "identity" else GAME_FIRST
    photos = _photos(lib, sid, steam, types, season, cfg_headshots, intent)
    real = [e for e in photos if e.type != "headshot_fallback"]
    if intent == "dominance":           # the praise line gets the best picture unless it is already worn out
        fresh = sorted(real, key=lambda e: (uses.get(e.id, 0) >= 3, real.index(e)))
    else:
        fresh = sorted(real, key=lambda e: (uses.get(e.id, 0), real.index(e)))  # least used first, then best

    def photo(e, why, **kw):
        return _shot("photo", cl, asset=e.id, why=why, **kw)

    if presenter_left and intent in ("verdict", "identity") and rng.random() < 0.5:
        return [_shot("presenter", cl, why="selective host cutaway on an opinion line (treatment allows one)")]

    if intent == "injury_status":
        evidence = scene_type in ("report", "status_stamp") or "report" in scene_type
        if sid and sid != facts["players"][0]["gsis_id"] and fresh and cl.dur > 1.6 and not evidence:
            return [photo(fresh[0], f"identifies {sname} (a photo of him, not of the injury)")]
        why = ("the availability report on screen: the source, not generic injury footage"
               if evidence else "the beat's graphic; no source-evidence card exists for this beat")
        inset_why = f"the report stays on screen as a card over {sname} (identification, not the injury)"
        if evidence and cl.index > 0 and fresh:      # the report stays up, now as a card over who it is about
            return [_shot("inset", cl, asset=fresh[0].id, scene_type=scene_type, evidence=True, why=inset_why)]
        if evidence and fresh and cl.dur > T["max_shot"] * 1.3:      # one long line: report, then report + face
            target = cl.t0 + cl.dur * 0.55            # cut after the comma nearest the middle, else mid-line
            commas = [cl.tokens[i + 1]["start"] for i, t in enumerate(cl.tokens[:-1]) if re.search(r"[,;:]$", t["text"])]
            mid = min(commas, key=lambda x: abs(x - target)) if commas else \
                next((t["start"] for t in cl.tokens if t["start"] >= target), None)
            if mid is not None:
                first = [t for t in cl.tokens if t["start"] < mid]
                return [graphic(why, evidence=evidence, t1=mid - 0.04, _tokens=first),
                        _shot("inset", cl, t0=mid - 0.04, toks=[t for t in cl.tokens if t["start"] >= mid],
                              asset=fresh[0].id, scene_type=scene_type, evidence=True, why=inset_why)]
        return [graphic(why, evidence=evidence)]

    if intent == "teammate_absence":
        if fresh:
            return [photo(fresh[0], f"a verified photo of {sname} (player id matches the teammate in the facts)")]
        return [graphic(f"no cleared photo of {sname}; the beat's graphic carries it",
                        wanted=f"photo of {sname} ({steam})")]

    if intent == "team_result":
        teams = [e for e in lib.query(team=steam, types=("team_photo",)) if lib.available(e) and ML.cleared(e, cfg_headshots)]
        teams.sort(key=lambda e: (uses.get(e.id, 0), -(e.season or 0)))
        if teams:
            return [_shot("photo", cl, asset=teams[0].id, overlay="score",
                          why=f"team result over a {steam} team photo, labelled as a file photo (not this game)")]
        return [graphic("no cleared team photo", wanted=f"team photo ({steam})")]

    if intent == "punchline":
        b = _broll_for(lib, cl)
        if b:
            return [_shot("broll", cl, asset=b.id, effect="snap_zoom",
                          why=f"visual punchline: '{', '.join(sorted(set(b.relevance) & ({_word(t) for t in cl.tokens} | set(cl.cue_words))))}' "
                              "shown literally, labelled ILLUSTRATIVE, snapped in on the hit")]
        return [graphic("the beat's own gag carries the punchline (no matching B-roll)")]

    if intent in ("comparison", "data_story", "verdict"):
        if intent == "comparison" and fresh and rng.random() < T["inset"] and run < 2:
            return [_shot("inset", cl, asset=fresh[0].id, scene_type=scene_type,
                          why="the comparison graphic as a card over the player's photo: the data stays with him")]
        return [graphic({"comparison": "the comparison is a picture (the beat's graphic)",
                         "data_story": "data tells this part: the beat's graphic",
                         "verdict": "the decision is the graphic"}[intent])]

    # player-led intents: identity, dominance, stat_hook, stat_run
    if intent == "stat_run" and T["split_stat_runs"] and photos:
        nums = [t for t in cl.tokens if t["claim"] and re.search(r"\d", t["text"])]
        cuts = []
        pool = list(fresh)
        if len(pool) < len(nums):           # too few real photos for a cut per number: the labelled headshot joins
            pool += [e for e in photos if e.type == "headshot_fallback"]
        pool = pool or photos
        for k, tok in enumerate(nums):
            t0 = cl.t0 if k == 0 else tok["start"] - 0.06
            t1 = cl.t1 if k == len(nums) - 1 else nums[k + 1]["start"] - 0.06
            toks = [t for t in cl.tokens if t0 - 0.01 <= t["start"] < t1]
            e = pool[k % len(pool)] if len(pool) > 1 or k == 0 else photos[min(k, len(photos) - 1)]
            cuts.append(_shot("photo", cl, t0=t0, t1=t1, toks=toks, asset=e.id, overlay="stat", stat_claim=tok["claim"],
                              why=f"stat run: a new shot on each number ({tok['text']}), the number slams over him"))
        return cuts
    if intent == "stat_run":
        return [graphic("a run of numbers: the beat's graphic counts them")]

    use_photo = bool(photos) and (intent in ("identity", "dominance") or rng.random() < T["photo_bias"])
    if run >= 2 and last_kind == "photo" and intent == "stat_hook":
        use_photo = False                                    # break a long photo run with the data
    if not use_photo and intent in ("stat_hook",):
        return [graphic("the number is the hook: the beat's graphic")]
    if real:
        e = fresh[0]
        why = {"identity": f"a real photo of {sname} as he is named",
               "dominance": f"{sname} big, in his {steam} uniform, as the line praises him",
               "stat_hook": f"{sname} full-bleed with the number over him"}[intent]
        shot = photo(e, why, overlay="stat" if intent == "stat_hook" and T["stat_overlay"] and cl.claims else None,
                     stat_claim=next((c for c in cl.claims), None))
        wanted = None
        if e.type not in ML.GAME_PHOTOS and intent in ("dominance", "stat_hook"):
            wanted = f"game action photo of {sname} ({steam}, current season)"
        if wanted:
            shot["wanted"] = wanted
        if cl.dur > T["max_shot"] and len(fresh) > 1:          # long line: two shots, two photos
            mid = cl.t0 + cl.dur / 2
            a = dict(shot, t1=mid, _tokens=[t for t in cl.tokens if t["start"] < mid])
            b = photo(fresh[1], why + " (second angle)", t0=mid, toks=[t for t in cl.tokens if t["start"] >= mid])
            return [a, b]
        return [shot]
    fallback = next((e for e in photos if e.type == "headshot_fallback"), None)
    if fallback:
        return [photo(fallback, f"FALLBACK: no cleared photo of {sname} in a {steam} uniform; the owner-approved "
                                "ESPN headshot, labelled", wanted=f"action photo of {sname} ({steam}, current season)")]
    if intent in ("stat_hook", "dominance"):
        return [graphic(f"no cleared photo of {sname}: the beat's graphic", wanted=f"action photo of {sname} ({steam})")]
    return [_shot("missing", cl, why=f"no cleared photo of {sname}", wanted=f"action photo of {sname} ({steam})",
                  missing=f"{sname} — {steam} photo")]
