"""Task 2: what can a connection opened for ONE league reach, using the hosted API's token type?"""

from __future__ import annotations

from common import GLOBAL_DB, attempt, connect, league_db, record

A, B = league_db(1), league_db(2)
OTHER_ATTEMPTS = [
    "SELECT database_name FROM duckdb_databases() ORDER BY 1",
    "SELECT alias FROM md_list_databases() ORDER BY 1",
    f"SELECT count(*) FROM {B}.main.fantasy_teams",            # lazy reach without ATTACH
    f"ATTACH 'md:{B}'",
    f"SELECT count(*) FROM {B}.main.fantasy_teams",
    f"USE {B}",
    "USE " + A,
    f"INSERT INTO {B}.main._sync_meta VALUES (999, 999, 'spike-intrusion', now())",
    f"DELETE FROM {B}.main._sync_meta WHERE fence = 999",
    "CREATE DATABASE spike_tmp_isolation",
    "DROP DATABASE IF EXISTS spike_tmp_isolation",
    f"INSERT INTO {A}.main._sync_meta VALUES (998, 998, 'spike-own-write', now())",
    f"DELETE FROM {A}.main._sync_meta WHERE fence = 998",
]


def run(label: str, dsn: str, setup: list[str], kind: str = "api", **config) -> dict:
    con = connect(dsn, kind, **config)
    steps = [attempt(con, s) for s in setup] + [attempt(con, s) for s in OTHER_ATTEMPTS]
    con.close()
    return {"label": label, "dsn": dsn, "config": config, "steps": steps}


CASES = {
    "plain": lambda: run("plain attach_mode=single", f"md:{A}?attach_mode=single&saas_mode=true", []),
    "locked": lambda: run("global attached RO, then config locked",
            f"md:{A}?attach_mode=single&saas_mode=true",
            [f"ATTACH 'md:{GLOBAL_DB}' (READ_ONLY)",
             f"SET search_path = '{A}.main,{GLOBAL_DB}.main'",
             "SELECT count(*) FROM fantasy_teams", "SELECT count(*) FROM nfl_schedules",
             f"INSERT INTO {GLOBAL_DB}.main._global_meta VALUES ('intrusion')",
             "SET enable_external_access = false", "SET lock_configuration = true"]),
    "locked_nosaas": lambda: run("no saas_mode: global RO, external access off, config locked",
        f"md:{A}?attach_mode=single",
        [f"ATTACH 'md:{GLOBAL_DB}' (READ_ONLY)",
         f"SET search_path = '{A}.main,{GLOBAL_DB}.main'",
         "SET enable_external_access = false", "SET lock_configuration = true",
         "SELECT count(*) FROM fantasy_teams", "SELECT count(*) FROM nfl_schedules",
         "SELECT * FROM read_csv('/etc/hosts') LIMIT 1"]),
    "readonly_drop": lambda: run("access_mode=read_only: drop an EXISTING other league db",
        f"md:{A}?attach_mode=single&saas_mode=true",
        [f"DROP DATABASE IF EXISTS {league_db(59)}", "SHOW ALL DATABASES"], access_mode="read_only"),
    "readonly": lambda: run("access_mode=read_only connection", f"md:{A}?attach_mode=single&saas_mode=true", [],
            access_mode="read_only"),
}


def main() -> None:
    import json, subprocess, sys
    if len(sys.argv) > 1:  # child: one case per process (DuckDB caches one md: instance per process)
        if sys.argv[1] == "share":
            print(json.dumps(share_case()))
        else:
            print(json.dumps(CASES[sys.argv[1]]()))
        return
    out = {}
    for name in [*CASES, "share"]:
        res = subprocess.run([sys.executable, __file__, name], capture_output=True, text=True)
        out[name] = json.loads(res.stdout.strip().splitlines()[-1]) if res.returncode == 0 else {"crash": res.stderr[-1500:]}
    print(record("isolation", {"token": "MOTHERDUCK_API_TOKEN (standard read/write, same type as hosted API)",
                               "cases": [out[k] for k in CASES], "share": out["share"]}))


def share_case() -> dict:
    # Shares: a share of league A attached by URL; can it be written through?
    con = connect("md:", "laptop")
    share = attempt(con, f"CREATE SHARE IF NOT EXISTS spike_share_a FROM {A} (ACCESS RESTRICTED, VISIBILITY HIDDEN, UPDATE AUTOMATIC)")
    url = share
    con.close()
    share_case = {"create": share, "url_lookup": url}
    share_case_ = share_case
    if url and url["ok"] and url["rows"]:
        share_url = url["rows"][0][0]
        c2 = connect("md:", "api")
        share_case["steps"] = [attempt(c2, f"ATTACH '{share_url}' AS share_a"),
                               attempt(c2, "SELECT count(*) FROM share_a.main.fantasy_teams"),
                               attempt(c2, "INSERT INTO share_a.main._sync_meta VALUES (997, 997, 'x', now())")]
        c2.close()
    return share_case_


if __name__ == "__main__":
    main()
