"""End-user identity: a short-lived HMAC-signed assertion from the trusted frontend server.

The FANTASY_API_KEY only proves the caller is our own dashboard backend (service auth); it is never a user.
Who the user is comes from this header, signed with FANTASY_USER_ASSERTION_SECRET:

    X-Fantasy-User-Assertion: v1.<b64url(payload json)>.<b64url(hmac_sha256(secret, "v1." + b64url(payload)))>
    payload = {"sub": user_id, "aud": "fantasy-intel-api", "iat": int, "exp": int}

exp - iat <= 600 s, 60 s of clock skew is tolerated, b64url has no padding. No header means anonymous.
A header that is present but malformed, badly signed, expired, for another audience, or sent while no
secret is configured is a 401: never silently downgraded to anonymous. Failure details are not
echoed to the client and the header value is never logged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time

from fastapi import HTTPException, Request

from ..config import get_settings
from .context import Principal

HEADER = "X-Fantasy-User-Assertion"
AUDIENCE = "fantasy-intel-api"
VERSION = "v1"
MAX_LIFETIME = 600
CLOCK_SKEW = 60
MIN_SECRET_LENGTH = 32
_MAX_HEADER = 2048
_B64URL = re.compile(r"[A-Za-z0-9_-]+")


class InvalidUserAssertion(ValueError):
    """Deliberately vague; carries no part of the header."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    if not _B64URL.fullmatch(text):
        raise InvalidUserAssertion("malformed")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise InvalidUserAssertion("malformed") from None


def _mac(secret: str, signing_input: str) -> str:
    return _b64e(hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest())


def _check_secret(secret: object) -> str:
    if not isinstance(secret, str) or len(secret) < MIN_SECRET_LENGTH:
        raise InvalidUserAssertion("no usable secret")
    return secret


def verify_user_assertion(header_value: str | None, secret: str | None, now: float) -> Principal:
    """Return the Principal named by a valid assertion, else raise InvalidUserAssertion."""
    secret = _check_secret(secret)
    if not isinstance(header_value, str) or not header_value or len(header_value) > _MAX_HEADER:
        raise InvalidUserAssertion("malformed")
    parts = header_value.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        raise InvalidUserAssertion("malformed")
    _, body, sig = parts
    if not _B64URL.fullmatch(body) or not _B64URL.fullmatch(sig):
        raise InvalidUserAssertion("malformed")
    expected = _mac(secret, f"{VERSION}.{body}")
    # Compare the encoded forms: a byte-for-byte match means a canonical signature, no alternate encodings.
    if not hmac.compare_digest(expected.encode("ascii"), sig.encode("ascii")):
        raise InvalidUserAssertion("bad signature")
    try:
        payload = json.loads(_b64d(body))
    except ValueError:
        raise InvalidUserAssertion("malformed") from None
    if not isinstance(payload, dict):
        raise InvalidUserAssertion("malformed")
    sub, aud, iat, exp = (payload.get(k) for k in ("sub", "aud", "iat", "exp"))
    if not isinstance(sub, str) or aud != AUDIENCE:
        raise InvalidUserAssertion("wrong subject or audience")
    for n in (iat, exp):
        if isinstance(n, bool) or not isinstance(n, int):
            raise InvalidUserAssertion("malformed")
    if exp <= iat or exp - iat > MAX_LIFETIME:
        raise InvalidUserAssertion("bad lifetime")
    if now > exp + CLOCK_SKEW:
        raise InvalidUserAssertion("expired")
    if iat > now + CLOCK_SKEW:
        raise InvalidUserAssertion("issued in the future")
    try:
        return Principal(sub)
    except ValueError:
        raise InvalidUserAssertion("malformed subject") from None


def sign_user_assertion(user_id: str, secret: str, now: float, ttl: int = 300) -> str:
    """For tests and tools. The production signer lives in the frontend server."""
    secret = _check_secret(secret)
    Principal(user_id)  # same user-id rules the verifier applies
    if not 0 < ttl <= MAX_LIFETIME:
        raise ValueError(f"ttl must be 1..{MAX_LIFETIME} seconds")
    iat = int(now)
    payload = {"sub": user_id, "aud": AUDIENCE, "iat": iat, "exp": iat + ttl}
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{VERSION}.{body}.{_mac(secret, f'{VERSION}.{body}')}"


def current_principal(request: Request) -> Principal:
    """FastAPI dependency. Anonymous when the header is absent; 401 when it is present but unusable."""
    raw = request.headers.get(HEADER)
    if raw is None:
        return Principal.anonymous()
    try:
        return verify_user_assertion(raw, get_settings().user_assertion_secret, time.time())
    except InvalidUserAssertion:
        raise HTTPException(status_code=401, detail="Invalid or expired user assertion.") from None
