"""Stage 3: script.json. Python builds the outline and shot list; the LLM writes the prose.

The LLM may reference numbers, ranks, weeks, QBs, coaches and opponents only as `{{cNNN}}` claim
placeholders from the segment's allow-list. Placeholders are filled from facts.json deterministically,
once with the display form (captions, titles) and once with the spoken form (TTS). Output that writes a
digit, uses a placeholder it was not given or uses a banned phrase is rejected and re-prompted.
"""

from __future__ import annotations

import json
import re

from jsonschema import validate

from ..core import Episode, canonical_json, read_json, run_stage, sha256_bytes, write_json
from ..llm.base import ScriptLLM, make_llm

VERSION = 1
PLACEHOLDER = re.compile(r"\{\{(c\d{3})\}\}")
ALIAS = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
DIGIT = re.compile(r"\d")
# Never the argument: fantasy production and projections. Routes and first reads are not in our data.
# Injuries and health are never in our facts, so any mention is invented. Sample-size boasts are unsupported.
INVENTED_PHRASES = ("injury", "injured", "injuries", "health", "healthy", "hamstring", "ankle", "knee", "concussion",
                    "returns from", "return from", "not a small sample", "large sample", "reliable sample",
                    "high-volume offense", "guaranteed", "lock", "is limited", "be limited", "are limited")
BANNED_PHRASES = INVENTED_PHRASES + ("fantasy point", "points per game", "ppg", "ppr", "half-ppr", "projection", "projected",
                  "xfp", "expected fantasy", "route", "first read", "first-read")
BANNED = re.compile(r"\b(" + "|".join(re.escape(p) for p in BANNED_PHRASES) + r")s?\b", re.I)
WORDS_PER_SECOND = 2.55              # Kokoro am_michael at speed 1.1, measured on week 6

SYSTEM_PROMPT = """You write narration for a faceless YouTube show about NFL player usage for fantasy football managers.
The voice is a sharp, plain-spoken analyst: specific, calm, no hype, no clichés, no exclamation marks.

Hard rules:
1. Every number, percentage, rank, week, QB name, head coach and opponent MUST be written as a placeholder from
   that segment's allowed list, exactly like {{c012}}. Never write digits. Never spell numbers out as words
   (no "two games", "first", "fifty percent"); use the placeholder instead.
2. Never judge a number's direction yourself. Whether a team is pass- or run-leaning and whether a matchup is
   favorable or tough come ONLY from the descriptor placeholders provided for that.
3. Only state what the provided facts say. Do not invent injuries, depth-chart news, trades, quotes, reasons or
   anything not in the facts. You may describe what the numbers show (a role grew, a share fell) and what to
   watch next. If the facts include a QB change, you may point it out.
4. The argument is usage and role: snap share, target share, air-yard share, backfield rush share, volume,
   team pass tendency (PROE) and schedule. Never mention fantasy points, PPR, points per game, projections,
   expected points, routes or first reads.
5. Defense ranks are season-to-date; first means the most generous to that position. Say "so far this season".
6. Samples are small: never call a sample large or reliable. When you compare windows, name the windows with
   their placeholders ("his last {{recent_games}} games" versus "the {{earlier_games}} before").
7. Write for the ear: short sentences, player names in full on first mention, team nicknames from placeholders.
8. Vary sentence openings and structure across segments; do not reuse the same template for each player.
9. Hit each segment's target word count. Role segments need real substance: walk through each metric's
   earlier and recent values, then the volume.
10. Titles: no clickbait questions, no ALL CAPS words, no emojis."""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


METRIC_SLUG = {"snap share": "snap_share", "target share": "target_share", "air-yard share": "air_yard_share",
               "backfield rush share": "backfield_share"}


def build_outline(facts: dict) -> list[dict]:
    """Deterministic segment plan: allowed claims (as readable aliases), target length and shot.

    The model sees placeholders like {{snap_share_now}} or {{week15_opponent}}; semantic names keep it from
    grabbing the wrong value. Aliases are local to a segment and translated back to claim ids afterwards."""
    claims = {c["id"]: c for c in facts["claims"]}
    ep = facts["episode"]
    risers = [p for p in facts["players"] if p["role"] == "riser"]
    fallers = [p for p in facts["players"] if p["role"] == "faller"]
    segs: list[dict] = []

    def add(section: str, words: int, aliases: list[tuple[str, str]], shot: dict, player: dict | None = None,
            guide: str = ""):
        segs.append({"id": f"s{len(segs) + 1:02d}", "section": section, "target_words": words,
                     "claims": [{"alias": a, "id": cid, "means": claims[cid]["label"], "says": claims[cid]["display"]}
                                for a, cid in aliases],
                     "player": player["gsis_id"] if player else None,
                     "player_name": player["name"] if player else None,
                     "player_desc": (f"{player['name']} ({player['position']}, "
                                     f"{'usage riser' if player['role'] == 'riser' else 'usage faller'})")
                     if player else None,
                     "guide": guide, "shot": shot})

    def metric_aliases(p: dict, only_lead: bool = False) -> list[tuple[str, str]]:
        out = []
        for m in p["metrics"][:1] if only_lead else p["metrics"]:
            slug = METRIC_SLUG[m["label"]]
            out += [(f"{slug}_before", m["baseline"]), (f"{slug}_now", m["recent"]), (f"{slug}_change", m["change"])]
        return out

    def playoff_aliases(p: dict, prefix: str = "", ranks: bool = True) -> list[tuple[str, str]]:
        out = []
        for s in p["schedule"]:
            if not s["playoff"]:
                continue
            base = f"{prefix}week{s['week']}"
            out.append((f"{base}", s["week_label"]))
            out.append((f"{base}_opponent", s["opponent"]))
            if "rank" in s:
                out += [(f"{base}_defense_rank", s["rank"])] if ranks else []
                out.append((f"{base}_matchup_grade", s["band"]))
        return out

    def window_aliases(p: dict) -> list[tuple[str, str]]:
        return [("team", p["team_name"]), ("recent_games", p["recent_games"]), ("earlier_games", p["baseline_games"])]

    hero = (risers or fallers)[0]
    add("cold_open", 45, [("this_week", ep["week"]), *window_aliases(hero), *metric_aliases(hero, only_lead=True)],
        {"type": "hero_stat", "player": hero["gsis_id"], "metric": 0}, hero,
        "Open on this player's change without greeting the viewer. Then say this show tracks the biggest usage "
        "moves of the week.")
    add("method", 50, [("this_week", ep["week"]), ("playoff_weeks", ep["playoff_weeks"])], {"type": "method_card"},
        guide="Explain in plain words what we measure: snap share, target share, air-yard share and backfield "
              "rush share; recent games versus earlier games; plus team pass tendency and the schedule for the "
              "fantasy playoffs. Say the show is about role and opportunity, not last week's box score.")

    def player_block(p: dict, kind: str):
        role_word = "rising" if kind == "riser" else "falling"
        volume = [(_slug(p["volume"]["label"]), p["volume"]["recent"])]
        if "adot" in p["volume"]:
            volume.append(("average_depth_of_target", p["volume"]["adot"]))
        lead = p["metrics"][0]["label"]
        add(f"{kind}_role", 80, [*window_aliases(p), *metric_aliases(p), *volume],
            {"type": "usage_chart", "player": p["gsis_id"]}, p,
            f"Describe how this {p['position']}'s role is {role_word}, leading with {lead}. Compare the recent "
            "window with the earlier window for each metric (say \"his last {{recent_games}} games\", never a typed "
            "number). Mention the volume figure.")
        ctx = p["context"]
        add(f"{kind}_context", 55, [("team", p["team_name"]), ("qb", ctx["qbs_recent"]), ("head_coach", ctx["head_coach"]),
                                    ("proe", ctx["proe"]), ("proe_rank", ctx["proe_rank"]),
                                    ("pass_tendency", ctx["pass_tendency"]),
                                    ("playoff_weeks", ep["playoff_weeks"]), *playoff_aliases(p)],
            {"type": "context_card", "player": p["gsis_id"]}, p,
            "Give the team context (QB, pass tendency) and the fantasy-playoff schedule with each opponent, its "
            "defense rank and matchup grade. End with one concrete thing to watch.")

    if risers:
        add("risers_intro", 18, [], {"type": "section_card", "title": "Usage risers"},
            guide="A one-line transition into the risers.")
        for p in risers:
            player_block(p, "riser")
    if fallers:
        add("fallers_intro", 18, [], {"type": "section_card", "title": "Usage fallers"},
            guide="A one-line transition into the fallers.")
        for p in fallers:
            player_block(p, "faller")

    # Playoff watch: the two friendliest and two toughest Weeks 15-17 slates among featured players.
    def avg_rank(p):
        ranks = [s["rank_value"] for s in p["schedule"] if s["playoff"] and "rank_value" in s]
        return sum(ranks) / len(ranks) if ranks else 99

    ordered = sorted(facts["players"], key=avg_rank)
    watch = ordered[:2] + [p for p in ordered[-2:] if p not in ordered[:2]]
    aliases = [("playoff_weeks", ep["playoff_weeks"])]
    for p in watch:
        aliases += playoff_aliases(p, prefix=_slug(p["name"].split()[-1] if not p["name"].endswith("III")
                                                   else p["name"].split()[-2]) + "_", ranks=False)
    add("playoff_watch", 120, aliases,
        {"type": "playoff_table", "players": [p["gsis_id"] for p in facts["players"]]},
        guide="Name the featured players with the friendliest and the toughest fantasy-playoff schedules: "
              + ", ".join(f"{p['name']} ({'friendliest' if i < 2 else 'toughest'})" for i, p in enumerate(watch))
              + ". For each, name the opponents with their matchup grades; the table on screen shows the ranks. "
                "Never count the weeks or games.")
    return segs


def assign_blocks(outline: list[dict]) -> list[list[dict]]:
    """One LLM call per block: the opening, each player (role + context), and the playoff watch.
    Section intros ride with the block that follows them."""
    blocks: list[list[dict]] = []
    pending: list[dict] = []
    for seg in outline:
        if seg["section"] in ("cold_open", "method", "risers_intro", "fallers_intro"):
            pending.append(seg)
            if seg["section"] == "risers_intro":
                blocks.append(pending)
                pending = []
            continue
        if seg["section"].endswith("_context"):
            continue                                   # added together with its _role segment
        if seg["section"].endswith("_role"):
            ctx = next(s for s in outline if s["player"] == seg["player"] and s["section"].endswith("_context"))
            blocks.append(pending + [seg, ctx])
        else:
            blocks.append(pending + [seg])
        pending = []
    if pending:
        blocks.append(pending)
    return blocks


def block_schema(block: list[dict]) -> dict:
    """One string property per segment id. Placeholder and digit rules are enforced by check_block, not by a
    grammar regex: forcing them during sampling made the model degenerate into junk characters."""
    return {
        "type": "object", "additionalProperties": False, "required": [seg["id"] for seg in block],
        "properties": {seg["id"]: {"type": "string", "minLength": 20, "maxLength": 2400} for seg in block},
    }


def as_segments(data: dict, block: list[dict]) -> None:
    """Normalise the per-id object into the ordered segment list the rest of the stage uses."""
    if "segments" not in data:
        data["segments"] = [{"id": seg["id"], "text": data.pop(seg["id"])} for seg in block]


TITLE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["title_candidates"],
    "properties": {"title_candidates": {"type": "array", "minItems": 3, "maxItems": 3,
                                        "items": {"type": "string", "minLength": 10, "maxLength": 90}}},
}


def _claim_lines(seg_claims: list[dict]) -> str:
    return "\n".join(f"  {{{{{c['alias']}}}}} = {c['means']} (currently \"{c['says']}\")" for c in seg_claims) \
        or "  (none: this segment uses no numbers)"


def block_prompt(block: list[dict], used_openings: list[str]) -> str:
    parts = []
    for seg in block:
        who = f" about {seg['player_desc']}" if seg["player_desc"] else ""
        parts.append(f"Segment {seg['id']} ({seg['section']}{who}), about {seg['target_words']} words.\n"
                     f"Guide: {seg['guide']}\nPlaceholders you may use:\n{_claim_lines(seg['claims'])}")
    avoid = ""
    if used_openings:
        avoid = ("\n\nEarlier segments already opened like this; do not reuse these openings or their sentence "
                 "pattern:\n" + "\n".join(f"- {o}" for o in used_openings[-12:]))
    return ("Write these segments in order. A placeholder is replaced by the quoted words, so write grammar around "
            "them and never repeat their units (no \"percent\" or \"points\" right after a placeholder). Type the "
            "placeholder itself, never the words it reads as.\n\n"
            + "\n\n".join(parts) + avoid)


def title_aliases(facts: dict) -> list[dict]:
    claims = {c["id"]: c for c in facts["claims"]}
    pairs = [("this_week", facts["episode"]["week"])]
    for p in facts["players"]:
        m = p["metrics"][0]
        who = _slug(p["name"])
        pairs += [(f"{who}_{METRIC_SLUG[m['label']]}_before", m["baseline"]),
                  (f"{who}_{METRIC_SLUG[m['label']]}_now", m["recent"])]
    return [{"alias": a, "id": cid, "means": claims[cid]["label"], "says": claims[cid]["display"]} for a, cid in pairs]


def title_prompt(facts: dict, aliases: list[dict]) -> str:
    who = "\n".join(f"  {p['role']}: {p['name']} ({p['position']}), biggest move in {p['metrics'][0]['label']}"
                    for p in facts["players"])
    return ("Write three distinct YouTube titles for this week's usage risers and fallers episode, each under 70 "
            "characters after placeholders are filled. Use placeholders for every number and week. You may name "
            f"players.\n\nPlayers:\n{who}\n\nPlaceholders:\n{_claim_lines(aliases)}")


NUMBER_WORDS = re.compile(
    r"\b(zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|third|"
    r"fourth|fifth|sixth|seventh|eighth|ninth|tenth|double|doubled|triple|tripled)\b", re.I)
UNIT_AFTER_PLACEHOLDER = re.compile(r"\}\}\s*(%|percent|percentage|points?|pts)\b", re.I)
UNIT_TAIL = r"(\s*-?\s*(?:percentage[ -]points?|percent(?:age)?|points?|pts|%))"
TEXT_FORMATS = ("text", "team", "week", "weeks_range")


def text_forms(claim: dict) -> set[str]:
    """Every way the model might type a non-numeric claim's value."""
    forms = {claim["spoken"], claim["display"]}
    if claim["format"] == "weeks_range":
        a, b = int(claim["value"][0]), int(claim["value"][-1])
        forms |= {f"Weeks {a}{sep}{b}" for sep in ("-", "–", " through ", " to ", " and ")}
    return forms


def repair(text: str, seg_claims: list[dict], claims: dict[str, dict]) -> str:
    """Deterministic fixes for the model's two common slips:
    1. a unit typed after a placeholder that already says it ("{{x_change}} percentage points");
    2. a name, team or week typed out instead of its placeholder ("Week fifteen" -> {{week15}});
    3. a claim's exact display string typed instead of its placeholder ("88%" -> {{snap_share_now}}), only when
       exactly one allowed claim has that display string, so the narrated value equals the claim by construction.
    Spelled-out numbers are never mapped back and stay errors."""
    for c in seg_claims:
        if claims[c["id"]]["display"][:1] in "+−":       # "+{{x_change}}" when x_change already reads "+12 pts"
            text = re.sub(r"[+\-−]\s*(\{\{" + re.escape(c["alias"]) + r"\}\})", r"\1", text)
        if claims[c["id"]]["spoken"].endswith(("percent", "points")):
            text = re.sub(r"\{\{" + re.escape(c["alias"]) + r"\}\}" + UNIT_TAIL, "{{" + c["alias"] + "}}",
                          text, flags=re.I)
    for c in seg_claims:
        first = claims[c["id"]]["display"].split(" ")[0]
        if first in ("Week", "Weeks"):                  # "Weeks {{playoff_weeks}}" -> "{{playoff_weeks}}"
            text = re.sub(r"\bWeeks?\s+(\{\{" + re.escape(c["alias"]) + r"\}\})", r"\1", text, flags=re.I)
    numeric = [c for c in seg_claims if claims[c["id"]]["format"] not in TEXT_FORMATS]
    displays: dict[str, list[dict]] = {}
    for c in numeric:
        displays.setdefault(claims[c["id"]]["display"], []).append(c)
    for disp, cs in sorted(displays.items(), key=lambda kv: -len(kv[0])):
        if len(cs) == 1 and re.search(r"\d", disp):
            text = re.sub(rf"(?<![\w.{{]){re.escape(disp)}(?![\w%}}])", "{{" + cs[0]["alias"] + "}}", text)
    textual = [c for c in seg_claims if claims[c["id"]]["format"] in TEXT_FORMATS and "," not in claims[c["id"]]["spoken"]]
    for c in sorted(textual, key=lambda c: -len(claims[c["id"]]["spoken"])):
        for form in sorted(text_forms(claims[c["id"]]), key=len, reverse=True):
            if len(form) >= 4:
                text = re.sub(rf"(?<![{{\w]){re.escape(form)}(?![\w}}])", "{{" + c["alias"] + "}}", text, flags=re.I)
    return text


def check_text(text: str, allowed: set[str], where: str, spoken_to_alias: dict[str, str] | None = None) -> list[str]:
    errors = []
    used = set(ALIAS.findall(text))
    if used - allowed:
        errors.append(f"{where}: invented placeholders {sorted(used - allowed)}; the only ones allowed here are "
                      f"{sorted(allowed) or 'none'}")
    bare = ALIAS.sub(" ", text)
    if DIGIT.search(bare):
        errors.append(f"{where}: digits outside placeholders")
    if "{" in bare or "}" in bare:
        errors.append(f"{where}: malformed placeholder")
    found = sorted({m.group(1).lower() for m in NUMBER_WORDS.finditer(bare)})
    for word in found:
        alias = (spoken_to_alias or {}).get(word)
        errors.append(f"{where}: the word {word!r} is a number; write {{{{{alias}}}}} instead" if alias
                      else f"{where}: the word {word!r} is a number; delete or rewrite the phrase containing it")
    if UNIT_AFTER_PLACEHOLDER.search(text):
        errors.append(f"{where}: a unit word follows a placeholder; the placeholder already says its unit")
    errors += [f"{where}: banned phrase {m.group(0)!r}" for m in BANNED.finditer(bare)]
    return errors


def check_block(data: dict, block: list[dict]) -> list[str]:
    ids = [s["id"] for s in data["segments"]]
    if ids != [s["id"] for s in block]:
        return [f"segments must be exactly {[s['id'] for s in block]} in order, got {ids}"]
    errors = []
    for seg, out in zip(block, data["segments"]):
        spoken = {c["says"].lower(): c["alias"] for c in seg["claims"]}
        errors += check_text(out["text"], {c["alias"] for c in seg["claims"]}, seg["id"], spoken)
        if not out["text"].rstrip().endswith((".", "!", "?", "\u201d", '"')):
            errors.append(f"{seg['id']}: ends mid-sentence; finish the last sentence")
        words, target = len(out["text"].split()), seg["target_words"]
        if not max(5, 0.7 * target - 5) <= words <= 1.4 * target + 8:
            errors.append(f"{seg['id']}: {words} words but the target is {seg['target_words']}; "
                          + ("expand it" if words < seg["target_words"] else "shorten it"))
    return errors


VERIFY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["sentences"],
    "properties": {"sentences": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["sentence", "verdict", "why"],
        "properties": {"sentence": {"type": "string", "maxLength": 500},
                       "verdict": {"type": "string", "enum": ["SUPPORTED", "WATCH_ITEM", "UNSUPPORTED", "CONTRADICTS"]},
                       "why": {"type": "string", "maxLength": 200}}}}},
}
VERIFY_SYSTEM = """You fact-check narration for an NFL usage show. Judge EVERY sentence of the text, one by one:
- SUPPORTED: every factual statement in it appears in the facts list (or is the player's name, team or position,
  or plainly describes a listed number going up or down).
- WATCH_ITEM: it only says what to watch or monitor next and asserts no new fact.
- UNSUPPORTED: it states anything not in the facts: injuries, health, a player or QB being limited or returning,
  play-calling style, offensive preference, volume or pace, coaching intent, depth-chart news, or a prediction.
  A "watch" sentence that slips in such a premise ("watch if the QB is limited") is UNSUPPORTED.
- CONTRADICTS: it conflicts with the facts (e.g. calls a run-leaning team pass-heavy, a tough matchup favorable,
  or assigns a number to the wrong window or metric).
Be strict: the facts list is the only source of truth."""


def verify_prompt(block: list[dict], data: dict, claims: dict[str, dict]) -> str:
    parts = []
    for seg, out in zip(block, data["segments"]):
        facts = "\n".join(f"  - {c['means']}: {claims[c['id']]['display']}" for c in seg["claims"])
        who = (f"Player: {seg['player_desc']}. Windows: 'recent' means his most recent games; 'earlier' means the "
               "games just before those. " if seg["player_desc"] else "")
        text = ALIAS.sub(lambda m: next((claims[c["id"]]["display"] for c in seg["claims"] if c["alias"] == m.group(1)),
                                        m.group(0)), out["text"])
        given = f"\nAlso established (from our data): {seg['guide']}" if seg["section"] == "playoff_watch" else ""
        parts.append(f"Segment {seg['id']} ({seg['section']}). {who}\nFacts:\n{facts or '  (none)'}{given}\n"
                     f"Text:\n{text}")
    return "\n\n".join(parts)


def verify_block(llm: ScriptLLM, ep: Episode, name: str, block: list[dict], data: dict,
                 claims: dict[str, dict]) -> list[str]:
    prompt = verify_prompt(block, data, claims)
    raw = llm.generate(VERIFY_SYSTEM, prompt, VERIFY_SCHEMA, 1500)
    n = len(list(ep.path("prompts").glob(f"{name}.verify*.json"))) + 1
    (ep.path("prompts") / f"{name}.verify{n}.json").write_text(raw)
    try:
        verdicts = json.loads(raw)["sentences"]
    except (json.JSONDecodeError, KeyError):
        return []
    return [f"{v['verdict'].lower()}: \"{v['sentence']}\" ({v['why']}); remove or fix this sentence"
            for v in verdicts if v["verdict"] in ("UNSUPPORTED", "CONTRADICTS")]


def to_claim_ids(text: str, seg_claims: list[dict]) -> str:
    alias = {c["alias"]: c["id"] for c in seg_claims}
    return ALIAS.sub(lambda m: "{{" + alias[m.group(1)] + "}}", text)


def _ask(llm: ScriptLLM, ep: Episode, name: str, prompt: str, schema: dict, checker, fixer,
         max_tokens: int | None = None) -> tuple[dict, int]:
    """Generate, repair, validate, re-prompt with the problems; raise after max_retries."""
    retries = ep.cfg["llm"]["max_retries"]
    message, errors = prompt, []
    for attempt in range(1, retries + 2):
        raw = llm.generate(SYSTEM_PROMPT, message, schema, max_tokens)
        (ep.path("prompts") / f"{name}.attempt{attempt}.json").write_text(raw)
        try:
            data = json.loads(raw)
            validate(data, schema)
            fixer(data)
            errors = checker(data)
        except Exception as exc:  # malformed JSON or schema violation
            errors = [f"invalid JSON for the schema: {exc}"]
        if not errors:
            return data, attempt
        ep.log(f"    {name}: attempt {attempt} rejected: {errors[:3]}")
        # A fresh draft with the problems listed works better than asking the model to patch its own text,
        # which it tends to reproduce verbatim.
        message = prompt + "\n\nA previous draft was rejected for these problems. Write a fresh draft that avoids " \
            "every one of them:\n- " + "\n- ".join(errors[:20])
    write_json(ep.path("script_errors.json"), {"call": name, "errors": errors})
    raise RuntimeError(f"script call {name} rejected after {retries + 1} attempts; see script_errors.json")


def fill(text: str, claims: dict[str, dict], form: str) -> str:
    return PLACEHOLDER.sub(lambda m: claims[m.group(1)][form], text)


def cta_segment(ep: Episode, facts: dict, claims: dict) -> dict:
    cta = ep.cfg["cta"]
    week_claim = facts["episode"]["week"]
    tail = f" Find the full usage tool at {cta['url']}." if cta["url"] else ""
    template = f"That's the usage report for {{{{{week_claim}}}}}. {cta['channel_line']}{tail}"
    return {"section": "cta", "text": template, "claims": [week_claim]}


def write_script(ep: Episode, llm: ScriptLLM) -> None:
    facts = read_json(ep.path("facts.json"))
    claims = {c["id"]: c for c in facts["claims"]}
    outline = build_outline(facts)
    prompts_dir = ep.path("prompts")
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "system.txt").write_text(SYSTEM_PROMPT)

    texts: dict[str, str] = {}
    openings: list[str] = []
    calls = []
    for i, block in enumerate(assign_blocks(outline), start=1):
        name = f"block{i:02d}"
        prompt = block_prompt(block, openings)
        (prompts_dir / f"{name}.prompt.txt").write_text(prompt)
        accepted = prompts_dir / f"{name}.accepted.json"
        prompt_sha = sha256_bytes(prompt.encode())
        by_id = {seg["id"]: seg for seg in block}

        def fixer(d, by_id=by_id, b=block):
            as_segments(d, b)
            for out in d["segments"]:
                if out["id"] in by_id:
                    out["text"] = repair(out["text"], by_id[out["id"]]["claims"], claims)

        def checker(d, b=block, n=name):
            return check_block(d, b) or verify_block(llm, ep, n, b, d, claims)

        # Resume: a block accepted earlier for the identical prompt is reused, so one stubborn block does
        # not throw away the others.
        cached = read_json(accepted) if accepted.exists() else None
        if cached and cached["prompt_sha256"] == prompt_sha:
            data, attempts = cached["data"], cached["attempts"]
        else:
            data, attempts = _ask(llm, ep, name, prompt, block_schema(block), checker, fixer,
                                  max_tokens=int(sum(seg["target_words"] for seg in block) * 3.2) + 150)
            write_json(accepted, {"prompt_sha256": prompt_sha, "attempts": attempts, "data": data})
        for out in data["segments"]:
            texts[out["id"]] = to_claim_ids(out["text"], by_id[out["id"]]["claims"])
            openings.append(" ".join(ALIAS.sub("#", out["text"]).split()[:7]))
        calls.append({"name": name, "segments": [s["id"] for s in block], "attempts": attempts,
                      "prompt_sha256": sha256_bytes(prompt.encode())})
        ep.log(f"    {name}: {[s['section'] for s in block]} ok ({attempts} attempt(s))")
    t_aliases = title_aliases(facts)
    prompt = title_prompt(facts, t_aliases)
    (prompts_dir / "titles.prompt.txt").write_text(prompt)

    def fix_titles(d):
        d["title_candidates"] = [repair(t, t_aliases, claims) for t in d["title_candidates"]]

    allowed = {a["alias"] for a in t_aliases}
    data, attempts = _ask(llm, ep, "titles", prompt, TITLE_SCHEMA,
                          lambda d: [e for t in d["title_candidates"] for e in check_text(t, allowed, "title")],
                          fix_titles)
    title_templates = [to_claim_ids(t, t_aliases) for t in data["title_candidates"]]
    calls.append({"name": "titles", "attempts": attempts, "prompt_sha256": sha256_bytes(prompt.encode())})

    segments = [{"id": seg["id"], "section": seg["section"], "player": seg["player"],
                 "target_words": seg["target_words"], "text_template": texts[seg["id"]]} for seg in outline]
    cta = cta_segment(ep, facts, claims)
    segments.append({"id": f"s{len(segments) + 1:02d}", "section": "cta", "player": None,
                     "target_words": len(cta["text"].split()), "text_template": cta["text"]})
    shots = []
    for seg, plan in zip(segments, outline + [{"shot": {"type": "cta_card"}}]):
        seg["text_display"] = fill(seg["text_template"], claims, "display")
        seg["text_spoken"] = fill(seg["text_template"], claims, "spoken")
        seg["claims_used"] = sorted(set(PLACEHOLDER.findall(seg["text_template"])))
        seg["shot"] = f"shot_{seg['id']}"
        shots.append({"id": seg["shot"], "segment": seg["id"], **plan["shot"]})
    used_ids = sorted({c for s in segments for c in s["claims_used"]}
                      | {c for t in title_templates for c in PLACEHOLDER.findall(t)})
    words = sum(len(s["text_spoken"].split()) for s in segments)
    script = {
        "episode_id": ep.id, "season": ep.season, "week": ep.week, "format": ep.fmt,
        "title_candidates": [fill(t, claims, "display") for t in title_templates],
        "title_templates": title_templates,
        "claims": [claims[c] for c in used_ids],
        "narration_segments": segments, "shots": shots,
        "expected_duration_seconds": round(words / WORDS_PER_SECOND),
        "made_for_kids": False, "synthetic_voice": True,
        "cta": {"url": ep.cfg["cta"]["url"] or None, "text": fill(cta["text"], claims, "display")},
        "facts_sha256": sha256_bytes(canonical_json(facts).encode()),
        "llm": {**llm.info(), "system_prompt_sha256": sha256_bytes(SYSTEM_PROMPT.encode()), "calls": calls},
    }
    write_json(ep.path("script.json"), script)
    ep.log(f"  script: {len(segments)} segments, {words} words (~{script['expected_duration_seconds']}s), "
           f"{len(used_ids)} claims used")


def run(ep: Episode) -> None:
    llm_cfg = {k: v for k, v in ep.cfg["llm"].items() if k != "base_url"}
    run_stage(ep, "script", VERSION, {"llm": llm_cfg, "cta": ep.cfg["cta"], "prompt": SYSTEM_PROMPT},
              ["facts.json"], ["script.json"], lambda: write_script(ep, make_llm(ep.cfg["llm"])))
