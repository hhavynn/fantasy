"""Supabase access-token verification.

The Next.js server forwards the signed-in user's Supabase access token as `Authorization: Bearer <jwt>`; the service
API key (X-API-Key) only proves the caller is our dashboard. This module verifies the token itself against the
project's JWKS (ES256 keys today; RS256 accepted for a future rotation), checks `iss`, `aud` and `exp`, and turns the
verified claims into a Principal. No header is anonymous. A header that is present but unusable is a 401 and is never
downgraded to anonymous. Token values are never logged or echoed."""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid

import jwt
from fastapi import HTTPException, Request

from ..config import get_settings
from ..leagues.context import Principal

log = logging.getLogger(__name__)

ISSUER_SUFFIX = "/auth/v1"
JWKS_PATH = "/auth/v1/.well-known/jwks.json"
AUDIENCE = "authenticated"
ALGORITHMS = ("ES256", "RS256")
_MAX_TOKEN = 4096
_BEARER = re.compile(r"Bearer ([A-Za-z0-9_.-]{20,4096})")      # exact scheme, one space, compact JWS alphabet
_CLIENTS: dict[str, jwt.PyJWKClient] = {}
_CLIENTS_LOCK = threading.Lock()
REJECTED = "Invalid or expired session."


class InvalidToken(ValueError):
    """Deliberately vague; never carries the token."""


def jwks_client(supabase_url: str) -> jwt.PyJWKClient:
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(supabase_url)
        if client is None:
            client = _CLIENTS[supabase_url] = jwt.PyJWKClient(supabase_url + JWKS_PATH, cache_keys=True,
                                                               lifespan=3600, timeout=5)
        return client


def verify_supabase_jwt(token: str, *, jwks: jwt.PyJWKClient, issuer: str, now: float | None = None) -> dict:
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN or token.count(".") != 2:
        raise InvalidToken("malformed")
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in ALGORITHMS:
            raise InvalidToken("algorithm")
        key = jwks.get_signing_key_from_jwt(token).key   # refreshes the key set once on an unknown kid
        claims = jwt.decode(token, key, algorithms=list(ALGORITHMS), audience=AUDIENCE, issuer=issuer,
                            options={"require": ["exp", "sub", "iss", "aud"], "verify_signature": True},
                            leeway=30)
    except InvalidToken:
        raise
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise InvalidToken("rejected") from None
    try:
        uuid.UUID(str(claims.get("sub")))
    except (ValueError, TypeError, AttributeError):
        raise InvalidToken("subject") from None
    if now is not None and claims["exp"] < now - 30:
        raise InvalidToken("expired")
    return claims


def current_principal(request: Request) -> Principal:
    """FastAPI dependency. Anonymous when there is no Authorization header; 401 when it is present but unusable."""
    raw = request.headers.get("authorization")
    if raw is None:
        return Principal.anonymous()
    settings = get_settings()
    m = _BEARER.fullmatch(raw.strip()) if settings.supabase_url else None
    if m is None:
        raise HTTPException(401, REJECTED)
    try:
        claims = verify_supabase_jwt(m.group(1), jwks=jwks_client(settings.supabase_url),
                                     issuer=settings.supabase_issuer, now=time.time())
        return Principal.from_claims(claims)
    except (InvalidToken, ValueError):
        raise HTTPException(401, REJECTED) from None
    except Exception:  # noqa: BLE001 - a JWKS fetch failure must not become a 500 that echoes anything
        log.warning("token verification failed: %s", "jwks error")
        raise HTTPException(401, REJECTED) from None
