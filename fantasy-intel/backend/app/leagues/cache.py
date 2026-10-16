"""Tenant- and league-scoped caching.

A cache key for personalized data must name who it is for and which league it came from, or a
process-wide cache would hand one tenant's answer to another (the same team id "1" exists in every
league). `scoped_cache_key` therefore always embeds a hash of the tenant id, the provider, the opaque
internal league id and the season, and only accepts parts that cannot forge another key's structure.
The raw user id is hashed so keys can be logged without carrying identifiers.

The cache never calls the store, so it cannot know a context was revoked: callers obtain the context
from `TenancyStore.authorize` on each request and only then consult the cache.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Any, Callable

from .context import LeagueContext

SEPARATOR = "|"
_KEY_VERSION = "v1"
_MISSING = object()


def tenant_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def _check_part(label: str, value: object) -> str:
    text = str(value)
    if not text:
        raise ValueError(f"cache key {label} cannot be empty")
    if SEPARATOR in text:
        raise ValueError(f"cache key {label} cannot contain {SEPARATOR!r}")
    return text


def scoped_cache_key(ctx: LeagueContext, namespace: str, *parts: object, data_version: str) -> str:
    """`v1|u:<tenant hash>|<provider>|<league id>|<season>|<namespace>|<parts...>|dv:<data version>`.

    Every component is free of the separator and the fixed prefix has a fixed length, so the key is
    unambiguous: different (tenant, league, namespace, parts, version) tuples cannot collide."""
    if not isinstance(ctx, LeagueContext):
        raise TypeError("cache keys require a LeagueContext")
    fields = [
        _KEY_VERSION,
        "u:" + tenant_hash(ctx.tenant_id),
        ctx.provider.value,
        ctx.league_id,
        str(ctx.season),
        _check_part("namespace", namespace),
        *(_check_part("part", p) for p in parts),
        "dv:" + _check_part("data_version", data_version),
    ]
    return SEPARATOR.join(fields)


class TenantScopedCache:
    """Bounded, thread-safe LRU. Reads and writes need a LeagueContext, so a bare string key (which
    could come from a request) cannot reach another tenant's entry."""

    def __init__(self, max_entries: int = 256):
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.max_entries = max_entries
        self._lock = threading.Lock()
        # key -> (value, league_id, tenant hash)
        self._data: OrderedDict[str, tuple[Any, str, str]] = OrderedDict()

    def get(self, ctx: LeagueContext, namespace: str, *parts: object, data_version: str, default: Any = None) -> Any:
        key = scoped_cache_key(ctx, namespace, *parts, data_version=data_version)
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            self._data.move_to_end(key)
            return entry[0]

    def put(self, ctx: LeagueContext, namespace: str, *parts: object, data_version: str, value: Any) -> None:
        key = scoped_cache_key(ctx, namespace, *parts, data_version=data_version)
        with self._lock:
            self._data[key] = (value, ctx.league_id, tenant_hash(ctx.tenant_id))
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def get_or_compute(self, ctx: LeagueContext, namespace: str, *parts: object, data_version: str,
                       compute: Callable[[], Any]) -> Any:
        """Computed outside the lock: two racing threads may both compute, the last put wins."""
        hit = self.get(ctx, namespace, *parts, data_version=data_version, default=_MISSING)
        if hit is not _MISSING:
            return hit
        value = compute()
        self.put(ctx, namespace, *parts, data_version=data_version, value=value)
        return value

    def invalidate_league(self, league_id: str) -> int:
        with self._lock:
            doomed = [k for k, (_, lid, _) in self._data.items() if lid == league_id]
            for k in doomed:
                del self._data[k]
            return len(doomed)

    def invalidate_tenant(self, user_id: str) -> int:
        h = tenant_hash(user_id)
        with self._lock:
            doomed = [k for k, (_, _, th) in self._data.items() if th == h]
            for k in doomed:
                del self._data[k]
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
