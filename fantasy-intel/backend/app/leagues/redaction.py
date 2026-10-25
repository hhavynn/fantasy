"""Error text that may be stored or shown: class name + first line, secrets redacted, length capped.

Used for anything provider- or credential-adjacent that could end up in a log line, a sync job's error_detail or a
response body. Redaction runs on the whole message before the cut."""

from __future__ import annotations

import re

_ERROR_MAX = 240

_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_COOKIE_LINE = re.compile(r"(?i)\b(set-cookie|cookie)\b\s*[=:]\s*.*")
# Authorization headers: the scheme (Basic / Token / Digest / ...) is followed by the credential, so the
# whole rest of the line goes. OAuth `code` / `state` URL parameters are one-time secrets too.
_AUTH_LINE = re.compile(r"(?i)\b((?:proxy-)?authorization)\b\s*[=:]\s*.*")
_OAUTH_PARAM = re.compile(r"(?i)([?&](?:code|state))=[^&\s#]+")
_KEYED_SECRET = re.compile(
    r"(?i)\b(espn_s2|swid|access_token|refresh_token|id_token|client_secret|api[_-]?key|x-api-key|"
    r"authorization|token|secret|password|passwd|session)\b[\"']?\s*[=:]\s*[\"']?[^\s\"',;&]+")
_GUID = re.compile(r"(?i)[{(]?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}[})]?")
_LONG_RUN = re.compile(r"[A-Za-z0-9+/_%=-]{32,}")
_HEX = re.compile(r"[0-9a-fA-F]+")


def _redact_run(m: re.Match) -> str:
    """Long hex, base64 and token-looking runs. URL paths and hyphenated words are mostly single-case,
    so they survive; a real token is mixed-case with digits, hex, or very long."""
    s = m.group(0)
    mixed = any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s)
    if mixed or _HEX.fullmatch(s) or len(s) >= 80:
        return "[redacted]"
    return s


def sanitize_error(err: BaseException | str, *, max_len: int = _ERROR_MAX) -> str:
    """Class name plus the first line of the message, secrets redacted, length capped.
    Redaction runs on the whole message before the cut, so a token cannot be truncated into
    something the patterns no longer recognise."""
    if isinstance(err, BaseException):
        name, text = type(err).__name__, str(err)
    else:
        name, text = "", str(err)
    text = text[:4000]
    text = _COOKIE_LINE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _AUTH_LINE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _OAUTH_PARAM.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _KEYED_SECRET.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _GUID.sub("[redacted]", text)
    text = _LONG_RUN.sub(_redact_run, text)
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    first = re.sub(r"[\x00-\x1f\x7f]", " ", first)
    out = f"{name}: {first}" if name and first else (name or first or "error")
    return out if len(out) <= max_len else out[: max_len - 1] + "…"
