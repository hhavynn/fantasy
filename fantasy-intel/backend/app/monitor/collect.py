"""Free-tier usage collectors, one per source (plan Decisions, "Usage budgets").

Each collector returns a SourceResult: `ok` with its samples, `not_configured` (its token or connection is not set:
an optional source) or `unavailable` (it was set up but the call failed or answered something unexpected). A
collector never raises and never puts provider text into its result: a failure is a short code such as `http_403`.

HTTP goes through an injectable callable `(method, url, headers, body) -> (status, body)` so tests use fakes.

Quotas are the free plans' limits, in decimal units (500 MB = 500,000,000 bytes), which errs towards alerting early:
  * Supabase: database size 500 MB; accounts 50,000 (a proxy for monthly active users).
  * Cloudflare R2: storage 10 GB; Class A operations 1,000,000 and Class B 10,000,000 per month; object count is
    information only.
  * Vercel Hobby: Fluid Active CPU 4 hours, Function Invocations 1,000,000 and Fluid Provisioned Memory 360 GB-hours
    per month.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

Http = Callable[..., tuple[int, bytes]]
Lister = Callable[[], Iterable[tuple[str, int]]]

MB, GB = 1_000_000, 1_000_000_000
METRIC_RE = re.compile(r"^[a-z0-9_]{1,64}$")
CLOUDFLARE_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
VERCEL_CHARGES = "https://api.vercel.com/v1/billing/charges"


@dataclass(frozen=True)
class Sample:
    metric: str
    used: float
    quota: float | None          # None: information only, never alerts
    unit: str
    injected: bool = False       # forced with --inject: alerts and is printed, never stored

    @property
    def ratio(self) -> float | None:
        return self.used / self.quota if self.quota else None


@dataclass(frozen=True)
class SourceResult:
    source: str
    status: str                  # ok | not_configured | unavailable
    samples: tuple[Sample, ...] = ()
    reason: str = ""             # a short public-safe code when unavailable


class SourceShapeError(ValueError):
    """A provider answered something this collector does not understand (the code is public-safe)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def urllib_http(method: str, url: str, headers: dict[str, str], body: bytes | None = None,
                timeout: float = 30) -> tuple[int, bytes]:
    """The real HTTP client: never raises; a network failure is status 0."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        try:
            return err.code, err.read()
        except OSError:
            return err.code, b""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""


def month_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _status_code(status: int) -> str:
    return "network" if status == 0 else f"http_{status}"


def _number(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise SourceShapeError("bad_number")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise SourceShapeError("bad_number") from None
    if out != out or out < 0:          # NaN or negative
        raise SourceShapeError("bad_number")
    return out


# ---- Supabase Postgres ----------------------------------------------------------------------------------------

def collect_postgres(con) -> SourceResult:
    """`con`: a psycopg connection as fi_sync (fi.usage_counters()), or None."""
    if con is None:
        return SourceResult("postgres", "not_configured")
    try:
        db_bytes, accounts = con.execute("SELECT db_bytes, accounts FROM fi.usage_counters()").fetchone()
        con.commit()
    except Exception as err:   # noqa: BLE001 - the type only: messages can carry connection details
        try:
            con.rollback()
        except Exception:   # noqa: BLE001
            pass
        return SourceResult("postgres", "unavailable", reason=type(err).__name__)
    return SourceResult("postgres", "ok", (Sample("supabase_db_bytes", float(db_bytes), 500 * MB, "bytes"),
                                           Sample("supabase_accounts", float(accounts), 50_000, "accounts")))


# ---- Cloudflare R2 storage (a bucket listing) -------------------------------------------------------------------

def collect_r2(lister: Lister | None) -> SourceResult:
    """`lister()` yields (key, size) for every object (app.db.r2.list_objects), or None when R2 is not configured."""
    if lister is None:
        return SourceResult("r2", "not_configured")
    try:
        total = count = 0
        for _key, size in lister():
            total += int(size)
            count += 1
    except Exception as err:   # noqa: BLE001
        code = getattr(err, "code", None)
        reason = code if isinstance(code, str) and METRIC_RE.match(code.lower()) else type(err).__name__
        return SourceResult("r2", "unavailable", reason=reason)
    return SourceResult("r2", "ok", (Sample("r2_storage_bytes", float(total), 10 * GB, "bytes"),
                                     Sample("r2_objects", float(count), None, "objects")))


# ---- Cloudflare R2 operations (GraphQL Analytics API) -----------------------------------------------------------

R2_CLASS_A = frozenset({
    "ListBuckets", "PutBucket", "ListObjects", "PutObject", "CopyObject", "CompleteMultipartUpload",
    "CreateMultipartUpload", "LifecycleStorageTierTransition", "ListMultipartUploads", "UploadPart", "UploadPartCopy",
    "ListParts", "PutBucketEncryption", "PutBucketCors", "PutBucketLifecycleConfiguration"})
R2_CLASS_B = frozenset({
    "HeadBucket", "HeadObject", "GetObject", "UsageSummary", "GetBucketEncryption", "GetBucketLocation",
    "GetBucketCors", "GetBucketLifecycleConfiguration"})
R2_FREE = frozenset({"DeleteObject", "DeleteBucket", "AbortMultipartUpload"})

R2_OPERATIONS_QUERY = """
query R2Operations($accountTag: string!, $start: Time, $end: Time) {
  viewer {
    accounts(filter: { accountTag: $accountTag }) {
      r2OperationsAdaptiveGroups(limit: 10000, filter: { datetime_geq: $start, datetime_leq: $end }) {
        sum { requests }
        dimensions { actionType }
      }
    }
  }
}
""".strip()


def r2_operation_class(action: str) -> str:
    """'A', 'B' or 'free'. An action we do not know counts as Class A (the scarcer budget), so it alerts early."""
    if action in R2_FREE:
        return "free"
    return "B" if action in R2_CLASS_B else "A"


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_r2_operations(status: int, body: bytes) -> tuple[Sample, Sample]:
    if status != 200:
        raise SourceShapeError(_status_code(status))
    try:
        doc = json.loads(body)
    except ValueError:
        raise SourceShapeError("bad_shape") from None
    if not isinstance(doc, dict):
        raise SourceShapeError("bad_shape")
    if doc.get("errors"):
        raise SourceShapeError("graphql_error")
    try:
        groups = doc["data"]["viewer"]["accounts"][0]["r2OperationsAdaptiveGroups"]
        if not isinstance(groups, list):
            raise TypeError
        totals = {"A": 0.0, "B": 0.0, "free": 0.0}
        for g in groups:
            action = g["dimensions"]["actionType"]
            if not isinstance(action, str):
                raise TypeError
            totals[r2_operation_class(action)] += _number(g["sum"]["requests"])
    except (KeyError, IndexError, TypeError):
        raise SourceShapeError("bad_shape") from None
    return (Sample("r2_class_a_ops", totals["A"], 1_000_000, "requests"),
            Sample("r2_class_b_ops", totals["B"], 10_000_000, "requests"))


def collect_r2_operations(token: str | None, account_id: str | None, http: Http, now: datetime) -> SourceResult:
    """Month-to-date Class A and B operations for the whole account (the free allowance is per account)."""
    if not token or not account_id:
        return SourceResult("r2_operations", "not_configured")
    body = json.dumps({"query": R2_OPERATIONS_QUERY,
                       "variables": {"accountTag": account_id, "start": _iso_z(month_start(now)), "end": _iso_z(now)}})
    try:
        status, payload = http("POST", CLOUDFLARE_GRAPHQL,
                               {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                               body.encode())
        return SourceResult("r2_operations", "ok", parse_r2_operations(status, payload))
    except SourceShapeError as err:
        return SourceResult("r2_operations", "unavailable", reason=err.code)
    except Exception as err:   # noqa: BLE001
        return SourceResult("r2_operations", "unavailable", reason=type(err).__name__)


# ---- Vercel (FOCUS billing charges, JSONL) ----------------------------------------------------------------------

_HOURS = {"hour": 1, "hours": 1, "hr": 1, "hrs": 1, "h": 1, "minute": 1 / 60, "minutes": 1 / 60, "min": 1 / 60,
          "second": 1 / 3600, "seconds": 1 / 3600, "sec": 1 / 3600, "s": 1 / 3600,
          "millisecond": 1 / 3_600_000, "milliseconds": 1 / 3_600_000, "ms": 1 / 3_600_000}
_GB_HOURS = {f"{g}-{t}": f for g in ("gigabyte", "gigabytes", "gb") for t, f in (
    ("hour", 1), ("hours", 1), ("hr", 1), ("hrs", 1), ("second", 1 / 3600), ("seconds", 1 / 3600), ("sec", 1 / 3600),
    ("s", 1 / 3600))}

# ServiceName -> (metric, quota, unit, unit conversion table or None for a plain count)
VERCEL_SERVICES = {
    "Fluid Active CPU": ("vercel_active_cpu_hours", 4, "hours", _HOURS),
    "Function Invocations": ("vercel_invocations", 1_000_000, "invocations", None),
    "Fluid Provisioned Memory": ("vercel_provisioned_memory_gb_hours", 360, "gb_hours", _GB_HOURS),
}


def _unit_key(unit) -> str:
    return re.sub(r"[\s_]+", "-", str(unit or "").strip().lower())


def parse_vercel_charges(body: bytes) -> list[Sample]:
    """Sum ConsumedQuantity by ServiceName over the `Usage` records (FOCUS v1.3: ServiceName, ConsumedQuantity,
    ConsumedUnit, ChargeCategory). A service with no record this month is 0. Any record without ServiceName or
    ConsumedQuantity, a quantity that is not a number, or a unit we cannot convert raises SourceShapeError."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise SourceShapeError("bad_shape") from None
    totals = {name: 0.0 for name in VERCEL_SERVICES}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            raise SourceShapeError("bad_shape") from None
        if not isinstance(rec, dict):
            raise SourceShapeError("bad_shape")
        if "ServiceName" not in rec or "ConsumedQuantity" not in rec:
            raise SourceShapeError("missing_fields")
        if rec.get("ChargeCategory", "Usage") != "Usage":
            continue
        name = rec["ServiceName"]
        if name not in VERCEL_SERVICES:
            continue
        qty = _number(rec["ConsumedQuantity"])
        table = VERCEL_SERVICES[name][3]
        if table is not None:
            factor = table.get(_unit_key(rec.get("ConsumedUnit")))
            if factor is None:
                raise SourceShapeError("unknown_unit")
            qty *= factor
        totals[name] += qty
    return [Sample(metric, round(totals[name], 6), quota, unit)
            for name, (metric, quota, unit, _) in VERCEL_SERVICES.items()]


def _iso_ms(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def collect_vercel(token: str | None, team_id: str | None, http: Http, now: datetime) -> SourceResult:
    """Month-to-date (UTC calendar month) usage from GET /v1/billing/charges."""
    if not token:
        return SourceResult("vercel", "not_configured")
    query = {"from": _iso_ms(month_start(now)), "to": _iso_ms(now), **({"teamId": team_id} if team_id else {})}
    url = f"{VERCEL_CHARGES}?{urllib.parse.urlencode(query)}"
    try:
        status, payload = http("GET", url, {"Authorization": f"Bearer {token}", "Accept": "application/jsonl"}, None)
        if status != 200:
            raise SourceShapeError(_status_code(status))
        return SourceResult("vercel", "ok", tuple(parse_vercel_charges(payload)))
    except SourceShapeError as err:
        return SourceResult("vercel", "unavailable", reason=err.code)
    except Exception as err:   # noqa: BLE001
        return SourceResult("vercel", "unavailable", reason=type(err).__name__)


# ---- --inject ---------------------------------------------------------------------------------------------------

def parse_inject(spec: str) -> Sample:
    """`metric=used/quota`, e.g. `vercel_invocations=800000/1000000`: a forced sample that proves an alert fires."""
    m = re.fullmatch(r"([a-z0-9_]{1,64})=([0-9]+(?:\.[0-9]+)?)/([0-9]+(?:\.[0-9]+)?)", spec.strip())
    if not m or float(m.group(3)) <= 0:
        raise ValueError("expected metric=used/quota with a lowercase metric name and a positive quota")
    return Sample(m.group(1), float(m.group(2)), float(m.group(3)), "injected", injected=True)


def collect_all(env: dict[str, str], *, sync_con, http: Http, now: datetime,
                r2_lister: Lister | None) -> list[SourceResult]:
    return [
        collect_postgres(sync_con),
        collect_r2(r2_lister),
        collect_r2_operations(env.get("CLOUDFLARE_ANALYTICS_TOKEN", "").strip() or None,
                              env.get("R2_ACCOUNT_ID", "").strip() or None, http, now),
        collect_vercel(env.get("VERCEL_USAGE_TOKEN", "").strip() or None,
                       env.get("VERCEL_TEAM_ID", "").strip() or None, http, now),
    ]
