"""A minimal S3 client for Cloudflare R2: AWS Signature Version 4 over urllib, standard library only.

The hosted API downloads the published snapshot with it (an R2 token scoped to Object Read), and
scripts/publish_snapshot.py uploads with a read/write token. Keeping it dependency-free keeps the Vercel
bundle unchanged (tests/test_hosting.py checks the API imports no sync-only packages).

Secrets never appear in repr(), exceptions or logs: errors carry the HTTP status, the S3 error code and
the object key only.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_CHUNK = 1 << 20


class R2Error(Exception):
    def __init__(self, status: int | None, code: str, key: str):
        super().__init__(f"R2 {code} ({status}) for {key!r}")
        self.status, self.code, self.key = status, code, key


@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    bucket: str = "fantasy-intel"
    region: str = "auto"
    endpoint: str | None = None   # tests point this at a local server; default is the account's R2 endpoint

    @property
    def base_url(self) -> str:
        return (self.endpoint or f"https://{self.account_id}.r2.cloudflarestorage.com").rstrip("/")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _quote(s: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(s, safe=safe)


def sign(method: str, host: str, path: str, query: dict[str, str], headers: dict[str, str], payload_sha256: str,
         access_key_id: str, secret_access_key: str, region: str, now: datetime, service: str = "s3") -> dict[str, str]:
    """Headers for a SigV4-signed request: `headers` plus host, x-amz-date, x-amz-content-sha256 and
    Authorization. `path` is the raw object path ('/bucket/key'); it is URI-encoded here once."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    all_headers = {**{k.lower(): str(v).strip() for k, v in headers.items()},
                   "host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_sha256}
    signed = ";".join(sorted(all_headers))
    canonical = "\n".join([
        method,
        _quote(path, safe="-_.~/"),
        "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items())),
        "".join(f"{k}:{all_headers[k]}\n" for k in sorted(all_headers)),
        signed,
        payload_sha256,
    ])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_access_key).encode(), date), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    all_headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
                                    f"SignedHeaders={signed}, Signature={signature}")
    return all_headers


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _request(cfg: R2Config, method: str, key: str, *, query: dict[str, str] | None = None, data=None,
             payload_sha256: str = EMPTY_SHA256, headers: dict[str, str] | None = None, timeout: float = 60):
    path = f"/{cfg.bucket}/{key}" if key else f"/{cfg.bucket}"
    host = urllib.parse.urlsplit(cfg.base_url).netloc
    signed = sign(method, host, path, query or {}, headers or {}, payload_sha256,
                  cfg.access_key_id, cfg.secret_access_key, cfg.region, datetime.now(timezone.utc))
    qs = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted((query or {}).items()))
    url = cfg.base_url + _quote(path, safe="-_.~/") + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, data=data, method=method, headers=signed)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as err:
        code = "HTTPError"
        try:
            node = ET.fromstring(err.read()).find("Code")
            code = node.text if node is not None and node.text else code
        except ET.ParseError:
            pass
        raise R2Error(err.code, code, key) from None
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise R2Error(None, type(err).__name__, key) from None


_STREAM_ERRORS = (OSError, http.client.HTTPException)   # timeouts, resets, IncompleteRead while reading a body


def get_bytes(cfg: R2Config, key: str, timeout: float = 15) -> bytes:
    with _request(cfg, "GET", key, timeout=timeout) as resp:
        try:
            return resp.read()
        except _STREAM_ERRORS as err:
            raise R2Error(None, type(err).__name__, key) from None


def _part_path(dest: Path) -> Path:
    """A temporary name no other download can share: two processes (or threads) fetching the same object at once
    each write their own file and rename it into place, so neither truncates or deletes the other's."""
    return dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_ident()}.part")


def download(cfg: R2Config, key: str, dest: Path, expected_sha256: str | None = None, timeout: float = 120,
             size: int | None = None, parts: int = 8, min_parallel_bytes: int = 16 << 20,
             expected_sha256_prefix: str | None = None) -> Path:
    """Stream `key` to `dest` via a temporary name, check its sha256, then rename into place, so a reader
    never sees a partial file. When `size` is known and large, `parts` ranged GETs run in parallel: one R2
    stream from Vercel's iad1 manages ~30 MB/s, eight together fetch the 95 MB snapshot in ~0.5-1 s."""
    dest = Path(dest)
    part = _part_path(dest)
    try:
        if size and size >= min_parallel_bytes and parts > 1:
            _download_ranges(cfg, key, part, size, parts, timeout)
            digest = file_sha256(part)
        else:
            digest = _download_stream(cfg, key, part, timeout)
        if expected_sha256 and digest != expected_sha256:
            raise R2Error(None, "ChecksumMismatch", key)
        if expected_sha256_prefix and not digest.startswith(expected_sha256_prefix):
            raise R2Error(None, "ChecksumMismatch", key)
        if size is not None and part.stat().st_size != size:
            raise R2Error(None, "SizeMismatch", key)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def _download_stream(cfg: R2Config, key: str, part: Path, timeout: float) -> str:
    h = hashlib.sha256()
    with _request(cfg, "GET", key, timeout=timeout) as resp, open(part, "wb") as f:
        try:
            while chunk := resp.read(_CHUNK):
                h.update(chunk)
                f.write(chunk)
        except _STREAM_ERRORS as err:   # a body cut off mid-stream is an R2 failure like any other
            raise R2Error(None, type(err).__name__, key) from None
    return h.hexdigest()


def _download_ranges(cfg: R2Config, key: str, part: Path, size: int, parts: int, timeout: float) -> None:
    step = -(-size // parts)
    fd = os.open(part, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.ftruncate(fd, size)

        def fetch(start: int) -> int:
            end = min(size, start + step) - 1
            with _request(cfg, "GET", key, headers={"range": f"bytes={start}-{end}"}, timeout=timeout) as resp:
                if resp.status != 206:   # a server that ignores Range would write the whole object at `start`
                    raise R2Error(resp.status, "RangeNotHonoured", key)
                off = start
                try:
                    while chunk := resp.read(_CHUNK):
                        os.pwrite(fd, chunk, off)
                        off += len(chunk)
                except _STREAM_ERRORS as err:
                    raise R2Error(None, type(err).__name__, key) from None
            if off != end + 1:
                raise R2Error(None, "ShortRange", key)
            return off - start

        with ThreadPoolExecutor(parts) as pool:
            if sum(pool.map(fetch, range(0, size, step))) != size:
                raise R2Error(None, "SizeMismatch", key)
    finally:
        os.close(fd)


def put_file(cfg: R2Config, key: str, path: Path, content_type: str = "application/octet-stream",
             timeout: float = 600) -> str:
    path = Path(path)
    digest = file_sha256(path)
    with open(path, "rb") as f:
        _request(cfg, "PUT", key, data=f, payload_sha256=digest, timeout=timeout,
                 headers={"content-type": content_type, "content-length": str(path.stat().st_size)}).close()
    return digest


def put_bytes(cfg: R2Config, key: str, body: bytes, content_type: str = "application/json") -> None:
    _request(cfg, "PUT", key, data=body, payload_sha256=hashlib.sha256(body).hexdigest(),
             headers={"content-type": content_type, "content-length": str(len(body))}).close()


def delete(cfg: R2Config, key: str) -> None:
    _request(cfg, "DELETE", key).close()


def list_keys(cfg: R2Config, prefix: str) -> list[str]:
    return [key for key, _ in list_objects(cfg, prefix)]


def list_objects(cfg: R2Config, prefix: str) -> list[tuple[str, int]]:
    """(key, size in bytes) for every object under `prefix` (ListObjectsV2, all pages)."""
    ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
    out, token = [], None
    while True:
        query = {"list-type": "2", "prefix": prefix, **({"continuation-token": token} if token else {})}
        with _request(cfg, "GET", "", query=query) as resp:
            root = ET.fromstring(resp.read())
        for c in root.findall(f"{ns}Contents") or root.findall("Contents"):
            size = c.findtext(f"{ns}Size") or c.findtext("Size") or "0"
            out.append((c.findtext(f"{ns}Key") or c.findtext("Key"), int(size)))
        token = root.findtext(f"{ns}NextContinuationToken") or root.findtext("NextContinuationToken")
        if not token:
            return out
