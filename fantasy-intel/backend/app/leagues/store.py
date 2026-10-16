"""Tenancy store: who is connected to which league, plus the last good provider snapshots.

Lives in its OWN DuckDB file (settings.tenancy_db_path), never in the analytics database, and
app/db/publish.py copies only the analytics file to MotherDuck, so none of this reaches the hosted copy.

Authorization model:
  * `connect` / `authorize` / `list_contexts` / `context_for_key` are the only places a LeagueContext is
    issued, and each one filters on the *verified* principal. Nothing here takes a bare user id.
  * Every other method takes a LeagueContext and re-checks, in the same transaction as its read or write,
    that the principal's connection is still active. A context obtained before `revoke` stops working.
  * Anything that is not a clear "yes" (anonymous, unknown id, someone else's league, revoked) raises
    LeagueAccessDenied. HTTP maps that to 404, never 403, so a probe cannot tell the cases apart.
    Database outages are NOT turned into "denied": they propagate so callers answer 5xx, not a false 404.

Concurrency: DuckDB allows one writer, and a held connection would lock other processes (a maintenance
script) out of the file. So every operation opens a short-lived connection and runs in one transaction,
and a per-store RLock serialises operations inside this process (the API runs sync routes on a thread
pool). Use one store per file (`app.api.leagues_v2.get_tenancy_store` caches them); two stores on one
file would each have their own lock.

Secrets: nothing secret is stored. `credential_refs` holds metadata for a vault handle, and error text
written to league_sync_status is passed through `sanitize_error` first.
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ..config import get_settings
from ..db.connection import connect as open_connection, fetch_dicts, utcnow
from .capabilities import ProviderCapabilities
from .context import (
    AccessMethod, Freshness, LeagueAccessDenied, LeagueContext, LeagueKey, Principal, Provider, Role,
    SyncStatus, check_internal_league_id, issue_context, new_internal_league_id,
)
from .models import Provenance

SCHEMA_PATH = Path(__file__).with_name("tenancy_schema.sql")

SNAPSHOT_KINDS = ("settings", "teams", "rosters")
DATA_ORIGINS = ("live", "fixture")

_REF_ID = re.compile(r"[A-Za-z0-9_.:-]{8,128}")
_TEAM_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_NAME_MAX = 200
_ERROR_MAX = 240


# ---- error sanitizing --------------------------------------------------------------------------

_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_COOKIE_LINE = re.compile(r"(?i)\b(set-cookie|cookie)\b\s*[=:]\s*.*")
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
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _KEYED_SECRET.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _GUID.sub("[redacted]", text)
    text = _LONG_RUN.sub(_redact_run, text)
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    first = re.sub(r"[\x00-\x1f\x7f]", " ", first)
    out = f"{name}: {first}" if name and first else (name or first or "error")
    return out if len(out) <= max_len else out[: max_len - 1] + "…"


# ---- helpers -----------------------------------------------------------------------------------

def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


@dataclass(frozen=True)
class LeagueMeta:
    """Registry facts a LeagueContext does not carry."""
    data_origin: str            # 'live' | 'fixture'
    is_public: bool | None


_CONTEXT_SQL = """
SELECT r.league_id, r.provider, r.external_league_id, r.season, r.name, r.is_public, r.data_origin,
       c.role, c.access_method, c.my_team_external_id,
       s.status AS sync_status, s.last_synced_at, s.source_updated_at, s.last_error
FROM league_connections c
JOIN league_registry r ON r.league_id = c.league_id
LEFT JOIN league_sync_status s ON s.league_id = c.league_id
WHERE c.user_id = ? AND c.revoked_at IS NULL
"""


def _default_capabilities() -> dict[Provider, ProviderCapabilities] | None:
    """Capabilities from the provider registry, imported lazily so the tenancy layer works (with
    `capabilities=None`) while the registry is absent or broken. Never raises."""
    try:
        from .providers import registry
        return {c.provider: c for c in registry.all_capabilities()}
    except Exception:  # noqa: BLE001 - authorization must not depend on the provider layer
        return None


class TenancyStore:
    def __init__(self, path: Path | str | None = None, *,
                 capabilities: Callable[[], dict[Provider, ProviderCapabilities] | None] | None = None):
        self.path = Path(path) if path is not None else get_settings().tenancy_db_path
        self._capabilities = capabilities or _default_capabilities
        self._lock = threading.RLock()
        self._ready = False

    # ---- plumbing ----

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        with self._lock:
            con = open_connection(self.path)
            try:
                if not self._ready:
                    con.execute(SCHEMA_PATH.read_text())   # idempotent CREATE TABLE IF NOT EXISTS
                    self._ready = True
                con.execute("BEGIN TRANSACTION")
                try:
                    yield con
                    con.execute("COMMIT")
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
            finally:
                con.close()

    @staticmethod
    def _require_principal(principal: object) -> Principal:
        if not isinstance(principal, Principal) or not principal.is_authenticated:
            raise LeagueAccessDenied("unknown league")
        return principal

    def _context_from_row(self, row: dict, principal: Principal,
                          caps: dict[Provider, ProviderCapabilities] | None) -> LeagueContext:
        try:
            provider = Provider(row["provider"])
            key = LeagueKey(provider, row["external_league_id"], int(row["season"]))
            status = SyncStatus(row["sync_status"]) if row["sync_status"] else SyncStatus.NEVER
            return issue_context(
                league_id=row["league_id"],
                key=key,
                principal=principal,
                role=Role(row["role"]),
                access=AccessMethod(row["access_method"]),
                name=row["name"],
                my_team_external_id=row["my_team_external_id"],
                freshness=Freshness(status, row["last_synced_at"], row["source_updated_at"], row["last_error"]),
                capabilities=caps.get(provider) if caps else None,
            )
        except (ValueError, TypeError, KeyError):
            raise LeagueAccessDenied("unknown league") from None

    def _authorize_in(self, con, principal: Principal, league_id: str) -> LeagueContext:
        rows = fetch_dicts(con, _CONTEXT_SQL + " AND c.league_id = ?", [principal.user_id, league_id])
        if not rows:
            raise LeagueAccessDenied("unknown league")
        return self._context_from_row(rows[0], principal, self._capabilities())

    @staticmethod
    def _require_active(con, ctx: LeagueContext) -> None:
        """The context must still be backed by an active connection of its own principal, to the same
        provider league. Run inside the operation's transaction."""
        if not isinstance(ctx, LeagueContext):
            raise LeagueAccessDenied("unknown league")
        row = con.execute(
            "SELECT 1 FROM league_connections c JOIN league_registry r ON r.league_id = c.league_id "
            "WHERE c.user_id = ? AND c.league_id = ? AND c.revoked_at IS NULL "
            "AND r.provider = ? AND r.external_league_id = ? AND r.season = ?",
            [ctx.principal.user_id, ctx.league_id, ctx.key.provider.value,
             ctx.key.external_league_id, ctx.key.season]).fetchone()
        if row is None:
            raise LeagueAccessDenied("unknown league")

    # ---- connect / authorize ----

    def connect(self, principal: Principal, key: LeagueKey, access_method: AccessMethod, *, name: str | None,
                is_public: bool | None, data_origin: str, credential_ref: str | None = None) -> LeagueContext:
        """Create or reactivate this principal's connection to a provider league season.

        The registry row is shared: the same (provider, external id, season) always maps to one internal
        league id, but it is the connection that grants access, so a second user gets nothing from it
        until they connect (and prove access) themselves."""
        self._require_principal(principal)
        if not isinstance(key, LeagueKey) or not isinstance(access_method, AccessMethod):
            raise TypeError("key must be a LeagueKey and access_method an AccessMethod")
        if data_origin not in DATA_ORIGINS:
            raise ValueError(f"data_origin must be one of {DATA_ORIGINS}")
        if is_public is not None and not isinstance(is_public, bool):
            raise TypeError("is_public must be True, False or None")
        if access_method is AccessMethod.PUBLIC and is_public is not True:
            raise PermissionError("This league is private or its visibility is unknown; public access is not allowed.")
        if access_method is AccessMethod.OWNER_ENV:
            owner = get_settings().owner_user_id
            if owner is None or principal.user_id != owner or key.provider is not Provider.ESPN:
                raise PermissionError("Server-held ESPN credentials may only be used by the deployment owner.")
        if access_method is AccessMethod.OAUTH and not credential_ref:
            raise PermissionError("OAuth connections need a credential reference from the vault.")
        if credential_ref is not None and not _REF_ID.fullmatch(credential_ref):
            raise ValueError("malformed credential reference")
        clean_name = (name.strip()[:_NAME_MAX] or None) if isinstance(name, str) else None

        with self._tx() as con:
            now = utcnow()
            con.execute("INSERT INTO app_users VALUES (?, ?) ON CONFLICT DO NOTHING", [principal.user_id, now])

            reg = con.execute(
                "SELECT league_id FROM league_registry WHERE provider = ? AND external_league_id = ? AND season = ?",
                [key.provider.value, key.external_league_id, key.season]).fetchone()
            if reg is None:
                league_id = new_internal_league_id()
                con.execute("INSERT INTO league_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            [league_id, key.provider.value, key.external_league_id, key.season,
                             clean_name, is_public, data_origin, now])
            else:
                league_id = reg[0]
                # Latest connect wins: a provider can rename a league or change its visibility.
                con.execute("UPDATE league_registry SET name = ?, is_public = ?, data_origin = ? WHERE league_id = ?",
                            [clean_name, is_public, data_origin, league_id])

            if credential_ref is not None:
                existing = con.execute("SELECT user_id, league_id FROM credential_refs WHERE ref_id = ?",
                                       [credential_ref]).fetchone()
                if existing is None:
                    con.execute("INSERT INTO credential_refs VALUES (?, ?, ?, ?, ?, ?, NULL)",
                                [credential_ref, principal.user_id, league_id, key.provider.value,
                                 access_method.value, now])
                elif tuple(existing) != (principal.user_id, league_id):
                    raise PermissionError("That credential reference belongs to someone else.")
                else:
                    con.execute("UPDATE credential_refs SET revoked_at = NULL WHERE ref_id = ?", [credential_ref])

            conn = con.execute("SELECT revoked_at FROM league_connections WHERE user_id = ? AND league_id = ?",
                               [principal.user_id, league_id]).fetchone()
            if conn is None:
                con.execute("INSERT INTO league_connections VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)",
                            [principal.user_id, league_id, Role.OWNER.value, access_method.value,
                             credential_ref, now])
            elif conn[0] is not None:     # reactivate: a fresh start, the old team pick is not carried over
                con.execute("UPDATE league_connections SET role = ?, access_method = ?, credential_ref = ?, "
                            "my_team_external_id = NULL, created_at = ?, revoked_at = NULL "
                            "WHERE user_id = ? AND league_id = ?",
                            [Role.OWNER.value, access_method.value, credential_ref, now,
                             principal.user_id, league_id])
            else:                          # already active: only the way in may change
                con.execute("UPDATE league_connections SET access_method = ?, credential_ref = ? "
                            "WHERE user_id = ? AND league_id = ?",
                            [access_method.value, credential_ref, principal.user_id, league_id])

            con.execute("INSERT INTO league_sync_status VALUES (?, ?, NULL, NULL, NULL) ON CONFLICT DO NOTHING",
                        [league_id, SyncStatus.NEVER.value])
            return self._authorize_in(con, principal, league_id)

    def authorize(self, principal: Principal, league_id: str) -> LeagueContext:
        self._require_principal(principal)
        check_internal_league_id(league_id)
        with self._tx() as con:
            return self._authorize_in(con, principal, league_id)

    def list_contexts(self, principal: Principal) -> list[LeagueContext]:
        if not isinstance(principal, Principal) or not principal.is_authenticated:
            return []
        with self._tx() as con:
            rows = fetch_dicts(con, _CONTEXT_SQL + " ORDER BY r.season DESC, r.name, r.league_id",
                               [principal.user_id])
        caps = self._capabilities()
        out = []
        for row in rows:
            try:
                out.append(self._context_from_row(row, principal, caps))
            except LeagueAccessDenied:
                continue    # one corrupt row must not hide the user's other leagues
        return out

    def context_for_key(self, principal: Principal, key: LeagueKey) -> LeagueContext | None:
        """This principal's active context for a provider league, or None. Used before a (re)connect to
        decide whether a failure may be recorded: only someone already connected may touch a league's
        sync status, so a stranger's failed attempt cannot mark another tenant's league as failed."""
        if not isinstance(principal, Principal) or not principal.is_authenticated:
            return None
        with self._tx() as con:
            rows = fetch_dicts(
                con, _CONTEXT_SQL + " AND r.provider = ? AND r.external_league_id = ? AND r.season = ?",
                [principal.user_id, key.provider.value, key.external_league_id, key.season])
            if not rows:
                return None
            return self._context_from_row(rows[0], principal, self._capabilities())

    def league_meta(self, ctx: LeagueContext) -> LeagueMeta:
        with self._tx() as con:
            self._require_active(con, ctx)
            row = con.execute("SELECT data_origin, is_public FROM league_registry WHERE league_id = ?",
                              [ctx.league_id]).fetchone()
        return LeagueMeta(data_origin=row[0], is_public=row[1])

    # ---- changing a connection ----

    def set_my_team(self, ctx: LeagueContext, external_team_id: str) -> LeagueContext:
        """Pick the team this user analyses. It must exist in the league's stored 'teams' snapshot, so a
        client cannot point the analysis at a team id from another league."""
        if not isinstance(external_team_id, str) or not _TEAM_ID.fullmatch(external_team_id):
            raise ValueError("malformed team id")
        with self._tx() as con:
            self._require_active(con, ctx)
            row = con.execute("SELECT payload FROM league_snapshots WHERE league_id = ? AND kind = 'teams'",
                              [ctx.league_id]).fetchone()
            teams = json.loads(row[0]) if row else []
            if isinstance(teams, dict):
                teams = teams.get("teams", [])
            known = {str(t.get("external_team_id")) for t in teams if isinstance(t, dict)}
            if external_team_id not in known:
                raise ValueError("That team is not part of this league.")
            con.execute("UPDATE league_connections SET my_team_external_id = ? WHERE user_id = ? AND league_id = ?",
                        [external_team_id, ctx.principal.user_id, ctx.league_id])
            return self._authorize_in(con, ctx.principal, ctx.league_id)

    def revoke(self, ctx: LeagueContext) -> None:
        """End this principal's connection and its credential refs. Idempotent. When nobody is connected
        any more the stored snapshots and sync state are dropped too, so provider data (possibly fetched
        with the owner's cookies) does not linger without an authorized reader."""
        if not isinstance(ctx, LeagueContext):
            raise LeagueAccessDenied("unknown league")
        with self._tx() as con:
            now = utcnow()
            uid, lid = ctx.principal.user_id, ctx.league_id
            con.execute("UPDATE league_connections SET revoked_at = ? "
                        "WHERE user_id = ? AND league_id = ? AND revoked_at IS NULL", [now, uid, lid])
            con.execute("UPDATE credential_refs SET revoked_at = ? "
                        "WHERE user_id = ? AND league_id = ? AND revoked_at IS NULL", [now, uid, lid])
            left = con.execute("SELECT count(*) FROM league_connections WHERE league_id = ? AND revoked_at IS NULL",
                               [lid]).fetchone()[0]
            if left == 0:
                con.execute("DELETE FROM league_snapshots WHERE league_id = ?", [lid])
                con.execute("DELETE FROM league_sync_status WHERE league_id = ?", [lid])

    # ---- sync status ----

    def _league_exists(self, con, league_id: str) -> bool:
        return con.execute("SELECT 1 FROM league_registry WHERE league_id = ?", [league_id]).fetchone() is not None

    def _target_id(self, con, target: LeagueContext | str) -> str | None:
        if isinstance(target, LeagueContext):
            self._require_active(con, target)
            return target.league_id
        league_id = check_internal_league_id(target)
        return league_id if self._league_exists(con, league_id) else None

    def record_sync_success(self, target: LeagueContext | str, *, fetched_at: datetime,
                            source_updated_at: datetime | None = None) -> None:
        with self._tx() as con:
            league_id = self._target_id(con, target)
            if league_id is None:
                return
            con.execute("INSERT INTO league_sync_status VALUES (?, 'ok', ?, ?, NULL) ON CONFLICT (league_id) "
                        "DO UPDATE SET status = 'ok', last_synced_at = excluded.last_synced_at, "
                        "source_updated_at = excluded.source_updated_at, last_error = NULL",
                        [league_id, _naive_utc(fetched_at), _naive_utc(source_updated_at)])

    def record_sync_failure(self, target: LeagueContext | str, error: BaseException | str) -> bool:
        """Mark ONE league failed with a sanitized message; the last good sync times are kept. Returns
        False when the league is unknown (nothing is created for it)."""
        message = sanitize_error(error)
        with self._tx() as con:
            league_id = self._target_id(con, target)
            if league_id is None:
                return False
            con.execute("INSERT INTO league_sync_status VALUES (?, 'failed', NULL, NULL, ?) ON CONFLICT (league_id) "
                        "DO UPDATE SET status = 'failed', last_error = excluded.last_error",
                        [league_id, message])
            return True

    # ---- snapshots ----

    def save_snapshot(self, ctx: LeagueContext, kind: str, payload: dict | list, provenance: Provenance) -> None:
        if kind not in SNAPSHOT_KINDS:
            raise ValueError(f"snapshot kind must be one of {SNAPSHOT_KINDS}")
        if not isinstance(payload, (dict, list)):
            raise TypeError("snapshot payload must be a dict or list")
        if provenance.provider is not ctx.key.provider:
            raise ValueError("provenance provider does not match the league's provider")
        body = json.dumps(payload, default=str, allow_nan=False)
        with self._tx() as con:
            self._require_active(con, ctx)
            con.execute(
                "INSERT INTO league_snapshots VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (league_id, kind) DO UPDATE SET "
                "payload = excluded.payload, provider = excluded.provider, source = excluded.source, "
                "fetched_at = excluded.fetched_at, source_updated_at = excluded.source_updated_at",
                [ctx.league_id, kind, body, provenance.provider.value, provenance.source,
                 _naive_utc(provenance.fetched_at), _naive_utc(provenance.source_updated_at)])

    def load_snapshot(self, ctx: LeagueContext, kind: str) -> tuple[dict | list, Provenance] | None:
        if kind not in SNAPSHOT_KINDS:
            raise ValueError(f"snapshot kind must be one of {SNAPSHOT_KINDS}")
        with self._tx() as con:
            self._require_active(con, ctx)
            row = con.execute(
                "SELECT payload, provider, source, fetched_at, source_updated_at FROM league_snapshots "
                "WHERE league_id = ? AND kind = ?", [ctx.league_id, kind]).fetchone()
        if row is None:
            return None
        return json.loads(row[0]), Provenance(Provider(row[1]), row[2], row[3], row[4])
