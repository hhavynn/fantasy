"""Can a share attached by URL be written or dropped through? Child process per token identity."""
import json, subprocess, sys
from common import attempt, connect, league_db, record

def child_attach(url: str) -> dict:
    con = connect("md:", "api", access_mode="read_only")
    steps = [attempt(con, f"ATTACH '{url}' AS share_a"),
             attempt(con, "SELECT count(*) FROM share_a.main.fantasy_teams"),
             attempt(con, "INSERT INTO share_a.main._sync_meta VALUES (997, 997, 'x', now())"),
             attempt(con, "DETACH share_a"),
             attempt(con, f"DROP DATABASE IF EXISTS {league_db(58)}")]
    return {"steps": steps}

def child_attach_rw(url: str) -> dict:
    con = connect("md:", "api")
    return {"steps": [attempt(con, f"ATTACH '{url}' AS share_a"),
                      attempt(con, "INSERT INTO share_a.main._sync_meta VALUES (996, 996, 'x', now())")]}

if __name__ == "__main__":
    if len(sys.argv) > 2:
        print(json.dumps({"ro": child_attach, "rw": child_attach_rw}[sys.argv[1]](sys.argv[2]))); sys.exit()
    con = connect("md:", "laptop")
    url = con.execute(f"CREATE SHARE IF NOT EXISTS spike_share_a FROM {league_db(1)} "
                      "(ACCESS RESTRICTED, VISIBILITY HIDDEN, UPDATE AUTOMATIC)").fetchone()[0]
    con.execute(f"CREATE DATABASE IF NOT EXISTS {league_db(59)} FROM {league_db(1)}")  # restore the one dropped earlier
    con.close()
    out = {}
    for mode in ("ro", "rw"):
        r = subprocess.run([sys.executable, __file__, mode, url], capture_output=True, text=True)
        out[mode] = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {"crash": r.stderr[-800:]}
    print(record("share", out))
    for m, v in out.items():
        print("##", m)
        for s in v.get("steps", []):
            print(" ", "OK " if s["ok"] else "ERR", s["sql"][:70].replace(url, "<share>"), "->", str(s.get("rows"))[:80] if s["ok"] else s["error"][:200])
        print(v.get("crash", ""))
