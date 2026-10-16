"""Stage 10: provenance.json, the episode's rights and reproducibility record.

Git commit, every model and checkpoint with its license and hash, tool versions, the data snapshot hash
and source sync, every prompt sent to the LLM, and the hashes of the final files and the QC result.
"""

from __future__ import annotations

import json
import os
import subprocess
from importlib import metadata as importlib_metadata
from pathlib import Path

from .. import headshots
from ..core import STUDIO_ROOT, Episode, read_json, sha256_file, sha256_path, utcnow, write_json
from .script import SYSTEM_PROMPT, VERIFY_SYSTEM

HASH_CACHE = Path(os.path.expanduser("~/.cache/fantasy-studio/hashes.json"))
PACKAGES = ("kokoro", "misaki", "torch", "espeakng-loader", "phonemizer-fork", "spacy", "en-core-web-sm",
            "duckdb", "matplotlib", "pillow", "numpy", "soundfile", "httpx", "jsonschema")
KOKORO_FILES = ("kokoro-v1_0.pth", "config.json")


def _cmd(*args: str, strip: bool = True) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
        return out.strip() if strip else out
    except (OSError, subprocess.CalledProcessError):
        return ""


def cached_sha256(path: Path) -> str | None:
    """sha256 of a large file, cached by (path, size, mtime) so the 18 GB GGUF is hashed once."""
    if not path.exists():
        return None
    st = path.stat()
    key = f"{path}:{st.st_size}:{int(st.st_mtime)}"
    cache = json.loads(HASH_CACHE.read_text()) if HASH_CACHE.exists() else {}
    if key not in cache:
        cache[key] = sha256_file(path)
        HASH_CACHE.parent.mkdir(parents=True, exist_ok=True)
        HASH_CACHE.write_text(json.dumps(cache, indent=1))
    return cache[key]


def git_state() -> dict:
    root = STUDIO_ROOT
    return {"commit": _cmd("git", "-C", str(root), "rev-parse", "HEAD"),
            "branch": _cmd("git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"),
            "dirty_files": [l[3:] for l in _cmd("git", "-C", str(root), "status", "--porcelain", "--", ".",
                                                 strip=False).splitlines() if l.strip()]}


def kokoro_files(repo: str, voice: str) -> dict:
    from huggingface_hub import try_to_load_from_cache
    from .tts import parse_voice
    out = {}
    for name in (*KOKORO_FILES, *(f"voices/{v}.pt" for v, _ in parse_voice(voice))):
        path = try_to_load_from_cache(repo, name)
        if isinstance(path, str):
            out[name] = {"sha256": cached_sha256(Path(path)), "revision": Path(path).parts[-2 - name.count("/")]}
        else:
            out[name] = {"sha256": None, "revision": None}
    return out


def package_versions() -> dict:
    out = {}
    for name in PACKAGES:
        try:
            out[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            out[name] = None
    return out


def _clone_info(cfg) -> dict | None:
    name = cfg["shorts"].get("voice_clone") if "shorts" in cfg.raw else None
    if not name:
        return None
    from ..voice import consent, weights_info
    rec = consent(name)
    return {"voice": name, "converter": weights_info(),
            "consent": {k: rec.get(k) for k in ("speaker", "test", "declaration", "declared_at", "embedding_sha256",
                                                "recordings")} if rec else None}


def spoken_voice(ep: Episode, timings: dict) -> tuple[str, dict]:
    """The voice that actually spoke, as recorded at synthesis (a Short uses [shorts] voice, not [tts] voice), and
    the episode's effective TTS settings."""
    from .tts import tts_config
    cfg = tts_config(ep)
    return timings.get("voice") or cfg["voice"], cfg


def build(ep: Episode) -> dict:
    cfg = ep.cfg
    script = read_json(ep.path("script.json"))
    snapshot = read_json(ep.path("snapshot.json"))
    timings = read_json(ep.path("audio/timings.json"))
    voice, tts_cfg = spoken_voice(ep, timings)
    qc = read_json(ep.path("qc.json")) if ep.path("qc.json").exists() else None
    ffmpeg_version = _cmd("ffmpeg", "-version").splitlines()
    prompts = {p.name: p.read_text() for p in sorted(ep.path("prompts").glob("*.txt"))}
    gguf = cfg.model_file()
    facts_players = read_json(ep.path("facts.json"))["players"] if ep.path("facts.json").exists() else []
    return {
        "episode_id": ep.id,
        "generated_at": utcnow(),
        "git": git_state(),
        "models": {
            "script_llm": {
                "runtime": "llama.cpp llama-server", "runtime_license": "MIT",
                "runtime_version": _cmd("llama-server", "--version").splitlines()[0:1],
                "model": cfg["llm"]["model"], "repo": cfg["llm"]["model_repo"],
                "file": gguf.name, "sha256": cached_sha256(gguf),
                "weights_license": cfg["llm"]["model_license"],
                "base_model": "Qwen/Qwen3-30B-A3B-Instruct-2507 (Apache-2.0)",
                "served_model": script["llm"].get("served_model"),
                "params": {k: cfg["llm"].get(k) for k in ("temperature", "max_tokens", "presence_penalty",
                                                          "repeat_penalty", "max_retries")},
            },
            "tts": {
                "engine": "Kokoro-82M", "repo": cfg["tts"]["repo"], "code_license": "Apache-2.0",
                "weights_license": cfg["tts"]["license"], "voice": voice,
                "voice_origin": "built-in Kokoro voice or a weighted mix of them (not a clone of any person)",
                "speed": tts_cfg["speed"], "speed_scale": tts_cfg.get("speed_scale", 1.0),
                "files": kokoro_files(cfg["tts"]["repo"], voice),
                "g2p": {"misaki": "Apache-2.0",
                        "espeak-ng fallback": "GPL-3.0; run locally, never distributed; its output audio is "
                                              "not covered by the GPL",
                        "fallback_words": timings.get("fallback_words", [])},
            },
            "voice_clone": _clone_info(cfg),
            "music": "procedural, generated by studio/audio_design.py (no third-party audio)",
            "third_party_images": [h for h in [headshots.record(cfg, pl) for pl in facts_players] if h]
                                  if ep.fmt == "short" else [],
        },
        "tools": {
            "ffmpeg": ffmpeg_version[0] if ffmpeg_version else None,
            "ffmpeg_gpl_build": any("--enable-gpl" in l for l in ffmpeg_version),
            "ffmpeg_note": "used locally to encode; the binary is not distributed",
            "font": "Inter 4.1 (SIL OFL 1.1), bundled",
            "python_packages": package_versions(),
        },
        "data": {
            "snapshot_sha256": snapshot["content_sha256"],
            "data_through_week": snapshot["data_through_week"],
            "source_sync": snapshot["source_sync"],
            "sources": "nflverse (CC BY 4.0): play-by-play, snap counts, schedules, rosters",
            "tables": sorted(snapshot["tables"]),
        },
        "prompts": {
            "writer_system": SYSTEM_PROMPT,
            "fact_check_system": VERIFY_SYSTEM,
            "calls": script["llm"]["calls"],
            "files": prompts,
        },
        "content": {"made_for_kids": script["made_for_kids"], "synthetic_voice": script["synthetic_voice"],
                    "visuals": "charts and text cards from our data only; no footage, logos or photos"},
        "qc": {"passed": qc["passed"], "sha256": sha256_file(ep.path("qc.json"))} if qc else None,
        "files": {f: sha256_path(ep.path(f)) for f in
                  ("snapshot.json", "facts.json", "script.json", "audio", "shots", "final.mp4", "captions.srt",
                   "thumbnail.png", "metadata.json") if ep.path(f).exists()},
    }


def run(ep: Episode) -> None:
    write_json(ep.path("provenance.json"), build(ep))
    ep.log("  provenance: provenance.json")
