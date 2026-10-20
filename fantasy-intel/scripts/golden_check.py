"""Local check that a real league answers identically through the legacy path and the per-league path (Phase 2).

    cd backend && uv run python ../scripts/golden_check.py [path/to/fantasy.duckdb]

Copies the database (default: FANTASY_DB_PATH), starts a throwaway Postgres with the control-plane migrations,
publishes the copy as a private league into a temporary object directory, makes a test user a member whose saved
team is the database's own team (`is_mine`), then requests every legacy GET case both ways and compares them with
the same masking as the committed goldens. Prints counts and the names of differing cases, never response bodies
(the data is private). Needs `initdb`/`pg_ctl` (brew install postgresql). Exit 0 when every case matches.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401

sys.path.insert(0, str(_bootstrap.BACKEND / "tests"))


def main() -> int:
    from app.config import get_settings
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else get_settings().db_path
    if not src.exists():
        print(f"no database at {src}", file=sys.stderr)
        return 2
    import duckdb
    from fastapi.testclient import TestClient

    from cp_seed import U1, seed_membership, seed_user
    from golden.golden import cases, response_text
    from league_fixtures import publish_local
    from pg_harness import PgCluster, PgDatabase
    from test_leagues_api import URL, _jwks_client, bearer

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = tmp / "league.duckdb"
        shutil.copyfile(src, db)                       # never hold a lock on the live file
        con = duckdb.connect(str(db), read_only=True)
        mine = con.execute("SELECT team_id FROM fantasy_teams WHERE is_mine").fetchone()
        season = con.execute("SELECT season FROM leagues").fetchone()[0]
        con.close()
        cluster = PgCluster()
        cluster.start()
        try:
            pg = PgDatabase(cluster, name="fi_golden")
            pg.wait_ready()
            pg.create()
            pub = publish_local(pg, db, tmp / "objects", tmp / "work", visibility="private", name="League")
            with pg.connect() as c:
                seed_user(c, U1)
                seed_membership(c, U1, pub.league_id, "admin")
                if mine:
                    c.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, %s)",
                              [U1, pub.league_id, str(mine[0])])
                c.commit()
            os.environ.update(FANTASY_DB_PATH=str(db), ESPN_SEASON=str(season), FANTASY_API_KEY="",
                              MULTILEAGUE_ENABLED="1", SUPABASE_URL=URL, SUPABASE_DB_URL=pg.dsn_for("fi_api"),
                              LEAGUE_FILES_DIR=str(tmp / "objects"), SNAPSHOT_BUCKET="", SERVE_SNAPSHOT="")
            import app.control_plane.jwt_auth as jwt_auth
            client_jwks = _jwks_client()
            jwt_auth.jwks_client = lambda url: client_jwks
            import tempfile as tf
            tf.tempdir = str(tmp / "cache")
            (tmp / "cache").mkdir()
            from app.main import app
            client = TestClient(app, raise_server_exceptions=False)
            con = duckdb.connect(str(db), read_only=True)
            try:
                cs = cases(app, con, client)
            finally:
                con.close()
            headers = bearer(U1)

            class Scoped:
                def get(self, path):
                    return client.get(f"/v2/leagues/{pub.league_id}{path}", headers=headers)

            differ = [c.name for c in cs if response_text(client, c) != response_text(Scoped(), c)]
        finally:
            cluster.stop()
    print(f"{len(cs)} cases compared; {len(cs) - len(differ)} identical; {len(differ)} differ")
    for name in differ:
        print(f"  differs: {name}")
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main())
