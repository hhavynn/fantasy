"""Tenancy store: connections are the only grant of access, and every read re-checks them."""

import threading
from datetime import datetime, timedelta

import duckdb
import pytest

from app.config import get_settings
from app.db.connection import utcnow
from app.leagues.context import (
    AccessMethod, LeagueAccessDenied, LeagueKey, Principal, Provider, SyncStatus, issue_context,
)
from app.leagues.models import Provenance
from app.leagues.store import TenancyStore, sanitize_error

ALICE, BOB = Principal("alice"), Principal("bob")
SLEEPER = LeagueKey(Provider.SLEEPER, "12345", 2026)
ESPN = LeagueKey(Provider.ESPN, "12345", 2026)


@pytest.fixture
def store(tmp_path):
    return TenancyStore(tmp_path / "tenancy.duckdb", capabilities=lambda: None)


def connect(store, who=ALICE, key=SLEEPER, **kw):
    kw = {"name": "My League", "is_public": True, "data_origin": "fixture", **kw}
    return store.connect(who, key, kw.pop("access", AccessMethod.PUBLIC), **kw)


def prov(provider=Provider.SLEEPER, source="fixture:x"):
    return Provenance(provider, source, utcnow())


def rows(store, sql, params=()):
    con = duckdb.connect(str(store.path), read_only=True)
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


# ---- connect rules -----------------------------------------------------------------------------

def test_anonymous_cannot_connect_or_list(store):
    with pytest.raises(LeagueAccessDenied):
        connect(store, Principal.anonymous())
    assert store.list_contexts(Principal.anonymous()) == []


def test_connect_issues_context_and_reuses_registry_row(store):
    a = connect(store, ALICE)
    b = connect(store, BOB)
    assert a.league_id == b.league_id and a.principal != b.principal
    assert a.league_id.startswith("lg_") and "12345" not in a.league_id
    assert a.freshness.sync_status is SyncStatus.NEVER and a.role.value == "owner"
    assert rows(store, "SELECT count(*) FROM league_registry")[0][0] == 1
    assert rows(store, "SELECT count(*) FROM league_connections")[0][0] == 2


def test_same_external_id_on_two_providers_is_two_leagues(store):
    s = connect(store, ALICE, key=SLEEPER)
    e = connect(store, ALICE, key=ESPN)
    assert s.league_id != e.league_id
    assert rows(store, "SELECT count(*) FROM league_registry")[0][0] == 2
    assert {c.provider for c in store.list_contexts(ALICE)} == {Provider.SLEEPER, Provider.ESPN}


def test_same_provider_different_season_is_a_different_league(store):
    a = connect(store, key=LeagueKey(Provider.SLEEPER, "12345", 2025))
    b = connect(store, key=LeagueKey(Provider.SLEEPER, "12345", 2026))
    assert a.league_id != b.league_id


@pytest.mark.parametrize("is_public", [False, None])
def test_public_access_to_a_non_public_league_is_refused(store, is_public):
    with pytest.raises(PermissionError):
        connect(store, is_public=is_public)
    assert store.list_contexts(ALICE) == []


def test_owner_env_is_only_for_the_owner_on_espn(store, monkeypatch):
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "alice")
    with pytest.raises(PermissionError):
        connect(store, BOB, key=ESPN, is_public=False, access=AccessMethod.OWNER_ENV)       # not the owner
    with pytest.raises(PermissionError):
        connect(store, ALICE, key=SLEEPER, is_public=False, access=AccessMethod.OWNER_ENV)  # not ESPN
    ctx = connect(store, ALICE, key=ESPN, is_public=False, access=AccessMethod.OWNER_ENV)
    assert ctx.access is AccessMethod.OWNER_ENV


def test_owner_env_refused_when_no_owner_is_configured(store, monkeypatch):
    monkeypatch.delenv("FANTASY_OWNER_USER_ID", raising=False)
    with pytest.raises(PermissionError):
        connect(store, ALICE, key=ESPN, is_public=False, access=AccessMethod.OWNER_ENV)


def test_oauth_needs_a_credential_ref_that_belongs_to_the_connector(store):
    with pytest.raises(PermissionError):
        connect(store, key=LeagueKey(Provider.YAHOO, "449.l.1", 2026), is_public=False, access=AccessMethod.OAUTH)
    yahoo = LeagueKey(Provider.YAHOO, "449.l.1", 2026)
    ctx = connect(store, ALICE, key=yahoo, is_public=False, access=AccessMethod.OAUTH, credential_ref="ref_alice_0001")
    assert ctx.access is AccessMethod.OAUTH
    assert rows(store, "SELECT user_id, league_id FROM credential_refs") == [("alice", ctx.league_id)]
    with pytest.raises(PermissionError):    # someone else cannot claim alice's ref
        connect(store, BOB, key=yahoo, is_public=False, access=AccessMethod.OAUTH, credential_ref="ref_alice_0001")
    assert store.list_contexts(BOB) == []


def test_concurrent_connects_create_one_registry_row(store):
    errors = []

    def go(i):
        try:
            connect(store, Principal(f"user{i}"))
        except Exception as err:   # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert rows(store, "SELECT count(*) FROM league_registry")[0][0] == 1
    assert rows(store, "SELECT count(*) FROM league_connections")[0][0] == 8


# ---- authorize / list --------------------------------------------------------------------------

def test_authorize_requires_this_principals_active_connection(store):
    ctx = connect(store, ALICE)
    assert store.authorize(ALICE, ctx.league_id).league_id == ctx.league_id
    for who in (BOB, Principal.anonymous()):
        with pytest.raises(LeagueAccessDenied):
            store.authorize(who, ctx.league_id)


@pytest.mark.parametrize("bad", ["", "lg_short", "12345", "../etc/passwd", "lg_" + "x" * 80, None, 12345])
def test_authorize_rejects_malformed_ids(store, bad):
    connect(store, ALICE)
    with pytest.raises(LeagueAccessDenied):
        store.authorize(ALICE, bad)


def test_authorize_unknown_but_well_formed_id(store):
    with pytest.raises(LeagueAccessDenied):
        store.authorize(ALICE, "lg_" + "a" * 24)


def test_list_contexts_only_returns_the_principals_active_connections(store):
    a = connect(store, ALICE, key=SLEEPER)
    connect(store, BOB, key=ESPN)
    assert [c.league_id for c in store.list_contexts(ALICE)] == [a.league_id]
    store.revoke(a)
    assert store.list_contexts(ALICE) == []
    assert len(store.list_contexts(BOB)) == 1


def test_capabilities_come_from_the_injected_source_and_default_to_none(tmp_path):
    from app.leagues.capabilities import ProviderCapabilities
    caps = ProviderCapabilities(Provider.SLEEPER, enabled=False, reason="off")
    with_caps = TenancyStore(tmp_path / "a.duckdb", capabilities=lambda: {Provider.SLEEPER: caps})
    assert connect(with_caps).capabilities is caps
    broken = TenancyStore(tmp_path / "b.duckdb", capabilities=lambda: None)
    assert connect(broken).capabilities is None


def test_reconnect_reactivates_a_revoked_connection(store):
    ctx = connect(store, ALICE)
    store.save_snapshot(ctx, "teams", [{"external_team_id": "1"}], prov())
    store.set_my_team(ctx, "1")
    store.revoke(ctx)
    again = connect(store, ALICE)
    assert again.league_id == ctx.league_id
    assert again.my_team_external_id is None          # a fresh start
    assert len(store.list_contexts(ALICE)) == 1
    assert rows(store, "SELECT count(*) FROM league_connections")[0][0] == 1


# ---- my team -----------------------------------------------------------------------------------

def test_set_my_team_must_be_in_the_stored_teams_snapshot(store):
    ctx = connect(store)
    with pytest.raises(ValueError):                    # no snapshot yet
        store.set_my_team(ctx, "1")
    store.save_snapshot(ctx, "teams", [{"external_team_id": "1"}, {"external_team_id": "2"}], prov())
    assert store.set_my_team(ctx, "2").my_team_external_id == "2"
    assert store.authorize(ALICE, ctx.league_id).my_team_external_id == "2"
    with pytest.raises(ValueError):
        store.set_my_team(ctx, "3")
    with pytest.raises(ValueError):
        store.set_my_team(ctx, "1; DROP TABLE league_connections")


def test_my_team_is_per_user(store):
    a, b = connect(store, ALICE), connect(store, BOB)
    store.save_snapshot(a, "teams", [{"external_team_id": "1"}, {"external_team_id": "2"}], prov())
    store.set_my_team(a, "1")
    store.set_my_team(b, "2")
    assert store.authorize(ALICE, a.league_id).my_team_external_id == "1"
    assert store.authorize(BOB, a.league_id).my_team_external_id == "2"


# ---- revocation --------------------------------------------------------------------------------

def test_revoked_context_stops_working_everywhere(store):
    ctx = connect(store, ALICE)
    store.save_snapshot(ctx, "settings", {"a": 1}, prov())
    store.revoke(ctx)
    with pytest.raises(LeagueAccessDenied):
        store.authorize(ALICE, ctx.league_id)
    with pytest.raises(LeagueAccessDenied):
        store.load_snapshot(ctx, "settings")
    with pytest.raises(LeagueAccessDenied):
        store.save_snapshot(ctx, "settings", {"a": 2}, prov())
    with pytest.raises(LeagueAccessDenied):
        store.set_my_team(ctx, "1")
    with pytest.raises(LeagueAccessDenied):
        store.record_sync_success(ctx, fetched_at=utcnow())
    with pytest.raises(LeagueAccessDenied):
        store.league_meta(ctx)


def test_revoke_marks_connection_and_credential_refs_but_not_other_users(store):
    yahoo = LeagueKey(Provider.YAHOO, "449.l.1", 2026)
    a = connect(store, ALICE, key=yahoo, is_public=False, access=AccessMethod.OAUTH, credential_ref="ref_alice_0001")
    b = connect(store, BOB, key=yahoo, is_public=False, access=AccessMethod.OAUTH, credential_ref="ref_bob_000001")
    store.revoke(a)
    assert rows(store, "SELECT user_id, revoked_at IS NOT NULL FROM league_connections ORDER BY user_id") == [
        ("alice", True), ("bob", False)]
    assert rows(store, "SELECT user_id, revoked_at IS NOT NULL FROM credential_refs ORDER BY user_id") == [
        ("alice", True), ("bob", False)]
    assert store.authorize(BOB, b.league_id).league_id == b.league_id
    store.revoke(a)                                    # idempotent


def test_last_revoke_drops_snapshots_but_not_while_someone_else_is_connected(store):
    a, b = connect(store, ALICE), connect(store, BOB)
    store.save_snapshot(a, "settings", {"x": 1}, prov())
    store.revoke(a)
    assert store.load_snapshot(b, "settings") is not None
    store.revoke(b)
    assert rows(store, "SELECT count(*) FROM league_snapshots")[0][0] == 0
    assert rows(store, "SELECT count(*) FROM league_sync_status")[0][0] == 0


# ---- snapshots ---------------------------------------------------------------------------------

def test_snapshot_round_trip_and_replace(store):
    ctx = connect(store)
    p = Provenance(Provider.SLEEPER, "fixture:s", datetime(2026, 9, 1, 12), datetime(2026, 9, 1, 11))
    store.save_snapshot(ctx, "settings", {"name": "x", "nested": [1, 2]}, p)
    payload, back = store.load_snapshot(ctx, "settings")
    assert payload == {"name": "x", "nested": [1, 2]} and back == p
    store.save_snapshot(ctx, "settings", {"name": "y"}, p)
    assert store.load_snapshot(ctx, "settings")[0] == {"name": "y"}
    assert store.load_snapshot(ctx, "rosters") is None


def test_snapshot_validation(store):
    ctx = connect(store)
    with pytest.raises(ValueError):
        store.save_snapshot(ctx, "players", {}, prov())
    with pytest.raises(TypeError):
        store.save_snapshot(ctx, "settings", "not json container", prov())
    with pytest.raises(ValueError):                       # espn provenance on a sleeper league
        store.save_snapshot(ctx, "settings", {}, prov(Provider.ESPN))
    with pytest.raises(ValueError):
        store.load_snapshot(ctx, "bogus")


def test_snapshot_reads_filter_by_league(store):
    a = connect(store, ALICE, key=SLEEPER)
    b = connect(store, ALICE, key=ESPN)
    store.save_snapshot(a, "teams", [{"external_team_id": "1", "name": "sleeper team"}], prov())
    store.save_snapshot(b, "teams", [{"external_team_id": "1", "name": "espn team"}], prov(Provider.ESPN, "espn:x"))
    assert store.load_snapshot(a, "teams")[0][0]["name"] == "sleeper team"
    assert store.load_snapshot(b, "teams")[0][0]["name"] == "espn team"


def test_a_context_cannot_read_a_league_it_is_not_connected_to(store):
    a = connect(store, ALICE, key=SLEEPER)
    b = connect(store, BOB, key=ESPN)
    store.save_snapshot(a, "settings", {"secret": "alice"}, prov())
    # Bob's own context, pointed at alice's league id by a buggy caller, cannot see it.
    import dataclasses
    fields = {f.name: getattr(b, f.name) for f in dataclasses.fields(b) if f.name != "_issuer"}
    forged = issue_context(**{**fields, "league_id": a.league_id})   # minted without authorization
    with pytest.raises(LeagueAccessDenied):
        store.load_snapshot(forged, "settings")


def test_nothing_is_built_without_the_store():
    from app.leagues.context import LeagueContext
    with pytest.raises(RuntimeError):
        LeagueContext("lg_" + "a" * 24, SLEEPER, ALICE, None, None, None, None, None)   # type: ignore[arg-type]


# ---- sync status -------------------------------------------------------------------------------

def test_sync_success_then_failure_keeps_last_good_times(store):
    ctx = connect(store)
    t1, t2 = datetime(2026, 9, 1, 12), datetime(2026, 9, 1, 10)
    store.record_sync_success(ctx, fetched_at=t1, source_updated_at=t2)
    fresh = store.authorize(ALICE, ctx.league_id).freshness
    assert (fresh.sync_status, fresh.last_synced_at, fresh.source_updated_at) == (SyncStatus.OK, t1, t2)
    store.record_sync_failure(ctx.league_id, ConnectionError("timeout"))
    fresh = store.authorize(ALICE, ctx.league_id).freshness
    assert fresh.sync_status is SyncStatus.FAILED and fresh.last_synced_at == t1
    assert fresh.last_error == "ConnectionError: timeout"
    store.record_sync_success(ctx, fetched_at=t1 + timedelta(hours=1))
    assert store.authorize(ALICE, ctx.league_id).freshness.last_error is None


def test_aware_datetimes_are_stored_as_naive_utc(store):
    from datetime import timezone
    ctx = connect(store)
    store.record_sync_success(ctx, fetched_at=datetime(2026, 9, 1, 14, tzinfo=timezone(timedelta(hours=2))))
    assert store.authorize(ALICE, ctx.league_id).freshness.last_synced_at == datetime(2026, 9, 1, 12)


def test_failure_in_one_league_leaves_the_other_untouched(store):
    a = connect(store, ALICE, key=SLEEPER)
    b = connect(store, ALICE, key=ESPN)
    store.record_sync_success(b, fetched_at=datetime(2026, 9, 1))
    assert store.record_sync_failure(a.league_id, "boom") is True
    assert store.authorize(ALICE, a.league_id).freshness.sync_status is SyncStatus.FAILED
    other = store.authorize(ALICE, b.league_id).freshness
    assert other.sync_status is SyncStatus.OK and other.last_error is None


def test_failure_for_an_unknown_league_creates_nothing(store):
    assert store.record_sync_failure("lg_" + "z" * 24, "boom") is False
    assert rows(store, "SELECT count(*) FROM league_sync_status")[0][0] == 0


SECRETS = [
    "AEBkYWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXowMTIzNDU2Nzg5QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo",
    "{B1A2C3D4-0000-4321-8765-0123456789AB}",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlc2lnbmF0dXJlMTIzNDU2",
    "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "sk-live-REDACTME1234567890",
]


@pytest.mark.parametrize("message", [
    f"request failed espn_s2={SECRETS[0]} SWID={SECRETS[1]}",
    f"401 for Cookie: espn_s2={SECRETS[0]}; SWID={SECRETS[1]}; other=zzz",
    f"Authorization: Bearer {SECRETS[2]}",
    f'bad json {{"espn_s2": "{SECRETS[0]}", "swid": "{SECRETS[1]}"}}',
    f"hash {SECRETS[3]} mismatch",
    f"token={SECRETS[4]}&x=1",
    f"blob {SECRETS[0]} trailing",
])
def test_sanitized_errors_contain_no_secrets(message):
    out = sanitize_error(RuntimeError(message + "\nsecond line with " + SECRETS[2]))
    for secret in SECRETS:
        assert secret not in out
    assert out.startswith("RuntimeError: ") and "\n" not in out and len(out) <= 240
    assert "second line" not in out


def test_sanitized_errors_are_capped_and_keep_useful_text():
    assert len(sanitize_error("x " * 1000)) <= 240
    assert sanitize_error(ValueError("league 12345 not found")) == "ValueError: league 12345 not found"
    assert sanitize_error(KeyError()).startswith("KeyError")


def test_stored_failure_message_is_sanitized(store):
    ctx = connect(store)
    store.record_sync_failure(ctx, RuntimeError(f"espn_s2={SECRETS[0]} failed"))
    stored = rows(store, "SELECT last_error FROM league_sync_status")[0][0]
    assert SECRETS[0] not in stored and "redacted" in stored


# ---- isolation from the analytics database -----------------------------------------------------

def test_tenancy_db_is_a_separate_file_and_is_never_published():
    import inspect
    from app.db import publish
    s = get_settings()
    assert s.tenancy_db_path != s.db_path and s.tenancy_db_path.name == "tenancy.duckdb"
    assert "tenancy" not in inspect.getsource(publish).lower()


def test_store_creates_only_tenancy_tables(store):
    connect(store)
    names = {r[0] for r in rows(store, "SELECT table_name FROM information_schema.tables")}
    assert names == {"app_users", "league_registry", "league_connections", "league_sync_status",
                     "league_snapshots", "credential_refs"}


# ---- credentials -------------------------------------------------------------------------------

def test_disabled_vault_refuses_to_store_or_return_anything(store):
    from app.leagues.credentials import CredentialStorageUnavailable, DisabledVault, Secret
    ctx = connect(store)
    vault = DisabledVault()
    with pytest.raises(CredentialStorageUnavailable, match="No credential encryption key / KMS is configured"):
        vault.put(ctx, "ref_alice_0001", Secret("hunter2"))
    with pytest.raises(CredentialStorageUnavailable):
        vault.get(ctx, "ref_alice_0001")
    vault.revoke(ctx, "ref_alice_0001")     # nothing stored, nothing to do


def test_owner_env_credentials_only_for_the_owner_on_owner_env(store, monkeypatch):
    from app.leagues.credentials import CredentialAccessDenied, CredentialStorageUnavailable, OwnerEnvCredentials
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "alice")
    monkeypatch.setenv("ESPN_S2", "AEBsecretvalue123456")
    monkeypatch.setenv("SWID", "{SECRET-SWID}")
    creds = OwnerEnvCredentials()
    owner_ctx = connect(store, ALICE, key=ESPN, is_public=False, access=AccessMethod.OWNER_ENV)
    cookies = creds.for_context(owner_ctx)
    assert (cookies.espn_s2, cookies.swid) == ("AEBsecretvalue123456", "{SECRET-SWID}")
    for text in (repr(cookies), str(cookies), f"{cookies}", f"{[cookies]}", repr({"c": cookies})):
        assert "AEBsecret" not in text and "SECRET-SWID" not in text
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(cookies)

    public_ctx = connect(store, ALICE, key=SLEEPER)                       # owner, but not an OWNER_ENV connection
    with pytest.raises(CredentialAccessDenied):
        creds.for_context(public_ctx)
    espn_public = connect(store, BOB, key=ESPN, is_public=True)           # OWNER_ENV-looking league, wrong user
    with pytest.raises(CredentialAccessDenied):
        creds.for_context(espn_public)
    with pytest.raises(CredentialAccessDenied):
        creds.for_owner(BOB, Provider.ESPN)
    with pytest.raises(CredentialAccessDenied):
        creds.for_owner(ALICE, Provider.SLEEPER)
    monkeypatch.delenv("ESPN_S2")
    with pytest.raises(CredentialStorageUnavailable):
        creds.for_owner(ALICE, Provider.ESPN)


def test_secret_wrapper_never_shows_its_value():
    from app.leagues.credentials import Secret
    s = Secret("hunter2")
    assert s.reveal() == "hunter2" and bool(s)
    assert "hunter2" not in repr(s) + str(s) + f"{s}" + repr([s])
    with pytest.raises(AttributeError):
        s._value = "x"
