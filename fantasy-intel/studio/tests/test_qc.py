"""Every QC gate must fail on a deliberately broken input, and pass on a clean one."""

from __future__ import annotations

import copy
import subprocess

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from studio.core import read_json, write_json
from studio.stages import facts, qc, script


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def media(tmp_path, name: str, video: str, audio: str | None, extra: tuple[str, ...] = ()):
    out = tmp_path / name
    args = ["-f", "lavfi", "-i", video]
    if audio:
        args += ["-f", "lavfi", "-i", audio]
    ffmpeg(*args, *extra, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out))
    return out


TONE = "aevalsrc=0.3*sin(2*PI*440*t):s=48000:d=4"
MOVING = "testsrc2=s=320x180:r=30:d=4"


@pytest.fixture
def scripted(episode):
    facts.run(episode)
    script.run(episode)
    return episode


def failures(gate) -> str:
    assert not gate.passed, gate
    return " ".join(gate.failures)


# ------------------------------------------------------------------------------------ data claims

def test_claims_pass_then_fail_on_a_wrong_number(scripted):
    snap, f, s = (read_json(scripted.path(p)) for p in ("snapshot.json", "facts.json", "script.json"))
    assert qc.gate_claims(snap, f, s).passed
    bad = copy.deepcopy(s)
    bad["claims"][0]["value"] = 0.99
    assert "data says" in failures(qc.gate_claims(snap, f, bad))
    bad = copy.deepcopy(s)
    bad["claims"][0]["display"] = "99%"
    assert "data renders" in failures(qc.gate_claims(snap, f, bad))


def test_claims_fail_when_the_data_changed_under_the_script(scripted):
    snap, f, s = (read_json(scripted.path(p)) for p in ("snapshot.json", "facts.json", "script.json"))
    snap["tables"]["player_week_usage"][0]["offense_pct"] = 0.5
    snap["content_sha256"] = "different"
    assert "different snapshot" in failures(qc.gate_claims(snap, f, s))


def test_narration_fails_on_numbers_not_from_claims(scripted):
    s = read_json(scripted.path("script.json"))
    assert qc.gate_narration(s).passed
    bad = copy.deepcopy(s)
    seg = bad["narration_segments"][0]
    seg["text_template"] += " He saw 12 targets."
    assert "typed outside any claim" in failures(qc.gate_narration(bad))
    bad = copy.deepcopy(s)
    bad["narration_segments"][0]["text_template"] += " He saw twelve targets."
    assert "number words" in failures(qc.gate_narration(bad))
    bad = copy.deepcopy(s)
    bad["narration_segments"][0]["text_spoken"] = "something else entirely"
    assert "what TTS read" in failures(qc.gate_narration(bad))
    bad = copy.deepcopy(s)
    bad["narration_segments"][0]["text_template"] += " Great for fantasy points."
    assert "banned" in failures(qc.gate_narration(bad))


# ------------------------------------------------------------------------------------------- media

def test_media_gates_pass_on_clean_media(tmp_path):
    good = media(tmp_path, "good.mp4", MOVING, TONE, ("-shortest",))
    assert qc.gate_video(good).passed
    assert qc.gate_av_sync(good).passed
    assert qc.gate_audio(good, []).passed
    assert qc.gate_duration(good, 3, 5).passed


def test_duration_out_of_bounds(tmp_path):
    good = media(tmp_path, "good.mp4", MOVING, TONE, ("-shortest",))
    assert "outside" in failures(qc.gate_duration(good, 480, 720))


def test_long_silence_fails(tmp_path):
    gap = "aevalsrc=if(between(t\\,1\\,3)\\,0\\,0.3*sin(2*PI*440*t)):s=48000:d=4"
    v = media(tmp_path, "gap.mp4", MOVING, gap, ("-shortest",))
    assert "silence" in failures(qc.gate_audio(v, []))


def test_clipped_or_empty_narration_fails(tmp_path):
    good = media(tmp_path, "good.mp4", MOVING, TONE, ("-shortest",))
    hot = tmp_path / "hot.wav"
    sf.write(hot, np.ones(24000, dtype=np.float32), 24000)
    empty = tmp_path / "empty.wav"
    sf.write(empty, np.zeros(0, dtype=np.float32), 24000)
    msg = failures(qc.gate_audio(good, [hot, empty]))
    assert "hot.wav: narration clipped" in msg and "empty.wav: empty audio" in msg


def test_black_frames_fail(tmp_path):
    v = media(tmp_path, "black.mp4", "color=black:s=320x180:r=30:d=4", TONE, ("-shortest",))
    assert "black frames" in failures(qc.gate_video(v))


def test_frozen_frames_fail(tmp_path):
    v = media(tmp_path, "frozen.mp4", "color=0x6aaeff:s=320x180:r=30:d=8",
              "aevalsrc=0.3*sin(2*PI*440*t):s=48000:d=8", ("-shortest",))
    assert "frozen" in failures(qc.gate_video(v))


def test_av_length_mismatch_fails(tmp_path):
    v = media(tmp_path, "mismatch.mp4", MOVING, "aevalsrc=0.3*sin(2*PI*440*t):s=48000:d=2")
    assert "vs audio" in failures(qc.gate_av_sync(v))


# --------------------------------------------------------------------------------- text and rights

@pytest.mark.parametrize("geometry", [
    {"font": "Inter-SemiBold.ttf", "font_size": 46, "max_width_px": 1500, "max_lines": 2},     # long form
    {"font": "Inter-Black.ttf", "font_size": 64, "max_width_px": 860, "max_lines": 2},         # Shorts
])
def test_subtitle_overflow_fails(geometry):
    ok = {**geometry, "cues": [{"start": 0, "end": 1.5, "text": "Snap share rose to 88%."}]}
    assert qc.gate_subtitles(ok).passed
    long = " ".join(["overflowing"] * 40)
    assert "lines" in failures(qc.gate_subtitles({**geometry, "cues": [{"start": 0, "end": 2, "text": long}]}))


def test_originality_fails_on_a_near_copy_and_a_recycled_cast():
    narration = ["Snap share jumped from a fringe role to nearly every down for the Eagles receiver this week."] * 3
    players = ["a", "b", "c", "d"]
    history = [{"episode_id": "2026-w05-long", "narration": narration + ["cta"], "players": players}]
    msg = failures(qc.gate_originality("2026-w06-long", narration, players, history))
    assert "similar" in msg and "already featured" in msg
    fresh = ["A completely different script about other players and other teams entirely."]
    assert qc.gate_originality("2026-w06-long", fresh, ["x", "y"], history).passed
    # Its own earlier entry is never compared with itself.
    assert qc.gate_originality("2026-w05-long", narration, players, history).passed


def test_license_gate_blocks_banned_weights(episode):
    assert qc.gate_licenses(episode.cfg, {"kokoro", "misaki"}).passed
    assert "f5-tts" in failures(qc.gate_licenses(episode.cfg, {"kokoro", "f5-tts"}))
    episode.cfg.raw["tts"] = {**episode.cfg["tts"], "repo": "coqui/XTTS-v2", "license": "CPML"}
    msg = failures(qc.gate_licenses(episode.cfg, set()))
    assert "allowlist" in msg and "xtts" in msg


def test_metadata_must_stay_private_and_disclosed(tmp_path):
    thumb = tmp_path / "t.png"
    Image.new("RGB", (1280, 720)).save(thumb)
    meta = {"privacy_status": "private", "self_declared_made_for_kids": False, "contains_synthetic_media": True,
            "description": "x\nNarrated with an AI voice.\nData: nflverse", "title": "Usage risers",
            "tags": ["a"], "chapters": [{"start": 0}, {"start": 20}, {"start": 40}]}
    assert qc.gate_metadata(meta, thumb).passed
    msg = failures(qc.gate_metadata({**meta, "privacy_status": "public", "description": "x"}, thumb))
    assert "private" in msg and "AI-voice" in msg


def test_integrity_fails_when_an_input_changes_after_its_stage(scripted):
    f = read_json(scripted.path("facts.json"))
    f["players"] = f["players"][:1]
    write_json(scripted.path("facts.json"), f)
    msg = failures(qc.gate_integrity(scripted))
    assert "script: facts.json changed" in msg
