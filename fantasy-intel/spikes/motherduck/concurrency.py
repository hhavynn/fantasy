"""Task 4: parallel writers (different DBs, same DB) with concurrent readers. One process per actor."""
import json, subprocess, sys, time
from common import connect, league_db, pct, record

TABLES = ["fantasy_teams", "matchups", "transactions", "lineup_snapshots", "waiver_recommendations"]

def writer(db: str, iters: int):
    con = connect(f"md:{db}", "laptop"); times, errs = [], []
    for _ in range(iters):
        t0 = time.perf_counter()
        try:
            con.execute("BEGIN")
            for t in TABLES:  # rewrite each table in place (DELETE + re-INSERT the same rows)
                con.execute(f"CREATE OR REPLACE TEMP TABLE _buf AS SELECT * FROM {db}.main.{t}")
                con.execute(f"DELETE FROM {db}.main.{t}")
                con.execute(f"INSERT INTO {db}.main.{t} SELECT * FROM _buf")
            con.execute(f"UPDATE {db}.main._sync_meta SET data_version = data_version + 1")
            con.execute("COMMIT")
        except Exception as e:
            errs.append(str(e)[:90])
            try: con.execute("ROLLBACK")
            except Exception: pass
        times.append(round((time.perf_counter() - t0) * 1000, 1))
    return {"db": db, "write_ms": pct(times), "errors": errs}

def reader(db: str, seconds: float):
    con = connect(f"md:{db}?attach_mode=single", "api"); n, errs, bad = 0, [], 0
    t_end = time.time() + seconds
    while time.time() < t_end:
        try:
            con.execute("BEGIN")
            c = [con.execute(f"SELECT count(*) FROM {db}.main.{t}").fetchone()[0] for t in TABLES]
            con.execute("ROLLBACK")
            bad += any(x == 0 for x in c)  # a torn read would show an emptied table
            n += 1
        except Exception as e:
            errs.append(str(e)[:90])
    return {"db": db, "reads": n, "torn_reads": bad, "errors": errs[:5]}

def spawn(*args):
    return subprocess.Popen([sys.executable, __file__, *map(str, args)], stdout=subprocess.PIPE, text=True)

def collect(ps):
    return [json.loads(p.communicate()[0].strip().splitlines()[-1]) for p in ps]

if __name__ == "__main__":
    if len(sys.argv) > 1:
        role, db, n = sys.argv[1], sys.argv[2], float(sys.argv[3])
        print(json.dumps(writer(db, int(n)) if role == "w" else reader(db, n))); sys.exit()
    out = {}
    t0 = time.time()
    dbs = [league_db(i) for i in range(10, 15)]
    ps = [spawn("w", d, 10) for d in dbs] + [spawn("r", d, 40) for d in dbs]
    res = collect(ps)
    out["different_dbs"] = {"wall_s": round(time.time() - t0, 1), "writers": res[:5], "readers": res[5:]}
    t0 = time.time()
    same = league_db(20)
    ps = [spawn("w", same, 10), spawn("w", same, 10), spawn("r", same, 30)]
    res = collect(ps)
    out["same_db"] = {"wall_s": round(time.time() - t0, 1), "writers": res[:2], "reader": res[2]}
    print(record("concurrency", out))
    print(json.dumps(out, indent=1)[:4000])
