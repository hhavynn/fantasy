"""Synthetic onboarding additions to e2e_multileague.py, never imported by the application.

Real server actions, authorization, mutations and snapshots stay in use. Only the two provider readers and drain
dispatch are overridden, so submitting forms cannot contact ESPN, Sleeper or GitHub. Each viewport gets its own user.
"""
import uuid

WIDTHS = (320, 390, 768, 1440)
SWID = "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}"


def private_id(width, *, denied=False):
    return int(f"167{width}{'02' if denied else '01'}")


def seed(db, session_cookie, auth_url, ready_leagues):
    from cp_seed import seed_job, seed_league, seed_membership, seed_user

    users = {}
    with db.connect() as con:
        other = str(uuid.UUID(int=0x167))
        seed_user(con, other)
        for width in WIDTHS:
            uid = str(uuid.UUID(int=0x16700 + width))
            seed_user(con, uid)
            for ready_league in ready_leagues:
                seed_membership(con, uid, ready_league)
            # Joining an existing private league requires an owned team (the first valid-cookie import is a
            # different contract). Seed somebody else's league to exercise the real non-member refusal.
            denied = seed_league(con, visibility="private", name="Other User Private League",
                                 external_id=str(private_id(width, denied=True)))
            seed_membership(con, other, denied, "admin")
            states = {}
            for state, status, error in (
                ("no_data", None, None), ("queued", "queued", None), ("running", "running", None),
                ("queued_stalled", "queued", None), ("running_stalled", "running", None),
                ("failed", "dead", "provider_5xx"), ("empty", "dead", "provider_empty"),
                ("access", "dead", "provider_auth"),
            ):
                lid = seed_league(con, visibility="private", name=f"Onboarding {width} {state}",
                                  external_id=f"167{width}{len(states) + 10}")
                seed_membership(con, uid, lid)
                states[state] = lid
                if status:
                    job = seed_job(con, lid, status=status, error_code=error)
                    if state.endswith("stalled"):
                        con.execute("UPDATE fi.sync_jobs SET run_after = now() - interval '2 hours', "
                                    "started_at = now() - interval '2 hours', attempt = 1 WHERE id = %s", [job])
            users[str(width)] = {"cookie": session_cookie(auth_url, uid), "states": states,
                                 "privateId": str(private_id(width)), "deniedId": str(private_id(width, denied=True))}
        con.commit()
    return {"users": users, "otherCookie": session_cookie(auth_url, other)}


def install(app):
    from app.api import connect
    from app.control_plane.credential_crypto import generate_keypair
    from app.sync import espn_source

    class Espn:
        def probe(self, league_id, season):
            if league_id in {private_id(w, denied=d) for w in WIDTHS for d in (False, True)}:
                return "private"
            return "public" if league_id == 999 else "missing"

        def read_league(self, league_id, season, credential=None, **kwargs):
            # Ownership is derived by the real parser from the synthetic SWID, not granted by this fixture.
            owner = "{11111111-2222-3333-4444-555555555555}" if str(league_id).endswith("02") else SWID
            return "ok", {"id": league_id, "seasonId": season, "settings": {"name": "Synthetic Private Import"},
                          "teams": [{"id": 1, "name": "Synthetic Owner", "owners": [owner]}]}

        league_teams = staticmethod(espn_source.league_teams)

    def sleeper(lid):
        from app.sleeper.client import InvalidLeagueId
        if lid != "1190000000000000001":
            raise InvalidLeagueId("Synthetic missing league")
        return {"league_id": lid, "season": "2026"}

    import os
    _, public = generate_keypair()
    os.environ["CREDENTIALS_PUBKEY_V1"] = public
    app.dependency_overrides[connect.espn] = lambda: Espn()
    app.dependency_overrides[connect.sleeper_reader] = lambda: sleeper
    app.dependency_overrides[connect.drain_dispatcher] = lambda: (lambda: False)
    # The expired-public-league probe uses the direct dispatcher rather than the route dependency.
    connect.dispatch_drain = lambda: False
