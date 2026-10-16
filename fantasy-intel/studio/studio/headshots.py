"""Player headshots from ESPN's CDN, used at the channel owner's direction.

These are ESPN's (and its photo agencies') photos and are NOT licensed to this channel: the owner chose to use
them, as is common on fantasy TikTok, accepting the risk of copyright claims. They are off with
`[shorts] headshots = false`, which brings back the original jersey graphic everywhere. Files are cached in
assets/headshots/ (git-ignored, never committed) and provenance records each one's URL and hash so it is
always clear which videos carry a third-party photo.
"""

from __future__ import annotations

import io
from pathlib import Path

import httpx
from PIL import Image

from .core import STUDIO_ROOT, sha256_file

URL = "https://a.espncdn.com/i/headshots/nfl/players/full/{id}.png"
CACHE = STUDIO_ROOT / "assets" / "headshots"
RIGHTS = "ESPN headshot; not licensed to this channel; used at the channel owner's direction"


def enabled(cfg) -> bool:
    return bool(cfg["shorts"].get("headshots", False))


def fetch(espn_id) -> Path | None:
    """The cached headshot PNG for an ESPN player id, downloading it once; None if ESPN has none."""
    if not espn_id:
        return None
    path = CACHE / f"{int(espn_id)}.png"
    if path.exists():
        return path
    try:
        r = httpx.get(URL.format(id=int(espn_id)), timeout=20, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if r.status_code != 200 or not r.content.startswith(b"\x89PNG"):
        return None
    try:
        im = Image.open(io.BytesIO(r.content))
        im.verify()
    except Exception:
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return path


def for_player(cfg, player: dict) -> Path | None:
    return fetch(player.get("espn_id")) if enabled(cfg) else None


def record(cfg, player: dict) -> dict | None:
    """Provenance entry for the headshot a video used (None when it used none)."""
    path = for_player(cfg, player)
    if path is None:
        return None
    return {"player": player["name"], "espn_id": player.get("espn_id"),
            "url": URL.format(id=int(player["espn_id"])), "sha256": sha256_file(path), "rights": RIGHTS}
