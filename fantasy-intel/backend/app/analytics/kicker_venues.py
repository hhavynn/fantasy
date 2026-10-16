"""Game venue resolution for kicker projections (stdlib only).

The venue of a game is resolved per GAME, never from the home team's current stadium:

  1. `game_overrides` in kicker_data/stadiums.json (reviewed corrections, e.g. the 2025 international games that
     nflverse lists under the home team's own stadium);
  2. nflverse `stadium_id` looked up in the registry;
  3. otherwise the venue is UNKNOWN (no coordinates, no roof class): weather is never fetched for it and the
     model's venue inputs fall back to their training means, with a flag.

Roof: the registry stores the venue's CAPABILITY (open_air | fixed | retractable). A retractable roof's game-day
position is not known before it is announced, so pregame it is `retractable_unknown`; the position nflverse
records for a past game ('open' / 'closed') is an OBSERVATION and is used only in retrospective analysis.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

REGISTRY_PATH = Path(__file__).with_name("kicker_data") / "stadiums.json"
ROOF_CLASSES = ("open_air", "fixed", "retractable")


@lru_cache(maxsize=1)
def registry() -> dict:
    data = json.loads(REGISTRY_PATH.read_text())
    if data.get("schema") != "kicker-venues/v1":
        raise ValueError(f"unsupported venue registry schema {data.get('schema')!r}")
    for vid, v in data["venues"].items():
        if v["roof"] not in ROOF_CLASSES:
            raise ValueError(f"venue {vid}: roof {v['roof']!r} is not one of {ROOF_CLASSES}")
    for gid, o in data["game_overrides"].items():
        if o["venue_id"] not in data["venues"]:
            raise ValueError(f"override {gid} points at unknown venue {o['venue_id']}")
    return data


def resolve(game: dict, reg: dict | None = None) -> dict:
    """Venue for one schedule row (keys used: game_id, season, stadium_id, stadium, roof, location).
    Returns {venue_id, name, source, roof_class, roof_status, outdoor, lat, lon, elevation_m, country, tz,
    surface, neutral, notes}; unknown fields are None."""
    reg = reg or registry()
    notes: list[str] = []
    gid, sid = game.get("game_id"), game.get("stadium_id")
    override = reg["game_overrides"].get(gid) if gid else None
    if override:
        vid, source = override["venue_id"], "override"
        notes.append(f"venue corrected: {override['reason']}")
    elif sid in reg["venues"]:
        vid, source = sid, "registry"
    else:
        vid, source = None, "unknown"
        notes.append(f"venue {sid or game.get('stadium') or 'unknown'} is not in the venue registry")
    v = reg["venues"].get(vid) if vid else None
    season = game.get("season")
    if v and season is not None:
        lo, hi = v.get("seasons") or [None, None]
        if (lo is not None and season < lo) or (hi is not None and season > hi):
            notes.append(f"{v['name']} is registered for seasons {lo}-{hi or 'now'}, not {season}")
    roof_class = v["roof"] if v else None
    observed = (game.get("roof") or "").lower() or None
    if roof_class == "retractable":
        status = "retractable_unknown"
    else:
        status = roof_class
    if v and observed and source == "registry":
        implied = {"outdoors": "open_air", "dome": "fixed", "open": "retractable", "closed": "retractable"}.get(observed)
        if implied and implied != roof_class:
            notes.append(f"nflverse roof '{observed}' disagrees with the registry ({roof_class}); registry used")
    return {"venue_id": vid, "name": v["name"] if v else game.get("stadium"), "source": source,
            "roof_class": roof_class, "roof_status": status,
            "outdoor": None if roof_class is None else roof_class == "open_air",
            "lat": v["lat"] if v else None, "lon": v["lon"] if v else None,
            "elevation_m": v["elevation_m"] if v else None, "country": v["country"] if v else None,
            "tz": v["tz"] if v else None, "surface": v["surface"] if v else None,
            "neutral": (game.get("location") or "").lower() == "neutral", "notes": notes}


def observed_roof_status(venue: dict, nflverse_roof: str | None) -> str | None:
    """RETROSPECTIVE ONLY: the roof position nflverse recorded for a completed game at a retractable venue."""
    if venue.get("roof_class") != "retractable":
        return venue.get("roof_status")
    r = (nflverse_roof or "").lower()
    return {"open": "retractable_open", "closed": "retractable_closed"}.get(r, "retractable_unknown")
