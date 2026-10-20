"""Drive the fi-md-spike probe (iad1). Server-side timings come from the function; client time is HTTP round trip."""
import json, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from common import league_db, pct, record

URL = "https://fi-md-spike.vercel.app/api/probe"
KEY = open(sys.argv[1]).read().strip()

def call(mode, league):
    req = urllib.request.Request(f"{URL}?mode={mode}&league={league}", headers={"x-spike-key": KEY})
    t0 = time.perf_counter()
    body = json.loads(urllib.request.urlopen(req, timeout=60).read())
    body["client_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return body

warm = [call("warm", league_db(1 + (i % 40))) for i in range(80)]
errs = [w for w in warm if "error" in w]
ok = [w for w in warm if "error" not in w]
first_attach = [w for w in ok if w["reused_base"] and w["attach_ms"] > 0 and w["instance_calls"] <= 41]
cold = [call("cold", league_db(41 + i)) for i in range(15)]
with ThreadPoolExecutor(10) as ex:
    burst = list(ex.map(lambda i: call("warm", league_db(1 + i)), range(20)))
out = {
    "region": ok[0]["region"] if ok else None, "errors": errs[:3],
    "warm_instances_seen": len({w["instance_started"] for w in ok}),
    "warm_reused_base_total_ms": pct([w["total_ms"] for w in ok if w["reused_base"]]),
    "warm_reused_base_attach_ms": pct([w["attach_ms"] for w in ok if w["reused_base"]]),
    "warm_query_ms": pct([w["query_ms"] for w in ok if w["reused_base"]]),
    "warm_new_base_total_ms (instance's first request: extension + auth)": pct([w["total_ms"] for w in ok if not w["reused_base"]]),
    "warm_client_ms": pct([w["client_ms"] for w in ok if w["reused_base"]]),
    "fresh_duckdb_instance_total_ms": pct([c["total_ms"] for c in cold if "error" not in c]),
    "fresh_instance_connect_ms": pct([c["connect_ms"] for c in cold if "error" not in c]),
    "cold_errors": [c for c in cold if "error" in c][:3],
    "burst10_instances": len({b["instance_started"] for b in burst}),
    "burst10_ok": len([b for b in burst if "error" not in b]),
    "burst10_errors": [b["error"] for b in burst if "error" in b][:5],
    "burst10_total_ms": pct([b["total_ms"] for b in burst if "error" not in b]),
    "burst10_client_ms": pct([b["client_ms"] for b in burst]),
}
print(record("latency_vercel", out)); print(json.dumps(out, indent=1))
