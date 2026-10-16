"""JSON shapes served by /v2: the LeagueView, and normalized settings / teams / rosters snapshots.

Snapshots are stored in exactly these shapes, so reads serialize from storage without touching a provider.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from .context import LeagueContext
from .models import LeagueSettings, Provenance, Roster, Team


def iso(value: datetime | None) -> str | None:
    """Stored timestamps are naive UTC; the trailing Z lets JavaScript parse them as UTC, not local time."""
    return None if value is None else value.isoformat() + "Z"


def to_jsonable(obj: Any) -> Any:
    """Recursive plain-JSON form of the provider-neutral models (dataclasses, enums, sets, datetimes)."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return iso(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k.value if isinstance(k, Enum) else k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted((to_jsonable(v) for v in obj), key=str)
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    raise TypeError(f"cannot serialize {type(obj).__name__}")


def settings_to_json(settings: LeagueSettings) -> dict:
    out = to_jsonable(settings)
    out["lineup"].update(flex_slots=settings.lineup.flex_slots, superflex_slots=settings.lineup.superflex_slots,
                         is_superflex=settings.lineup.is_superflex)
    out["scoring"]["points_per_reception"] = settings.scoring.points_per_reception   # None = the rules do not say
    return out


def teams_to_json(teams: list[Team]) -> list[dict]:
    return [{**to_jsonable(t), "scoped_id": t.scoped_id} for t in teams]


def rosters_to_json(rosters: list[Roster]) -> list[dict]:
    return [{**to_jsonable(r), "player_ids": r.player_ids()} for r in rosters]


def provenance_to_json(p: Provenance) -> dict:
    return {"provider": p.provider.value, "source": p.source, "fetched_at": iso(p.fetched_at),
            "source_updated_at": iso(p.source_updated_at)}


def league_view(ctx: LeagueContext, *, data_origin: str) -> dict:
    """The LeagueView. `capabilities` is null (and `missing_capabilities` empty) only when the provider
    registry could not supply them; the frontend must treat null as "unknown", not "all available"."""
    caps = ctx.capabilities
    fr = ctx.freshness
    return {
        "league_id": ctx.league_id,
        "provider": ctx.provider.value,
        "external_league_id": ctx.key.external_league_id,
        "season": ctx.season,
        "name": ctx.name,
        "role": ctx.role.value,
        "access": ctx.access.value,
        "my_team_external_id": ctx.my_team_external_id,
        "sync": {
            "status": fr.sync_status.value,
            "last_synced_at": iso(fr.last_synced_at),
            "source_updated_at": iso(fr.source_updated_at),
            "last_error": fr.last_error,
        },
        "data_origin": data_origin,
        "missing_capabilities": [c.value for c in caps.missing()] if caps else [],
        "capabilities": caps.as_dict() if caps else None,
    }
