"""Task 3: atomic multi-table writes under a kill, fencing, cross-DB snapshot, reader lag."""
from __future__ import annotations
import json, os, subprocess, sys, time
from common import GLOBAL_DB, connect, league_db, pct, record

L = league_db(3)
TABLES = ["fantasy_teams", "matchups", "transactions", "lineup_snapshots", "waiver_recommendations"]

def counts(con):
    return {t: con.execute(f"SELECT count(*) FROM {L}.main.{t}").fetchone()[0] for t in TABLES} | \
           {"data_version": con.execute(f"SELECT max(data_version) FROM {L}.main._sync_meta").fetchone()[0]}

def child_kill():
    con = connect(f"md:{L}", "laptop")
    con.execute("BEGIN")
    for t in TABLES:
        con.execute(f"DELETE FROM {L}.main.{t}")
    con.execute(f"UPDATE {L}.main._sync_meta SET data_version = data_version + 1000")
    sys.stdout.flush()
    os._exit(9)  # die mid-transaction, no ROLLBACK, no COMMIT

def fence_commit(token: int, payload: str) -> dict:
    """Commit only if our fence is newer than the committed one (the spec 3.4 rule)."""
    con = connect(f"md:{L}", "laptop")
    con.execute("BEGIN")
    con.execute(f"INSERT INTO {L}.main.matchups SELECT * FROM {L}.main.matchups LIMIT 0")  # stand-in for the data writes
    n = con.execute(f"UPDATE {L}.main._sync_meta SET fence = ?, fi_global_version = ? WHERE fence < ?",
                    [token, payload, token]).fetchone()[0]
    if n == 1:
        con.execute("COMMIT")
    else:
        con.execute("ROLLBACK")
    return {"token": token, "updated": n, "committed": n == 1}

def child_lag_reader(target: int) -> None:
    con = connect(f"md:{L}?attach_mode=single", "api")
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 120:
        v = con.execute(f"SELECT max(data_version) FROM {L}.main._sync_meta").fetchone()[0]
        if v >= target:
            print(json.dumps({"seen_after_s": time.perf_counter() - t0})); return
        time.sleep(0.1)
    print(json.dumps({"seen_after_s": None}))

def main():
    if len(sys.argv) > 1:
        {"kill": child_kill}.get(sys.argv[1], lambda: child_lag_reader(int(sys.argv[2])))()
        return
    out = {}
    con = connect("md:", "laptop")
    con.execute(f"UPDATE {L}.main._sync_meta SET fence = 0")
    before = counts(con); con.close()
    r = subprocess.run([sys.executable, __file__, "kill"], capture_output=True, text=True)
    con = connect("md:", "laptop"); after = counts(con); con.close()
    out["atomic_kill"] = {"exit": r.returncode, "before": before, "after": after, "intact": before == after}
    # does the killed client's server-side transaction linger and block writers?
    probes = []
    for i in range(12):
        c = connect(f"md:{L}", "laptop")
        try:
            c.execute(f"UPDATE {L}.main._sync_meta SET data_version = data_version"); probes.append((i * 10, "ok")); break
        except Exception as e:
            probes.append((i * 10, str(e)[:60])); time.sleep(10)
    out["post_kill_writer_probe_s"] = probes
    print("post-kill", probes, flush=True)

    # fence: B (6) commits first, then superseded A (5) must be rejected; then a fresh 7 succeeds
    out["fence"] = [fence_commit(6, "B"), fence_commit(5, "A-stale"), fence_commit(7, "C")]
    con = connect("md:", "laptop")
    out["fence_final"] = con.execute(f"SELECT fence, fi_global_version FROM {L}.main._sync_meta").fetchall()

    # cross-DB snapshot: reader txn spanning league + global, writer bumps both mid-transaction
    rcon = connect(f"md:{L}?attach_mode=single", "api")
    rcon.execute(f"ATTACH IF NOT EXISTS 'md:{GLOBAL_DB}' (READ_ONLY)")
    rcon.execute("BEGIN")
    r1 = rcon.execute(f"SELECT (SELECT max(data_version) FROM {L}.main._sync_meta), (SELECT max(version) FROM {GLOBAL_DB}.main._global_meta)").fetchone()
    # one transaction may write only one database (measured above), so the writer commits each separately
    con.execute(f"UPDATE {L}.main._sync_meta SET data_version = data_version + 1")
    con.execute(f"INSERT INTO {GLOBAL_DB}.main._global_meta VALUES ('v' || (SELECT count(*) + 1 FROM {GLOBAL_DB}.main._global_meta))")
    time.sleep(2)
    r2 = rcon.execute(f"SELECT (SELECT max(data_version) FROM {L}.main._sync_meta), (SELECT max(version) FROM {GLOBAL_DB}.main._global_meta)").fetchone()
    rcon.execute("ROLLBACK")
    r3 = rcon.execute(f"SELECT (SELECT max(data_version) FROM {L}.main._sync_meta), (SELECT max(version) FROM {GLOBAL_DB}.main._global_meta)").fetchone()
    out["snapshot"] = {"in_txn_before": r1, "in_txn_after_writer_commit": r2, "after_txn": r3, "stable_in_txn": r1 == r2}
    rcon.close()

    # reader lag: a separate process with the API token polls until it sees the writer's commit
    lags = []
    for _ in range(15):
        cur = con.execute(f"SELECT max(data_version) FROM {L}.main._sync_meta").fetchone()[0]
        p = subprocess.Popen([sys.executable, __file__, "lag", str(cur + 1)], stdout=subprocess.PIPE, text=True)
        time.sleep(4)  # let the reader connect and start polling first
        t_commit = time.perf_counter()
        con.execute(f"UPDATE {L}.main._sync_meta SET data_version = data_version + 1")
        res = json.loads(p.communicate()[0].strip().splitlines()[-1])
        lags.append(res["seen_after_s"])
    out["reader_poll_seen_after_s_raw"] = lags
    print(record("consistency", out))
    print(json.dumps({k: v for k, v in out.items() if k != "reader_poll_seen_after_s_raw"}, indent=1, default=str))

if __name__ == "__main__":
    main()
