"""Shared helpers for the Phase 0 MotherDuck spike. Tokens are read from fantasy-intel/.env and never printed."""

from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
ENV_FILE = HERE.parent.parent / ".env"
LOCAL_DB = HERE.parent.parent / "data" / "fantasy.duckdb"
N_LEAGUES = 60
GLOBAL_DB = "spike_fi_global"


def league_db(i: int) -> str:
    return f"spike_lg_{i:03d}"


def _env() -> dict[str, str]:
    out = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def token(kind: str = "api") -> str:
    """kind='api' -> the hosted API's token (MOTHERDUCK_API_TOKEN); 'laptop' -> MOTHERDUCK_TOKEN."""
    key = {"api": "MOTHERDUCK_API_TOKEN", "laptop": "MOTHERDUCK_TOKEN"}[kind]
    val = os.getenv(key) or _env().get(key)
    if not val:
        raise SystemExit(f"{key} missing")
    return val


def connect(dsn: str, kind: str = "api", **config) -> duckdb.DuckDBPyConnection:
    if not os.getenv("HOME"):
        os.environ["HOME"] = tempfile.gettempdir()
    return duckdb.connect(dsn, config={"motherduck_token": token(kind), **config})


def attempt(con, sql: str, params=None) -> dict:
    t0 = time.perf_counter()
    try:
        rows = con.execute(sql, params or []).fetchall()
        return {"sql": sql, "ok": True, "rows": [list(map(str, r)) for r in rows[:20]],
                "ms": round((time.perf_counter() - t0) * 1000, 1)}
    except Exception as err:  # record the failure, never raise: the outcome IS the measurement
        msg = str(err).replace(token("api"), "<redacted>").replace(token("laptop"), "<redacted>")
        return {"sql": sql, "ok": False, "error": f"{type(err).__name__}: {msg[:400]}",
                "ms": round((time.perf_counter() - t0) * 1000, 1)}


@contextmanager
def timer(out: dict, key: str):
    t0 = time.perf_counter()
    yield
    out[key] = round((time.perf_counter() - t0) * 1000, 1)


def pct(values: list[float]) -> dict:
    v = sorted(values)
    if not v:
        return {}
    p = lambda q: v[min(len(v) - 1, int(round(q * (len(v) - 1))))]
    return {"n": len(v), "p50": p(0.5), "p95": p(0.95), "max": v[-1], "mean": round(statistics.mean(v), 1)}


def record(name: str, payload: dict) -> Path:
    RESULTS.mkdir(exist_ok=True)
    payload = {"recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "duckdb": duckdb.__version__, **payload}
    path = RESULTS / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path
