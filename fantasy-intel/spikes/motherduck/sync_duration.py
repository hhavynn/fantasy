"""Task 6a: time a full Los Subies ESPN sync by stage against a scratch copy (live DB untouched),
then time committing the league tables into spike_lg_060 in one transaction."""
import dataclasses, shutil, sys, tempfile, time
from pathlib import Path
BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND))
from app.analytics.pipeline import run_analytics  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.connection import open_db  # noqa: E402
from app.espn.client import EspnClient  # noqa: E402
from app.espn.sync import fetch_all, write_all  # noqa: E402
from common import LOCAL_DB, connect, league_db, record  # noqa: E402
from tables import LEAGUE_TABLES  # noqa: E402

scratch = Path(tempfile.mkdtemp()) / "fantasy.duckdb"
shutil.copy(LOCAL_DB, scratch)
s = dataclasses.replace(get_settings(), db_path=scratch)
out = {}
t0 = time.perf_counter()
client = EspnClient(s.league_id, s.season, s.espn_s2, s.swid)
payloads = fetch_all(client)
out["fetch_s"] = round(time.perf_counter() - t0, 1)
out["espn_requests"] = getattr(client, "request_count", None)
t1 = time.perf_counter()
with open_db(scratch) as con:
    stats = write_all(con, payloads, swid=s.swid, my_team_id=s.my_team_id)
out["write_local_s"] = round(time.perf_counter() - t1, 1)
t2 = time.perf_counter()
with open_db(scratch) as con:
    run_analytics(con, s.season)
out["analytics_local_s"] = round(time.perf_counter() - t2, 1)
t3 = time.perf_counter()
md = connect("md:", "laptop")
md.execute(f"ATTACH '{scratch}' AS src (READ_ONLY)")
target = league_db(60)
tables = [r[0] for r in md.execute("SELECT table_name FROM duckdb_tables() WHERE database_name='src'").fetchall()
          if r[0] in LEAGUE_TABLES]
md.execute("BEGIN")
for t in tables:
    md.execute(f'CREATE OR REPLACE TABLE {target}.main."{t}" AS SELECT * FROM src.main."{t}"')
md.execute(f"UPDATE {target}.main._sync_meta SET data_version = data_version + 1, committed_at = now()")
md.execute("COMMIT")
out["commit_to_motherduck_s"] = round(time.perf_counter() - t3, 1)
out["total_s"] = round(time.perf_counter() - t0, 1)
out["warnings"] = len(stats.get("warnings", []))
print(record("sync_duration", out)); print(out)
