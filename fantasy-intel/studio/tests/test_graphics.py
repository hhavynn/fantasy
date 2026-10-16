"""Graphic styles: tokens, the pixel kit, the visual QA gates, and every scene drawn for synthetic players."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from conftest import _rows, make_snapshot
from studio.graphics import STYLES, choose, kit, pixel, pixelart, vector, vqa
from studio.graphics.render import scene_types, unsupported
from studio.stages import story
from studio.stages.facts import ALL, build_facts
from studio.stages.render_motion import Ctx

SCENE_TYPES = ["player_card", "target_balls", "split", "volume", "lineup", "decision", "presence",
               "teammate_out", "status_stamp"]


def test_tokens_define_every_style():
    for name in ("premium", "broadcast"):
        tk = kit.tokens(name)
        for key in ("kicker", "label", "title", "display", "number_l", "number_m", "number_s"):
            weight, px, track = getattr(tk.type, key)
            assert weight in ("regular", "semibold", "bold", "black") and px >= 28
        assert len(tk.motion.enter) == 4 and tk.motion.transition_dur <= 0.4
        assert kit.SAFE[2] - tk.space.margin_right <= kit.W - vector.look(name).mr
    px = kit.tokens("pixel")
    assert px.grid.w * px.grid.scale == kit.W and px.grid.h * px.grid.scale == kit.H
    assert len(vars(px.palette)) <= 16                     # a deliberate, limited palette


def test_bezier_is_monotonic_and_pinned():
    f = kit.bezier(0.16, 1.0, 0.3, 1.0)
    ys = [f(x / 50) for x in range(51)]
    assert ys[0] == 0 and ys[-1] == 1 and all(b >= a - 1e-9 for a, b in zip(ys, ys[1:]))


def test_pixel_font_covers_the_story_text():
    for ch, rows in pixel.GLYPHS.items():
        assert len(rows.split("|")) == 7, ch
    assert pixel.supported("WEEK 6 · 21% → 9%? (-12 PTS) #86")
    assert pixel.text_width("AB", 2) == 2 * (5 + 1 + 5)
    assert all(pixel.text_width(line, 2) <= 100 for line in pixel.wrap("RIDING IT OUT OR BENCHING HIM?", 100, 2))


def test_pixel_sprites_have_hard_edges():
    for spr in (pixel.player((0, 51, 141), (198, 12, 48), "86"), pixel.football(), pixel.football(True)):
        a = np.asarray(spr.getchannel("A"))
        assert set(np.unique(a)) <= {0, 255}               # no anti-aliased edge pixels


def test_sentence_case_keeps_names():
    assert vector.sentence("STILL STARTING KINCAID?") == "Still starting kincaid?"

    class C:
        p = {"name": "Dalton Kincaid", "gainers": [], "teammate": None}
    assert vector.sentence("STILL STARTING KINCAID?", C) == "Still starting Kincaid?"
    assert vector.sentence("Already mixed Case") == "Already mixed Case"


def _ctx(snap, gsis, story_angle="vanishing_targets"):
    f = build_facts(snap, ALL, set(), only=gsis)
    claims = {c["id"]: c for c in f["claims"]}
    c = Ctx(f, claims, (255, 92, 92), story_angle)
    return f, c


def _scenes_for(f, c):
    beats, _, _ = story.compose(f, c.claims, "seed", {})
    out = []
    for b in beats:
        words = story.fill(b["text_template"], c.claims, "display").split()
        toks = [{"text": w, "start": 0.3 * i, "end": 0.3 * (i + 1), "claim": None, "emph": False}
                for i, w in enumerate(words)]
        out.append(({**b["scene"], "_start": 0.0, "_dur": 4.0, "_tokens": toks}, toks))
    return out


def _vanishing():
    snap = make_snapshot()
    snap["tables"]["player_week_usage"] = [r for r in snap["tables"]["player_week_usage"] if r["gsis_id"] != "00-FALLER"]
    snap["tables"]["player_week_usage"] += _rows("00-FALLER", "DAL", "TE", [0.70, 0.66, 0.67, 0.65, 0.66],
                                                 [0.21, 0.29, 0.12, 0.07, 0.11])
    return snap


def _fill_in():
    snap = make_snapshot()
    snaps = [None if w in (4, 5) else 0.95 for w in range(1, 6)]
    shares = [None if w in (4, 5) else 0.30 for w in range(1, 6)]
    snap["tables"]["player_week_usage"] += _rows("00-STAR", "PHI", "WR", snaps, shares)
    snap["tables"]["nfl_players"].append({"gsis_id": "00-STAR", "display_name": "Star Receiver", "position": "WR"})
    return snap


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("which", ["vanishing", "fill_in"])
def test_every_scene_draws_inside_the_safe_zone_with_sourced_numbers(style, which):
    """Two different synthetic players and stories: each scene renders settled, and the visual QA finds no
    text outside the safe zone, no overlaps, and no number that is unsourced or differs from its claim."""
    snap, gsis = (_vanishing(), "00-FALLER") if which == "vanishing" else (_fill_in(), "00-RISER")
    f, c = _ctx(snap, gsis)
    scenes = _scenes_for(f, c)
    assert not unsupported(style, [s["type"] for s, _ in scenes])
    samples = []
    for i, (sc, toks) in enumerate(scenes):
        c.tokens = toks
        if style == "pixel":
            F = pixelart.draw(sc, 3.9, 4.0, c, measure=True)
            pixelart.caption(F, toks[:3], 0, c)
            log = F.log
        else:
            F = vector.draw(style, sc, 3.9, 4.0, c, measure=True)
            log = F.log + vector.caption(style, F.img, toks[:3], 0, 1.0, c, measure=True)
        samples.append({"frame": f"s{i}_97.png", "t": 3.9, "scene": sc["type"], "log": log})
    top = pixelart.CAPTION_BOX[1] * pixelart.S if style == "pixel" else vector.CAPTION_ZONE
    result = {name: failures for name, failures, _ in
              vqa.gates({"style": "x", "samples": samples, "numbers_visible": {}, "caption_top": top}, c.claims, None)}
    for gate in ("vqa_safe_zone", "vqa_overlap", "vqa_numbers", "vqa_text_size", "vqa_caption_clear"):
        assert not result[gate], (gate, result[gate][:3])


def _sample(**e):
    base = {"text": "LABEL", "box": [100, 400, 300, 440], "cap_px": 30, "fill": [255, 255, 255], "alpha": 1.0,
            "src": None, "role": "label", "contrast": 12.0}
    return {**base, **e}


def test_visual_qa_catches_each_defect():
    claims = {"c1": {"display": "21%"}}
    bad = [_sample(box=[900, 400, 1010, 440]),                         # past the right rail
           _sample(text="tiny", box=[100, 500, 140, 510], cap_px=12),     # unreadable on a phone
           _sample(text="dim", box=[100, 600, 200, 640], contrast=1.8),   # low contrast
           _sample(text="A", box=[100, 700, 300, 760]), _sample(text="B", box=[150, 710, 320, 770]),   # overlap
           _sample(text="68%", box=[100, 800, 200, 840], role="number"),  # number with no source
           _sample(text="22%", box=[100, 900, 200, 940], role="number", src="c1"),   # differs from its claim
           _sample(text="under", box=[100, 1380, 200, 1420])]                     # hidden by the captions
    res = {n: f for n, f, _ in vqa.gates({"style": "premium", "samples": [{"frame": "s01_x_97.png", "log": bad}],
                                          "numbers_visible": {"c1": {"text": "21%", "seconds": 0.2}},
                                          "caption_top": 1396}, claims, None)}
    assert all(res[g] for g in ("vqa_safe_zone", "vqa_text_size", "vqa_contrast", "vqa_overlap", "vqa_numbers",
                                "vqa_number_dwell", "vqa_caption_clear"))
    good = [_sample(text="21%", role="number", src="c1")]
    res = {n: f for n, f, _ in vqa.gates({"style": "premium", "samples": [{"frame": "s01_x_97.png", "log": good}],
                                          "numbers_visible": {"c1": {"text": "21%", "seconds": 2.0}}}, claims, None)}
    assert not any(res.values())


def test_pixel_grid_gate_flags_smoothing(tmp_path):
    art = Image.fromarray((np.random.default_rng(0).integers(0, 255, (384, 216, 3))).astype(np.uint8))
    art.resize((1080, 1920), Image.NEAREST).save(tmp_path / "s01_a_97.png")
    art.resize((1080, 1920), Image.BILINEAR).save(tmp_path / "s01_b_97.png")
    samples = [{"frame": "s01_a_97.png", "log": []}, {"frame": "s01_b_97.png", "log": []}]
    res = {n: f for n, f, _ in vqa.gates({"style": "pixel", "samples": samples, "numbers_visible": {}}, {}, tmp_path)}
    assert len(res["vqa_pixel_grid"]) == 1 and "s01_b" in res["vqa_pixel_grid"][0]


def test_auto_style_prefers_by_angle_and_falls_back(monkeypatch):
    class Cfg:
        raw = {"graphics": {"auto": {"vanishing_targets": "premium"}}}
    assert choose(Cfg, "vanishing_targets", ["player_card", "split"]) == "premium"
    assert choose(Cfg, "vanishing_targets", ["mirror"]) == "original"          # no design for a mystery scene yet
    assert choose(Cfg, "mystery", ["mirror"]) == "original"
    assert set(SCENE_TYPES) <= scene_types("pixel") and set(SCENE_TYPES) <= scene_types("premium")


def test_pixel_transition_steps_through_whole_frames():
    a = Image.new("RGB", (1080, 1920), (200, 200, 200))
    b = Image.new("RGB", (1080, 1920), (40, 220, 90))
    levels = set()
    for k in np.linspace(0, 0.999, 30):
        f = np.asarray(pixelart.transition(a, b, float(k)))
        assert (f == f[0, 0]).all()                       # one colour per frame: no partial blocks
        levels.add(tuple(f[0, 0]))
    assert len(levels) <= 8                               # four steps down, four up


@pytest.mark.parametrize("style", STYLES)
def test_name_suffixes_never_stand_in_for_the_surname(style):
    f, c = _ctx(_fill_in(), "00-RISER")
    c.p["name"] = "Harold Fannin Jr."
    scenes = {sc["type"]: (sc, toks) for sc, toks in _scenes_for(f, c)}
    for kind in ("lineup", "teammate_out"):
        sc, toks = scenes[kind]
        c.tokens = toks
        F = pixelart.draw(sc, 3.9, 4.0, c) if style == "pixel" else vector.draw(style, sc, 3.9, 4.0, c)
        assert not any(e["text"].strip(".").upper() == "JR" for e in F.log), kind
