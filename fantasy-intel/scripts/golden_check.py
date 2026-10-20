"""Local check that a real league answers identically through the legacy path and the per-league path (Phase 2).

    cd backend && uv run python ../scripts/golden_check.py [path/to/fantasy.duckdb] [--job-path]

Copies the database (default: FANTASY_DB_PATH), starts a throwaway Postgres with the control-plane migrations,
publishes the copy as a private league into a temporary object directory, makes a test user a member whose saved
team is the database's own team (`is_mine`), then requests every legacy GET case both ways and compares them with
the same masking as the committed goldens. Prints counts and the names of differing cases, never response bodies
(the data is private). Needs `initdb`/`pg_ctl` (brew install postgresql). Exit 0 when every case matches.

--job-path (Phase 3): the per-league side is built the way a sync job builds it. The league is fetched from ESPN once
(with this laptop's .env cookies), then the same payloads are applied twice: the laptop way (write + analytics into
the database copy, served by the legacy routes) and the job way (app/sync/build.py on the published global file and
the previous league file, member SWIDs scrubbed, published with a fence and served by /v2).
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401

sys.path.insert(0, str(_bootstrap.BACKEND / "tests"))


def job_build(pg, pub, db: Path, tmp: Path, settings, season: int) -> None:
    """Refresh `db` the laptop way and publish a job-built version of the same league from the same payloads."""
    import copy

    import psycopg

    from app.analytics.pipeline import run_analytics
    from app.db.connection import open_db, utcnow
    from app.db.league_publish import DirWriter, commit_league
    from app.espn.sync import fetch_all, write_synced
    from app.sync import espn_source
    from app.sync.build import build_league_file
    cred = {"espn_s2": settings.espn_s2, "swid": settings.swid} if settings.has_espn_credentials else None
    with espn_source.espn_client(settings.league_id, season, cred) as client:
        payloads = fetch_all(client)
    with open_db(db) as con:
        write_synced(con, copy.deepcopy(payloads), swid=settings.swid, my_team_id=settings.my_team_id)
        run_analytics(con, season)
    objects = tmp / "objects"
    work = tmp / "job"
    work.mkdir()
    built = build_league_file(work, global_path=objects / "global" / f"{pub.global_version}.duckdb",
                              previous=objects / "leagues" / pub.league_id / f"{pub.data_version}.duckdb",
                              payloads=payloads, owner_swid=settings.swid,
                              stamp=utcnow().isoformat(timespec="seconds"))
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as con:
        fence = con.execute("UPDATE fi.leagues SET sync_fence = sync_fence + 1 WHERE id = %s RETURNING sync_fence",
                            [pub.league_id]).fetchone()[0]
        con.commit()
        applied, _ = commit_league(DirWriter(objects), con, pub.league_id, built.league_path, built.league_version,
                                   pub.global_version, fence)
    if not applied:
        raise SystemExit("the job-built version was not published")
    print("per-league side: built by the sync job path")


def main() -> int:
    from app.config import get_settings
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    job_path = "--job-path" in sys.argv[1:]
    settings = get_settings()
    src = Path(args[0]) if args else settings.db_path
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
            if job_path:
                job_build(pg, pub, db, tmp, settings, season)
            with pg.connect() as c:
                seed_user(c, U1)
                seed_membership(c, U1, pub.league_id, "admin")
                if mine:
                    c.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, %s)",
                              [U1, pub.league_id, str(mine[0])])
                c.commit()
            os.environ.update(FANTASY_DB_PATH=str(db), ESPN_SEASON=str(season), FANTASY_API_KEY="",
                              MULTILEAGUE_ENABLED="1", SUPABASE_URL=URL, LEAGUE_FILES_DIR=str(tmp / "objects"),
                              SNAPSHOT_BUCKET="", SERVE_SNAPSHOT="")
            os.environ["SUPABASE_DB_URL"] = pg.dsn_for("fi_api")       # the throwaway cluster's fi_api role
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
