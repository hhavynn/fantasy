"""Snapshot refresh on a new sync, no stale narration WAVs, and the real Shorts voice in provenance."""

from __future__ import annotations

import numpy as np
import soundfile as sf

from studio.core import Config, Episode, read_json, write_json
from studio.stages import provenance, snapshot, tts


def _ep(tmp_path, monkeypatch, fmt="short"):
    monkeypatch.setenv("STUDIO_EPISODES", str(tmp_path / "episodes"))
    cfg = Config.load()
    # the owner's enrolled voice is local and git-ignored (as in conftest's episode fixture); test_voice covers cloning
    cfg.raw["shorts"] = {**cfg.raw["shorts"], "voice_clone": ""}
    ep = Episode(2026, 6, fmt, cfg, log=lambda *_: None, slug="someone" if fmt == "short" else None)
    ep.dir.mkdir(parents=True)
    return ep


def test_a_new_sync_of_the_same_data_refreshes_the_snapshot(tmp_path, monkeypatch):
    ep = _ep(tmp_path, monkeypatch)
    sync = {"finished_at": "2026-10-01T00:00:00Z"}

    class Con:
        def close(self):
            pass
    monkeypatch.setattr(snapshot, "connect", lambda _path: Con())
    monkeypatch.setattr(snapshot, "build_snapshot", lambda con, s, w: {"tables": {"player_week_usage": [1]},
                                                                      "data_through_week": 5, "source_sync": dict(sync)})
    snapshot.run(ep)
    first = ep.path("snapshot.json").read_text()
    snapshot.run(ep)                                   # same data, same sync: untouched
    assert ep.path("snapshot.json").read_text() == first
    sync["finished_at"] = "2026-10-15T00:00:00Z"       # a fresh sync of identical data
    snapshot.run(ep)
    snap = read_json(ep.path("snapshot.json"))
    assert snap["source_sync"]["finished_at"] == "2026-10-15T00:00:00Z"


def test_regenerated_narration_leaves_no_stale_segments(tmp_path, monkeypatch):
    ep = _ep(tmp_path, monkeypatch)
    write_json(ep.path("script.json"), {"narration_segments": [{"id": "s01", "text_spoken": "hello there"}]})
    (ep.path("audio")).mkdir()
    sf.write(ep.path("audio/s02.wav"), np.zeros(100, np.float32), 24000)     # from an earlier, longer script
    monkeypatch.setattr(tts, "load_pronunciations", lambda: {})
    monkeypatch.setattr(tts, "make_pipeline", lambda cfg: None)
    monkeypatch.setattr(tts, "lexicon_misses", lambda text: [])
    monkeypatch.setattr(tts, "synthesize", lambda *a, **k: (np.zeros(2400, np.float32), [], []))
    tts._run(ep)
    assert sorted(p.name for p in ep.path("audio").glob("*.wav")) == ["s01.wav"]


def test_provenance_records_the_voice_that_spoke(tmp_path, monkeypatch):
    ep = _ep(tmp_path, monkeypatch)
    shorts_voice = ep.cfg["shorts"]["voice"]
    assert provenance.spoken_voice(ep, {"voice": shorts_voice})[0] == shorts_voice
    assert provenance.spoken_voice(ep, {})[0] == shorts_voice          # no record: the Short's own setting
    long_ep = _ep(tmp_path / "long", monkeypatch, fmt="long")
    assert provenance.spoken_voice(long_ep, {})[0] == long_ep.cfg["tts"]["voice"]


def test_name_suffixes_are_spoken_as_words_one_token_each():
    """"Jr." is read "junior" (not "jay are"); each suffix stays one inline word so timestamps line up."""
    lex = tts.load_pronunciations()
    for text, word, phon in [("Is Kevin Coleman Jr.'s new role real?", "Jr.'s", "ʤˈunjəɹz"),
                             ("Kevin Coleman Jr. went from 10% to 27%.", "Jr.", "ʤˈunjəɹ"),
                             ("Odell Beckham Jr's role", "Jr's", "ʤˈunjəɹz"),
                             ("Marvin Harrison Jr", "Jr", "ʤˈunjəɹ"),
                             ("Coleman Sr. played", "Sr.", "sˈinjəɹ")]:
        out = tts.apply_pronunciations(text, lex)
        assert f"[{word}](/{phon}/)" in out and out.count("[") == 1, out


def test_the_backends_pandas_guard_stays_out_of_the_studio():
    """facts.py imports the backend, whose fast-fail pandas finder makes find_spec raise. torch asks find_spec about
    optional packages while Kokoro loads, so the guard must not outlive that import here."""
    import importlib.util

    from studio.stages import facts  # noqa: F401
    importlib.util.find_spec("pandas")
