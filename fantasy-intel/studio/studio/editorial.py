"""Editorial desk for Shorts: which stories are worth a video, and which format tells each one.

Simple, inspectable rules, not a model. Every candidate gets eight 0-5 scores with the reason for each, so a
person can see why a story was picked or dropped. The scores are a heuristic for choosing what to make; they
are not a prediction of views or retention (only real analytics can say that).

Formats (each has its own opening, narration shape, visuals, transitions, music and ending):

  mystery      a contradiction in comparable games: the same playing time, very different targets.
               Opens on the contradiction, investigates with clues from the data, closes on a verdict.
  opportunity  a back whose teammate's absence handed him the backfield. Opens on the workload, shows the
               old split, the absence, the current status (dated), and how long the window may stay open.
  template     everything else: the earlier storyline engine (story.py), until more formats exist.
"""

from __future__ import annotations

import json
from pathlib import Path

CRITERIA = ("relevance", "hook", "surprise", "evidence", "actionability", "timeliness", "visual", "distinctiveness")
MIN_TOTAL = 24                 # of 40
MIN_EACH = {"relevance": 2, "evidence": 2}

SAME_SNAPS_PTS = 0.04          # two games this close in snap share are "the same playing time"
TARGET_SWING = 3.0             # ...and one had at least 3x the other's targets
TARGET_SWING_MIN = 5           # with at least this many in the big game
VOLUME_DROP = 0.8              # team targets this far below the game before: "threw less"
OPPORTUNITY_PTS = 30           # a vacated backfield role this big (displayed points) is a story on its own


def _v(claims: dict, cid):
    return claims[cid]["value"] if cid else None


def mystery_case(p: dict, claims: dict) -> dict | None:
    """Two consecutive recent games with the same playing time and very different targets."""
    games = [g for g in p.get("details", {}).get("games", []) if g.get("targets") and g.get("snap_share")]
    for a, b in zip(games, games[1:]):
        sa, sb = _v(claims, a["snap_share"]), _v(claims, b["snap_share"])
        ta, tb = _v(claims, a["targets"]), _v(claims, b["targets"])
        hi, lo = max(ta, tb), min(ta, tb)
        if abs(sa - sb) <= SAME_SNAPS_PTS and hi >= TARGET_SWING_MIN and hi >= TARGET_SWING * max(lo, 1):
            return {"games": [a, b], "falling": tb < ta}
    return None


def mystery_clues(p: dict, claims: dict, case: dict) -> list[dict]:
    """What the data can show about the swing, in order. Each clue is a fact, not a cause."""
    a, b = case["games"]
    clues = []
    ta, tb = _v(claims, a.get("team_targets")), _v(claims, b.get("team_targets"))
    if ta and tb and case["falling"] and tb <= VOLUME_DROP * ta:
        clues.append({"kind": "volume"})
    spread = p.get("details", {}).get("spread") or {}
    for r in spread.get("returned", []):
        if r.get("snaps") and _v(claims, r["snaps"]) >= _v(claims, b["snaps"]):
            clues.append({"kind": "returned", "who": r})
            break
    return clues


def opportunity_case(p: dict, claims: dict) -> dict | None:
    tm = p.get("teammate")
    lead = p["metrics"][0]
    if (p["position"] == "RB" and tm and tm["kind"] == "vacated" and not tm["returned"]
            and "carries" in p.get("details", {}) and lead["column"] == "backfield_rush_share"
            and round(_v(claims, lead["change"]) * 100) >= OPPORTUNITY_PTS):
        return {"teammate": tm}
    return None


def choose_format(p: dict, claims: dict) -> str:
    if opportunity_case(p, claims):
        return "opportunity"
    case = mystery_case(p, claims)
    if case and mystery_clues(p, claims, case):
        return "mystery"
    return "template"


def recognizable(p: dict) -> bool:
    """A rough, public proxy for name recognition: a first-round pick or a 5th-year+ veteran."""
    return p.get("draft_round") == 1 or (p.get("years_exp") or 0) >= 5


def history(episodes_dir: Path, season: int, before_week: int | None = None, exclude: str = "") -> list[dict]:
    """Earlier Shorts (with their structure), oldest first; `before_week` keeps only earlier weeks."""
    path = episodes_dir / "history.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return [r for r in rows if r.get("format") == "short" and r.get("season") == season and r.get("episode_id") != exclude
            and r.get("structure") and (before_week is None or r["week"] < before_week)]


def score(p: dict, claims: dict, fmt: str, recent: list[dict]) -> dict:
    """Eight 0-5 scores with reasons. `recent`: prior Shorts' history rows, oldest first."""
    s, why = {}, {}
    lead = p["metrics"][0]
    snaps = next((m for m in p["metrics"] if m["column"] == "offense_pct"), None)
    level = _v(claims, snaps["recent"]) if snaps else 0
    if p["position"] == "RB":
        bf = next((m for m in p["metrics"] if m["column"] == "backfield_rush_share"), None)
        level = max(level, _v(claims, bf["recent"]) if bf else 0)
    rel = 5 if level >= 0.75 else 4 if level >= 0.6 else 3 if level >= 0.45 else 2 if level >= 0.3 else 1
    if recognizable(p):
        rel = min(5, rel + 1)
    s["relevance"], why["relevance"] = rel, f"recent usage level {level:.0%}" + (", recognizable" if recognizable(p) else "")

    dot = max(abs(m["delta_over_threshold"]) for m in p["metrics"])
    if fmt == "mystery":
        s["hook"], why["hook"] = 5, "a contradiction in comparable games"
    elif fmt == "opportunity":
        pts = round(_v(claims, lead["change"]) * 100)
        s["hook"], why["hook"] = (5 if pts >= 50 else 4), f"backfield share up {pts} pts with the starter out"
    else:
        s["hook"], why["hook"] = 2, "a usage change without a twist"
    s["surprise"] = 5 if dot >= 8 else 4 if dot >= 5 else 3 if dot >= 3 else 2 if dot >= 1.5 else 1
    why["surprise"] = f"largest move {dot:.1f}x its threshold"
    if fmt == "mystery":
        case = mystery_case(p, claims)
        a, b = case["games"]
        s["surprise"], why["surprise"] = 5, (f"{claims[a['targets']]['display']} then {claims[b['targets']]['display']} "
                                             f"targets on {claims[a['snaps']]['display']} and "
                                             f"{claims[b['snaps']]['display']} snaps")

    ev = 1 + (2 if lead.get("sustained", True) else 0) + (1 if len(p["recent_weeks"]) >= 2 else 0) \
        + (1 if len(p["baseline_weeks"]) >= 3 else 0)
    notes = ["lead metric moved in every recent game" if lead.get("sustained", True) else "lead metric not sustained",
             f"{len(p['recent_weeks'])} recent / {len(p['baseline_weeks'])} earlier games"]
    if fmt == "mystery":
        notes.append("single-game comparison: says what happened, not what will")
        ev -= 1
    s["evidence"], why["evidence"] = max(1, min(5, ev)), "; ".join(notes)

    tm = p.get("teammate")
    if fmt == "opportunity":
        code = _v(claims, tm["status"])[0]
        s["actionability"], why["actionability"] = 5, f"start/sit while the starter is out (status: {code})"
    elif fmt == "mystery":
        s["actionability"], why["actionability"] = 3, "hold/start decision; no waiver call"
    else:
        s["actionability"], why["actionability"] = 3 if p["role"] == "riser" else 2, "general usage note"

    time_ = 3
    tnotes = ["data through the latest week"]
    if fmt == "opportunity" and tm and "status" in tm:
        code, week, _ = _v(claims, tm["status"])
        if week == p.get("_episode_week", week):
            time_ += 2
            tnotes.append(f"decision hinges on this week's report ({code}); expires at kickoff")
    s["timeliness"], why["timeliness"] = min(5, time_), "; ".join(tnotes)

    if fmt in ("mystery", "opportunity"):
        s["visual"], why["visual"] = 5, "format has its own story-specific scenes (counts, faces, a reveal)"
    else:
        s["visual"], why["visual"] = 2, "generic template scenes (card, chart, lineup)"

    dist, dnotes = 5, []
    last2 = [r.get("structure", {}).get("format", r.get("structure", {}).get("angle")) for r in recent[-2:]]
    if len(last2) == 2 and all(f == fmt for f in last2):
        dist -= 2
        dnotes.append(f"third {fmt} in a row")
    if any(p["gsis_id"] in r.get("players", []) for r in recent[-6:]):
        dist -= 2
        dnotes.append("featured in the last six Shorts")
    s["distinctiveness"], why["distinctiveness"] = max(0, dist), "; ".join(dnotes) or "fresh format and player"

    total = sum(s[k] for k in CRITERIA)
    reject = [f"total {total} < {MIN_TOTAL}"] if total < MIN_TOTAL else []
    reject += [f"{k} {s[k]} < {v}" for k, v in MIN_EACH.items() if s[k] < v]
    if s["hook"] < 4 and not recognizable(p):
        reject.append("no clear reason to care: a usage move without a twist, on a player few will know")
    return {"player": p["name"], "format": fmt, "scores": s, "reasons": why, "total": total,
            "recognizable": recognizable(p), "verdict": "reject" if reject else "make", "reject_reasons": reject}


def board_markdown(rows: list[dict]) -> str:
    head = "| player | format | " + " | ".join(c[:5] for c in CRITERIA) + " | total | verdict |"
    lines = ["# Editorial board", "",
             "Heuristic scores (0-5) for choosing what to make; not a prediction of audience retention.", "",
             head, "|" + "---|" * (len(CRITERIA) + 4)]
    for r in sorted(rows, key=lambda r: -r["total"]):
        lines.append(f"| {r['player']} | {r['format']} | " + " | ".join(str(r["scores"][c]) for c in CRITERIA)
                     + f" | {r['total']} | {r['verdict']}{' (' + '; '.join(r['reject_reasons']) + ')' if r['reject_reasons'] else ''} |")
    lines += ["", "Reasons:", ""]
    for r in sorted(rows, key=lambda r: -r["total"]):
        lines.append(f"- **{r['player']}** ({r['format']}): " + "; ".join(f"{k}: {r['reasons'][k]}" for k in CRITERIA))
    return "\n".join(lines) + "\n"
