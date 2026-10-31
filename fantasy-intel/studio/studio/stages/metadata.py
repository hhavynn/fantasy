"""Stage 8: metadata.json for a PRIVATE upload (title options, description with chapters, AI-voice disclosure
and data attribution). The episode's entry in episodes/history.jsonl is written by record_history, once QC passes."""

from __future__ import annotations

import json

from ..core import Episode, read_json, run_stage, write_json

VERSION = 1
AI_DISCLOSURE = "Narrated with an AI voice."
AI_DISCLOSURE_CLONE = "Narrated with an AI voice (a synthetic version of the creator's own voice)."
ATTRIBUTION = ("Data: nflverse (CC BY 4.0): play-by-play, snap counts and schedules. "
               "Usage figures are from games before the week shown.")
CHAPTER_NAMES = {"cold_open": "Intro", "method": "What we measure", "playoff_watch": "Fantasy playoff schedules",
                 "cta": "Wrap-up"}


def _ts(t: float) -> str:
    t = int(t)
    return f"{t // 60}:{t % 60:02d}"


def chapters(script: dict, durations: dict[str, float], players: list[dict]) -> list[dict]:
    """YouTube chapters: start at 0:00, at least 3, each at least 10 s (short ones merge into the previous)."""
    names = {p["gsis_id"]: p for p in players}
    out, t = [], 0.0
    for seg in script["narration_segments"]:
        sec = seg["section"]
        if sec in CHAPTER_NAMES:
            title = CHAPTER_NAMES[sec]
        elif sec.endswith("_role"):
            title = f"{'Riser' if sec.startswith('riser') else 'Faller'}: {names[seg['player']]['name']}"
        else:
            title = None                     # intros and context segments continue the current chapter
        if title and (not out or t - out[-1]["start"] >= 10):
            out.append({"start": t, "title": title})
        t += durations[seg["id"]]
    return out


def _run(ep: Episode) -> None:
    script = read_json(ep.path("script.json"))
    facts = read_json(ep.path("facts.json"))
    claims = {c["id"]: c for c in facts["claims"]}
    timings = read_json(ep.path("audio/timings.json"))
    durations = {s["id"]: s["duration"] for s in timings["segments"]}
    short = ep.fmt == "short"
    chaps = [] if short else chapters(script, durations, facts["players"])
    week = claims[facts["episode"]["week"]]["display"]
    risers = [p["name"] for p in facts["players"] if p["role"] == "riser"]
    fallers = [p["name"] for p in facts["players"] if p["role"] == "faller"]
    hero = facts["players"][0]
    m = hero.get("metrics", [None])[0]
    hook = (f"{hero['name']}'s {m['label']} went from {claims[m['baseline']]['display']} to "
            f"{claims[m['recent']]['display']}.") if m else ""
    cta = script["cta"]
    disclosure = AI_DISCLOSURE_CLONE if (short and ep.cfg["shorts"].get("voice_clone")) else AI_DISCLOSURE
    if hero.get("flame"):                   # flame / praise: one game against the season (stages/flame.py)
        from .flame import description_lines
        series = script["structure"].get("series")
        attribution = (ATTRIBUTION if not series else
                       "Data: nflverse (CC BY 4.0): game stats, play-by-play and schedules, through the game shown.")
        lines = [*description_lines(hero, claims, script["structure"]), "", disclosure, attribution, "",
                 ("#shorts #nfl #football #aiparody" if series == "ai_parody" else "#shorts #nfl #football")
                 if series else "#shorts #fantasyfootball #nfl"]
    elif short:
        lines = [hook, f"Over his last {claims[hero['recent_games']]['display']} games versus the "
                 f"{claims[hero['baseline_games']]['display']} before. Usage, not box scores.", "",
                 disclosure, ATTRIBUTION, "", "#shorts #fantasyfootball #nfl"]
    else:
        lines = [
            hook,
            f"The {week} usage report: the biggest NFL role changes by snap share, target share, air-yard share and "
            "backfield rush share, with team pass tendency and each player's fantasy-playoff schedule. "
            "No fantasy points, no projections.",
            "",
            *[f"{_ts(c['start'])} {c['title']}" for c in chaps],
            "",
            "Risers: " + ", ".join(risers),
            "Fallers: " + ", ".join(fallers),
            "",
            AI_DISCLOSURE,
            ATTRIBUTION,
        ]
    if cta["url"]:
        lines += ["", f"Free usage tool: {cta['url']}"]
    if hero.get("flame") and script["structure"].get("series"):     # NFL entertainment: football tags, not waivers
        from .showrunner import tags as series_tags
        tags = series_tags(hero, claims)
    else:
        tags = (["shorts"] if short else []) + ["fantasy football", "nfl usage", "snap share", "target share", "fantasy football waivers",
            f"fantasy football {week.lower()}", *risers, *fallers]
    meta = {
        "episode_id": ep.id,
        "title": script["title_candidates"][0],
        "title_candidates": script["title_candidates"],
        "description": "\n".join(lines),
        "tags": tags[:30],
        "category_id": "17",                       # Sports
        "default_language": "en",
        "default_audio_language": "en",
        "privacy_status": "private",               # never public or scheduled; publishing is a human action
        "self_declared_made_for_kids": script["made_for_kids"],
        "contains_synthetic_media": script["synthetic_voice"],
        "chapters": chaps,
        "files": {"video": "final.mp4", "thumbnail": "thumbnail.png", "captions": "captions.srt"},
    }
    if script["structure"].get("series"):   # soundboard clips (sfx_palette registry): the ones that could draw a claim
        from ..sfx_palette import PALETTE
        meta["contains_soundboard_audio"] = sorted(
            {c["sfx"] for seg in script["narration_segments"] for c in seg["scene"].get("cues", [])
             if c.get("sfx") and PALETTE.get(c["sfx"], {}).get("registry")})
    if script["structure"].get("series") == "ai_parody":
        meta["ai_parody"] = True
    if short:
        # What varies between Shorts, recorded so analytics can compare one variable at a time later.
        meta["experiment"] = {**script["structure"], "duration_s": round(sum(durations.values()), 1),
                              "player": hero["name"], "position": hero["position"]}
    write_json(ep.path("metadata.json"), meta)
    ep.log(f"  metadata: {len(chaps)} chapters, title: {meta['title']!r}")


def record_history(ep: Episode) -> bool:
    """Add this episode to episodes/history.jsonl (replacing an earlier row for it). Called only once QC has
    passed: a failed Short never ships, so it must not count against the next ones' originality and variety
    gates. A hook variant is an experiment beside its story, never history. Returns whether a row was written."""
    if ep.fmt == "short" and read_json(ep.path("story.json")).get("variant_of"):
        ep.log("  history: hook variant, not added")
        return False
    script = read_json(ep.path("script.json"))
    facts = read_json(ep.path("facts.json"))
    history = ep.cfg.episodes_dir / "history.jsonl"
    entries = [json.loads(line) for line in history.read_text().splitlines() if line.strip()] if history.exists() else []
    entries = [e for e in entries if e["episode_id"] != ep.id]
    entry = {"episode_id": ep.id, "season": ep.season, "week": ep.week, "format": ep.fmt,
             "players": [p["gsis_id"] for p in facts["players"]],
             "narration": [s["text_display"] for s in script["narration_segments"]]}
    if ep.fmt == "short":
        entry |= {"structure": script["structure"], "order": read_json(ep.path("story.json"))["order"]}
    entries.append(entry)
    entries.sort(key=lambda e: (e["season"], e["week"], e["format"], e.get("order", 0)))
    history.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
    ep.log(f"  history: {ep.id} added")
    return True


def run(ep: Episode) -> None:
    run_stage(ep, "metadata", VERSION, {"cta": ep.cfg["cta"]},
              ["script.json", "facts.json", "audio/timings.json"], ["metadata.json"], lambda: _run(ep))
