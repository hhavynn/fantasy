"""review.md: the two-minute human accuracy check before anything is published.

Every sentence as it is shown, every number in it with the exact data it came from, the weekly series behind
the story, things a person should look at (QB changes, odd single games, names to listen for), and the
QC result.
"""

from __future__ import annotations

from ..core import Episode, read_json
from ..numbers import render
from .script import PLACEHOLDER

ODD_GAME_SNAPS = 0.30


def describe_source(src: dict) -> str:
    k = src["kind"]
    weeks = src.get("weeks") or src.get("recent_weeks")
    wk = f"weeks {', '.join(map(str, weeks))}" if weeks else ""
    if k == "usage_mean":
        return f"player_week_usage.{src['column']}, mean of {wk}"
    if k == "usage_delta":
        return (f"player_week_usage.{src['column']}: mean of weeks {src['recent_weeks']} minus mean of weeks "
                f"{src['baseline_weeks']}")
    if k == "games_count":
        return f"games played in {wk}"
    if k in ("team_context", "team_context_rank"):
        return f"team_context.{src['column']} ({src['team']}){', league rank' if k.endswith('rank') else ''}"
    if k in ("defense_rank", "defense_band"):
        return f"defense_context.ranks.{src['rank_key']} ({src['team']}){', graded 1-10 / 23-32' if k == 'defense_band' else ''}"
    if k in ("opponent", "playoff_opponents"):
        return f"nfl_schedules ({src['team']}, week{'s' if 'weeks' in src else ''} {src.get('week') or src.get('weeks')})"
    if k == "playoff_summary":
        return f"defense_context.ranks.{src['rank_key']} for {src['team']} opponents in weeks {src['weeks']}"
    if k == "qb_names":
        return f"nfl_schedules QB of record ({src['team']}, {wk})"
    if k == "proe_tendency":
        return f"team_context.proe league rank ({src['team']}): top 10 pass-heavy, bottom 10 run-heavy"
    return k.replace("_", " ")


def flags(facts: dict, claims: dict, fallback_words: list[str], text: str) -> list[str]:
    out = []
    for p in facts["players"]:
        qbs = claims[p["context"]["qbs_recent"]]["value"]
        if len(qbs) > 1:
            out.append(f"{p['name']}: QB change in the recent window ({', '.join(qbs)}). Is the role tied to the QB?")
        snaps = next((m["series"] for m in p["metrics"] if m["column"] == "offense_pct"), None)
        if snaps is None:
            continue
        vals = [s["value"] for s in snaps if s["value"] is not None]
        med = sorted(vals)[len(vals) // 2] if vals else 0
        odd = [s for s in snaps if s["value"] is not None and s["value"] < ODD_GAME_SNAPS <= med]
        for s in odd:
            out.append(f"{p['name']}: week {s['week']} snap share {render('pct', s['value'])[0]} is far below his "
                       f"usual. Injury exit or blowout? Check before publishing.")
    names = [w for w in fallback_words if w in text]
    if names:
        out.append("Listen for these pronunciations (espeak-ng fallback): " + ", ".join(names))
    return out


def build(ep: Episode) -> str:
    script = read_json(ep.path("script.json"))
    facts = read_json(ep.path("facts.json"))
    claims = {c["id"]: c for c in facts["claims"]}
    qc = read_json(ep.path("qc.json")) if ep.path("qc.json").exists() else None
    timings = read_json(ep.path("audio/timings.json")) if ep.path("audio/timings.json").exists() else {}
    lines = [f"# Review: {script['title_candidates'][0]}", "",
             f"Episode `{ep.id}`. Data through week {facts['data_through_week']}. Video: `final.mp4`.", ""]
    if qc:
        failed = [g for g in qc["gates"] if not g["passed"]]
        lines.append(f"**QC: {'PASSED' if qc['passed'] else 'FAILED'}** ({len(qc['gates']) - len(failed)}/"
                     f"{len(qc['gates'])} gates)")
        for g in failed:
            lines.append(f"- {g['name']}: " + "; ".join(g["failures"][:5]))
        lines.append("")
    lines += ["## Title options", *[f"{i}. {t}" for i, t in enumerate(script["title_candidates"], 1)], ""]
    lines += ["## What it says, and where every number comes from", ""]
    for seg in script["narration_segments"]:
        lines.append(f"**{seg['section']}**: {seg['text_display']}")
        for cid in dict.fromkeys(PLACEHOLDER.findall(seg["text_template"])):
            c = claims[cid]
            lines.append(f"- `{c['display']}`: {c['label']}. *{describe_source(c['source'])}*")
        lines.append("")
    lines += ["## Weekly data behind the story", ""]
    for p in facts["players"]:
        weeks = [s["week"] for s in p["metrics"][0]["series"]]
        lines.append(f"**{p['name']}** ({p['position']}, {claims[p['team_name']]['display']}), recent window weeks "
                     f"{p['recent_weeks']}, earlier window weeks {p['baseline_weeks']}")
        lines.append("")
        lines.append("| metric | " + " | ".join(f"Wk {w}" for w in weeks) + " |")
        lines.append("|---|" + "---|" * len(weeks))
        for m in p["metrics"]:
            vals = {s["week"]: render("pct", s["value"])[0] if s["value"] is not None else "—" for s in m["series"]}
            lines.append(f"| {m['label']} | " + " | ".join(vals.get(w, "—") for w in weeks) + " |")
        lines.append("")
    text = " ".join(s["text_display"] for s in script["narration_segments"])
    checks = flags(facts, claims, timings.get("fallback_words", []), text)
    lines += ["## Check by eye", *([f"- {c}" for c in checks] or ["- Nothing flagged automatically."]),
              "- Does the story make sense for a fantasy manager this week?",
              "- Watch the video once with sound: captions in sync, names pronounced right.", ""]
    return "\n".join(lines)


def run(ep: Episode) -> None:
    ep.path("review.md").write_text(build(ep))
    ep.log("  review: review.md")
