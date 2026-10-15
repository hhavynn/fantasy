from __future__ import annotations

import re

from studio.core import read_json, run_stage, write_json
from studio.numbers import fmt_pct, fmt_pp, fmt_signed_pct1, fmt_team, ordinal_words
from studio.stages import facts, script
from studio.stages.facts import evaluate_claim
from studio.stages.script import check_text, repair, to_claim_ids


def test_number_formats():
    assert fmt_pct(0.875) == ("88%", "eighty-eight percent")
    assert fmt_pct(0.125) == ("13%", "thirteen percent")          # half-up, not banker's rounding
    assert fmt_pp(-0.12) == ("−12 pts", "twelve percentage points")
    assert fmt_signed_pct1(-4.349) == ("−4.3%", "minus four point three percent")
    assert ordinal_words(21) == "twenty-first" and ordinal_words(12) == "twelfth" and ordinal_words(30) == "thirtieth"
    assert fmt_team("SF") == ("49ers", "Niners")


def test_selection_and_claims_rederive(episode):
    facts.run(episode)
    f = read_json(episode.path("facts.json"))
    roles = {p["gsis_id"]: p["role"] for p in f["players"]}
    assert roles == {"00-RISER": "riser", "00-FALLER": "faller"}   # steady and one-game-exit players excluded
    snap = read_json(episode.path("snapshot.json"))
    for claim in f["claims"]:
        assert evaluate_claim(snap, claim["source"]) == claim["value"], claim["id"]
    riser = next(p for p in f["players"] if p["role"] == "riser")
    assert riser["metrics"][0]["label"] == "snap share"             # biggest move relative to threshold leads


def test_stage_skips_when_inputs_unchanged(episode):
    calls = []

    def work():
        calls.append(1)
        write_json(episode.path("out.json"), {"n": len(calls)})

    assert run_stage(episode, "demo", 1, {"a": 1}, ["snapshot.json"], ["out.json"], work)
    assert not run_stage(episode, "demo", 1, {"a": 1}, ["snapshot.json"], ["out.json"], work)
    assert run_stage(episode, "demo", 1, {"a": 2}, ["snapshot.json"], ["out.json"], work)   # params changed
    assert len(calls) == 2


def test_checks_reject_numbers_invented_placeholders_and_banned_phrases():
    allowed = {"snap_share_now", "qb"}
    assert check_text("Snap share hit {{snap_share_now}} with {{qb}}.", allowed, "s") == []
    errs = " ".join(check_text("He played 3 games, two of them big, {{made_up}}. His PPR line and routes.",
                               allowed, "s"))
    for needle in ("digits", "'two'", "made_up", "'PPR'", "'routes'"):
        assert needle in errs
    assert check_text("Watch if he returns from injury.", allowed, "s")


def test_repair_maps_typed_values_back_to_placeholders():
    claims = {"c1": {"display": "Week 15", "spoken": "Week fifteen", "format": "week", "value": 15},
              "c2": {"display": "+12 pts", "spoken": "twelve percentage points", "format": "pp", "value": 0.12},
              "c3": {"display": "88%", "spoken": "eighty-eight percent", "format": "pct", "value": 0.88}}
    seg = [{"alias": "week15", "id": "c1"}, {"alias": "snap_change", "id": "c2"}, {"alias": "snap_now", "id": "c3"}]
    text = repair("In Week fifteen he gained +{{snap_change}} percentage points to 88%.", seg, claims)
    assert text == "In {{week15}} he gained {{snap_change}} to {{snap_now}}."
    assert to_claim_ids(text, seg) == "In {{c1}} he gained {{c2}} to {{c3}}."


def test_script_end_to_end_with_fake_llm(episode):
    facts.run(episode)
    script.run(episode)
    s = read_json(episode.path("script.json"))
    assert s["made_for_kids"] is False and s["synthetic_voice"] is True
    assert len(s["title_candidates"]) == 3
    claims = {c["id"]: c for c in s["claims"]}
    for seg in s["narration_segments"]:
        assert not re.search(r"\{\{|\}\}", seg["text_display"] + seg["text_spoken"])
        assert not re.search(r"\d", seg["text_spoken"]), seg["text_spoken"]     # TTS reads words only
        assert set(seg["claims_used"]) <= set(claims)
    assert s["narration_segments"][-1]["section"] == "cta"
    assert {sh["type"] for sh in s["shots"]} >= {"hero_stat", "usage_chart", "context_card", "playoff_table"}
