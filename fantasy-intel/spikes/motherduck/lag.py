"""Reader lag: wall-clock time from the writer's COMMIT returning to a separate API-token process seeing it."""
import json, subprocess, sys, time
from common import connect, league_db, pct, record
L = league_db(5)

def reader(target: int):
    con = connect(f"md:{L}?attach_mode=single", "api")
    con.execute(f"SELECT 1 FROM {L}.main._sync_meta").fetchall()
    print("ready", flush=True)
    while True:
        if con.execute(f"SELECT max(data_version) FROM {L}.main._sync_meta").fetchone()[0] >= target:
            print(json.dumps({"seen_at": time.time()}), flush=True); return
        time.sleep(0.02)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        reader(int(sys.argv[1])); sys.exit()
    w = connect(f"md:{L}", "laptop"); lags = []
    for _ in range(20):
        cur = w.execute(f"SELECT max(data_version) FROM {L}.main._sync_meta").fetchone()[0]
        p = subprocess.Popen([sys.executable, __file__, str(cur + 1)], stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "ready"
        w.execute(f"UPDATE {L}.main._sync_meta SET data_version = data_version + 1")
        committed = time.time()
        seen = json.loads(p.stdout.readline())["seen_at"]; p.wait()
        lags.append(round((seen - committed) * 1000, 1))
    print(record("lag", {"lag_ms_raw": lags, "lag_ms": pct(lags), "note": "writer=laptop token, reader=API token, separate processes, 20 ms poll"}))
    print(pct(lags), lags)
