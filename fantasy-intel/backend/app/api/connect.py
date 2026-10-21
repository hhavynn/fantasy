"""Connect flows under /v2 (Phase 3b): import a public ESPN league, connect a private one with the caller's cookies,
invites, the caller's credential, manual refresh, admin delete. Behind MULTILEAGUE_CONNECT_ENABLED (and
MULTILEAGUE_ENABLED): with either off every route here is a plain 404.

Every league route authorizes first (leagues_v2.league_tx). Cookies:
  * are read from the request body by `secret_body`, which never echoes input in an error (FastAPI's 422 would);
  * are checked with one GET of the league's own ESPN endpoint, then encrypted to the credential public key
    (the API cannot decrypt them) and stored through fi.store_credential;
  * are never logged, returned or kept in an exception message.
After the request transaction committed work, a background task starts the sync drain with a fine-grained token
that can only dispatch Actions (the job row exists whatever that call does).
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from ..config import get_settings
from .leagues_v2 import LEAGUE_LIMIT, NOT_FOUND, REQUEST_TX, SIGN_IN, SIGNUPS_CLOSED, _json, _no_content, _signed_in, league_tx, require_enabled

log = logging.getLogger(__name__)
REPO = "hhavynn/fantasy"
WORKFLOW = "sync-drain.yml"
CONSENT = ("I understand these cookies give access to my ESPN account. They are stored encrypted, used only to read "
           "this league, and stop working when I sign out of ESPN everywhere.")
BAD_BODY = "The request body is not valid."
ESPN_UNREACHABLE = "ESPN could not be reached. Try again in a few minutes."


def require_connect_enabled() -> None:
    if not get_settings().multileague_connect_enabled:
        raise HTTPException(404, "Not Found")


router = APIRouter(prefix="/v2", dependencies=[Depends(require_enabled), Depends(require_connect_enabled)])


# ---- input ------------------------------------------------------------------------------------------

async def secret_body(request: Request) -> dict:
    """The JSON object body; a malformed one is a fixed 422 that never repeats what was sent."""
    try:
        body = await request.json()
    except Exception:   # noqa: BLE001 - any decoding problem is the same answer
        raise HTTPException(422, BAD_BODY) from None
    if not isinstance(body, dict):
        raise HTTPException(422, BAD_BODY)
    return body


_DIGITS = re.compile(r"[0-9]{1,12}")
_COOKIE = re.compile(r"[A-Za-z0-9%+/=._-]{20,2000}")
_SWID = re.compile(r"\{?[0-9A-Fa-f-]{36}\}?")


def parse_league(value: Any, season: Any, default_season: int) -> tuple[str, int]:
    """A league id or an ESPN league URL (…?leagueId=123&seasonId=2026), and the season."""
    text = str(value or "").strip()
    url_season = None
    if not _DIGITS.fullmatch(text):
        q = parse_qs(urlparse(text).query)
        text = (q.get("leagueId") or [""])[0]
        url_season = (q.get("seasonId") or [None])[0]
    if not _DIGITS.fullmatch(text):
        raise HTTPException(422, "Enter an ESPN league id or the league's URL.")
    raw = season if season not in (None, "") else url_season
    try:
        year = int(raw) if raw not in (None, "") else int(default_season)
    except (TypeError, ValueError):
        raise HTTPException(422, "The season must be a year.") from None
    if not 1990 <= year <= 2100:
        raise HTTPException(422, "The season must be a year.")
    return text.lstrip("0") or "0", year


def parse_cookies(body: dict) -> dict:
    if body.get("consent") is not True:
        raise HTTPException(422, "Confirm that you understand what the cookies give access to.")
    s2, swid = body.get("espn_s2"), body.get("swid")
    if not isinstance(s2, str) or not _COOKIE.fullmatch(s2.strip()) or not isinstance(swid, str) \
            or not _SWID.fullmatch(swid.strip()):
        raise HTTPException(422, "Paste both ESPN cookies: espn_s2 and SWID.")
    swid = swid.strip()
    return {"espn_s2": s2.strip(), "swid": swid if swid.startswith("{") else "{" + swid.strip("{}") + "}"}


# ---- side effects (overridden in tests) -------------------------------------------------------------

def espn():
    """The ESPN reads this module uses; tests override it through app.dependency_overrides."""
    from ..sync import espn_source
    return espn_source


def dispatch_drain() -> bool:
    """Start the sync drain now (workflow_dispatch). Best effort: the scheduled drain picks the job up anyway."""
    import requests
    token = get_settings().github_dispatch_token
    if not token:
        return False
    try:
        r = requests.post(f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
                          json={"ref": "main"}, timeout=5,
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"})
    except requests.RequestException as err:
        log.warning("sync drain dispatch failed: %s", type(err).__name__)
        return False
    if r.status_code != 204:
        log.warning("sync drain dispatch answered HTTP %s", r.status_code)
    return r.status_code == 204


def drain_dispatcher():
    return dispatch_drain


def _keyring():
    from ..control_plane.credential_crypto import PublicKeyring
    keys = get_settings().credential_public_keys
    if not keys:
        raise HTTPException(503, "Private leagues are not available in this deployment.")
    return PublicKeyring(keys)


def _mutation_error(err) -> HTTPException:
    from ..control_plane import mutations as m
    status, detail = {
        m.SignInRequired: (401, SIGN_IN),
        m.NotAdmin: (403, "Only a league admin can do that."),
        m.NotAMember: (404, NOT_FOUND),
        m.NotOwner: (403, "Only the person who connected these cookies can remove them."),
        m.CredentialExists: (409, "Another member's cookies already sync this league."),
        m.LeagueUnavailable: (404, NOT_FOUND),
        m.LeaguePublic: (409, "This league is public on ESPN: import it without cookies."),
        m.LeagueQuota: (429, "You have added too many leagues today. Try again tomorrow."),
        m.NoCredential: (409, "Store your ESPN cookies first."),
        m.NotLeagueOwner: (403, "Your ESPN account does not own a team in this league. Ask a member for an invite."),
        m.InviteInvalid: (404, "This invite is not valid (used, expired or revoked)."),
        m.LastAdmin: (409, "A league needs at least one admin."),
        m.SignupsClosed: (403, SIGNUPS_CLOSED),
        m.LeagueLimit: (409, LEAGUE_LIMIT),
        m.SyncPaused: (409, "Syncing is paused for this league: its ESPN cookies stopped working or ESPN no longer has "
                            "it. A member can store fresh cookies in the league's settings."),
    }.get(type(err), (400, "That could not be done."))
    return HTTPException(status, detail)


def _mutate(fn):
    from ..control_plane import mutations as m
    try:
        return fn()
    except m.ControlPlaneError as err:
        raise _mutation_error(err) from None


# ---- import / connect -------------------------------------------------------------------------------

_SLEEPER_URL = re.compile(r"sleeper\.(?:com|app)/leagues/(\d{6,20})(?:/|$|\?)")
SLEEPER_UNAVAILABLE = "Sleeper leagues are temporarily unavailable."


def parse_sleeper_league(value: Any) -> str:
    """A Sleeper league id (the long number) or a sleeper.com/leagues/<id>/… address."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{6,20}", text):
        return text
    m = _SLEEPER_URL.search(text)
    if m is None:
        raise HTTPException(422, "Enter a Sleeper league id or the league's sleeper.com address.")
    return m[1]


def sleeper_reader():
    """league id -> Sleeper's league record (one GET; raises InvalidLeagueId when Sleeper has no NFL league)."""
    import tempfile

    from ..sleeper.client import SleeperClient
    return lambda lid: SleeperClient(cache_dir=Path(tempfile.gettempdir()) / "fi-sleeper", timeout=10.0).league(lid)


def _import_sleeper(background, body: dict, con, principal, read, dispatch):
    from ..control_plane import mutations
    from ..sleeper.client import InvalidLeagueId, SleeperError
    if get_settings().sleeper_source != "live":
        raise HTTPException(503, SLEEPER_UNAVAILABLE)
    lid = parse_sleeper_league(body.get("league"))
    try:
        league = read(lid)
    except InvalidLeagueId:
        raise HTTPException(404, "Sleeper has no NFL league with that id.") from None
    except (SleeperError, OSError):
        raise HTTPException(503, "Sleeper could not be reached. Try again in a minute.") from None
    season = int(league.get("season") or get_settings().season)
    league_id = _mutate(lambda: mutations.import_public_league(con, principal, "sleeper", lid, season))
    background.add_task(dispatch)
    return _json({"league_id": league_id, "visibility": "public", "provider": "sleeper"}, 201)


@router.post("/leagues/import", status_code=201)
def import_league(background: BackgroundTasks, body: dict = Depends(secret_body), tx=REQUEST_TX,
                  source=Depends(espn), dispatch=Depends(drain_dispatcher), sleeper=Depends(sleeper_reader)):
    """A public league: read it once without cookies, register it, pin it, queue its first sync. `provider`:
    "espn" (default) or "sleeper" (always public; needs SLEEPER_SOURCE=live)."""
    from ..control_plane import mutations
    con, principal = tx
    if not principal.is_authenticated:
        raise HTTPException(401, SIGN_IN)
    provider = str(body.get("provider") or "espn").strip().lower()
    if provider == "sleeper":
        return _import_sleeper(background, body, con, principal, sleeper, dispatch)
    if provider != "espn":
        raise HTTPException(422, "The provider must be espn or sleeper.")
    external_id, season = parse_league(body.get("league"), body.get("season"), get_settings().season)
    verdict = source.probe(int(external_id), season)
    if verdict == "private":
        return _json({"detail": "This league is private on ESPN. Connect it with your ESPN cookies.",
                      "needs_cookies": True}, 409)
    if verdict == "missing":
        raise HTTPException(404, "ESPN has no league with that id for that season.")
    if verdict != "public":
        raise HTTPException(503, ESPN_UNREACHABLE)
    league_id = _mutate(lambda: mutations.import_public_league(con, principal, "espn", external_id, season))
    background.add_task(dispatch)
    return _json({"league_id": league_id, "visibility": "public"}, 201)


@router.post("/leagues/connect", status_code=201)
def connect_league(background: BackgroundTasks, body: dict = Depends(secret_body), tx=REQUEST_TX,
                   source=Depends(espn), dispatch=Depends(drain_dispatcher)):
    """A private ESPN league with the caller's cookies (consent required). A league that turns out to be public is
    imported instead and the cookies are dropped unused."""
    from ..control_plane import mutations
    con, principal = tx
    if not principal.is_authenticated:
        raise HTTPException(401, SIGN_IN)
    external_id, season = parse_league(body.get("league"), body.get("season"), get_settings().season)
    cookies = parse_cookies(body)
    anonymous = source.probe(int(external_id), season)
    if anonymous == "public":
        league_id = _mutate(lambda: mutations.import_public_league(con, principal, "espn", external_id, season))
        background.add_task(dispatch)
        return _json({"league_id": league_id, "visibility": "public", "cookies": "not_needed"}, 201)
    if anonymous == "unknown":
        raise HTTPException(503, ESPN_UNREACHABLE)
    verdict, payload = source.read_league(int(external_id), season, cookies, views=("mTeam", "mSettings"))
    if verdict == "private":
        raise HTTPException(400, "ESPN did not accept these cookies for this league.")
    if verdict == "missing":
        raise HTTPException(404, "ESPN has no league with that id for that season.")
    if verdict != "ok":
        raise HTTPException(503, ESPN_UNREACHABLE)
    teams = source.league_teams(payload, cookies["swid"])
    owned = next((t["team_id"] for t in teams if t["owned"]), None)
    name = ((payload.get("settings") or {}).get("name") or None)
    keyring = _keyring()
    league_id = _mutate(lambda: mutations.connect_private_league(con, principal, "espn", external_id, season, name,
                                                                 owned is not None))
    credential = "stored"
    try:
        with con.transaction():                     # a savepoint: a member joining an already-synced league
            cid = str(uuid.uuid4())
            enc = keyring.encrypt(cookies, league_id=league_id, credential_id=cid, owner_user_id=principal.user_id)
            mutations.store_credential(con, principal, league_id, enc, cid)
            mutations.start_private_sync(con, principal, league_id)
    except mutations.CredentialExists:
        credential = "existing"                     # never replaces another member's live credential
    except mutations.ControlPlaneError as err:
        raise _mutation_error(err) from None
    finally:
        cookies.clear()
    if owned is not None:
        _mutate(lambda: mutations.set_perspective(con, principal, league_id, owned))
    if credential == "stored":
        background.add_task(dispatch)
    return _json({"league_id": league_id, "visibility": "private", "cookies": credential, "team_id": owned,
                  "teams": [{"team_id": t["team_id"], "name": t["name"]} for t in teams]}, 201)


# ---- the caller's credential --------------------------------------------------------------------------

def signed_in_league(ltx=Depends(league_tx, scope="function")):
    """league_tx, then sign-in: an unknown league is 404 and an anonymous caller 401 before any body is read."""
    con, access = ltx
    return con, access, _signed_in(access)


@router.put("/leagues/{league_id}/credential")
def replace_credential(background: BackgroundTasks, sltx=Depends(signed_in_league),
                       body: dict = Depends(secret_body), source=Depends(espn),
                       dispatch=Depends(drain_dispatcher)):
    """Attach or replace the caller's own cookies (a member; only when no one else's credential is live)."""
    from ..control_plane import mutations
    con, access, principal = sltx
    cookies = parse_cookies(body)
    row = con.execute("SELECT external_id, season FROM fi.leagues WHERE id = %s", [access.league_id]).fetchone()
    verdict, _ = source.read_league(int(row[0]), row[1], cookies)
    if verdict == "private":
        raise HTTPException(400, "ESPN did not accept these cookies for this league.")
    if verdict != "ok":
        raise HTTPException(503, ESPN_UNREACHABLE)
    keyring = _keyring()
    try:
        cid = str(uuid.uuid4())
        enc = keyring.encrypt(cookies, league_id=access.league_id, credential_id=cid, owner_user_id=principal.user_id)
        _mutate(lambda: mutations.store_credential(con, principal, access.league_id, enc, cid))
        queued = _mutate(lambda: mutations.request_sync(con, principal, access.league_id))
    finally:
        cookies.clear()
    if queued:
        background.add_task(dispatch)
    return _json({"cookies": "stored"})


@router.delete("/leagues/{league_id}/credential", status_code=204)
def remove_credential(ltx=Depends(league_tx, scope="function")):
    from ..control_plane import mutations
    con, access = ltx
    principal = _signed_in(access)
    _mutate(lambda: mutations.revoke_credential(con, principal, access.league_id))
    return _no_content()


# ---- invites ----------------------------------------------------------------------------------------------

@router.get("/leagues/{league_id}/invites")
def invites(ltx=Depends(league_tx, scope="function")):
    from ..control_plane import mutations
    con, access = ltx
    principal = _signed_in(access)
    if access.role != "admin":
        raise HTTPException(403, "Only a league admin can do that.")
    return _json({"invites": mutations.list_invites(con, principal, access.league_id)})


@router.post("/leagues/{league_id}/invites", status_code=201)
def create_invite(ltx=Depends(league_tx, scope="function")):
    """A single-use, 7-day viewer invite. The token is returned once and only its hash is stored."""
    from ..control_plane import mutations
    con, access = ltx
    principal = _signed_in(access)
    invite_id, token = _mutate(lambda: mutations.create_invite(con, principal, access.league_id))
    return _json({"invite_id": invite_id, "token": token}, 201)


@router.delete("/leagues/{league_id}/invites/{invite_id}", status_code=204)
def revoke_invite(invite_id: uuid.UUID, ltx=Depends(league_tx, scope="function")):
    from ..control_plane import mutations
    con, access = ltx
    principal = _signed_in(access)
    _mutate(lambda: mutations.revoke_invite(con, principal, str(invite_id)))
    return _no_content()


@router.post("/invites/redeem")
def redeem_invite(body: dict = Depends(secret_body), tx=REQUEST_TX):
    from ..control_plane import mutations
    con, principal = tx
    if not principal.is_authenticated:
        raise HTTPException(401, SIGN_IN)
    league_id = _mutate(lambda: mutations.redeem_invite(con, principal, body.get("token")))
    return _json({"league_id": league_id})


# ---- refresh and delete ------------------------------------------------------------------------------------

@router.post("/leagues/{league_id}/sync", status_code=202)
def refresh(background: BackgroundTasks, ltx=Depends(league_tx, scope="function"),
            dispatch=Depends(drain_dispatcher)):
    from ..control_plane import mutations
    con, access = ltx
    principal = _signed_in(access)
    queued = _mutate(lambda: mutations.request_sync(con, principal, access.league_id))
    if queued:
        background.add_task(dispatch)
    return _json({"queued": queued is not None}, 202)


@router.delete("/leagues/{league_id}", status_code=204)
def delete(ltx=Depends(league_tx, scope="function")):
    from ..control_plane import mutations
    con, access = ltx
    principal = _signed_in(access)
    _mutate(lambda: mutations.delete_league(con, principal, access.league_id))
    return _no_content()


def nudge_probe(principal, league_id: str) -> None:
    """Spec §2: a 404 for a public league whose visibility check expired starts a probe (rate-limited in Postgres:
    one per league per 5 minutes). Its own short transaction (the request's own rolls back with the 404); never
    changes the answer, never raises. Called from the thread pool (league_tx, league_scope)."""
    settings = get_settings()
    if not (settings.multileague_connect_enabled and settings.supabase_db_url):
        return
    from ..control_plane import mutations
    from ..control_plane.pg import get_control_plane
    try:
        with get_control_plane().request_tx(principal) as con:
            queued = mutations.request_probe(con, league_id)
    except Exception as err:   # noqa: BLE001 - best effort; the caller gets its 404 regardless
        log.warning("probe request failed: %s", type(err).__name__)
        return
    if queued:
        dispatch_drain()
