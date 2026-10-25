"""Postgres access for the request path.

The API connects as `fi_api` (NOINHERIT, member of anon + authenticated, no table grants of its own). Every request
runs exactly one transaction: `SET LOCAL ROLE anon|authenticated` and transaction-local `request.jwt.claims`, so
Supabase's auth.uid() and the RLS policies decide what the request can see. Both settings end with the transaction,
which is what makes a Supavisor transaction-mode pool safe: the next transaction on the same physical connection
starts as plain fi_api with no claims. Server-side prepared statements are off (Supavisor transaction mode)."""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

from ..config import get_settings
from ..leagues.context import Principal

log = logging.getLogger(__name__)

_ROLE_SQL = {True: "SET LOCAL ROLE authenticated", False: "SET LOCAL ROLE anon"}   # constants, never built from input
STATEMENT_TIMEOUT_MS = 5000


class ControlPlaneUnavailable(RuntimeError):
    """Postgres cannot be reached or is not configured. Routes answer 503; nothing is ever treated as 'denied'."""


class AccountGone(RuntimeError):
    """A signature-valid token whose Supabase account no longer exists. Routes answer 401: the caller is signed out."""


class ControlPlane:
    def __init__(self, dsn: str, *, max_size: int = 4):
        self._pool = ConnectionPool(dsn, min_size=0, max_size=max_size, open=False, timeout=5,
                                    kwargs={"prepare_threshold": None, "autocommit": False, "connect_timeout": 5},
                                    check=ConnectionPool.check_connection)
        self._lock = threading.Lock()
        self._opened = False

    def _ensure_open(self) -> None:
        with self._lock:
            if not self._opened:
                self._pool.open(wait=False)
                self._opened = True

    @contextmanager
    def request_tx(self, principal: Principal) -> Iterator[psycopg.Connection]:
        if not isinstance(principal, Principal):
            raise TypeError("request_tx needs a Principal")
        self._ensure_open()
        try:
            with self._pool.connection() as con:
                with con.transaction():
                    # One round trip for the whole setup (pipeline mode): role, claims, timeout and, for a signed-in
                    # caller, whether the account still exists.
                    with con.pipeline():
                        con.execute(_ROLE_SQL[principal.is_authenticated])
                        con.execute("SELECT set_config('request.jwt.claims', %s, true), "
                                    "set_config('statement_timeout', %s, true), "
                                    "set_config('fi.signups_open', %s, true)",
                                    [principal.claims_json, str(STATEMENT_TIMEOUT_MS),
                                     "on" if get_settings().signups_enabled else "off"])
                        account = (con.execute("SELECT fi.current_account_exists()")
                                   if principal.is_authenticated else None)
                    if account is not None and not account.fetchone()[0]:
                        raise AccountGone("account no longer exists")
                    yield con
        except (psycopg.OperationalError, PoolTimeout) as err:
            log.warning("control plane unavailable: %s", type(err).__name__)
            raise ControlPlaneUnavailable("The control-plane database is unavailable.") from None

    def close(self) -> None:
        with self._lock:
            if self._opened:
                self._pool.close()
                self._opened = False


_PLANES: dict[str, ControlPlane] = {}
_PLANES_LOCK = threading.Lock()


def get_control_plane() -> ControlPlane:
    dsn = get_settings().supabase_db_url
    if not dsn:
        raise ControlPlaneUnavailable("SUPABASE_DB_URL is not configured.")
    with _PLANES_LOCK:
        plane = _PLANES.get(dsn)
        if plane is None:
            plane = _PLANES[dsn] = ControlPlane(dsn)
        return plane
