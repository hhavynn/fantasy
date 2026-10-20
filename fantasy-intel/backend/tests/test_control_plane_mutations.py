"""The request path's writes: preferences through RLS, everything else through the SECURITY DEFINER wrappers."""
import psycopg
import pytest

from app.control_plane import mutations as m
from app.control_plane.authz import authorize_league_in
from app.control_plane.credential_crypto import PublicKeyring, generate_keypair
from app.control_plane.pg import ControlPlane
from app.leagues.context import Principal
from cp_seed import U1, U2, U3, seed_auth_user, seed_league, seed_membership, seed_user

ANON = Principal.anonymous()


def user(uid):
    return Principal.from_claims({"sub": uid, "role": "authenticated"})


@pytest.fixture
def plane(pg):
    p = ControlPlane(pg.dsn_for("fi_api"), max_size=2)
    yield p
    p.close()


@pytest.fixture
def leagues(pg):
    with pg.connect() as con:
        for u in (U1, U2):
            seed_user(con, u)
        seed_auth_user(con, U3)                 # an account that has not used the app yet
        pub = seed_league(con, visibility="public")
        priv = seed_league(con, visibility="private", checked_at=None)
        seed_membership(con, U1, priv, "admin")
        seed_membership(con, U2, priv, "viewer")
        con.commit()
    return pub, priv


def test_ensure_app_user_and_me(plane, leagues):
    with plane.request_tx(user(U3)) as con:
        assert m.ensure_app_user(con) == U3
        profile = m.me(con, user(U3))
        assert profile == {"user_id": U3, "is_platform_admin": False, "pins": [], "created_at": profile["created_at"]}
    with plane.request_tx(ANON) as con, pytest.raises(psycopg.errors.InsufficientPrivilege):
        m.ensure_app_user(con)            # anon cannot even execute the function: stronger than a sign-in error
    with plane.request_tx(ANON) as con, pytest.raises(m.SignInRequired):
        m.me(con, ANON)


def test_pins_and_perspective_are_per_user_and_need_a_visible_league(plane, leagues):
    pub, priv = leagues
    with plane.request_tx(user(U1)) as con:
        m.ensure_app_user(con)
        m.set_pin(con, user(U1), pub, True)
        m.set_pin(con, user(U1), pub, True)        # idempotent
        m.set_perspective(con, user(U1), pub, "6")
        m.set_perspective(con, user(U1), pub, "7")  # upsert
        assert sorted(m.list_my_leagues(con, user(U1))) == sorted([pub, priv])
        a = authorize_league_in(con, user(U1), pub)
        assert a.pinned and a.perspective_team_id == "7"
    with plane.request_tx(user(U2)) as con:
        assert authorize_league_in(con, user(U2), pub).perspective_team_id is None
        with pytest.raises(ValueError):
            m.set_perspective(con, user(U2), pub, "6; DROP")
    with plane.request_tx(user(U1)) as con:
        m.set_pin(con, user(U1), pub, False)
        m.set_perspective(con, user(U1), pub, None)
        a = authorize_league_in(con, user(U1), pub)
        assert not a.pinned and a.perspective_team_id is None
    with plane.request_tx(ANON) as con, pytest.raises(m.SignInRequired):
        m.set_pin(con, ANON, pub, True)
    with plane.request_tx(ANON) as con, pytest.raises(m.SignInRequired):
        m.set_perspective(con, ANON, pub, "6")


def test_touch_league_only_moves_last_viewed_at(pg, plane, leagues):
    pub, priv = leagues
    with plane.request_tx(ANON) as con:
        m.touch_league(con, pub)
        m.touch_league(con, priv)        # the private one is invisible: silently no-op
    with pg.connect() as su:
        rows = dict(su.execute("SELECT id, last_viewed_at IS NOT NULL FROM fi.leagues").fetchall())
    assert rows[pub] is True and rows[priv] is False


def test_invite_flow_through_the_wrappers(plane, leagues):
    _, priv = leagues
    with plane.request_tx(user(U1)) as con:
        invite_id, token = m.create_invite(con, user(U1), priv)
        assert len(token) >= 22 and token.isprintable() and len(invite_id) == 36
    with plane.request_tx(user(U2)) as con, pytest.raises(m.NotAdmin):
        m.create_invite(con, user(U2), priv)
    with plane.request_tx(user(U1)) as con, pytest.raises(ValueError):
        m.create_invite(con, user(U1), priv, ttl_days=8)
    with plane.request_tx(user(U3)) as con:
        assert m.redeem_invite(con, user(U3), token) == priv
        assert authorize_league_in(con, user(U3), priv).role == "viewer"
    with plane.request_tx(user(U3)) as con, pytest.raises(m.InviteInvalid):
        m.redeem_invite(con, user(U3), token)
    with plane.request_tx(user(U3)) as con, pytest.raises(m.InviteInvalid):
        m.redeem_invite(con, user(U3), "not a token")
    with plane.request_tx(ANON) as con, pytest.raises(m.SignInRequired):
        m.redeem_invite(con, ANON, token)
    with plane.request_tx(user(U1)) as con:
        invite_id, token2 = m.create_invite(con, user(U1), priv)
        m.revoke_invite(con, user(U1), invite_id)
    with plane.request_tx(user(U3)) as con, pytest.raises(m.InviteInvalid):
        m.redeem_invite(con, user(U3), token2)


def test_admin_wrappers_map_errors(plane, leagues):
    _, priv = leagues
    with plane.request_tx(user(U1)) as con, pytest.raises(m.LastAdmin):
        m.demote_admin(con, user(U1), priv, U1)
    with plane.request_tx(user(U1)) as con, pytest.raises(m.NotAMember):
        m.promote_admin(con, user(U1), priv, U3)
    with plane.request_tx(user(U1)) as con:
        m.promote_admin(con, user(U1), priv, U2)
    with plane.request_tx(user(U2)) as con:
        m.demote_admin(con, user(U2), priv, U1)
    with plane.request_tx(user(U1)) as con, pytest.raises(m.NotAdmin):
        m.promote_admin(con, user(U1), priv, U1)


def test_credential_wrappers(plane, leagues):
    _, priv = leagues
    pub = PublicKeyring({"v1": generate_keypair()[1]})
    cid = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"
    enc = pub.encrypt({"espn_s2": "AEBx" + "y" * 60, "swid": "{11111111-2222-4333-8444-555555555555}"},
                      league_id=priv, credential_id=cid, owner_user_id=U2)
    with plane.request_tx(user(U3)) as con, pytest.raises(m.NotAMember):
        m.store_credential(con, user(U3), priv, enc, cid)
    with plane.request_tx(user(U2)) as con:
        assert m.store_credential(con, user(U2), priv, enc, cid) == cid
    with plane.request_tx(user(U1)) as con, pytest.raises(m.CredentialExists):
        m.store_credential(con, user(U1), priv, enc, "1f1f1f1f-1f1f-4f1f-8f1f-1f1f1f1f1f1f")
    with plane.request_tx(user(U1)) as con, pytest.raises(m.NotOwner):
        m.revoke_credential(con, user(U1), priv)
    with plane.request_tx(user(U2)) as con:
        m.revoke_credential(con, user(U2), priv)
        assert authorize_league_in(con, user(U2), priv).status == "stale"
    with plane.request_tx(user(U2)) as con, pytest.raises(TypeError):
        m.store_credential(con, user(U2), priv, {"ciphertext": b"x"}, cid)
