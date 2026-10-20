"""Graphic styles: tokens, the pixel kit, the visual QA gates, and every scene drawn for synthetic players."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from conftest import _rows, make_snapshot
from studio.graphics import STYLES, choose, kit, paper, pixel, pixelart, vector, vqa
from studio.graphics.render import scene_types, unsupported
from studio.stages import story
from studio.stages.facts import ALL, build_facts
from studio.stages.render_motion import Ctx

SCENE_TYPES = ["player_card", "target_balls", "split", "volume", "lineup", "decision", "presence",
               "teammate_out", "status_stamp"]
FORMAT_TYPES = ["mirror", "which", "rival", "zoom_out", "verdict", "volume_balls",            # mystery
                "giant_number", "carry_grid", "handoff", "report", "window", "fork"]         # opportunity


def test_tokens_define_every_style():
    for name in ("premium", "broadcast", "paper"):
        tk = kit.tokens(name)
        for key in ("kicker", "label", "title", "display", "number_l", "number_m", "number_s"):
            weight, px, track = getattr(tk.type, key)
            assert weight in kit.FONT_FILES and px >= 28
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
        raw = {"graphics": {"auto": {"vanishing_targets": "premium", "opportunity": "pixel"}}}
    assert choose(Cfg, "vanishing_targets", ["player_card", "split"]) == "premium"
    assert choose(Cfg, "mystery", ["mirror", "zoom_out"]) == "original"        # no preference for the angle
    assert choose(Cfg, "opportunity", ["giant_number", "carry_grid", "fork"]) == "pixel"
    assert choose(Cfg, "vanishing_targets", ["player_card", "not_a_scene"]) == "original"   # no design: fall back
    Default = type("Default", (), {"raw": {"graphics": {"auto_default": "premium", "auto": {"fill_in": "broadcast"}}}})
    assert choose(Default, "mystery", ["mirror", "zoom_out"]) == "premium"      # the default covers every angle
    assert choose(Default, "fill_in", ["presence", "teammate_out"]) == "broadcast"
    for style in STYLES:
        assert set(SCENE_TYPES + FORMAT_TYPES) <= scene_types(style), style
        assert not unsupported(style, FORMAT_TYPES)


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


# ------------------------------------------------------------------------------- format scenes

def _mystery():
    """A tight end with the same snaps in two games and 8 targets then 1, a same-position teammate back in the
    second game, and a season (2, 2, 3, 8, 1) whose 8 is an outlier: the mystery format's full sequence."""
    snap = make_snapshot()
    snap["tables"]["player_week_usage"] += _rows("00-MYST", "SEA", "TE", [0.17, 0.50, 0.65, 0.65, 0.65],
                                                 [2 / 35, 2 / 35, 3 / 35, 8 / 35, 1 / 35])
    snap["tables"]["player_week_usage"] += _rows("00-OTHER", "SEA", "TE", [0.80, 0.80, None, None, 0.85],
                                                 [0.15, 0.15, None, None, 4 / 35])
    snap["tables"]["nfl_players"] += [{"gsis_id": "00-MYST", "display_name": "Mystery Endman II", "position": "TE"},
                                      {"gsis_id": "00-OTHER", "display_name": "Other Endman", "position": "TE"}]
    snap["tables"]["nfl_team_week"] = [{"team": "SEA", "week": w, "team_targets": n}
                                       for w, n in zip(range(1, 6), (33, 30, 34, 36, 28))]
    return snap, "00-MYST"


def _rb_rows(gid, snaps, carries, team_carries):
    return [{"week": w, "gsis_id": gid, "team": "NYJ", "opponent": "XXX", "position": "RB",
             "offense_snaps": int(s * 60), "team_offense_snaps": 60, "offense_pct": s, "targets": 1,
             "target_share": 1 / 35, "air_yards": 0.0, "air_yards_share": 0.0, "adot": 0.0, "carries": n,
             "designed_rushes": n, "backfield_rush_share": n / tn, "team_rush_share": 0.4, "team_rb_carries": tn,
             "rz_targets": 0, "rz_carries": 0, "gl_carries": 0}
            for w, (s, n, tn) in enumerate(zip(snaps, carries, team_carries), start=1) if s is not None]


def _opportunity():
    """A backup running back with 38 of 40 carries while the starter missed Weeks 4 and 5 (knee), and 19 of 70
    with him: the opportunity format's full sequence."""
    snap = make_snapshot()
    team = [23, 24, 23, 20, 20]
    snap["tables"]["player_week_usage"] += _rb_rows("00-BACKUP", [0.35, 0.35, 0.35, 0.75, 0.75], [6, 7, 6, 19, 19], team)
    snap["tables"]["player_week_usage"] += _rb_rows("00-STARTER", [0.65, 0.65, 0.65, None, None], [17, 17, 17, 0, 0],
                                                    team)
    snap["tables"]["nfl_players"] += [{"gsis_id": "00-BACKUP", "display_name": "Quinn Reserve", "position": "RB"},
                                      {"gsis_id": "00-STARTER", "display_name": "Drew Lead", "position": "RB"}]
    snap["tables"]["nfl_injuries"] += [
        {"gsis_id": "00-STARTER", "team": "NYJ", "week": w, "report_status": "Out", "report_primary_injury": "Knee",
         "practice_status": "Did Not Participate In Practice"} for w in (4, 5)] + [
        {"gsis_id": "00-STARTER", "team": "NYJ", "week": 6, "report_status": None, "report_primary_injury": "Knee",
         "practice_status": "Limited Participation in Practice"}]
    snap["source_sync"] = {"finished_at": "2026-10-15 10:20:33"}
    return snap, "00-BACKUP"


def _format_scenes(which):
    """The scenes the format composes for the player, plus the variants its other stories use (the volume clue,
    the verdict board and its call, the starter-out handoff, the window and its closing, a revealed player)."""
    snap, gsis = _mystery() if which == "mystery" else _opportunity()
    f, c = _ctx(snap, gsis, which)
    scenes = _scenes_for(f, c)
    assert [s["type"] for s, _ in scenes] == (["mirror", "which", "rival", "zoom_out", "zoom_out"] if which == "mystery"
                                              else ["giant_number", "carry_grid", "carry_grid", "report", "fork"])
    toks = scenes[0][1]
    extra = ([{"type": "mirror", "games": [4, 5], "stage": "collide"},
              {"type": "mirror", "games": [4, 5], "stage": "targets"},
              {"type": "volume_balls", "games": [4, 5]}, {"type": "verdict", "games": [4, 5]},
              {"type": "verdict", "games": [4, 5], "stage": "call", "stamp": ["HOLD HIM", "DON'T START HIM"]}]
             if which == "mystery" else
             [{"type": "giant_number", "reveal_player": True}, {"type": "carry_grid", "stage": "recent"},
              {"type": "handoff"}, {"type": "window"}, {"type": "window", "stage": "closing"}])
    scenes += [({**sc, "_start": 0.0, "_dur": 4.0, "_tokens": toks}, toks) for sc in extra]
    return f, c, scenes


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("which", ["mystery", "opportunity"])
def test_format_scenes_draw_inside_the_safe_zone_with_sourced_numbers(style, which):
    """Every mystery and opportunity scene, settled and mid-scene: no text outside the safe zone, too small,
    low in contrast, under the captions or overlapping, and every number is its claim's display string."""
    f, c, scenes = _format_scenes(which)
    samples = []
    for i, (sc, toks) in enumerate(scenes):
        c.tokens = toks
        for t, tag in ((0.4, "10"), (1.8, "45"), (3.9, "97")):
            if style == "pixel":
                F = pixelart.draw(sc, t, 4.0, c, measure=True)
                pixelart.caption(F, toks[:3], 0, c)
                log = F.log
            else:
                F = vector.draw(style, sc, t, 4.0, c, measure=True)
                log = F.log + vector.caption(style, F.img, toks[:3], 0, 1.0, c, measure=True)
            samples.append({"frame": f"s{i:02d}_{sc['type']}_{tag}.png", "t": t, "scene": sc["type"], "log": log})
        assert any(e["src"] in c.claims and any(ch.isdigit() for ch in e["text"]) for e in log), \
            f"{sc['type']} {sc.get('stage')}: no claim number on screen"
    top = pixelart.CAPTION_BOX[1] * pixelart.S if style == "pixel" else vector.CAPTION_ZONE
    result = {name: failures for name, failures, _ in
              vqa.gates({"style": "x", "samples": samples, "numbers_visible": {}, "caption_top": top}, c.claims, None)}
    for gate in ("vqa_safe_zone", "vqa_overlap", "vqa_numbers", "vqa_text_size", "vqa_caption_clear", "vqa_contrast"):
        assert not result[gate], (gate, result[gate][:3])


@pytest.mark.parametrize("style", STYLES)
def test_format_scenes_reveal_numbers_as_they_are_said(style):
    """A target count is not on screen before the narration says it, and is once it has (the mirror's hook)."""
    f, c, scenes = _format_scenes("mystery")
    sc = scenes[0][0]
    a, b = (g for g in c.p["details"]["games"])
    words = [("Same", None), ("snaps", None), ("both", None), ("weeks.", None), ("8", a["targets"]),
             ("targets,", None), ("then", None), ("1.", b["targets"])]
    c.tokens = [{"text": w, "start": 0.3 * i, "end": 0.3 * (i + 1), "claim": cid, "emph": False}
                for i, (w, cid) in enumerate(words)]
    said = c.at(claim=b["targets"])

    def shown(t):
        F = pixelart.draw(sc, t, 4.0, c) if style == "pixel" else vector.draw(style, sc, t, 4.0, c)
        return {e["src"] for e in F.log if e["alpha"] > 0.95}
    assert b["targets"] not in shown(max(0.0, said - 0.3)) and b["targets"] in shown(said + 0.5)
    assert a["snaps"] in shown(0.5) and b["snaps"] in shown(0.5)              # the snaps are the premise


@pytest.mark.parametrize("style", STYLES)
def test_square_and_football_counts_are_the_claims(style):
    """One square (or football) per carry: the opportunity grid draws exactly the claimed counts."""
    f, c, scenes = _format_scenes("opportunity")
    car = c.p["details"]["carries"]
    assert (c.claims[car["own_recent"]]["value"], c.claims[car["team_recent"]]["value"]) == (38, 40)
    sc, toks = scenes[1]                                                       # carry_grid, recent
    c.tokens = toks
    if style == "pixel":
        drawn = []
        orig = pixel.football.__wrapped__

        def spy(ghost=False, tint=None):
            drawn.append("ghost" if ghost else tint or "leather")
            return orig(ghost, tint)
        import studio.graphics.pixel_formats as PF
        PF.X.football = spy
        try:
            pixelart.draw(sc, 3.9, 4.0, c)
        finally:
            PF.X.football = pixel.football
        assert drawn.count("gold") == 38 and drawn.count("ghost") == 2
    else:
        import studio.graphics.vector_formats as VF
        cells = []
        orig = VF._cell
        VF._cell = lambda F, L, box, color, ghost, k: cells.append(ghost) or orig(F, L, box, color, ghost, k)
        try:
            vector.draw(style, sc, 3.9, 4.0, c)
        finally:
            VF._cell = orig
        assert cells.count(False) == 38 and cells.count(True) == 2


def test_paper_page_turn_runs_from_the_old_page_to_the_new():
    a = Image.new("RGB", (1080, 1920), (200, 190, 170))
    b = Image.new("RGB", (1080, 1920), (20, 30, 40))
    assert np.array_equal(np.asarray(paper.page(a, b, 0.0)), np.asarray(a))
    assert np.array_equal(np.asarray(paper.page(a, b, 1.0)), np.asarray(b))
    mid = np.asarray(paper.page(a, b, 0.6))
    assert (mid == (200, 190, 170)).all(axis=-1).any() and not (mid == (200, 190, 170)).all()   # sliding away


def test_paper_pen_is_deterministic_and_draws_progressively():
    def draw(k):
        F = kit.Frame(Image.new("RGB", (1080, 1920), (0, 0, 0)))
        paper.circle(F, (400, 600, 600, 700), k, (242, 189, 82), 7)
        return np.asarray(F.img).sum(axis=-1) > 0
    half, full = draw(0.5), draw(1.0)
    assert not draw(0.0).any() and 0 < half.sum() < full.sum()
    assert np.array_equal(full, draw(1.0))                 # the same strokes on every render
    ys, xs = np.nonzero(full)
    assert xs.min() < 400 and xs.max() > 600 and ys.min() < 600 and ys.max() > 700    # round the box, not on it


def test_paper_sticker_has_a_white_outline():
    im = Image.new("RGBA", (200, 300), (0, 0, 0, 0))
    im.paste((180, 20, 40, 255), (40, 40, 160, 260))
    st = paper.make_sticker(im, 14, 0)
    a = np.asarray(st)
    assert st.width > 200 and st.height > 300
    rgb = a[..., :3].astype(int)
    white = (rgb.min(axis=-1) > 230) & (a[..., 3] > 250)
    assert white.sum() > 2 * 10 * (120 + 220)               # a band (~12 px of the 14) right round the subject


def test_paper_sheet_is_seeded_grain():
    base = Image.new("RGB", (1080, 1920), (7, 11, 18))
    tk = kit.tokens("paper")
    g1, g2 = np.asarray(paper.grain(base, tk), np.int16), np.asarray(paper.grain(base, tk), np.int16)
    assert np.array_equal(g1, g2) and g1[..., 0].std() > 2


@pytest.mark.parametrize("style", [s for s in STYLES if s != "pixel"])
def test_big_compare_draws_inside_the_safe_zone_with_sourced_numbers(style):
    f, c = _ctx(_vanishing(), "00-FALLER")
    toks = [{"text": w, "start": 0.3 * i, "end": 0.3 * (i + 1), "claim": None, "emph": False}
            for i, w in enumerate("his share went from here to there".split())]
    c.tokens = toks
    F = vector.draw(style, {"type": "big_compare", "metric": 0, "_start": 0.0, "_dur": 4.0}, 3.9, 4.0, c, measure=True)
    log = F.log + vector.caption(style, F.img, toks[:3], 0, 1.0, c, measure=True)
    assert sum(e["role"] == "number" for e in F.log) >= 2
    sample = {"frame": "s0_97.png", "t": 3.9, "scene": "big_compare", "log": log}
    result = {name: failures for name, failures, _ in
              vqa.gates({"style": "x", "samples": [sample], "numbers_visible": {}, "caption_top": vector.CAPTION_ZONE},
                        c.claims, None)}
    for gate in ("vqa_safe_zone", "vqa_overlap", "vqa_numbers", "vqa_text_size", "vqa_caption_clear"):
        assert not result[gate], (gate, result[gate][:3])
