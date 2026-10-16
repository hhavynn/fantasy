"""Creative diagnostics: is this Short structurally different from the recent ones, and is it a good Short?

Two kinds of check, kept apart on purpose:

  measured (gate)  facts about the script that a program can establish: which show, opening mechanism, scene
                   sequence, ending mechanism and beat progression it shares with each recent Short; how many
                   distinct metrics it uses; how long any one chart holds the screen; whether it ends on a
                   recommendation, condition or rule. These fail QC when a Short repeats a recent one.
  judgement (notes) questions only a person can answer (is it interesting, does the pacing suit the story,
                   is the takeaway obvious to a casual fan). They are written to creative_review.md as review
                   prompts with the evidence, never scored.

Nothing here predicts retention or virality. When channel analytics exist (first-three-second retention,
average percentage viewed, completion, rewatches, shares, comments), RETENTION_FIELDS names the columns a
future analytics import should join to these fingerprints.
"""

from __future__ import annotations

DIMENSIONS = ("family", "hook_mechanism", "scenes", "ending_mechanism", "beats")
WINDOW = 8                  # compare with this many recent Shorts
REPEAT_MAX = 3              # sharing this many of the five dimensions with one recent Short is a repeat
STATS_MAX = 3               # distinct usage metrics on screen: one concept per Short
CHART_HOLD_MAX_S = 9.0      # one chart beat on screen longer than this is static
CHART_SCENES = {"bar_timeline", "target_balls", "split", "volume", "presence", "flag_chart", "zoom_out", "lag"}
RETENTION_FIELDS = ("retention_3s", "avg_pct_viewed", "completion_rate", "rewatches", "shares", "comments", "ctr")


def fingerprint(structure: dict) -> dict:
    """The five dimensions, with the fields older history rows did not record derived the same way new scripts
    record them (the family from the format or storyline, the opening and ending as mechanisms)."""
    from .shows import ENDING_OF_SCENE, HOOK_MECHANISM, LEGACY
    fmt = structure.get("format")
    family = structure.get("family") or LEGACY.get(fmt) or (
        "template:" + structure["angle"] if fmt in (None, "template") and structure.get("angle") else fmt)
    hook = structure.get("hook_mechanism") or HOOK_MECHANISM.get(structure.get("hook"), structure.get("hook"))
    scenes = tuple(structure.get("scenes") or ())
    ending = structure.get("ending_mechanism")
    if not ending:
        last = (structure.get("ending") or "").split(":")[0] or (scenes[-1] if scenes else None)
        ending = ENDING_OF_SCENE.get(last, last) if last else None
    return {"family": family, "hook_mechanism": hook, "scenes": scenes, "ending_mechanism": ending,
            "beats": tuple(structure.get("beats") or ())}


def from_script(script: dict) -> dict:
    """A script's structure with the fields older scripts did not record rebuilt from its shots (scene sequence,
    the ending as the last scene, the format as the family), so old episodes can be audited the same way."""
    from .shows import ENDING_OF_SCENE, LEGACY
    st = dict(script.get("structure") or {})
    shots = [s["type"] for s in script.get("shots", [])]
    st.setdefault("scenes", shots)
    st.setdefault("family", LEGACY.get(st.get("format"), st.get("format") or "template:" + str(st.get("angle"))))
    if "ending_mechanism" not in st and shots:
        st["ending_mechanism"] = ENDING_OF_SCENE.get(shots[-1], shots[-1])
    return st


def shared(a: dict, b: dict) -> list[str]:
    fa, fb = fingerprint(a), fingerprint(b)
    return [d for d in DIMENSIONS if fa[d] and fa[d] == fb[d]]


def compare(structure: dict, recent: list[dict]) -> list[dict]:
    """Per recent Short (history rows, oldest first): the dimensions this one shares with it."""
    out = []
    for e in recent[-WINDOW:]:
        s = e.get("structure") or {}
        dims = shared(structure, s)
        out.append({"episode_id": e["episode_id"], "shared": dims, "repeat": len(dims) >= REPEAT_MAX})
    return out


def checks(script: dict, durations: dict | None = None) -> list[tuple[str, bool, str]]:
    """Mechanical checks on one script: (name, passed, evidence)."""
    st = script["structure"]
    out = []
    metrics = st.get("metrics") or []
    out.append(("one_concept", len(metrics) <= STATS_MAX, f"{len(metrics)} usage metrics on screen: {metrics}"))
    out.append(("clear_takeaway", bool(st.get("ending_mechanism")) and st.get("ending_mechanism") != "buttons",
                f"ending mechanism: {st.get('ending_mechanism')}"))
    if durations:
        long_ = [(seg["id"], round(durations.get(seg["id"], 0), 1)) for seg, shot in
                 zip(script["narration_segments"], script["shots"])
                 if shot["type"] in CHART_SCENES and durations.get(seg["id"], 0) > CHART_HOLD_MAX_S]
        out.append(("no_static_chart", not long_, f"chart beats over {CHART_HOLD_MAX_S:.0f} s: {long_ or 'none'}"))
    return out


def review_notes(script: dict, comparisons: list[dict]) -> str:
    """Prompts for the human review, with the evidence a reviewer needs. Not scored."""
    st = script["structure"]
    lines = ["# Creative review", "",
             "Measured facts first; the questions below need a person. Nothing here predicts views.", "",
             f"- Show: **{st.get('family')}**; question: {st.get('question')}",
             f"- Opening mechanism: {st.get('hook_mechanism')}; ending mechanism: {st.get('ending_mechanism')}",
             f"- Scenes: {' > '.join(st.get('scenes', []))}",
             f"- Visual device: {st.get('visual_device')}", ""]
    reps = [c for c in comparisons if c["shared"]]
    lines.append("Shared with recent Shorts: " + ("; ".join(f"{c['episode_id']} ({', '.join(c['shared'])})" for c in reps)
                                                 if reps else "nothing"))
    lines += ["", "For the reviewer:",
              "1. Does the first line make someone who doesn't know the player keep watching?",
              "2. Does the narration add meaning, or just read what's on screen?",
              "3. Does the pacing suit the story (a warning should not feel like a hype video)?",
              "4. Is the takeaway obvious, and is the uncertainty stated honestly?",
              "5. Would a regular viewer feel they've seen this video before?"]
    return "\n".join(lines) + "\n"
