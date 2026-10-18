"""One HTTP client for every collector: retries with exponential backoff + jitter, a per-host
politeness delay, Retry-After support and a small on-disk response cache.

Collectors never talk to httpx directly, so rate-limit behaviour and caching are uniform and tests
can swap the transport (``Fetcher(transport=httpx.MockTransport(...))``).
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0 Safari/537.36"
)
# Reddit asks API-style clients for a descriptive UA; a browser UA on .json endpoints gets 403'd.
BOT_UA = "fantasy-intel-social/0.1 (fantasy football trend research; python-httpx)"


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, blocked: bool = False):
        super().__init__(message)
        self.status = status
        self.blocked = blocked  # 401/403/429 after retries: the source is refusing us right now


@dataclass
class Response:
    url: str
    status: int
    text: str
    from_cache: bool = False

    def json(self) -> Any:
        return json.loads(self.text)


class Fetcher:
    def __init__(
        self,
        cache_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        retries: int = 3,
        backoff_s: float = 1.5,
        min_interval_s: float = 1.0,
        timeout_s: float = 20.0,
        sleep=time.sleep,
    ):
        self.cache_dir = cache_dir
        self.retries = retries
        self.backoff_s = backoff_s
        self.min_interval_s = min_interval_s
        self.sleep = sleep
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.client = httpx.Client(
            transport=transport, timeout=timeout_s, follow_redirects=True,
            headers={"User-Agent": DEFAULT_UA, "Accept-Language": "en-US,en;q=0.9"},
        )
        self.requests_made = 0

    def _cache_path(self, url: str, params: dict | None) -> Path | None:
        if not self.cache_dir:
            return None
        h = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def _polite(self, host: str, interval: float | None = None) -> None:
        with self._lock:
            gap = self.min_interval_s if interval is None else min(interval, self.min_interval_s)
            wait = self._last.get(host, 0) + gap - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last[host] = time.monotonic()

    def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        cache_ttl_s: float = 0,
        min_interval_s: float | None = None,
    ) -> Response:
        cp = self._cache_path(url, params) if cache_ttl_s > 0 else None
        if cp and cp.exists() and time.time() - cp.stat().st_mtime < cache_ttl_s:
            data = json.loads(cp.read_text())
            return Response(data["url"], data["status"], data["text"], from_cache=True)

        host = urlparse(url).netloc
        last_status: int | None = None
        last_err = ""
        for attempt in range(self.retries + 1):
            self._polite(host, min_interval_s)
            try:
                r = self.client.get(url, params=params, headers=headers)
                self.requests_made += 1
            except httpx.HTTPError as e:  # DNS, TLS, timeout: transient
                last_err = f"{type(e).__name__}: {e}"
                last_status = None
            else:
                last_status = r.status_code
                if r.status_code < 400:
                    resp = Response(str(r.url), r.status_code, r.text)
                    if cp:
                        cp.parent.mkdir(parents=True, exist_ok=True)
                        cp.write_text(json.dumps({"url": resp.url, "status": resp.status, "text": resp.text}))
                    return resp
                last_err = f"HTTP {r.status_code}"
                if r.status_code in (400, 404, 410):
                    break  # not transient
                if r.status_code in (401, 403) and attempt >= 1:
                    break  # a second refusal is a block, not a blip
                retry_after = r.headers.get("retry-after")
                if retry_after and retry_after.isdigit() and attempt < self.retries:
                    self.sleep(min(float(retry_after), 60.0))
                    continue
            if attempt < self.retries:
                self.sleep(self.backoff_s * (2**attempt) + random.uniform(0, 0.5))
        blocked = last_status in (401, 403, 429)
        raise FetchError(f"{url}: {last_err}", status=last_status, blocked=blocked)

    def close(self) -> None:
        self.client.close()
