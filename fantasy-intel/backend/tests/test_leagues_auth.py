"""User assertions: signed, short-lived, audience-bound; the header is the only source of user identity."""

import base64
import hashlib
import hmac
import json

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.leagues.auth import (
    HEADER, InvalidUserAssertion, current_principal, sign_user_assertion, verify_user_assertion,
)
from app.leagues.context import Principal

SECRET = "s" * 40
NOW = 1_800_000_000


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def forge(payload: dict, secret: str = SECRET) -> str:
    body = b64(json.dumps(payload).encode())
    sig = b64(hmac.new(secret.encode(), f"v1.{body}".encode(), hashlib.sha256).digest())
    return f"v1.{body}.{sig}"


def good(**over) -> dict:
    return {"sub": "alice", "aud": "fantasy-intel-api", "iat": NOW, "exp": NOW + 300, **over}


def test_round_trip():
    token = sign_user_assertion("alice", SECRET, NOW)
    assert verify_user_assertion(token, SECRET, NOW + 10) == Principal("alice")
    assert token.count(".") == 2 and "=" not in token


def test_documented_format_is_what_a_foreign_signer_would_produce():
    assert verify_user_assertion(forge(good()), SECRET, NOW) == Principal("alice")


@pytest.mark.parametrize("sub", ["google|1234", "auth0:abc.def@example.com", "u_1-2"])
def test_user_ids_with_provider_punctuation(sub):
    assert verify_user_assertion(sign_user_assertion(sub, SECRET, NOW), SECRET, NOW).user_id == sub


def test_wrong_secret_and_tampering_are_rejected():
    token = sign_user_assertion("alice", SECRET, NOW)
    with pytest.raises(InvalidUserAssertion):
        verify_user_assertion(token, "t" * 40, NOW)
    version, body, sig = token.split(".")
    other = b64(json.dumps(good(sub="mallory")).encode())
    for bad in (f"{version}.{other}.{sig}",                      # payload swapped, old signature
                f"{version}.{body}.{sig[:-2]}AA",                # signature mangled
                f"{version}.{body}.{sig}x",                      # signature extended
                f"v2.{body}.{sig}",                              # unknown version
                f"{version}.{body}",                             # missing segment
                f"{version}.{body}.{sig}.extra",                 # extra segment
                "", "garbage", "...", "v1..", f"{version}.{body}.{sig}\n"):
        with pytest.raises(InvalidUserAssertion):
            verify_user_assertion(bad, SECRET, NOW)


def test_signature_over_the_version_prefix_is_required():
    body = b64(json.dumps(good()).encode())
    sig_without_prefix = b64(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest())
    with pytest.raises(InvalidUserAssertion):
        verify_user_assertion(f"v1.{body}.{sig_without_prefix}", SECRET, NOW)


def test_padded_or_non_ascii_encodings_are_rejected():
    token = sign_user_assertion("alice", SECRET, NOW)
    with pytest.raises(InvalidUserAssertion):
        verify_user_assertion(token + "=", SECRET, NOW)
    with pytest.raises(InvalidUserAssertion):
        verify_user_assertion(token.replace("v1.", "v1.é", 1), SECRET, NOW)


def test_expiry_and_clock_skew():
    token = sign_user_assertion("alice", SECRET, NOW, ttl=300)
    assert verify_user_assertion(token, SECRET, NOW + 300 + 60)           # inside skew
    with pytest.raises(InvalidUserAssertion):
        verify_user_assertion(token, SECRET, NOW + 300 + 61)              # expired
    assert verify_user_assertion(token, SECRET, NOW - 60)                 # issued slightly in our future
    with pytest.raises(InvalidUserAssertion):
        verify_user_assertion(token, SECRET, NOW - 61)


@pytest.mark.parametrize("payload", [
    good(aud="someone-else"), good(aud=None), {k: v for k, v in good().items() if k != "aud"},
    good(exp=NOW + 601), good(exp=NOW), good(exp=NOW - 5),                # lifetime too long / empty / negative
    good(sub=None), good(sub=""), good(sub=123), good(sub="has space"), good(sub="x" * 200),
    good(iat="1"), good(exp=True), good(iat=1.5),
    {"sub": "alice"},
])
def test_bad_claims_are_rejected_even_with_a_valid_signature(payload):
    with pytest.raises(InvalidUserAssertion):
        verify_user_assertion(forge(payload), SECRET, NOW)


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'"str"', b"null"])
def test_non_object_payloads_are_rejected(raw):
    body = b64(raw)
    sig = b64(hmac.new(SECRET.encode(), f"v1.{body}".encode(), hashlib.sha256).digest())
    with pytest.raises(InvalidUserAssertion):
        verify_user_assertion(f"v1.{body}.{sig}", SECRET, NOW)


@pytest.mark.parametrize("secret", [None, "", "short", "x" * 31])
def test_no_usable_secret_means_nothing_verifies_or_signs(secret):
    token = sign_user_assertion("alice", SECRET, NOW)
    with pytest.raises(InvalidUserAssertion):
        verify_user_assertion(token, secret, NOW)
    with pytest.raises(InvalidUserAssertion):
        sign_user_assertion("alice", secret, NOW)


def test_sign_rejects_bad_ttl_and_user():
    for ttl in (0, -1, 601):
        with pytest.raises(ValueError):
            sign_user_assertion("alice", SECRET, NOW, ttl=ttl)
    with pytest.raises(ValueError):
        sign_user_assertion("bad user", SECRET, NOW)


def test_error_never_echoes_the_header():
    token = sign_user_assertion("alice", SECRET, NOW)
    with pytest.raises(InvalidUserAssertion) as exc:
        verify_user_assertion(token, "t" * 40, NOW)
    assert token not in str(exc.value) and SECRET not in str(exc.value)


# ---- the FastAPI dependency --------------------------------------------------------------------

@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/who")
    def who(p: Principal = Depends(current_principal)):
        return {"user_id": p.user_id}

    return TestClient(app)


def test_absent_header_is_anonymous(client, monkeypatch):
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", SECRET)
    assert client.get("/who").json() == {"user_id": None}
    monkeypatch.delenv("FANTASY_USER_ASSERTION_SECRET")
    assert client.get("/who").json() == {"user_id": None}


def test_valid_header_names_the_user(client, monkeypatch):
    import time
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", SECRET)
    token = sign_user_assertion("alice", SECRET, time.time())
    assert client.get("/who", headers={HEADER: token}).json() == {"user_id": "alice"}


def test_invalid_header_is_401_not_anonymous(client, monkeypatch):
    import time
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", SECRET)
    stale = sign_user_assertion("alice", SECRET, time.time() - 1000)
    for value in ("garbage", stale, "", sign_user_assertion("alice", "t" * 40, time.time())):
        r = client.get("/who", headers={HEADER: value})
        assert r.status_code == 401, value
        assert value not in r.text or value == ""


def test_header_without_a_configured_secret_is_401(client, monkeypatch):
    import time
    token = sign_user_assertion("alice", SECRET, time.time())
    monkeypatch.delenv("FANTASY_USER_ASSERTION_SECRET", raising=False)
    assert client.get("/who", headers={HEADER: token}).status_code == 401
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", "too-short")      # < 32 chars counts as unset
    assert get_settings().user_assertion_secret is None
    assert client.get("/who", headers={HEADER: token}).status_code == 401


def test_the_service_api_key_is_never_a_user(client, monkeypatch):
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("FANTASY_API_KEY", "k" * 40)
    assert client.get("/who", headers={"X-API-Key": "k" * 40}).json() == {"user_id": None}


# ---- settings parsing --------------------------------------------------------------------------

def test_settings_defaults_are_off(monkeypatch):
    for name in ("MULTILEAGUE_ENABLED", "FANTASY_TENANCY_DB_PATH", "FANTASY_USER_ASSERTION_SECRET",
                 "FANTASY_OWNER_USER_ID", "SLEEPER_SOURCE", "ESPN_PUBLIC_IMPORT_ENABLED", "YAHOO_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    s = get_settings()
    assert (s.multileague_enabled, s.user_assertion_secret, s.owner_user_id) == (False, None, None)
    assert (s.sleeper_source, s.espn_public_import_enabled, s.yahoo_client_id) == ("disabled", False, None)
    assert s.tenancy_db_path.is_absolute() and s.tenancy_db_path.name == "tenancy.duckdb"


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("YES", True), (" True ", True),
                                          ("0", False), ("false", False), ("", False), ("on", False)])
def test_boolean_flags(monkeypatch, raw, expected):
    monkeypatch.setenv("MULTILEAGUE_ENABLED", raw)
    monkeypatch.setenv("ESPN_PUBLIC_IMPORT_ENABLED", raw)
    s = get_settings()
    assert s.multileague_enabled is expected and s.espn_public_import_enabled is expected


def test_settings_values_and_relative_tenancy_path(monkeypatch):
    from app.config import REPO_ROOT
    monkeypatch.setenv("FANTASY_TENANCY_DB_PATH", "data/other.duckdb")
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", " " + SECRET + " ")
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "alice")
    monkeypatch.setenv("SLEEPER_SOURCE", "FIXTURE")
    monkeypatch.setenv("YAHOO_CLIENT_ID", "cid")
    s = get_settings()
    assert s.tenancy_db_path == REPO_ROOT / "data/other.duckdb"
    assert s.user_assertion_secret == SECRET and s.owner_user_id == "alice"
    assert s.sleeper_source == "fixture" and s.yahoo_client_id == "cid"
