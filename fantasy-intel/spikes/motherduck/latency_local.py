import json, subprocess, sys
from common import league_db, pct, record, token
from probe_core import cold, warm_switch

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(json.dumps(cold(sys.argv[1], token("api")))); sys.exit()
    colds = []
    for i in range(20):
        r = subprocess.run([sys.executable, __file__, league_db(21 + i)], capture_output=True, text=True)
        colds.append(json.loads(r.stdout.strip().splitlines()[-1]))
    warms = [warm_switch(league_db(1 + (i % 40)), token("api")) for i in range(60)]
    first_pass = [w for w in warms[1:40]]   # each league attached for the first time in this process
    second_pass = warms[41:]                # already attached
    out = {"where": "laptop (Bay Area) -> MotherDuck", "cold_total": pct([c["total_ms"] for c in colds]),
           "cold_connect": pct([c["connect_ms"] for c in colds]), "cold_attach_global": pct([c["attach_global_ms"] for c in colds]),
           "cold_query": pct([c["query_ms"] for c in colds]),
           "warm_new_league_total": pct([w["total_ms"] for w in first_pass]),
           "warm_new_league_attach": pct([w["attach_ms"] for w in first_pass]),
           "warm_attached_total": pct([w["total_ms"] for w in second_pass]),
           "warm_query": pct([w["query_ms"] for w in second_pass])}
    print(record("latency_local", out)); print(json.dumps(out, indent=1))
