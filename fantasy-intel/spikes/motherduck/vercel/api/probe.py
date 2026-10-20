"""Throwaway Phase 0 probe: measures the league request path from a Vercel Function in iad1."""
import hmac, json, os, sys, time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(__file__))
import probe_core  # noqa: E402
import file_probe  # noqa: E402

STARTED = time.time()
CALLS = 0


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global CALLS
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if not hmac.compare_digest(self.headers.get("x-spike-key", ""), os.environ.get("SPIKE_KEY", "~")):
            self.send_response(404); self.end_headers(); return
        CALLS += 1
        t0 = time.perf_counter()
        try:
            if q.get("mode") == "file":
                hdr = {"Authorization": "Bearer " + os.environ["BLOB_READ_WRITE_TOKEN"]} if q.get("auth") == "blob" else {}
                res = (file_probe.download(q["url"], q["name"], hdr) if q.get("method") == "download"
                       else file_probe.range_read(q["url"], hdr))
            else:
                res = (probe_core.cold if q.get("mode") == "cold" else probe_core.warm_switch)(q["league"], os.environ["MOTHERDUCK_TOKEN"])
        except Exception as e:  # noqa: BLE001
            res = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
        res.update({"handler_ms": round((time.perf_counter() - t0) * 1000, 1), "instance_started": STARTED,
                    "instance_calls": CALLS, "region": os.environ.get("VERCEL_REGION")})
        body = json.dumps(res).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
        self.wfile.write(body)
