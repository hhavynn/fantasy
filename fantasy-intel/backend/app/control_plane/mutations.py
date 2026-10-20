"""Writes the request path may perform, all inside the caller's request transaction (so RLS applies) and all after the
caller authorized the league in that same transaction. Anything RLS cannot express goes through the SECURITY DEFINER
functions of migration 0002; their short error codes are mapped to typed exceptions here."""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid

from psycopg.errors import RaiseException

from ..leagues.context import Principal
from .credential_crypto import EncryptedCredential

TEAM_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{22,43}")


class ControlPlaneError(Exception):
    code = "error"


class SignInRequired(ControlPlaneError):
    code = "sign_in_required"


class NotAdmin(ControlPlaneError):
    code = "not_admin"


class NotAMember(ControlPlaneError):
    code = "not_a_member"


class NotOwner(ControlPlaneError):
    code = "not_owner"


class LastAdmin(ControlPlaneError):
    code = "last_admin"


class CredentialExists(ControlPlaneError):
    code = "credential_exists"


class InviteInvalid(ControlPlaneError):
    code = "invite_invalid"


class LeagueUnavailable(ControlPlaneError):
    code = "league_unavailable"


class LeaguePublic(ControlPlaneError):
    code = "league_public"


class LeagueQuota(ControlPlaneError):
    code = "league_quota"


class NoCredential(ControlPlaneError):
    code = "no_credential"


class SyncPaused(ControlPlaneError):
    code = "sync_paused"


class NotLeagueOwner(ControlPlaneError):
    """Joining an existing private league needs the caller's own cookies to show a team they own."""
    code = "not_a_league_member"


_BY_CODE = {cls.code: cls for cls in (SignInRequired, NotAdmin, NotAMember, NotOwner, LastAdmin, CredentialExists,
                                      InviteInvalid, LeagueUnavailable, LeaguePublic, LeagueQuota, NoCredential,
                                      NotLeagueOwner, SyncPaused)}


def _call(con, sql: str, params=()):
    """Run one SECURITY DEFINER call; translate its RAISE code into a typed error."""
    try:
        return con.execute(sql, params).fetchone()
    except RaiseException as err:
        code = (err.diag.message_primary or "").strip()
        if code in _BY_CODE:
            raise _BY_CODE[code](code) from None
        raise


def _require(principal: Principal) -> str:
    if not isinstance(principal, Principal) or not principal.is_authenticated:
        raise SignInRequired("sign_in_required")
    return principal.user_id  # type: ignore[return-value]


def ensure_app_user(con) -> str:
    return _call(con, "SELECT fi.ensure_app_user()::text")[0]


def me(con, principal: Principal) -> dict:
    uid = _require(principal)
    row = con.execute("SELECT is_platform_admin, created_at FROM fi.app_users WHERE id = %s", [uid]).fetchone()
    if row is None:
        ensure_app_user(con)
        row = con.execute("SELECT is_platform_admin, created_at FROM fi.app_users WHERE id = %s", [uid]).fetchone()
    pins = [r[0] for r in con.execute("SELECT league_id FROM fi.league_pins WHERE user_id = %s ORDER BY created_at", [uid])]
    return {"user_id": uid, "is_platform_admin": bool(row[0]), "created_at": row[1].isoformat(), "pins": pins}


def list_my_leagues(con, principal: Principal) -> list[str]:
    uid = _require(principal)
    rows = con.execute("""
        SELECT l.id FROM fi.leagues l
        WHERE EXISTS (SELECT 1 FROM fi.league_pins p WHERE p.league_id = l.id AND p.user_id = %s)
           OR EXISTS (SELECT 1 FROM fi.league_memberships m WHERE m.league_id = l.id AND m.user_id = %s AND m.revoked_at IS NULL)
        ORDER BY l.season DESC, l.name, l.id""", [uid, uid]).fetchall()
    return [r[0] for r in rows]


def set_pin(con, principal: Principal, league_id: str, pinned: bool) -> None:
    uid = _require(principal)
    ensure_app_user(con)
    if pinned:
        con.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", [uid, league_id])
    else:
        con.execute("DELETE FROM fi.league_pins WHERE user_id = %s AND league_id = %s", [uid, league_id])


def set_perspective(con, principal: Principal, league_id: str, team_external_id: str | None) -> None:
    uid = _require(principal)
    if team_external_id is None:
        con.execute("DELETE FROM fi.team_selections WHERE user_id = %s AND league_id = %s", [uid, league_id])
        return
    if not isinstance(team_external_id, str) or not TEAM_ID.fullmatch(team_external_id):
        raise ValueError("malformed team id")
    ensure_app_user(con)
    con.execute("""INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, league_id) DO UPDATE SET team_external_id = EXCLUDED.team_external_id, updated_at = now()""",
                [uid, league_id, team_external_id])


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def create_invite(con, principal: Principal, league_id: str, ttl_days: int = 7) -> tuple[str, str]:
    _require(principal)
    if not 1 <= int(ttl_days) <= 7:
        raise ValueError("ttl_days must be 1..7")
    token = secrets.token_urlsafe(16)
    row = _call(con, "SELECT fi.create_invite(%s, %s, make_interval(days => %s))::text",
                [league_id, _hash(token), int(ttl_days)])
    return row[0], token


def revoke_invite(con, principal: Principal, invite_id: str) -> None:
    _require(principal)
    _call(con, "SELECT fi.revoke_invite(%s)", [str(uuid.UUID(str(invite_id)))])


def redeem_invite(con, principal: Principal, token: str) -> str:
    _require(principal)
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise InviteInvalid("invite_invalid")
    return _call(con, "SELECT fi.redeem_invite(%s)", [_hash(token)])[0]


def promote_admin(con, principal: Principal, league_id: str, user_id: str) -> None:
    _require(principal)
    _call(con, "SELECT fi.promote_admin(%s, %s)", [league_id, str(uuid.UUID(str(user_id)))])


def demote_admin(con, principal: Principal, league_id: str, user_id: str) -> None:
    _require(principal)
    _call(con, "SELECT fi.demote_admin(%s, %s)", [league_id, str(uuid.UUID(str(user_id)))])


def store_credential(con, principal: Principal, league_id: str, enc: EncryptedCredential, credential_id: str) -> str:
    _require(principal)
    if not isinstance(enc, EncryptedCredential):
        raise TypeError("store_credential needs an EncryptedCredential")
    row = _call(con, "SELECT fi.store_credential(%s, %s, %s, %s, %s, %s)::text",
                [str(uuid.UUID(str(credential_id))), league_id, enc.ciphertext, enc.nonce, enc.wrapped_data_key,
                 enc.wrap_key_version])
    return row[0]


def revoke_credential(con, principal: Principal, league_id: str) -> None:
    _require(principal)
    _call(con, "SELECT fi.revoke_credential(%s)", [league_id])


# ---- Phase 3b: registering leagues and asking for syncs (supabase/migrations/20261021130000_connect_flows.sql) ----

def import_public_league(con, principal: Principal, provider: str, external_id: str, season: int) -> str:
    """Register (once) a league the API has just read without cookies, pin it, queue its first sync."""
    _require(principal)
    return _call(con, "SELECT fi.import_public_league(%s, %s, %s, NULL)", [provider, external_id, int(season)])[0]


def connect_private_league(con, principal: Principal, provider: str, external_id: str, season: int, name: str | None,
                           owns_team: bool) -> str:
    _require(principal)
    return _call(con, "SELECT fi.connect_private_league(%s, %s, %s, %s, %s)",
                 [provider, external_id, int(season), name, bool(owns_team)])[0]


def start_private_sync(con, principal: Principal, league_id: str) -> None:
    _require(principal)
    _call(con, "SELECT fi.start_private_sync(%s)", [league_id])


def request_sync(con, principal: Principal, league_id: str) -> str | None:
    _require(principal)
    return _call(con, "SELECT fi.request_sync(%s)::text", [league_id])[0]


def delete_league(con, principal: Principal, league_id: str) -> None:
    _require(principal)
    _call(con, "SELECT fi.delete_league(%s)", [league_id])


def request_probe(con, league_id: str) -> bool:
    """Any caller, signed in or not; the answer is for the API only (never sent to the client)."""
    return bool(_call(con, "SELECT fi.request_probe(%s)", [league_id])[0])


def list_invites(con, principal: Principal, league_id: str) -> list[dict]:
    """The league's open invites (admins only, by RLS). Token hashes are never selected."""
    _require(principal)
    rows = con.execute("""SELECT id::text, created_at, expires_at FROM fi.league_invites
                          WHERE league_id = %s AND redeemed_at IS NULL AND revoked_at IS NULL AND expires_at > now()
                          ORDER BY created_at DESC""", [league_id]).fetchall()
    return [{"invite_id": r[0], "created_at": r[1].isoformat(), "expires_at": r[2].isoformat()} for r in rows]
