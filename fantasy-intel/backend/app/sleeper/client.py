"""Raw, read-only Sleeper API access (https://docs.sleeper.com).

Sleeper's API is public and unauthenticated. This module only fetches JSON; turning it into
models and internal rows happens in `parse` / `normalize` (same split as the ESPN client).

Identifiers: every Sleeper id (league, user, owner, player, draft, bracket) is a string end to
end. A league id passed as an int is rejected rather than converted, so a caller that already
lost precision (JavaScript, a float column) fails loudly instead of querying the wrong league.

Politeness:
  * league / users / rosters / NFL state are cached in memory for `ttl` seconds;
  * `/players/nfl` (~5MB) is fetched at most once per `players_max_age` (default 24h, which is
    Sleeper's documented limit) and stored on disk, so restarts reuse it;
  * HTTP 429 and 5xx are retried with Retry-After (or exponential backoff), then raised.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

log = logging.getLogger("sleeper")

BASE_URL = "https://api.sleeper.app/v1"
DEFAULT_TTL = 60.0                    # seconds, league / users / rosters / state
PLAYERS_MAX_AGE = 24 * 3600.0         # Sleeper: call /players/nfl at most once per day
MAX_RETRIES = 3
BACKOFF_BASE = 1.0                    # seconds; doubled per attempt when no Retry-After
MAX_BACKOFF = 30.0
PLAYERS_FILE = "players_nfl.json"


class SleeperError(RuntimeError):
    pass


class InvalidLeagueId(SleeperError, ValueError):
    """The league id is malformed, unknown to Sleeper, or not an NFL league."""

    def __init__(self, league_id: Any, reason: str):
        self.league_id = league_id
        self.reason = reason
        super().__init__(f"Invalid Sleeper league id {league_id!r}: {reason}. Use the number in the "
                         "league URL (sleeper.com/leagues/<league_id>/...), as a string.")


class SleeperRateLimited(SleeperError):
    pass


class SleeperHTTPError(SleeperError):
    def __init__(self, status: int, url: str):
        self.status = status
        super().__init__(f"Sleeper returned HTTP {status} for {url}")


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, float], Response]     # (url, timeout) -> Response; raises OSError on network failure


def requests_transport(session=None) -> Transport:
    import requests

    s = session or requests.Session()

    def get(url: str, timeout: float) -> Response:
        try:
            r = s.get(url, timeout=timeout, headers={"Accept": "application/json"})
        except requests.RequestException as e:
            raise OSError(f"Sleeper request failed: {e}") from e
        return Response(r.status_code, r.headers, r.content)

    return get


def check_league_id(league_id: Any) -> str:
    """Sleeper league ids are digit strings (~19 digits, beyond 2^53). Never converted to int."""
    if not isinstance(league_id, str):
        raise TypeError(f"Sleeper league ids must be strings, got {type(league_id).__name__} {league_id!r}")
    lid = league_id.strip()
    if not lid.isdigit():
        raise InvalidLeagueId(league_id, "expected a string of digits")
    return lid


class SleeperClient:
    def __init__(self, *, cache_dir: Path | None = None, ttl: float = DEFAULT_TTL,
                 players_max_age: float = PLAYERS_MAX_AGE, transport: Transport | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 max_retries: int = MAX_RETRIES, base_url: str = BASE_URL, timeout: float = 30.0):
        if cache_dir is None:
            from ..config import DATA_DIR
            cache_dir = DATA_DIR / "sleeper"
        self.cache_dir = Path(cache_dir)
        self.ttl = ttl
        self.players_max_age = players_max_age
        self.transport = transport or requests_transport()
        self.clock = clock
        self.sleep = sleep
        self.max_retries = max_retries
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._players: dict[str, dict] | None = None
        self._players_fetched_at: float | None = None
        self._players_lock = threading.Lock()
        self.players_source: str | None = None     # memory | disk | network | stale_disk (last players() call)

    # ------------------------------------------------------------- endpoints --

    def league(self, league_id: str) -> dict:
        lid = check_league_id(league_id)
        data = self._get_cached(f"/league/{lid}")
        if not isinstance(data, dict):
            raise InvalidLeagueId(league_id, "Sleeper has no league with this id")
        if data.get("sport") not in (None, "nfl"):
            raise InvalidLeagueId(league_id, f"this is a {data.get('sport')} league, not NFL")
        return data

    def league_users(self, league_id: str) -> list[dict]:
        return self._league_list(league_id, "users")

    def league_rosters(self, league_id: str) -> list[dict]:
        return self._league_list(league_id, "rosters")

    def nfl_state(self) -> dict:
        data = self._get_cached("/state/nfl")
        if not isinstance(data, dict):
            raise SleeperError("Sleeper /state/nfl returned no data")
        return data

    def players(self, *, force_refresh: bool = False) -> dict[str, dict]:
        """All NFL players keyed by Sleeper player id (str). Team defenses are keyed by team code."""
        with self._players_lock:
            now = self.clock()
            if not force_refresh and self._players is not None and self._fresh(self._players_fetched_at, now):
                self.players_source = "memory"
                return self._players
            disk = None if force_refresh else self._read_players_file()
            if disk is not None and self._fresh(disk[0], now):
                self._players_fetched_at, self._players = disk
                self.players_source = "disk"
                return self._players
            try:
                data = self._get("/players/nfl")
                if not isinstance(data, dict) or not data:
                    raise SleeperError("Sleeper /players/nfl returned no players")
            except (SleeperError, OSError) as e:
                stale = disk or self._read_players_file()
                if stale is None:
                    raise
                log.warning("Sleeper players refresh failed (%s); using cached copy from %s", e,
                            time.strftime("%Y-%m-%d %H:%M", time.gmtime(stale[0])))
                self._players_fetched_at, self._players = stale
                self.players_source = "stale_disk"
                return self._players
            self._write_players_file(now, data)
            self._players_fetched_at, self._players = now, data
            self.players_source = "network"
            return data

    @property
    def players_fetched_at(self) -> float | None:
        return self._players_fetched_at

    # -------------------------------------------------------------- internals --

    def _league_list(self, league_id: str, what: str) -> list[dict]:
        lid = check_league_id(league_id)
        data = self._get_cached(f"/league/{lid}/{what}")
        if data is None:
            raise InvalidLeagueId(league_id, "Sleeper has no league with this id")
        if not isinstance(data, list):
            raise SleeperError(f"Sleeper /league/{lid}/{what} returned {type(data).__name__}, expected a list")
        return data

    def _fresh(self, fetched_at: float | None, now: float) -> bool:
        return fetched_at is not None and 0 <= now - fetched_at < self.players_max_age

    def get_json(self, path: str) -> Any:
        """GET any read-only API path (e.g. "/league/<id>/matchups/3") through the same TTL cache,
        retry and error handling as the typed methods. `path` starts with "/"."""
        if not path.startswith("/") or ".." in path:
            raise ValueError("Sleeper API paths start with / and never contain ..")
        return self._get_cached(path)

    def _get_cached(self, path: str) -> Any:
        now = self.clock()
        with self._lock:
            hit = self._cache.get(path)
            if hit is not None and now - hit[0] < self.ttl:
                return hit[1]
        data = self._get(path)
        if data is not None:                      # never cache a miss: the id may be fixed and retried
            with self._lock:
                self._cache[path] = (now, data)
        return data

    def _get(self, path: str) -> Any:
        """GET + JSON decode. Returns None for 404 (Sleeper's answer for unknown ids)."""
        url = self.base_url + path
        for attempt in range(self.max_retries + 1):
            try:
                r = self.transport(url, self.timeout)
            except OSError:
                if attempt == self.max_retries:
                    raise
                self.sleep(self._backoff(attempt, None))
                continue
            if r.status == 429 or 500 <= r.status < 600:
                if attempt == self.max_retries:
                    if r.status == 429:
                        raise SleeperRateLimited(f"Sleeper rate limit (HTTP 429) persisted after "
                                                 f"{self.max_retries} retries: {url}")
                    raise SleeperHTTPError(r.status, url)
                delay = self._backoff(attempt, r.headers)
                log.info("Sleeper HTTP %s for %s; retrying in %.1fs", r.status, path, delay)
                self.sleep(delay)
                continue
            if r.status == 404:
                return None
            if r.status != 200:
                raise SleeperHTTPError(r.status, url)
            if not r.body.strip():
                return None
            return json.loads(r.body)
        raise AssertionError("unreachable")

    @staticmethod
    def _backoff(attempt: int, headers: Mapping[str, str] | None) -> float:
        retry_after = None
        for k, v in (headers or {}).items():
            if k.lower() == "retry-after":
                try:
                    retry_after = float(v)
                except (TypeError, ValueError):
                    pass
        delay = retry_after if retry_after is not None and retry_after >= 0 else BACKOFF_BASE * 2 ** attempt
        return min(delay, MAX_BACKOFF)

    @property
    def _players_path(self) -> Path:
        return self.cache_dir / PLAYERS_FILE

    def _read_players_file(self) -> tuple[float, dict] | None:
        try:
            doc = json.loads(self._players_path.read_text())
            fetched_at, players = float(doc["fetched_at"]), doc["players"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return (fetched_at, players) if isinstance(players, dict) and players else None

    def _write_players_file(self, fetched_at: float, players: dict) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._players_path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"fetched_at": fetched_at, "players": players}))
        os.replace(tmp, self._players_path)        # atomic: readers never see a half-written file
