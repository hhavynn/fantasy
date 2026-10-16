"""review.md: the two-minute human accuracy check before anything is published.

Every sentence as it is shown, every number in it with the exact data it came from, the weekly series behind
the story, things a person should look at (QB changes, odd single games, names to listen for), and the
QC result.
"""

from __future__ import annotations

from ..core import Episode, read_json
from ..numbers import render
from .script import PLACEHOLDER, fill


def fill_display(template: str, claims: dict) -> str:
    return fill(template, claims, "display")

ODD_GAME_SNAPS = 0.30


def describe_source(src: dict) -> str:
    k = src["kind"]
    if k.startswith("fp_"):
        from .flame import describe
        return describe(src)
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
    if k == "usage_value":
        return f"player_week_usage.{src['column']}, week {src['week']} only (one game, not an average)"
    if k == "usage_sum":
        return f"player_week_usage.{src['column']}, summed over {wk}"
    if k == "team_week_sum":
        return f"player_week_usage.{src['column']} ({src['team']} team total per game), summed over {wk}"
    if k == "team_targets":
        return f"nfl_team_week.team_targets ({src['team']}, week {src['week']})"
    if k == "team_max_targets":
        return f"player_week_usage.targets: most by any other {src['team']} player, week {src['week']}"
    if k == "players_targeted":
        return f"player_week_usage: {src['team']} players with a target, week {src['week']}"
    if k in ("absent_weeks", "absent_together"):
        return f"player_week_usage: team played, no snaps ({wk})"
    if k == "absence_reason":
        return f"nfl_injuries.report_primary_injury ({wk})"
    if k == "player_status":
        return (f"nfl_rosters_weekly (IR) > nfl_injuries.report_status > practice_status, week {src['week']}; "
                "true only until this week's kickoff")
    if k == "data_as_of":
        return "sync_runs.finished_at (latest successful nflverse sync)"
    if k == "pooled_share":
        return (f"sum of player_week_usage.{src['column']} / sum of {src['team_column']} ({src['team']}), {wk} "
                "(pooled, so it matches the counts shown)")
    if k == "constant_weeks":
        return f"the window compared: {wk}"
    if k == "venue":
        return f"nfl_schedules ({src['team']}, week {src['week']}): home or away"
    return k.replace("_", " ")


def storyboard(ep: Episode, script: dict) -> list[str]:
    path = ep.path("work/timeline.json")
    if not path.exists():
        return []
    tl = read_json(path)
    segs = {s["id"]: s for s in script["narration_segments"]}
    out = ["## Storyboard", "", "| time | scene | in | on screen / said |", "|---|---|---|---|"]
    for sc in tl["scenes"]:
        seg = segs.get(sc.get("segment"), {})
        stage = f" ({sc['stage']})" if sc.get("stage") else ""
        out.append(f"| {sc['_start']:.1f}–{sc['_start'] + sc['_dur']:.1f}s | {sc['type']}{stage} | "
                   f"{sc.get('transition', 'push')} | {seg.get('text_display', '')} |")
    return out + [""]


def flags(facts: dict, claims: dict, fallback_words: list[str], text: str) -> list[str]:
    out = []
    for p in facts["players"]:
        if p.get("flame"):                   # one game: nothing windowed to flag
            continue
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
        said = list(dict.fromkeys(PLACEHOLDER.findall(seg["text_template"])))
        for cid in said:
            c = claims[cid]
            lines.append(f"- `{c['display']}`: {c['label']}. *{describe_source(c['source'])}*")
        shown = [c for c in seg.get("claims_on_screen", []) if c not in said]
        for cid in shown:
            c = claims[cid]
            lines.append(f"- on screen `{c['display']}`: {c['label']}. *{describe_source(c['source'])}*")
        lines.append("")
    lines += storyboard(ep, script)
    st = script["structure"]
    if st.get("hook_variants"):
        lines += ["## Opening hook variants", "", f"Used: **{st['hook']}**. Render another with `--hook <name>`.", ""]
        lines += [f"- `{k}`: {fill_display(v, claims)}" for k, v in st["hook_variants"].items()] + [""]
    if ep.path("editorial.json").exists():
        ed = read_json(ep.path("editorial.json"))
        lines += [f"## Editorial score: {ed['total']}/40 ({ed['format']}, {ed['verdict']})", "",
                  "Heuristic for choosing stories, not a retention prediction.", ""]
        lines += [f"- {k}: {ed['scores'][k]} ({ed['reasons'][k]})" for k in ed["scores"]] + [""]
        if ed.get("checks"):
            lines += ["Pre-render check:", ""]
            lines += [f"- {'yes' if v['pass'] else '**no**'}: {v['question']} ({v['why']})" for v in ed["checks"].values()]
            lines += [""] + ([f"Rejected: {'; '.join(ed['reject_reasons'])}", ""] if ed.get("reject_reasons") else [])
    if qc:
        tg = next((g for g in qc["gates"] if g["name"] == "timeliness"), None)
        if tg and tg.get("details", {}).get("valid_until"):
            lines += [f"**Publish before {tg['details']['valid_until']}** (kickoff): the injury status is only "
                      "current until then. Re-check the final injury report first.", ""]
    lines += ["## Weekly data behind the story", ""]
    for p in facts["players"]:
        if p.get("flame"):
            lead = p["lead"]
            lines += [f"**{p['name']}** ({p['position']}, {claims[p['team_name']]['display']}), {p['role']} on "
                      f"{lead['label']}: rank {lead['rank_value']} of {lead['games_value']} position games, "
                      f"z {lead['z_pos']:.2f} vs the position, {lead['z_own']:.2f} vs his own games", "",
                      "| week | " + lead["label"] + " |", "|---|---|"]
            lines += [f"| {claims[s['week_claim']]['display']} | {claims[s['value']]['display']} |" for s in lead["series"]]
            lines.append("")
            continue
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
