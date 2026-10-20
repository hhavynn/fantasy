"""Phase 0b: drive fi-md-spike's file probe (iad1) against private Vercel Blob and Supabase Storage.
Trial counts are small on purpose: every full download spends free-tier transfer."""
import json, sys, time, urllib.parse, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "motherduck"))
from common import pct, record  # noqa: E402

SCRATCH, = sys.argv[1:2]
S = Path(SCRATCH)
KEY = (S / "spike_key.txt").read_text().strip()
BLOB = (S / "blob_base.txt").read_text().strip()
SB_REF = "kdikrcmvfuzqsgahqaxf"
SB_KEY = next(k["api_key"] for k in json.loads((S / "sb_keys.json").read_text()) if k.get("name") == "service_role")
PROBE = "https://fi-md-spike.vercel.app/api/probe"


def sb_signed(name: str) -> str:
    req = urllib.request.Request(f"https://{SB_REF}.supabase.co/storage/v1/object/sign/spike/{name}",
                                 data=json.dumps({"expiresIn": 3600}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {SB_KEY}", "apikey": SB_KEY, "content-type": "application/json"})
    return f"https://{SB_REF}.supabase.co/storage/v1" + json.loads(urllib.request.urlopen(req).read())["signedURL"]


def probe(**q) -> dict:
    url = PROBE + "?" + urllib.parse.urlencode({"mode": "file", **q})
    t0 = time.perf_counter()
    body = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"x-spike-key": KEY}), timeout=180).read())
    body["client_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return body


def series(label, n, **q):
    runs = []
    for i in range(n):
        qq = dict(q)
        if q.get("method") == "download":
            qq["name"] = f"{label}_{int(time.time())}_{i}.duckdb"  # unique name forces a real fetch
        runs.append(probe(**qq))
    ok = [r for r in runs if "error" not in r]
    summary = {"n": n, "errors": [r["error"] for r in runs if "error" in r][:3]}
    for k in ("fetch_ms", "open_ms", "queries_ms", "total_ms", "attach_ms", "httpfs_load_ms", "http_requests", "http_bytes"):
        vals = [r[k] for r in ok if r.get(k) is not None]
        if vals:
            summary[k] = pct(vals)
    if ok and "bytes" in ok[0]:
        summary["bytes"] = ok[0]["bytes"]
    # warm repeat on the same instance: cached file, queries only
    if q.get("method") == "download" and ok:
        summary["warm_cached"] = probe(**{**q, "name": runs[-1].get("name", "") or qq["name"]})
    return summary


out = {}
ONLY = sys.argv[2] if len(sys.argv) > 2 else "all"
for f in (("compact", "compact_league", "compact_global") if ONLY in ("all", "blob") else ()):
    out[f"blob_download_{f}"] = series(f"b_{f}", 3, method="download", url=f"{BLOB}/spike/{f}.duckdb", auth="blob")
    out[f"blob_range_{f}"] = series(f"br_{f}", 5, method="range", url=f"{BLOB}/spike/{f}.duckdb", auth="blob")
for f in (("compact_league", "compact_global") if ONLY in ("all", "supabase") else ()):
    out[f"supabase_download_{f}"] = series(f"s_{f}", 3, method="download", url=sb_signed(f + ".duckdb"), auth="none")
    out[f"supabase_range_{f}"] = series(f"sr_{f}", 5, method="range", url=sb_signed(f + ".duckdb"), auth="none")
(Path(__file__).parent / f"results_{ONLY}.json").write_text(json.dumps(out, indent=2))
for k, v in out.items():
    print(k, json.dumps({x: (v[x]["p50"], v[x]["p95"]) if isinstance(v.get(x), dict) and "p50" in v[x] else v.get(x)
                         for x in ("fetch_ms", "open_ms", "queries_ms", "attach_ms", "httpfs_load_ms", "total_ms", "http_requests", "http_bytes", "bytes", "errors")}))
