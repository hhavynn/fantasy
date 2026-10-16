"""The edit-style visuals: the new scenes draw, react to their sound cues, and the visual audit measures what it says."""

from __future__ import annotations

import subprocess

import numpy as np
from PIL import Image
from test_series import qb_facts, receiver

from studio import edit_kit as K
from studio import edit_scenes as ES
from studio import flame_scenes as FPS
from studio import visual_audit as VA
from studio.stages import render_motion as R
from studio.stages import showrunner as SR


def _ctx(facts, claims, tokens=()):
    c = R.Ctx(facts, claims, FPS.ACCENT["flame"], "flame")
    c.tokens = list(tokens)
    return c


def _beats(fmt, praise=False):
    if praise:
        p, claims = receiver()
        facts = {"players": [p], "claims": list(claims.values())}
    else:
        _, facts, claims = qb_facts(latest_air=20.0, latest_yds=107)
    brief = SR.choose(facts["players"][0], [], fmt)
    beats, _ = SR.compose(facts, claims, brief)
    return facts, claims, beats


def test_the_three_formats_use_their_own_edit_scenes_and_every_one_draws():
    for fmt, praise, prefix in (("comparison", False, "vx_"), ("courtroom", False, "cx_"),
                                ("main_character", True, "hx_")):
        facts, claims, beats = _beats(fmt, praise)
        types = [b["scene"]["type"] for b in beats]
        assert all(t.startswith(prefix) for t in types), (fmt, types)
        assert len(set(types)) >= 3, f"{fmt}: too few distinct compositions {types}"
        c = _ctx(facts, claims)
        for b in beats:
            assert b["scene"]["type"] in FPS.SCENES and b["scene"]["type"] in ES.HERO
            for t in (0.0, 1.2, 2.9):
                img = R.draw_scene({**b["scene"], "_start": 0.0}, t, 3.0, c)
                assert img.size == (1080, 1920)


def test_edit_scenes_keep_their_sound_cues():
    facts, claims, beats = _beats("courtroom")
    b = next(b for b in beats if b["scene"]["type"] == "cx_order")
    scene = {**b["scene"], "cues": [{**q, "sfx": "gavel_single"} for q in b["scene"]["cues"]]}
    c = _ctx(facts, claims, [{"text": "Order!", "claim": None, "start": 0.1, "end": 0.4}])
    assert any("gavel_single" in kind for _, kind, _ in FPS.sounds(scene, 0.0, 2.0, c))


def test_the_picture_hits_on_the_sound_and_freezes_on_the_hard_stop():
    facts, claims, beats = _beats("comparison")
    b = beats[-1]
    assert b["scene"]["type"] == "vx_scoreboard"
    tokens = [{"text": w, "claim": None, "start": s, "end": s + 0.2}
              for w, s in (("Tell", 1.0), ("the", 1.2), ("deep", 1.4), ("safety", 1.6), ("home.", 2.0))]
    cues = [{**q, "sfx": "get_out" if q["event"] == "final_payoff" else "music_stop"} for q in b["scene"]["cues"]]
    scene = {**b["scene"], "cues": cues, "_start": 0.0}
    c = _ctx(facts, claims, tokens)
    assert [round(h, 2) for h, _, _ in K.hits(c, scene)] == [2.2, 2.2]
    calm = np.asarray(R.draw_scene(scene, 2.15, 3.0, c), np.float32)
    after = np.asarray(R.draw_scene(scene, 2.6, 3.0, c), np.float32)
    sat = lambda a: float((a.max(2) - a.min(2)).mean())
    assert sat(after) < sat(calm) * 0.5            # the colour drains on the stop


def test_cutouts_are_big_stickers_and_fall_back_to_the_jersey():
    lay = K.cutout(None, "DEN", "10", "Nix", 900, crop=(0.1, 0.0, 0.9, 1.0))
    assert lay.mode == "RGBA" and lay.height > 900
    assert lay.getchannel("A").getextrema() == (0, 255)


def _video(path, colors, secs=1.0):
    parts = []
    for col in colors:
        parts += ["-f", "lavfi", "-t", str(secs), "-i", f"color=c={col}:s=270x480:r=30"]
    n = len(colors)
    subprocess.run(["ffmpeg", "-v", "error", "-y", *parts, "-filter_complex",
                    "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1[v]", "-map", "[v]", str(path)],
                   check=True)


def test_the_visual_audit_counts_compositions_and_holds(tmp_path):
    ep = tmp_path / "ep"
    ep.mkdir()
    _video(ep / "final.mp4", ["red", "blue", "red"])
    m = VA.measure(ep)
    assert m["compositions"] == 2
    assert 0.8 <= m["longest_hold_s"] <= 1.2
    assert m["empty_share"] > 0.9                  # flat colour is empty space
    sheet = VA.contact_sheet(ep / "final.mp4", tmp_path / "sheet.jpg", n=6, title="t")
    assert Image.open(sheet).size[0] > 1000
