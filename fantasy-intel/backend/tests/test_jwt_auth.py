"""Supabase access tokens are verified here, not trusted from the frontend: signature against the project JWKS,
issuer, audience, expiry. Absent header = anonymous; present-but-bad = 401, never anonymous, never logged."""
import logging
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.control_plane.jwt_auth import InvalidToken, current_principal, verify_supabase_jwt
from app.leagues.context import Principal

URL = "https://example-project.supabase.co"
ISS = URL + "/auth/v1"
SUB = str(uuid.uuid4())


def jwk_for(kid, key):
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return {**jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True), "kid": kid, "alg": "ES256", "use": "sig"}
    return {**jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), "kid": kid, "alg": "RS256", "use": "sig"}


class FakeJwks:
    """A PyJWKClient whose fetch is served from memory; records how often the key set was fetched."""
    def __init__(self, keys: dict[str, object]):
        self.keys, self.fetches = keys, 0
        # cooldown_duration=0: production keeps PyJWT's default 30 s cooldown between unknown-kid refreshes (a storm
        # of bad kids must not hammer the JWKS endpoint); the test needs the second lookup to refresh at once.
        self.client = jwt.PyJWKClient(URL + "/auth/v1/.well-known/jwks.json", cache_keys=True, lifespan=3600,
                                      cooldown_duration=0)
        self.client.fetch_data = self._fetch        # type: ignore[method-assign]

    def _fetch(self):
        self.fetches += 1
        return {"keys": [jwk_for(kid, k) for kid, k in self.keys.items()]}


@pytest.fixture(scope="module")
def keys():
    return {"k1": ec.generate_private_key(ec.SECP256R1()), "k2": ec.generate_private_key(ec.SECP256R1()),
            "r1": rsa.generate_private_key(65537, 2048)}


@pytest.fixture
def jwks(keys):
    return FakeJwks({"k1": keys["k1"], "r1": keys["r1"]})


def mint(keys, kid="k1", alg="ES256", **over):
    now = int(time.time())
    claims = {"iss": ISS, "sub": SUB, "aud": "authenticated", "role": "authenticated", "iat": now, "exp": now + 3600, **over}
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, keys[kid], algorithm=alg, headers={"kid": kid})


def test_valid_tokens_verify_for_es256_and_rs256(keys, jwks):
    assert verify_supabase_jwt(mint(keys), jwks=jwks.client, issuer=ISS)["sub"] == SUB
    assert verify_supabase_jwt(mint(keys, kid="r1", alg="RS256"), jwks=jwks.client, issuer=ISS)["sub"] == SUB
    assert jwks.fetches == 1                     # key set cached across calls


@pytest.mark.parametrize("bad", [
    dict(aud="anon"), dict(aud=None), dict(iss="https://other.supabase.co/auth/v1"), dict(exp=int(time.time()) - 100),
    dict(exp=None), dict(sub=None), dict(sub="not-a-uuid"), dict(nbf=int(time.time()) + 600),
])
def test_wrong_claims_are_rejected(keys, jwks, bad):
    with pytest.raises(InvalidToken):
        verify_supabase_jwt(mint(keys, **bad), jwks=jwks.client, issuer=ISS)


def test_unknown_kid_refreshes_once_then_rejects(keys, jwks):
    token = mint(keys, kid="k2")
    with pytest.raises(InvalidToken):
        verify_supabase_jwt(token, jwks=jwks.client, issuer=ISS)
    assert jwks.fetches >= 1
    jwks.keys["k2"] = keys["k2"]                 # key rotation: the new key appears in the published set
    assert verify_supabase_jwt(token, jwks=jwks.client, issuer=ISS)["sub"] == SUB


def test_symmetric_and_none_algorithms_are_never_accepted(keys, jwks):
    now = int(time.time())
    claims = {"iss": ISS, "sub": SUB, "aud": "authenticated", "exp": now + 60}
    for token in (jwt.encode(claims, "secret", algorithm="HS256", headers={"kid": "k1"}),
                  jwt.encode(claims, None, algorithm="none", headers={"kid": "k1"})):   # type: ignore[arg-type]
        with pytest.raises(InvalidToken):
            verify_supabase_jwt(token, jwks=jwks.client, issuer=ISS)


@pytest.mark.parametrize("garbage", ["", "a.b", "a.b.c", "x" * 5000, "Bearer", "eyJhbGciOiJub25lIn0..", None, 5])
def test_garbage_is_rejected(jwks, garbage):
    with pytest.raises(InvalidToken):
        verify_supabase_jwt(garbage, jwks=jwks.client, issuer=ISS)


@pytest.fixture
def client(keys, jwks, monkeypatch):
    import app.control_plane.jwt_auth as module
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setattr(module, "jwks_client", lambda url: jwks.client)
    api = FastAPI()

    @api.get("/whoami")
    def whoami(p: Principal = Depends(current_principal)):
        return {"user_id": p.user_id}

    return TestClient(api)


def test_dependency_maps_header_to_principal(client, keys, caplog):
    assert client.get("/whoami").json() == {"user_id": None}
    assert client.get("/whoami", headers={"Authorization": f"Bearer {mint(keys)}"}).json() == {"user_id": SUB}
    with caplog.at_level(logging.DEBUG):
        bad = mint(keys, aud="anon")
        r = client.get("/whoami", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401 and r.json() == {"detail": "Invalid or expired session."}
    assert bad not in caplog.text and bad[-20:] not in caplog.text
    for header in ("Basic abc", "Bearer", "Bearer  ", "Token x", f"bearer {mint(keys)}", f"Bearer {mint(keys)} extra"):
        assert client.get("/whoami", headers={"Authorization": header}).status_code == 401, header


def test_jwks_outage_is_401_not_500(client, keys, monkeypatch):
    import app.control_plane.jwt_auth as module

    def boom(url):
        raise OSError("jwks unreachable")

    monkeypatch.setattr(module, "jwks_client", boom)
    assert client.get("/whoami").json() == {"user_id": None}
    assert client.get("/whoami", headers={"Authorization": f"Bearer {mint(keys)}"}).status_code == 401


def test_dependency_is_401_when_supabase_url_is_not_configured(client, keys, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL")
    assert client.get("/whoami").json() == {"user_id": None}
    assert client.get("/whoami", headers={"Authorization": f"Bearer {mint(keys)}"}).status_code == 401
