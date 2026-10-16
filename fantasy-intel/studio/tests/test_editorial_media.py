"""The editorial_media treatment: the rights-tracked catalog, its use checks, narration-aware shot selection,
subject-aware crops, the shot renderer and the QC around them. Everything here is synthetic: no network, no
real photos."""

from __future__ import annotations

import json
import shutil
import subprocess

import numpy as np
import pytest
import soundfile as sf
from PIL import Image, ImageDraw

from studio import media_library as ML
from studio import media_qc as QC
from studio import media_scenes as MS
from studio import shot_planner as SP
from studio import media_sources as SRC

SEASON = 2026
STAR = {"gsis_id": "00-STAR", "name": "Star Receiver", "team": "LA", "role": "praise", "position": "WR",
        "jersey": 12, "espn_id": None, "team_name": "Los Angeles Rams",
        "teammate": {"name": "Mate Runner", "gsis_id": "00-MATE", "espn_id": None}}


# ------------------------------------------------------------------------------------------- fixtures

def _photo(path, size=(1600, 2000), color=(40, 90, 160)):
    im = Image.new("RGB", size, color)
    d = ImageDraw.Draw(im)
    w, h = size
    d.rectangle((w * 0.35, h * 0.3, w * 0.65, h * 0.95), fill=(230, 230, 240))      # a "player"
    d.ellipse((w * 0.43, h * 0.18, w * 0.57, h * 0.32), fill=(200, 160, 130))       # his face
    im.save(path, quality=90)
    return path


def _entry(eid, mtype, **kw):
    base = dict(id=eid, type=mtype, path=f"{eid}.jpg", license="CC-BY-4.0", attribution="Test Photographer",
                source={"kind": "test", "url": f"https://example.org/{eid}"}, width=1600, height=2000,
                subject={"box": [0.35, 0.18, 0.65, 0.95], "face": [0.43, 0.18, 0.57, 0.32], "method": "test"},
                review={"status": "approved"})
    base.update(kw)
    return ML.Entry(**base)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(ML, "CACHE", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    entries = [
        _entry("star-game", "action_photo", player_id="00-STAR", name="Star Receiver", team="LA", season=2026,
               relevance=["game", "catch"]),
        _entry("star-camp", "practice_photo", player_id="00-STAR", name="Star Receiver", team="LA", season=2025),
        _entry("star-college", "action_photo", player_id="00-STAR", name="Star Receiver", team="BYU", season=2021),
        _entry("mate-portrait", "interview_still", player_id="00-MATE", name="Mate Runner", team="LA", season=2026),
        _entry("team-runout", "team_photo", team="LA", season=2025, subject={"box": [0.1, 0.2, 0.9, 0.9]}),
        _entry("broll-ruler", "context_broll", illustrative=True, relevance=["ruler", "measure"],
               license="CC0", subject={"box": [0.1, 0.4, 0.4, 0.7]}),
        _entry("broll-ice", "context_broll", illustrative=True, relevance=["injury", "ice"], license="CC0"),
    ]
    for e in entries:
        _photo(ML.CACHE / e.path)
    return ML.Library(entries, tmp_path / "manifest.json")


def _claims():
    return {"c1": {"id": "c1", "display": "24", "spoken": "twenty-four", "source": {"kind": "fp_value"}},
            "c2": {"id": "c2", "display": "17", "spoken": "seventeen", "source": {"kind": "fp_value"}},
            "c3": {"id": "c3", "display": "138", "spoken": "one hundred thirty-eight", "source": {"kind": "fp_value"}},
            "c4": {"id": "c4", "display": "1", "spoken": "one", "source": {"kind": "fp_value"}},
            "c5": {"id": "c5", "display": "27%", "spoken": "twenty-seven percent", "source": {"kind": "usage_mean"}},
            "c6": {"id": "c6", "display": "a quadricep injury", "spoken": "a quadricep injury",
                   "source": {"kind": "injury_report"}}}


def _tokens(text, start=0.0, step=0.32, claims=None):
    claims = claims or {}
    out, t = [], start
    for w in text.split():
        out.append({"text": w, "start": round(t, 3), "end": round(t + step * 0.9, 3),
                    "claim": claims.get(w.rstrip(".,?!")), "emph": any(ch.isdigit() for ch in w)})
        t += step
    return out


def _episode(lines, role="praise", scene_types=None):
    """script, facts, claims, tokens and durations for a list of (text, {word: claim}) beats."""
    claims = _claims()
    segs, shots, toks, durs = [], [], {}, {}
    for i, (text, cmap) in enumerate(lines):
        sid = f"s{i + 1:02d}"
        tk = _tokens(text, claims=cmap)
        toks[sid] = tk
        durs[sid] = round(tk[-1]["end"] + 0.25, 3)
        st = (scene_types or {}).get(sid, "statement")
        segs.append({"id": sid, "text_display": text, "scene": {"type": st, "cues": []}})
        shots.append({"type": st, "id": f"shot_{sid}"})
    facts = {"players": [dict(STAR, role=role)], "claims": list(claims.values()), "season": SEASON}
    return {"narration_segments": segs, "shots": shots, "structure": {"angle": role}}, facts, claims, toks, durs


def _plan(lib, lines, treatment="player_hype", episode_id="ep-1", **kw):
    script, facts, claims, toks, durs = _episode(lines, **kw)
    return SP.plan(script, facts, claims, toks, durs, lib, treatment, season=SEASON, cfg_headshots=True,
                   episode_id=episode_id)


# ---------------------------------------------------------------------------------------- the catalog

def test_every_media_type_in_the_brief_is_known():
    for t in ("action_photo", "catch_photo", "rush_photo", "celebration_photo", "sideline_photo", "practice_photo",
              "team_photo", "licensed_video", "context_broll", "source_evidence", "headshot_fallback"):
        assert t in ML.MEDIA_TYPES


def test_validation_rejects_unusable_rights_and_loose_entries():
    assert any("licence" in m for m in ML.validate(_entry("x", "action_photo", license="CC-BY-NC-4.0",
                                                         player_id="p", name="n", team="LA")))
    assert any("ESPN" in m for m in ML.validate(_entry("x", "action_photo", license="espn_override",
                                                      player_id="p", name="n", team="LA")))
    assert any("player_id" in m for m in ML.validate(_entry("x", "action_photo")))
    assert any("illustrative" in m for m in ML.validate(_entry("x", "context_broll")))
    assert any("tied to a player" in m for m in ML.validate(_entry("x", "context_broll", illustrative=True,
                                                                  player_id="p")))
    assert any("license_ref" in m for m in ML.validate(_entry("x", "team_photo", team="LA", license="licensed")))
    assert ML.validate(_entry("ok", "action_photo", player_id="p", name="n", team="LA")) == []


def test_commons_licence_names_map_and_noncommercial_is_refused():
    assert SRC.commons_license("CC BY-SA 4.0") == "CC-BY-SA-4.0"
    assert SRC.commons_license("Public domain") == "PD"
    assert SRC.commons_license("CC0") == "CC0"
    assert SRC.commons_license("CC BY-NC 2.0") is None
    assert SRC.commons_license("CC BY-ND 4.0") is None


def test_manifest_round_trips_and_never_saves_an_invalid_entry(lib, tmp_path):
    lib.save()
    again = ML.Library.load(tmp_path / "manifest.json")
    assert set(again.entries) == set(lib.entries)
    assert again.get("star-game").subject == lib.get("star-game").subject
    bad = _entry("bad", "action_photo", license="espn_override", player_id="p", name="n", team="LA")
    lib.entries["bad"] = bad
    with pytest.raises(ValueError):
        lib.save()
    assert "bad" not in json.loads((tmp_path / "manifest.json").read_text())["entries"].__repr__()


def _use(e, **kw):
    u = {"subject_id": "00-STAR", "subject_team": "LA", "intent": "dominance", "labels": ML.labels_for(e, SEASON),
         "names_player": True}
    u.update(kw)
    return ML.check_use(e, u, season=SEASON, cfg_headshots=True)


def test_a_photo_in_another_uniform_is_blocked_for_current_analysis(lib):
    probs = _use(lib.get("star-college"))
    assert any(p["check"] == "team" and p["severity"] == "block" for p in probs)
    assert not [p for p in _use(lib.get("star-game")) if p["severity"] == "block"]


def test_the_wrong_person_is_blocked(lib):
    probs = _use(lib.get("mate-portrait"))
    assert any(p["check"] == "identity" and p["severity"] == "block" for p in probs)


def test_generic_injury_imagery_never_stands_in_for_a_players_injury(lib):
    ice = lib.get("broll-ice")
    probs = _use(ice, subject_id=None, intent="injury_status", labels=[])
    assert any(p["check"] == "injury" and p["severity"] == "block" for p in probs)
    # a photo of the player himself is identification, not a picture of the injury
    assert not [p for p in _use(lib.get("star-game"), intent="injury_status") if p["check"] == "injury"]


def test_stock_under_a_line_about_a_player_must_be_labelled_illustrative(lib):
    ruler = lib.get("broll-ruler")
    assert any(p["check"] == "stock_implication" for p in _use(ruler, subject_id=None, labels=[]))
    assert not [p for p in _use(ruler, subject_id=None, labels=["ILLUSTRATIVE"]) if p["severity"] == "block"]
    assert "ILLUSTRATIVE" in ML.labels_for(ruler, SEASON)


def test_espn_headshots_follow_the_owner_switch_and_are_only_fallbacks(lib):
    e = _entry("espn-1", "headshot_fallback", license="espn_override", player_id="00-STAR", name="Star Receiver",
               team="LA", season=2026)
    assert ML.cleared(e, cfg_headshots=True)
    assert not ML.cleared(e, cfg_headshots=False)
    assert "FALLBACK" in ML.labels_for(e, SEASON)


def test_unreviewed_media_warns_in_preview_and_blocks_publishing(lib):
    e = lib.get("star-game")
    e.review = {"status": "pending"}
    u = {"subject_id": "00-STAR", "subject_team": "LA", "labels": []}
    assert [p["severity"] for p in ML.check_use(e, u, season=SEASON, cfg_headshots=True) if p["check"] == "review"] == ["warn"]
    assert [p["severity"] for p in ML.check_use(e, u, season=SEASON, cfg_headshots=True, publish=True)
            if p["check"] == "review"] == ["block"]


def test_old_photos_and_team_photos_carry_a_file_photo_label(lib):
    assert ML.labels_for(lib.get("team-runout"), SEASON) == ["FILE PHOTO 2025"]
    assert ML.labels_for(lib.get("star-game"), SEASON) == []
    old = _entry("old", "action_photo", player_id="00-STAR", name="S", team="LA", season=2023)
    assert ML.labels_for(old, SEASON) == ["FILE PHOTO 2023"]


def test_attribution_lines_name_author_source_and_licence(lib):
    lines = ML.attribution_lines([lib.get("star-game"), lib.get("star-game")])
    assert len(lines) == 1 and "Test Photographer" in lines[0] and "creativecommons.org/licenses/by/4.0" in lines[0]
    assert ML.short_credit(lib.get("star-game")) == "Test Photographer / CC BY 4.0"


# ------------------------------------------------------------------------------------------ the planner

def test_clauses_tile_the_beat_and_split_at_sentences():
    seg = {"id": "s01", "scene": {"cues": []}}
    toks = _tokens("Average NFL throw is long. His was short.")
    cls = SP.clauses(seg, toks, 0, 3.0)
    assert len(cls) == 2
    assert cls[0].t0 == 0.0 and cls[-1].t1 == 3.0 and cls[0].t1 == cls[1].t0


def test_a_comma_run_of_numbers_stays_one_clause():
    seg = {"id": "s01", "scene": {"cues": []}}
    toks = _tokens("17 catches, 138 yards, 1 touchdown.", claims={"17": "c2", "138": "c3", "1": "c4"})
    assert len(SP.clauses(seg, toks, 0, 3.0)) == 1


def test_brief_examples_get_the_right_media(lib):
    p = _plan(lib, [("Star Receiver dominated the Bills.", {}),
                    ("The league average is 9 yards. His? 3.", {}),
                    ("Mate Runner missed the game.", {}),
                    ("His target share tells a different story: 27% of the targets.", {"27%": "c5"})],
              treatment="fantasy_analysis")
    by_seg = {}
    for s in p.shots:
        by_seg.setdefault(s["segment"], []).append(s)
    dom = by_seg["s01"][0]
    assert dom["intent"] == "dominance" and dom["kind"] == "photo" and dom["asset"] == "star-game"
    assert by_seg["s02"][0]["intent"] == "comparison" and by_seg["s02"][0]["kind"] in ("graphic", "inset")
    mate = by_seg["s03"][0]
    assert mate["intent"] == "teammate_absence" and mate["asset"] == "mate-portrait"
    assert by_seg["s04"][0]["intent"] == "data_story" and by_seg["s04"][0]["kind"] == "graphic"


def test_the_wrong_uniform_is_never_picked(lib):
    p = _plan(lib, [("Star Receiver dominated.", {}), ("Star Receiver did it again.", {}),
                    ("Nobody stops Star Receiver.", {})])
    assert "star-college" not in {s.get("asset") for s in p.shots}


def test_a_stat_run_cuts_on_every_number_in_a_hype_edit(lib):
    p = _plan(lib, [("17 catches, 138 yards, 1 touchdown.", {"17": "c2", "138": "c3", "1": "c4"})])
    photos = [s for s in p.shots if s["kind"] == "photo"]
    assert len(photos) == 3 and [s["stat_claim"] for s in photos] == ["c2", "c3", "c4"]
    assert all(s["overlay"] == "stat" for s in photos)


def test_the_punchline_word_finds_matching_broll_labelled_illustrative(lib):
    script, facts, claims, toks, durs = _episode([("Measuring his passes? Grab a smaller ruler.", {})], role="flame")
    script["narration_segments"][0]["scene"]["cues"] = [{"word": "ruler", "event": "punchline", "sfx": "bruh"}]
    p = SP.plan(script, facts, claims, toks, durs, lib, "roast", season=SEASON, cfg_headshots=True, episode_id="x")
    punch = [s for s in p.shots if s["intent"] == "punchline"]
    assert punch and punch[0]["asset"] == "broll-ruler" and "ILLUSTRATIVE" in punch[0]["labels"]
    assert punch[0]["effect"] == "snap_zoom"


def test_an_injury_line_shows_the_report_not_injury_footage(lib):
    p = _plan(lib, [("Mate Runner has a quadricep injury per the report.", {"a": "c6"})],
              scene_types={"s01": "report"}, treatment="injury_opportunity")
    assert {s["kind"] for s in p.shots} <= {"graphic", "inset"}
    assert "broll-ice" not in {s.get("asset") for s in p.shots}
    assert p.shots[0]["evidence"] is True


def test_a_team_result_uses_a_team_photo(lib):
    p = _plan(lib, [("The Rams lost a shootout.", {})])
    assert p.shots[0]["intent"] == "team_result" and p.shots[0]["asset"] == "team-runout"
    assert p.shots[0]["labels"] == ["FILE PHOTO 2025"]


def test_missing_photos_fall_back_visibly_and_are_listed_as_gaps(lib):
    for eid in ("star-game", "star-camp"):
        lib.entries.pop(eid)
    p = _plan(lib, [("Star Receiver is the name to know.", {})])
    assert p.shots[0]["kind"] == "missing"
    assert p.gaps and "Star Receiver" in p.gaps[0]["wanted"]
    lib.add(_entry("espn-1", "headshot_fallback", license="espn_override", player_id="00-STAR",
                   name="Star Receiver", team="LA", season=2026, path="star-game.jpg"))
    _photo(ML.CACHE / "star-game.jpg")
    p = _plan(lib, [("Star Receiver is the name to know.", {})])
    assert p.shots[0]["asset"] == "espn-1" and "FALLBACK" in p.shots[0]["labels"] and p.gaps


def test_no_fixed_sequence_across_treatments_and_episodes(lib):
    lines = [("24 targets for Star Receiver.", {"24": "c1"}), ("The league average is 9. His? 24.", {}),
             ("17 catches, 138 yards, 1 touchdown.", {"17": "c2", "138": "c3", "1": "c4"}),
             ("Star Receiver dominated.", {}), ("The Rams lost a shootout.", {})]
    shapes = {tuple(s["kind"] for s in _plan(lib, lines, t).shots) for t in SP.TREATMENTS}
    assert len(shapes) >= 3
    sigs = {_plan(lib, lines, "trade_debate", episode_id=f"ep-{i}").signature for i in range(8)}
    assert len(sigs) >= 2
    first = _plan(lib, lines, "trade_debate", episode_id="ep-0")
    script, facts, claims, toks, durs = _episode(lines)
    redrawn = SP.plan(script, facts, claims, toks, durs, lib, "trade_debate", season=SEASON, cfg_headshots=True,
                      episode_id="ep-0", recent_signatures=[first.signature])
    assert redrawn.signature != first.signature        # a recent episode's shape is not repeated


def test_assets_are_spent_sparingly_and_reuse_changes_the_framing(lib):
    lines = [("Star Receiver dominated.", {}), ("Star Receiver again.", {}), ("Star Receiver once more.", {}),
             ("Star Receiver, still.", {})]
    p = _plan(lib, lines)
    pairs = [(s["asset"], s["crop"]["framing"]) for s in p.shots if s.get("asset")]
    assert len(pairs) == len(set(pairs))


def test_treatment_choice_follows_the_angle_and_rotates_off_repeats():
    assert SP.choose_treatment("praise") == "player_hype"
    assert SP.choose_treatment("flame") == "roast"
    assert SP.choose_treatment("opportunity") == "injury_opportunity"
    assert SP.choose_treatment("praise", ["player_hype", "player_hype"]) == "commentary"
    assert set(SP.TREATMENTS) == {"commentary", "player_hype", "roast", "fantasy_analysis", "injury_opportunity",
                                  "trade_debate"}


# ------------------------------------------------------------------------------------------- the crop

@pytest.mark.parametrize("framing", ["wide", "medium", "tight"])
def test_crops_are_9x16_inside_the_source_and_keep_the_face(framing):
    subject = {"box": [0.35, 0.18, 0.65, 0.95], "face": [0.43, 0.18, 0.57, 0.32]}
    (x0, y0, x1, y1), fits = MS.crop_window(1600, 2000, subject, framing)
    assert fits and abs((x1 - x0) / (y1 - y0) - 9 / 16) < 1e-6
    assert x0 >= 0 and y0 >= 0 and x1 <= 1600 + 1e-6 and y1 <= 2000 + 1e-6
    fx0, fy0, fx1, fy1 = MS.to_frame((x0, y0, x1, y1), 1600, 2000, subject["face"])
    assert 0 <= fx0 < fx1 <= MS.W and 0 <= fy0 < fy1 <= MS.H * 0.6     # face in the upper part of the frame


def test_a_burned_in_lower_third_is_cropped_out():
    subject = {"box": [0.22, 0.08, 0.86, 0.66], "face": [0.42, 0.21, 0.58, 0.5], "avoid": [0.1, 0.68, 0.8, 0.86]}
    (x0, y0, x1, y1), _ = MS.crop_window(1280, 720, subject, "medium")
    assert y1 <= 0.68 * 720 + 1


def test_motion_stays_inside_the_source():
    win, _ = MS.crop_window(1600, 2000, {"box": [0.35, 0.18, 0.65, 0.95]}, "medium")
    for motion in ("push", "pull", "drift", "drift_left", "snap"):
        for k in (0, 0.5, 1):
            x0, y0, x1, y1 = MS.motion_window(win, 1600, 2000, motion, k, hit_k=0.5 if motion == "snap" else 0)
            assert x0 >= -1e-6 and y0 >= -1e-6 and x1 <= 1600 + 1e-6 and y1 <= 2000 + 1e-6


def test_captions_go_where_the_player_is_not():
    face_low = (400, 1350, 680, 1550)
    assert MS.caption_zone((300, 900, 780, 1900), face_low) == "top"
    assert MS.caption_zone((300, 200, 780, 1100), (420, 260, 660, 520)) == "low"
    assert MS.overlay_zone("low", (420, 800, 660, 1000)) == "top"


# ------------------------------------------------------------------------------------------- rendering

class _Ctx:
    def __init__(self):
        from studio import motion as M
        self.team, self.story, self.tokens, self.headshot, self.accent = "LA", "praise", [], None, M.GOLD
        self.p = STAR


def _renderer(lib, look="punch"):
    scenes = {"s01": {"type": "statement", "cues": [], "_start": 0.0, "_dur": 2.0}}
    return MS.ShotRenderer(lib, _Ctx(), scenes, look, SEASON)


def _shot(asset, kind="photo", **kw):
    s = {"index": 1, "segment": "s01", "kind": kind, "asset": asset, "t0": 0.0, "t1": 2.0, "start": 0.0,
         "crop": {"framing": "medium", "motion": "push"}, "labels": [], "credit": "Test / CC BY 4.0"}
    s.update(kw)
    return s


def test_a_photo_shot_renders_full_frame_with_overlay_and_label(lib):
    r = _renderer(lib)
    toks = _tokens("24 targets", claims={"24": "c1"})
    img = r.draw(_shot("star-game", overlay="stat", stat_claim="c1", labels=["FILE PHOTO 2025"]), 1.0, 2.0, toks,
                 (255, 205, 64))
    assert img.size == (1080, 1920) and img.mode == "RGB"
    assert np.asarray(img).std() > 10


def test_a_small_source_becomes_a_card_not_mush(lib, tmp_path):
    _photo(ML.CACHE / "tiny.jpg", size=(300, 420))
    lib.add(_entry("tiny", "action_photo", path="tiny.jpg", player_id="00-STAR", name="Star Receiver", team="LA",
                   season=2026, width=300, height=420))
    r = _renderer(lib)
    _, _, _, full = r.geometry(_shot("tiny"))
    assert not full
    assert r.draw(_shot("tiny"), 0.5, 2.0, [], (255, 205, 64)).size == (1080, 1920)


def test_broll_snap_zoom_and_missing_slate_render(lib):
    r = _renderer(lib, "cold")
    a = r.draw(_shot("broll-ruler", kind="broll", effect="snap_zoom", labels=["ILLUSTRATIVE"]), 0.2, 2.0, [], (255, 0, 0))
    b = r.draw(_shot("broll-ruler", kind="broll", effect="snap_zoom", labels=["ILLUSTRATIVE"]), 1.9, 2.0, [], (255, 0, 0))
    assert np.abs(np.asarray(a, np.float32) - np.asarray(b, np.float32)).mean() > 2     # it snapped in
    slate = r.draw({"index": 2, "segment": "s01", "kind": "missing", "missing": "Star Receiver photo",
                    "t0": 0, "t1": 1}, 0.5, 2.0, [], (255, 0, 0))
    assert slate.size == (1080, 1920)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_licensed_clip_plays_through_the_reader(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=1",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    r = MS.ClipReader(str(clip))
    f0, f10 = r.frame(0), r.frame(10)
    r.close()
    assert f0.size == (1080, 1920) and np.abs(np.asarray(f0, np.float32) - np.asarray(f10, np.float32)).mean() > 0.5


# ------------------------------------------------------------------------------------------------- QC

class _Ep:
    def __init__(self, toks):
        self.claims = _claims()
        self.season = SEASON
        self.ctx = _Ctx()
        self.scenes = {"s01": {"type": "statement", "cues": []}}
        self.tokens = {"s01": toks}


def _qc_shot(asset, **kw):
    s = _shot(asset, intent="dominance", subject_id="00-STAR", subject_team="LA", names_player=True, dur=2.0,
              caption_zone="low", claims=[])
    s.update(kw)
    return s


def test_plan_qc_blocks_wrong_uniform_and_missing_labels(lib):
    ep = _Ep(_tokens("Star Receiver dominated"))
    sb = {"shots": [_qc_shot("star-college", labels=["FILE PHOTO 2021"]),
                    _qc_shot("team-runout", index=2, labels=[], subject_id=None)],
          "headshot_fallback_enabled": True}
    qc = QC.check_plan(sb, lib, ep)
    failed = {c["check"] for c in qc["checks"] if not c["ok"] and c["severity"] == "block"}
    assert {"team", "labels"} <= failed and not qc["pass"]


def test_plan_qc_checks_overlaid_numbers_against_claims(lib):
    toks = _tokens("24 targets", claims={"24": "c1"})
    ep = _Ep(toks)
    good = {"shots": [_qc_shot("star-game", overlay="stat", stat_claim="c1")], "headshot_fallback_enabled": True}
    assert QC.check_plan(good, lib, ep)["pass"]
    toks[0]["text"] = "42"
    assert not QC.check_plan(good, lib, ep)["pass"]


def test_slates_pass_an_experiment_but_block_publishing(lib):
    ep = _Ep(_tokens("Star"))
    sb = {"shots": [_qc_shot("star-game"), {"index": 2, "segment": "s01", "kind": "missing", "dur": 1.0,
                                            "caption_zone": "low"}], "headshot_fallback_enabled": True}
    assert QC.check_plan(sb, lib, ep, publish=False)["pass"]
    assert not QC.check_plan(sb, lib, ep, publish=True)["pass"]


# -------------------------------------------------------------------------------------------- end to end

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_reedit_writes_a_new_folder_and_leaves_the_source_untouched(lib, tmp_path):
    from studio.core import Config, sha256_path
    from studio.stages import editorial_media as EM
    src = tmp_path / "src"
    (src / "audio").mkdir(parents=True)
    (src / "work").mkdir()
    lines = [("Star Receiver dominated.", None), ("The Rams lost a shootout.", None)]
    script, facts, claims, toks, durs = _episode([(t, {}) for t, _ in lines])
    for seg in script["narration_segments"]:
        seg["text_template"] = seg["text_display"]
    script["episode_id"] = "test-reedit"
    (src / "script.json").write_text(json.dumps(script))
    facts["players"][0]["jersey"] = 12
    (src / "facts.json").write_text(json.dumps(facts))
    segs = []
    for sid, d in durs.items():
        words = [{"text": t["text"], "start": t["start"], "end": t["end"]} for t in toks[sid]]
        segs.append({"id": sid, "duration": d, "words": words})
        sf.write(src / f"audio/{sid}.wav", np.zeros(int(24000 * d), np.float32), 24000)
    (src / "audio/timings.json").write_text(json.dumps({"segments": segs}))
    total = sum(durs.values())
    sf.write(src / "work/mix.wav", (np.random.default_rng(1).standard_normal((int(48000 * total), 2)) * 0.01)
             .astype(np.float32), 48000)
    before = sha256_path(src)
    cfg = Config.load()
    cfg.raw["video"] = {**cfg.raw["video"], "fps": 10}
    out = tmp_path / "out"
    res = EM.make(src, out, cfg, "player_hype", lib=lib)
    assert sha256_path(src) == before
    for f in ("storyboard.json", "storyboard.md", "storyboard.png", "final.mp4", "captions.srt",
              "media_provenance.json", "media_qc.json", "visual_audit.json"):
        assert (out / f).exists(), f
    assert res["qc_preview_pass"]
    prov = json.loads((out / "media_provenance.json").read_text())
    assert {a["id"] for a in prov["assets"]} <= set(lib.entries)
    sb = json.loads((out / "storyboard.json").read_text())
    assert sb["shots"][0]["asset"] == "star-game"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_video_broll_entry_plans_and_renders_without_being_opened_as_a_still(lib, tmp_path):
    clip = tmp_path / "ruler.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=2",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    lib.entries.pop("broll-ruler")
    e = SRC.add_local(lib, clip, "broll-ruler-clip", "context_broll", "CC0", "", "https://example.org/clip",
                      illustrative=True, relevance=["ruler"])
    assert e.is_video and (e.width, e.height) == (640, 360)
    script, facts, claims, toks, durs = _episode([("Measuring his passes? Grab a smaller ruler.", {})], role="flame")
    script["narration_segments"][0]["scene"]["cues"] = [{"word": "ruler", "event": "punchline", "sfx": "bruh"}]
    p = SP.plan(script, facts, claims, toks, durs, lib, "roast", season=SEASON, cfg_headshots=True, episode_id="x")
    shot = next(s for s in p.shots if s.get("asset") == "broll-ruler-clip")
    r = _renderer(lib, "cold")
    assert r.zones(shot)[0] in ("low", "top")
    _, size, win, full = r.geometry(shot)
    assert size == (640, 360) and full and abs((win[2] - win[0]) / (win[3] - win[1]) - 9 / 16) < 1e-6
    assert r.draw(shot, shot["t0"] + 0.3, 3.0, toks["s01"], (255, 0, 0)).size == (1080, 1920)
    r.close()


def test_add_local_accepts_the_licence_reference_a_paid_file_needs(lib, tmp_path, monkeypatch):
    src = _photo(tmp_path / "paid.jpg")
    monkeypatch.setattr(SRC, "detect_subject", lambda path: {"box": [0.2, 0.1, 0.8, 0.95], "face": None,
                                                             "method": "test"})
    monkeypatch.setattr(ML.Library, "load", classmethod(lambda cls, path=None: lib))
    monkeypatch.setattr(ML.Library, "save", lambda self: None)
    SRC.main(["add-local", str(src), "--id", "paid-1", "--type", "team_photo", "--license", "licensed",
              "--license-ref", "INV-123", "--attribution", "Agency / Photographer", "--team", "LA",
              "--url", "https://example.org/paid"])
    assert lib.get("paid-1").license_ref == "INV-123" and ML.validate(lib.get("paid-1")) == []
