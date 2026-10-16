"""Cache keys name tenant, provider, league and season; no two tenants or leagues can share an entry."""

import hashlib
import threading

import pytest

from app.leagues.cache import SEPARATOR, TenantScopedCache, scoped_cache_key
from app.leagues.context import (
    AccessMethod, Freshness, LeagueKey, Principal, Provider, Role, SyncStatus, issue_context,
)


def make_ctx(user="alice", provider=Provider.SLEEPER, ext="12345", season=2026, league_id=None):
    return issue_context(
        league_id=league_id or "lg_" + hashlib.sha256(f"{user}{ext}{provider.value}".encode()).hexdigest()[:24],
        key=LeagueKey(provider, ext, season), principal=Principal(user), role=Role.OWNER,
        access=AccessMethod.PUBLIC, name=None, my_team_external_id=None, freshness=Freshness(SyncStatus.NEVER))


def key(ctx, ns="start_sit", *parts, dv="v1"):
    return scoped_cache_key(ctx, ns, *parts, data_version=dv)


def test_key_embeds_tenant_provider_league_and_season_but_not_the_raw_user_id():
    ctx = make_ctx("alice")
    k = key(ctx, "ns", "week3")
    assert ctx.league_id in k and "sleeper" in k and "2026" in k and "ns" in k and "week3" in k
    assert "alice" not in k and k.endswith("dv:v1")


def test_keys_differ_per_tenant_league_provider_season_namespace_part_and_version():
    base = make_ctx("alice")
    others = [
        make_ctx("bob"),                                          # other tenant, same league facts
        make_ctx("alice", league_id="lg_" + "q" * 24),            # other internal league
        make_ctx("alice", provider=Provider.ESPN),                # same external id, other provider
        make_ctx("alice", season=2025),
    ]
    keys = {key(base)} | {key(c) for c in others}
    assert len(keys) == 5
    assert len({key(base), key(base, "other_ns"), key(base, "ns", "a"), key(base, "ns", "b"),
                key(base, dv="v2")}) == 5


def test_two_tenants_in_the_same_league_do_not_share_keys():
    a, b = make_ctx("alice", league_id="lg_" + "s" * 24), make_ctx("bob", league_id="lg_" + "s" * 24)
    assert a.league_id == b.league_id and key(a) != key(b)


def test_parts_cannot_forge_another_keys_structure():
    ctx = make_ctx()
    with pytest.raises(ValueError):
        key(ctx, "ns", "a" + SEPARATOR + "b")
    with pytest.raises(ValueError):
        key(ctx, "ns" + SEPARATOR + "x")
    with pytest.raises(ValueError):
        key(ctx, "ns", dv="v1" + SEPARATOR + "u:x")
    with pytest.raises(ValueError):
        key(ctx, "")
    with pytest.raises(ValueError):
        key(ctx, "ns", dv="")
    # Different splits of the same text are different keys.
    assert key(ctx, "ns", "a", "bc") != key(ctx, "ns", "ab", "c")


def test_key_requires_a_context():
    with pytest.raises(TypeError):
        scoped_cache_key("alice", "ns", data_version="v1")          # type: ignore[arg-type]


def test_cache_hit_miss_and_isolation():
    cache = TenantScopedCache()
    a, b = make_ctx("alice"), make_ctx("bob")
    cache.put(a, "ns", "p", data_version="v1", value={"mine": 1})
    assert cache.get(a, "ns", "p", data_version="v1") == {"mine": 1}
    assert cache.get(b, "ns", "p", data_version="v1") is None
    assert cache.get(a, "ns", "p", data_version="v2") is None
    assert cache.get(a, "ns", "q", data_version="v1", default="miss") == "miss"


def test_cache_requires_a_context_for_get_and_put():
    cache = TenantScopedCache()
    with pytest.raises(TypeError):
        cache.get("some-key", "ns", data_version="v1")               # type: ignore[arg-type]
    with pytest.raises(TypeError):
        cache.put("some-key", "ns", data_version="v1", value=1)      # type: ignore[arg-type]


def test_lru_is_bounded_and_recency_aware():
    cache = TenantScopedCache(max_entries=2)
    ctx = make_ctx()
    cache.put(ctx, "ns", "1", data_version="v", value=1)
    cache.put(ctx, "ns", "2", data_version="v", value=2)
    assert cache.get(ctx, "ns", "1", data_version="v") == 1          # touch 1, so 2 is the oldest
    cache.put(ctx, "ns", "3", data_version="v", value=3)
    assert len(cache) == 2
    assert cache.get(ctx, "ns", "2", data_version="v") is None
    assert cache.get(ctx, "ns", "1", data_version="v") == 1 and cache.get(ctx, "ns", "3", data_version="v") == 3
    with pytest.raises(ValueError):
        TenantScopedCache(0)


def test_invalidate_league_and_tenant():
    cache = TenantScopedCache()
    a1 = make_ctx("alice", league_id="lg_" + "1" * 24)
    a2 = make_ctx("alice", league_id="lg_" + "2" * 24)
    b1 = make_ctx("bob", league_id="lg_" + "1" * 24)
    for c in (a1, a2, b1):
        cache.put(c, "ns", data_version="v", value=c.principal.user_id)
    assert cache.invalidate_league("lg_" + "1" * 24) == 2             # every tenant's view of that league
    assert cache.get(a2, "ns", data_version="v") == "alice" and len(cache) == 1
    cache.put(b1, "ns", data_version="v", value="bob")
    assert cache.invalidate_tenant("alice") == 1
    assert cache.get(a2, "ns", data_version="v") is None and cache.get(b1, "ns", data_version="v") == "bob"
    assert cache.invalidate_tenant("nobody") == 0 and cache.invalidate_league("lg_" + "9" * 24) == 0


def test_get_or_compute_computes_once_per_scope():
    cache, calls = TenantScopedCache(), []
    a, b = make_ctx("alice"), make_ctx("bob")
    for ctx in (a, a, b):
        cache.get_or_compute(ctx, "ns", data_version="v", compute=lambda c=ctx: calls.append(c) or c.principal.user_id)
    assert len(calls) == 2
    # a falsy cached value is still a hit
    cache.put(a, "z", data_version="v", value=0)
    assert cache.get_or_compute(a, "z", data_version="v", compute=lambda: 1 / 0) == 0


def test_thread_safety_under_contention():
    cache = TenantScopedCache(max_entries=50)
    ctxs = [make_ctx(f"user{i}") for i in range(4)]
    errors = []

    def work(i):
        try:
            for n in range(300):
                c = ctxs[(i + n) % 4]
                cache.put(c, "ns", str(n % 80), data_version="v", value=(c.principal.user_id, n))
                hit = cache.get(c, "ns", str(n % 80), data_version="v")
                assert hit is None or hit[0] == c.principal.user_id
                if n % 97 == 0:
                    cache.invalidate_tenant(c.principal.user_id)
        except Exception as err:   # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(cache) <= 50
