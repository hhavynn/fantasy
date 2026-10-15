"""Config, episode context, hashing and the idempotent stage runner.

Every stage reads versioned input files and writes versioned output files. A stage's input hash
covers its code version, its parameters and the sha256 of every input file; when the hash matches the
last successful run and the recorded outputs are unchanged on disk, the stage is skipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

STUDIO_ROOT = Path(__file__).resolve().parents[1]
ASSETS = STUDIO_ROOT / "assets"
FONTS = ASSETS / "fonts"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_path(path: Path) -> str:
    """File hash, or for a directory the hash of its (relative path, file hash) listing."""
    if path.is_dir():
        listing = [(p.relative_to(path).as_posix(), sha256_file(p))
                   for p in sorted(path.rglob("*")) if p.is_file()]
        return sha256_bytes(canonical_json(listing).encode())
    return sha256_file(path)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Config:
    raw: dict
    path: Path

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or STUDIO_ROOT / "config.toml"
        return cls(tomllib.loads(path.read_text()), path)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def _resolve(self, value: str) -> Path:
        p = Path(os.path.expanduser(value))
        return p if p.is_absolute() else (self.path.parent / p).resolve()

    @property
    def db_path(self) -> Path:
        return self._resolve(os.environ.get("STUDIO_DB") or self.raw["db_path"])

    @property
    def episodes_dir(self) -> Path:
        return self._resolve(os.environ.get("STUDIO_EPISODES") or self.raw["episodes_dir"])

    @property
    def season(self) -> int:
        return int(self.raw["season"])

    def model_file(self) -> Path:
        return self._resolve(self.raw["llm"]["model_file"])


@dataclass
class Episode:
    season: int
    week: int
    fmt: str                     # long | short
    cfg: Config
    force: set[str] = field(default_factory=set)
    log: Callable[[str], None] = print
    slug: str | None = None      # one Short per player: 2026-w06-short-braelon-allen

    @property
    def id(self) -> str:
        base = f"{self.season}-w{self.week:02d}-{self.fmt}"
        return f"{base}-{self.slug}" if self.slug else base

    @property
    def dir(self) -> Path:
        return self.cfg.episodes_dir / self.id

    def path(self, rel: str) -> Path:
        return self.dir / rel

    @property
    def size(self) -> tuple[int, int]:
        w, h = self.cfg["video"]["long_size" if self.fmt == "long" else "short_size"]
        return int(w), int(h)


def run_stage(
    ep: Episode,
    name: str,
    version: int,
    params: dict,
    input_files: Iterable[str],
    outputs: Iterable[str],
    fn: Callable[[], None],
) -> bool:
    """Run `fn` unless its inputs are unchanged since the last successful run. Returns True if it ran."""
    input_files, outputs = list(input_files), list(outputs)
    files = {rel: sha256_path(ep.path(rel)) for rel in input_files}
    input_hash = sha256_bytes(canonical_json({"stage": name, "version": version,
                                              "params": params, "files": files}).encode())
    record_path = ep.path(f".stages/{name}.json")
    if name not in ep.force and "all" not in ep.force and record_path.exists():
        record = read_json(record_path)
        if record.get("input_hash") == input_hash and all(
            ep.path(rel).exists() and sha256_path(ep.path(rel)) == digest
            for rel, digest in record.get("outputs", {}).items()
        ):
            ep.log(f"  {name}: unchanged, skipped")
            return False
    ep.log(f"  {name}: running")
    fn()
    missing = [rel for rel in outputs if not ep.path(rel).exists()]
    if missing:
        raise RuntimeError(f"stage {name} did not write {missing}")
    write_json(record_path, {
        "stage": name, "version": version, "input_hash": input_hash, "params": params,
        "inputs": files, "outputs": {rel: sha256_path(ep.path(rel)) for rel in outputs},
        "finished_at": utcnow(),
    })
    return True
