"""The multi-league site end to end, offline, for the browser parity matrix (multi-league Phase 5; frontend/e2e/
multileague.spec.ts). One process runs everything the API needs and then serves the API:

  * a throwaway Postgres with the control-plane migrations (backend/tests/pg_harness.py);
  * a fake Supabase auth server on AUTH_PORT serving only the JWKS, so the API and the web server verify real
    ES256-signed tokens exactly as in production (no test hook anywhere in the app);
  * three leagues published to a local object directory as production publishes them: an ESPN public league and an
    ESPN private one (the E2E fixture league, scripts/build_e2e_db.py) and a Sleeper league
    (scripts/build_e2e_sleeper.py); the viewer is a member of the private league with team 1 saved;
  * a fourth, public Sleeper league with DL and LB starting slots (build_e2e_sleeper.py --idp), the unsupported case of
    issue #253's journey (frontend/e2e/compat-journey-multileague.spec.ts); no other spec reads it;
  * a public ESPN league whose published data is partial: the E2E league with ESPN's projection missing for every
    other player (partial_copy), the partial-data state of the UX gates (frontend/e2e/ux-multileague.spec.ts);
  * the manifest (OUT/manifest.json): the league ids and the viewer's session cookie for the browser.

    cd backend && uv run python ../scripts/e2e_multileague.py OUT_DIR API_PORT AUTH_PORT

Every number is an invented test input; the signing key exists only in this process.
"""

import base64
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _bootstrap  # noqa: F401

sys.path.insert(0, str(_bootstrap.BACKEND / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import jwt  # noqa: E402
import psycopg  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

KEY = ec.generate_private_key(ec.SECP256R1())
VIEWER = str(uuid.UUID(int=0xE2E))


def partial_copy(src: Path, dest: Path) -> Path:
    """The E2E league with ESPN's weekly projection removed (NULL: unknown, never 0) for every other player, as a sync
    that stored the roster and the schedule but only some projections leaves it: both the weekly points table and the
    lineup snapshots (the projection the matchup shows per slot). Analytics derived at build time are left as built."""
    import shutil

    import duckdb

    shutil.copyfile(src, dest)
    con = duckdb.connect(str(dest))
    try:
        for table in ("espn_player_weekly_points", "lineup_snapshots"):
            con.execute(f"UPDATE {table} SET projected_points = NULL WHERE espn_player_id % 2 = 0")
    finally:
        con.close()
    return dest


def serve_jwks(port: int) -> str:
    jwk = {**jwt.algorithms.ECAlgorithm.to_jwk(KEY.public_key(), as_dict=True), "kid": "e2e", "alg": "ES256",
           "use": "sig"}
    body = json.dumps({"keys": [jwk]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):      # noqa: N802
            if self.path.split("?")[0].endswith("/.well-known/jwks.json"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}"


def session_cookie(url: str, uid: str) -> dict:
    """The Supabase session cookie @supabase/ssr reads (sb-<host>-auth-token = "base64-" + the session JSON)."""
    now = int(time.time())
    access = jwt.encode({"iss": url + "/auth/v1", "sub": uid, "aud": "authenticated", "role": "authenticated",
                         "email": "viewer@example.test", "iat": now, "exp": now + 6 * 3600},
                        KEY, algorithm="ES256", headers={"kid": "e2e"})
    session = {"access_token": access, "token_type": "bearer", "expires_in": 6 * 3600, "expires_at": now + 6 * 3600,
               "refresh_token": "e2e-refresh", "user": {"id": uid, "aud": "authenticated", "role": "authenticated",
                                                        "email": "viewer@example.test"}}
    name = f"sb-{url.split('//', 1)[1].split(':')[0].split('.')[0]}-auth-token"
    value = "base64-" + base64.urlsafe_b64encode(json.dumps(session).encode()).decode().rstrip("=")
    return {"name": name, "value": value}


def main() -> int:
    out, api_port, auth_port = Path(sys.argv[1]).resolve(), int(sys.argv[2]), int(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    import os

    from build_e2e_sleeper import build as build_sleeper
    from cp_seed import seed_league, seed_membership, seed_user
    from golden.golden import build_e2e_db
    from pg_harness import TEST_ROLE_PASSWORD, PgCluster, PgDatabase  # noqa: F401

    from app.db.league_publish import DirWriter, publish_league

    from pg_harness import _find
    if not os.environ.get("FI_TEST_PG_DSN") and not (_find("initdb") and _find("pg_ctl")):
        print("e2e_multileague: needs Postgres: set FI_TEST_PG_DSN or install postgresql (initdb, pg_ctl)",
              file=sys.stderr)
        return 2
    cluster = PgCluster()
    cluster.start()
    db = PgDatabase(cluster, "fi_e2e")
    db.wait_ready()
    db.create()
    db.reset()
    auth_url = serve_jwks(auth_port)

    espn_db = build_e2e_db(out / "e2e.duckdb")
    sleeper_db = out / "e2e-sleeper.duckdb"
    build_sleeper(sleeper_db)
    sleeper_idp_db = out / "e2e-sleeper-idp.duckdb"
    build_sleeper(sleeper_idp_db, idp=True)
    partial_db = partial_copy(espn_db, out / "e2e-partial.duckdb")
    objects = out / "objects"
    leagues = {}
    with db.connect() as su:
        seed_user(su, VIEWER)
        leagues["espn_public"] = seed_league(su, visibility="public", name="Fixture League", external_id="999")
        leagues["espn_private"] = seed_league(su, visibility="private", name="Private Fixture League",
                                              external_id="1999")
        leagues["sleeper"] = seed_league(su, provider="sleeper", visibility="public", name="Sleeper Fixture League",
                                         external_id="1190000000000000001")
        leagues["sleeper_idp"] = seed_league(su, provider="sleeper", visibility="public",
                                             name="Sleeper IDP Fixture League", external_id="1190000000000002531")
        leagues["espn_partial"] = seed_league(su, visibility="public", name="Partial Data League", external_id="4999")
        seed_membership(su, VIEWER, leagues["espn_private"], "viewer")
        su.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, '1')",
                   [VIEWER, leagues["espn_private"]])
        # Sync states for the UX gates (frontend/e2e/ux-multileague.spec.ts), both private with the viewer a member:
        # a league whose sync paused after one publish a week earlier (stale) and one connected but never synced
        # (blank: no snapshot and no job).
        leagues["espn_stale"] = seed_league(su, visibility="private", name="Stale Sync League", external_id="2999",
                                            status="stale")
        leagues["espn_blank"] = seed_league(su, visibility="private", name="Blank Sync League", external_id="3999")
        for kind in ("espn_stale", "espn_blank"):
            seed_membership(su, VIEWER, leagues[kind], "viewer")
        su.commit()
    with psycopg.connect(db.dsn_for("fi_sync"), prepare_threshold=None) as sync:
        for kind, src, stamp in (("espn_public", espn_db, "2026-10-21T12:00:00"),
                                 ("espn_private", espn_db, "2026-10-21T12:00:00"),
                                 ("sleeper", sleeper_db, "2026-10-21T12:00:00"),
                                 ("sleeper_idp", sleeper_idp_db, "2026-10-21T12:00:00"),
                                 ("espn_partial", partial_db, "2026-10-21T12:00:00"),
                                 ("espn_stale", espn_db, "2026-10-14T12:00:00")):
            res = publish_league(src, DirWriter(objects), sync, leagues[kind], stamp=stamp)
            assert res.applied, kind
    # publish_league commits the snapshot at now(); the stale league's control-plane snapshot (what league cards and the
    # switcher show) must be as old as its data, or the "stale" fixture would read as freshly published.
    with db.connect() as su:
        su.execute("UPDATE fi.league_snapshots SET committed_at = %s WHERE league_id = %s",
                   ["2026-10-14T12:00:00Z", leagues["espn_stale"]])
        su.execute("UPDATE fi.league_snapshot_history SET committed_at = %s WHERE league_id = %s",
                   ["2026-10-14T12:00:00Z", leagues["espn_stale"]])
        su.commit()
    (out / "manifest.json").write_text(json.dumps({"leagues": leagues, "cookie": session_cookie(auth_url, VIEWER),
                                                    "auth_url": auth_url}, indent=1))

    os.environ.update({
        "MULTILEAGUE_ENABLED": "1", "SUPABASE_URL": auth_url, "SUPABASE_DB_URL": db.dsn_for("fi_api"),
        "LEAGUE_FILES_DIR": str(objects), "SLEEPER_SOURCE": "live", "ESPN_SEASON": "2026", "NFL_SEASON": "2026",
        "FANTASY_DB_PATH": str(espn_db), "FANTASY_API_KEY": "", "MOTHERDUCK_DATABASE": "", "ESPN_MY_TEAM_ID": "",
        "CLASSIC_SITE_ENABLED": "0",
    })
    import uvicorn
    try:
        uvicorn.run("app.main:app", host="127.0.0.1", port=api_port, log_level="warning")
    finally:
        cluster.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
