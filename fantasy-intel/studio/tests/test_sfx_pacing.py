"""Punchline-aware sound effects (sfx_palette, series_scenes cues, series_audio mix) and the pacing audit."""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from studio import series_audio as SA
from studio import series_scenes as SS
from studio import sfx_palette as P
from studio import pacing
from studio.stages import showrunner as SR

from test_series import qb_facts, receiver


# ---------------------------------------------------------------------------------------------- palette

@pytest.mark.parametrize("sfx_id", [k for k, v in P.PALETTE.items() if v["make"] is not None and not v.get("registry")])
def test_every_effect_renders_clean(sfx_id):
    x = P.render(sfx_id, 0)
    assert x.dtype == np.float32 and np.isfinite(x).all()
    assert 0.05 < np.max(np.abs(x)) <= 1.0
    assert np.max(np.abs(x[-48:])) < 0.02                       # fades out: no click at the end


def test_every_recorded_file_is_licensed_in_the_manifest():
    man = json.loads((P.SFX_DIR / "manifest.json").read_text())
    rec = {r["file"]: r for r in man["recorded"]}
    assert set(rec) == set(P.RECORDED)
    for name, r in rec.items():
        assert r["license"] == "CC0-1.0" and r["commercial_use"] and "tiktok" in r["platforms"]
        assert hashlib.sha256((P.SFX_DIR / name).read_bytes()).hexdigest() == r["sha256"]
    on_disk = {p.name for p in P.SFX_DIR.glob("*.wav")}
    assert on_disk == set(P.RECORDED)                            # nothing unlicensed sitting in the folder


def test_formats_have_their_own_sound():
    assert set(P.FORMAT_SFX["courtroom"]) & {"gavel", "gavel_single", "evidence_stamp"}
    assert not set(P.FORMAT_SFX["courtroom"]) & {"choir", "crowd_cheer", "air_horn"}
    assert set(P.FORMAT_SFX["main_character"]) != set(P.FORMAT_SFX["fan_meltdown"])
    for fmt, ids in P.FORMAT_SFX.items():
        assert all(i in P.PALETTE for i in ids), fmt


# ---------------------------------------------------------------------------------------------- selection

def _cues(*events):
    return [{"event": e, "word": "x", "at": "start", "prefer": []} for e in events]


@pytest.fixture
def every_file(monkeypatch):
    """Selection logic as if every meme file were fetched (no audio is rendered by these tests)."""
    monkeypatch.setattr(P, "available", lambda s: True)


def test_selection_fits_the_beat_and_the_format_and_never_repeats_in_a_video(every_file):
    out = P.select(_cues("punchline", "stat_reveal", "roast_impact", "final_payoff"), "comparison", "flame", [], "s")
    ids = [c["sfx"] for c in out]
    assert all(i in P.FORMAT_SFX["comparison"] for i in ids)
    assert len(set(ids)) == len(ids)
    for c in out:
        assert c["event"] in P.PALETTE[c["sfx"]]["purpose"]
        assert P.PALETTE[c["sfx"]]["tone"] in ("any", "flame")


def test_repeatable_cues_may_reuse(every_file):
    cues = [{"event": "punchline", "claim": f"c{i}", "at": "start", "prefer": ["bruh"], "context": "setup_aside",
             "repeatable": True} for i in range(3)]
    out = P.select(cues, "comparison", "flame", [], "seed")
    assert [c["sfx"] for c in out] == ["bruh"] * 3


def test_recent_videos_steer_the_choice_and_selection_is_deterministic(every_file):
    cue = _cues("roast_impact")
    a = P.select(cue, "courtroom", "flame", [], "s")[0]["sfx"]
    b = P.select(cue, "courtroom", "flame", [[a], [a]], "s")[0]["sfx"]
    assert a != b
    assert P.select(cue, "courtroom", "flame", [], "s") == P.select(cue, "courtroom", "flame", [], "s")


# ---------------------------------------------------------------------------------------------- timing

TOKS = [{"text": "We", "start": 0.0, "end": 0.2, "claim": None}, {"text": "needed", "start": 0.2, "end": 0.5, "claim": None},
        {"text": "4.3", "start": 0.5, "end": 0.9, "claim": "c1"}, {"text": "ruler.", "start": 0.9, "end": 1.3, "claim": None}]


def test_anchors_resolve_to_the_word_or_the_number():
    assert SS.anchor_time({"word": "ruler", "at": "end"}, TOKS) == 1.3
    assert SS.anchor_time({"word": "needed", "at": "start"}, TOKS) == 0.2
    assert SS.anchor_time({"claim": "c1", "at": "start"}, TOKS) == 0.5
    assert SS.anchor_time({"word": "missing"}, TOKS) is None


class _C:
    tokens = TOKS


def test_the_hit_not_the_file_lands_on_the_word():
    scene = {"cues": [{"event": "stat_reveal", "claim": "c1", "at": "start", "sfx": "slide_down", "variant": 0,
                       "gain": -9},
                      {"event": "punchline", "word": "nowhere", "at": "end", "sfx": "boing", "variant": 0, "gain": -6}]}
    ev = SS.sounds(scene, 10.0, 2.0, _C())
    assert len(ev) == 1                                          # an unresolvable cue is dropped, not moved
    at, kind, _ = ev[0]
    assert kind.startswith("meme:slide_down")
    assert at + P.PALETTE["slide_down"]["attack"] == pytest.approx(10.5)


def test_every_cue_in_every_format_points_at_its_own_line():
    _, facts, claims = qb_facts()
    p = facts["players"][0]
    rp, rclaims = receiver()
    for person, cl, fmts in ((p, claims, ("comparison", "roast_monologue", "courtroom")),
                             (rp, rclaims, ("main_character", "fan_meltdown", "comparison"))):
        for fmt in fmts:
            brief = SR.choose(person, [], fmt)
            beats, _ = SR.compose({"players": [person]}, cl, brief)
            for b in beats:
                for c in b["scene"]["cues"]:
                    if c.get("claim"):
                        assert "{{" + c["claim"] + "}}" in b["text_template"], (fmt, c)
                    else:
                        assert c["word"].lower() in b["text_template"].lower(), (fmt, c)


def test_payoff_sounds_get_room_before_the_cut():
    _, facts, claims = qb_facts()
    brief = SR.choose(facts["players"][0], [], "roast_monologue")
    beats, _ = SR.compose(facts, claims, brief)
    SR.assign_sfx(beats, brief, [], "seed")
    assert beats[-1]["pause"] >= 0.45 and all(c.get("sfx") for c in beats[-1]["scene"]["cues"])


# ------------------------------------------------------------------------------------------------- mix

def _voice(seconds=3.0):
    t = np.arange(int(seconds * SA.SR)) / SA.SR
    v = 0.3 * np.sin(2 * np.pi * 180 * t) * (t > 0.5) * (t < 1.5)       # speech only between 0.5 and 1.5 s
    return v.astype(np.float32)


def test_meme_cues_are_in_the_mix_only_when_asked():
    st = {"music_mood": None, "music": False}
    ev = [(2.0, "meme:boom:0:stat_reveal", -3)]
    with_fx = SA.mix(_voice(), ev, st, [], {"music": False})[:, 0]
    without = SA.mix(_voice(), ev, st, [], {"music": False}, with_meme=False)[:, 0]
    i = int(2.0 * SA.SR)
    assert np.abs(with_fx[i:i + 4800]).max() > 0.1 and np.abs(without[i:i + 4800]).max() < 1e-3


def test_a_crowd_never_talks_over_the_narrator():
    st = {"music_mood": None, "music": False}
    x = SA.mix(_voice(), [(0.5, "meme:crowd_cheer:0:final_payoff", -3)], st, [], {"music": False})[:, 0]
    alone = SA.mix(np.zeros_like(_voice()), [(0.5, "meme:crowd_cheer:0:final_payoff", -3)], st, [],
                   {"music": False})[:, 0]
    assert np.abs(x).max() <= 1.0
    mid = slice(int(0.9 * SA.SR), int(1.2 * SA.SR))                 # while he speaks the crowd is pulled down
    assert np.sqrt(np.mean((x[mid] - _voice()[mid] / np.abs(_voice()).max() * 10 ** (-3 / 20)) ** 2)) < \
        np.sqrt(np.mean(alone[mid] ** 2))


def test_music_cut_silences_the_bed():
    st = {"music_mood": "court", "music": True}
    v = np.zeros(int(3 * SA.SR), np.float32)
    v[-2400:] = 0.3                                                  # a word at the very end, nothing before it
    x = SA.mix(v, [(1.0, "meme:music_cut:0:awkward_pause", 0)], st, [], {"music": True, "music_db": -20})[:, 0]
    assert np.abs(x[int(1.1 * SA.SR):int(1.8 * SA.SR)]).max() < 0.01 < np.abs(x[int(0.2 * SA.SR):int(0.9 * SA.SR)]).max()


# ---------------------------------------------------------------------------------------------- pacing audit

def _m(**kw):
    base = {"first_word_s": 0.05, "first_hook_s": 1.0, "first_payoff_s": 2.0, "longest_voice_gap_s": 0.4,
            "longest_voice_gap_at_s": 3.0, "duration_s": 15.0, "max_sync_error_s": 0.0, "static_pct": 10.0,
            "sfx_events": 3, "sfx_over_speech": [], "sfx": [{"sfx": "boom"}, {"sfx": "bonk"}]}
    return {**base, **kw}


ST = {"duration_range": [14, 19], "pacing": {"hook_s": 3.0, "payoff_s": 4.0, "gap_s": 0.6}}


def test_a_tight_short_passes():
    assert pacing.audit(_m(), ST)["pass"]


@pytest.mark.parametrize("kw,needle", [({"first_payoff_s": 8.6}, "late"), ({"longest_voice_gap_s": 1.4}, "without a word"),
                                       ({"first_word_s": 1.0}, "dead air"), ({"max_sync_error_s": 0.2}, "off its word"),
                                       ({"duration_s": 31.0}, "format"), ({"first_hook_s": 6.0}, "first sentence")])
def test_the_audit_fails_what_drags_or_misses(kw, needle):
    f = pacing.audit(_m(**kw), ST)
    assert not f["pass"] and any(needle in x for x in f["fail"])


def test_the_same_sound_set_as_a_recent_video_fails():
    f = pacing.audit(_m(), ST, prior_sfx=[["bonk", "boom"]])
    assert not f["pass"]


def test_softer_problems_are_warnings():
    f = pacing.audit(_m(first_payoff_s=5.0, static_pct=60.0, sfx_events=8), ST)
    assert f["pass"] and len(f["warn"]) == 3


def test_format_targets_are_short_form():
    lengths = {f: s["length"] for f, s in SR.FORMATS.items()}
    assert lengths["roast_monologue"] == (11, 16) and lengths["courtroom"] == (17, 23)
    assert all(hi <= 23 for _, hi in lengths.values())


# ------------------------------------------------------------------------------------------ the meme registry

def test_the_registry_is_complete_and_its_audio_never_enters_git():
    assert len(P.REGISTRY) >= 15
    for sid, e in P.REGISTRY.items():
        for key in ("name", "source", "contexts", "purpose", "tone", "class", "trim", "hit", "level_db", "fallback",
                    "reviewed"):
            assert key in e, (sid, key)
        assert e["fallback"] in P.PALETTE and not P.PALETTE[e["fallback"]].get("registry"), sid
        assert e["trim"][1] > e["trim"][0] and 0 <= e["hit"] < e["trim"][1] - e["trim"][0]
    ignore = (P.SFX_DIR.parents[1] / ".gitignore").read_text().splitlines()
    assert "assets/sfx/meme/" in ignore                            # the meme folder is never committed


@pytest.mark.parametrize("sid", sorted(P.REGISTRY))
def test_registry_sounds_load_at_unit_loudness(sid):
    if not P.available(sid):
        pytest.skip("meme file not fetched (python -m studio.sfx_fetch)")
    x = P.render(sid, 0)
    assert np.isfinite(x).all() and P.active_rms(x) == pytest.approx(1.0, rel=0.05)
    assert abs(x[-1]) < 0.05 * np.abs(x).max()                     # faded out


@pytest.mark.parametrize("context,expected", [("throw_out", "get_out"), ("stat_ruins_it", "fahhh"),
                                              ("setup_aside", "bruh")])
def test_the_joke_picks_the_sound(context, expected, monkeypatch):
    monkeypatch.setattr(P, "available", lambda s: True)
    event = {"throw_out": "final_payoff", "stat_ruins_it": "roast_impact", "setup_aside": "punchline"}[context]
    out = P.select([{"event": event, "word": "x", "at": "end", "prefer": [], "context": context}],
                   "comparison", "flame", [], "s")
    assert out[0]["sfx"] == expected


def test_praise_gets_praise_sounds(monkeypatch):
    monkeypatch.setattr(P, "available", lambda s: True)
    arrival = P.select([{"event": "hype_peak", "claim": "c", "at": "start", "prefer": [], "context": "arrival"}],
                       "main_character", "praise", [], "s")[0]["sfx"]
    peak = P.select([{"event": "hype_peak", "word": "more", "at": "end", "prefer": [], "context": "glaze_peak"}],
                    "main_character", "praise", [], "s")[0]["sfx"]
    assert arrival == "undertaker" and peak == "heavenly"


def test_a_missing_meme_file_falls_back_and_says_so(monkeypatch):
    monkeypatch.setattr(P, "available", lambda s: not P.PALETTE[s].get("registry"))
    out = P.select([{"event": "final_payoff", "word": "x", "at": "end", "prefer": [], "context": "throw_out"}],
                   "comparison", "flame", [], "s")[0]
    assert out["sfx"] == P.REGISTRY["get_out"]["fallback"] and out["fallback_used"] and "missing" in out["why"]


def test_a_beat_can_ask_for_nothing():
    out = P.select([{"event": "punchline", "word": "x", "at": "end", "prefer": [], "context": "none"}],
                   "comparison", "flame", [], "s")[0]
    assert out["sfx"] is None


def test_the_hard_stop_silences_the_music_for_good():
    st = {"music_mood": "court", "music": True}
    v = np.zeros(int(4 * SA.SR), np.float32)
    v[-2400:] = 0.3
    x = SA.mix(v, [(1.0, "meme:music_stop:0:hard_stop", 0)], st, [], {"music": True, "music_db": -20})[:, 0]
    assert np.abs(x[int(1.05 * SA.SR):int(3.8 * SA.SR)]).max() < 1e-3
    assert np.abs(x[int(0.2 * SA.SR):int(0.9 * SA.SR)]).max() > 0.01


@pytest.mark.skipif(not P.available("fahhh"), reason="meme file not fetched")
def test_a_reaction_sits_at_its_level_against_the_narration():
    t = np.arange(int(3 * SA.SR)) / SA.SR
    v = (0.3 * np.sin(2 * np.pi * 180 * t) * (t < 1.0)).astype(np.float32)
    st = {"music_mood": None, "music": False}
    x = SA.mix(v, [(1.5, "meme:fahhh:0:roast_impact", 0)], st, [], {"music": False})[:, 0]
    voice, react = P.active_rms(x[: int(1.0 * SA.SR)]), P.active_rms(x[int(1.5 * SA.SR):int(2.9 * SA.SR)])
    assert 20 * np.log10(react / voice) == pytest.approx(P.REGISTRY["fahhh"]["level_db"], abs=1.5)


def test_every_demo_format_uses_two_or_three_reactions_and_one_hard_stop():
    _, facts, claims = qb_facts()                          # real availability: fetched files or their fallbacks
    p = facts["players"][0]
    rp, rclaims = receiver()
    for person, cl, fmt in ((p, claims, "comparison"), (p, claims, "courtroom"), (rp, rclaims, "main_character"),
                            (p, claims, "roast_monologue")):
        brief = SR.choose(person, [], fmt)
        beats, _ = SR.compose({"players": [person]}, cl, brief)
        SR.assign_sfx(beats, brief, [], "seed")
        chosen = [c["sfx"] for b in beats for c in b["scene"]["cues"] if c.get("sfx")]
        reactions = [c["sfx"] for b in beats for c in b["scene"]["cues"]
                     if c.get("sfx") and (P.PALETTE[c["sfx"]].get("registry") or c.get("fallback_used"))]
        assert 1 <= len(reactions) <= 3, (fmt, chosen)
        stops = chosen.count("music_stop")
        assert stops == (0 if fmt == "roast_monologue" else 1), (fmt, chosen)
