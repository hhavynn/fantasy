"""Consent gating for a cloned narration voice (no model is loaded here)."""

from __future__ import annotations

import numpy as np
import soundfile as sf

from studio import voice
from studio.core import sha256_file, write_json


def _enrolled(tmp_path, monkeypatch, name="owner", test=False, declared=True):
    monkeypatch.setattr(voice, "VOICES", tmp_path)
    d = tmp_path / name
    d.mkdir()
    (d / "se.pt").write_bytes(b"embedding")
    write_json(d / "consent.json", {"speaker": name, "test": test,
                                    "declaration": voice.DECLARATION if declared else None,
                                    "embedding_sha256": sha256_file(d / "se.pt")})
    return d


def test_owner_voice_with_declaration_is_publishable(tmp_path, monkeypatch):
    _enrolled(tmp_path, monkeypatch)
    assert voice.consent_problems("owner") == []


def test_unenrolled_test_undeclared_or_swapped_voices_are_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "VOICES", tmp_path)
    assert "no consent record" in voice.consent_problems("nobody")[0]
    _enrolled(tmp_path, monkeypatch, "standin", test=True, declared=False)
    msgs = " ".join(voice.consent_problems("standin"))
    assert "stand-in" in msgs and "declaration" in msgs
    d = _enrolled(tmp_path, monkeypatch, "swapped")
    (d / "se.pt").write_bytes(b"someone else's embedding")
    assert "does not match" in voice.consent_problems("swapped")[0]


def test_recording_checks_catch_short_clipped_and_quiet_audio(tmp_path):
    sr = 24000
    clipped = tmp_path / "clipped.wav"
    sf.write(clipped, np.ones(sr * 5, dtype=np.float32), sr)
    quiet = tmp_path / "quiet.wav"
    sf.write(quiet, np.full(sr * 5, 0.001, dtype=np.float32), sr)
    _, problems = voice.check_recordings([clipped, quiet])
    text = " ".join(problems)
    assert "clipped" in text and "quiet" in text and "record at least" in text


def test_enroll_requires_the_speakers_declaration(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "VOICES", tmp_path)
    try:
        voice.enroll("owner", [], declared=False)
    except SystemExit as exc:
        assert "--i-am-the-speaker" in str(exc)
    else:
        raise AssertionError("enrollment without a declaration must fail")
