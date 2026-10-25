"""Raw provider payloads kept next to each league version (`raw/<lg>/<data_version>.json.gz`, spec §3.7), so a
`recompute` job can rebuild the league file against a new global file without fetching. Member SWIDs are hashed
before anything is serialised (app/espn/parse.scrub_member_ids); the job writes from the scrubbed payloads too, so
a fresh build and a recompute produce the same rows."""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from typing import Any

from ..espn.parse import scrub_member_ids
from ..espn.sync import EspnPayloads
from ..sleeper.write import SleeperPayloads

FORMAT = "espn-payloads-v1"
SLEEPER_FORMAT = "sleeper-payloads-v1"     # Sleeper's ids are public: nothing to scrub
_INT_KEYED = ("rosters", "transactions", "projections", "projection_failures")
_TIMES = ("fetch_started_at", "fetched_at")


def scrub(p: EspnPayloads) -> EspnPayloads:
    """The payloads with every SWID-shaped string replaced by its member hash (in place; returns `p`)."""
    for name in ("league", "pro_players", "pool", "draft"):
        setattr(p, name, scrub_member_ids(getattr(p, name)))
    for name in ("rosters", "transactions", "projections"):
        setattr(p, name, {k: scrub_member_ids(v) for k, v in getattr(p, name).items()})
    p.activity = scrub_member_ids(p.activity)
    p.cards = scrub_member_ids(p.cards)
    return p


def dump_payloads(p: EspnPayloads | SleeperPayloads) -> bytes:
    if isinstance(p, SleeperPayloads):
        doc = {"format": SLEEPER_FORMAT, **p.to_json()}
        return gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), mtime=0)
    scrub(p)
    doc: dict[str, Any] = {"format": FORMAT}
    for name in ("league", "pro_players", "pool", "draft", "activity", "cards", "projection_weeks"):
        doc[name] = getattr(p, name)
    for name in _INT_KEYED:
        doc[name] = {str(k): v for k, v in getattr(p, name).items()}
    for name in _TIMES:
        value = getattr(p, name)
        doc[name] = value.isoformat() if value is not None else None
    return gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), mtime=0)


def load_payloads(blob: bytes) -> EspnPayloads | SleeperPayloads:
    doc = json.loads(gzip.decompress(blob))
    if doc.get("format") == SLEEPER_FORMAT:
        return SleeperPayloads.from_json(doc)
    if doc.get("format") != FORMAT:
        raise ValueError(f"unknown raw payload format {doc.get('format')!r}")
    p = EspnPayloads(league=doc["league"])
    for name in ("pro_players", "pool", "draft"):
        setattr(p, name, doc.get(name))
    p.activity, p.cards = doc.get("activity") or [], doc.get("cards") or []
    p.projection_weeks = doc.get("projection_weeks") or []
    for name in _INT_KEYED:
        setattr(p, name, {int(k): v for k, v in (doc.get(name) or {}).items()})
    for name in _TIMES:
        setattr(p, name, datetime.fromisoformat(doc[name]) if doc.get(name) else None)
    return p
