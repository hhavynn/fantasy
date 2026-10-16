"""Fetch the meme registry's sounds (assets/sfx/registry.toml) into assets/sfx/meme/, which git ignores.

    uv run python -m studio.sfx_fetch            # only what is missing
    uv run python -m studio.sfx_fetch --all      # re-download everything

The files are soundboard clips with no license held by the channel; they are kept out of the repository on
purpose. Without them, the series falls back to its procedural sounds (sfx_palette.select notes it per cue).
"""

from __future__ import annotations

import sys
import urllib.request

from .sfx_palette import MEME_DIR, REGISTRY, meme_file

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"


def fetch(everything: bool = False) -> list[str]:
    MEME_DIR.mkdir(parents=True, exist_ok=True)
    got = []
    for sid, e in REGISTRY.items():
        path = meme_file(sid)
        if path.exists() and not everything:
            continue
        req = urllib.request.Request(e["source"], headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            path.write_bytes(r.read())
        got.append(sid)
    return got


if __name__ == "__main__":
    print("fetched:", fetch("--all" in sys.argv) or "nothing missing")
