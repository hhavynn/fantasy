"""Phase 0b, R2 series: upload the spike files to R2 and drive fi-md-spike's file probe (iad1) with
short-lived presigned GET URLs, so the probe never holds an R2 token."""
import hashlib, hmac, json, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
BACKEND = Path("/Users/havyn/Documents/CS/Fantasy/fantasy-intel/backend")
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.config import get_settings  # noqa: E402
from app.db import r2  # noqa: E402
from common import pct  # noqa: E402

S = Path(sys.argv[1])
KEY = (S / "spike_key.txt").read_text().strip()
PROBE = "https://fi-md-spike.vercel.app/api/probe"
cfg = get_settings().r2_config()


def presign(key: str, expires: int = 900) -> str:
    host = urllib.parse.urlsplit(cfg.base_url).netloc
    now = datetime.now(timezone.utc)
    amz, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    scope = f"{date}/{cfg.region}/s3/aws4_request"
    path = urllib.parse.quote(f"/{cfg.bucket}/{key}", safe="-_.~/")
    q = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{cfg.access_key_id}/{scope}",
         "X-Amz-Date": amz, "X-Amz-Expires": str(expires), "X-Amz-SignedHeaders": "host"}
    qs = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in sorted(q.items()))
    canonical = "\n".join(["GET", path, qs, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = r2._hmac(r2._hmac(r2._hmac(r2._hmac(("AWS4" + cfg.secret_access_key).encode(), date), cfg.region), "s3"), "aws4_request")
    return f"{cfg.base_url}{path}?{qs}&X-Amz-Signature={hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()}"


def probe(**q) -> dict:
    url = PROBE + "?" + urllib.parse.urlencode({"mode": "file", **q})
    t0 = time.perf_counter()
    body = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"x-spike-key": KEY}), timeout=180).read())
    body["client_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return body


out = {}
f = "compact"
r2.put_file(cfg, f"spike/{f}.duckdb", S / f"{f}.duckdb")
total = (S / f"{f}.duckdb").stat().st_size
for parts in (4, 8, 16):
    runs = [probe(method="pdownload", url=presign(f"spike/{f}.duckdb"), total=total, parts=parts,
                  name=f"p_{parts}_{int(time.time())}_{i}.duckdb") for i in range(5)]
    ok = [r for r in runs if "error" not in r]
    out[f"r2_parallel_{parts}"] = {"errors": [r["error"] for r in runs if "error" in r][:3],
                                   **({k: pct([r[k] for r in ok]) for k in ("fetch_ms", "total_ms")} if ok else {})}
r2.delete(cfg, f"spike/{f}.duckdb")
(S / "bench" / "results_r2_parallel.json").write_text(json.dumps(out, indent=2))
for k, v in out.items():
    print(k, {x: (v[x]["p50"], v[x]["p95"]) if isinstance(v.get(x), dict) else v.get(x) for x in ("fetch_ms", "total_ms", "errors")})
