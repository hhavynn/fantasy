"""The platform monitor (backend/app/monitor, scripts/platform_monitor.py): collectors with fake clients, the 70%
threshold, the GitHub issue dedupe/close logic with a fake `gh`, and the public-output audit (the log and the issues
are public: metric names, numbers, job ids, league ids and error codes only)."""
import importlib.util
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from app.monitor import alerts, collect, github
from app.monitor.alerts import DeadJob
from app.monitor.collect import Sample
from cp_seed import U1, seed_job, seed_league, seed_user

NOW = datetime(2026, 10, 20, 15, 30, tzinfo=timezone.utc)
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "platform_monitor.py"


class FakeHttp:
    def __init__(self, status=200, body=b""):
        self.status, self.body, self.calls = status, body, []

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, headers, body))
        return self.status, self.body


def by_metric(result):
    return {s.metric: s for s in result.samples}


# ---- collectors ---------------------------------------------------------------------------------------------

def test_postgres_collector_reads_size_and_accounts(pg):
    with pg.connect() as su:
        seed_user(su, U1)
        su.commit()
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as con:
        res = collect.collect_postgres(con)
    assert res.status == "ok"
    s = by_metric(res)
    assert s["supabase_accounts"].used == 1 and s["supabase_accounts"].quota == 50_000
    assert s["supabase_db_bytes"].used > 0 and s["supabase_db_bytes"].quota == 500_000_000


def test_postgres_collector_without_a_connection_is_not_configured():
    assert collect.collect_postgres(None).status == "not_configured"


def test_r2_collector_sums_sizes_and_counts_objects():
    res = collect.collect_r2(lambda: [("leagues/a/1.duckdb", 1_000), ("global/g.duckdb", 2_500)])
    s = by_metric(res)
    assert res.status == "ok"
    assert (s["r2_storage_bytes"].used, s["r2_storage_bytes"].quota) == (3_500, 10_000_000_000)
    assert (s["r2_objects"].used, s["r2_objects"].quota) == (2, None)          # information only


def test_r2_collector_against_the_fake_s3_listing():
    from app.db import r2
    from fake_r2 import FakeR2
    fake = FakeR2()
    try:
        fake.objects.update({"a": b"x" * 10, "b/c": b"y" * 5})
        res = collect.collect_r2(lambda: r2.list_objects(fake.config, ""))
    finally:
        fake.close()
    assert by_metric(res)["r2_storage_bytes"].used == 15 and by_metric(res)["r2_objects"].used == 2


def test_r2_collector_failures_are_unavailable_with_a_code_only():
    from app.db.r2 import R2Error

    def boom():
        raise R2Error(403, "AccessDenied", "")
    res = collect.collect_r2(boom)
    assert (res.status, res.reason, res.samples) == ("unavailable", "AccessDenied", ())
    assert collect.collect_r2(None).status == "not_configured"


def cf_body(groups):
    return json.dumps({"data": {"viewer": {"accounts": [{"r2OperationsAdaptiveGroups": groups}]}}, "errors": None}).encode()


def test_r2_operations_split_class_a_and_b_month_to_date():
    http = FakeHttp(body=cf_body([
        {"dimensions": {"actionType": "PutObject"}, "sum": {"requests": 700}},
        {"dimensions": {"actionType": "ListObjects"}, "sum": {"requests": 50}},
        {"dimensions": {"actionType": "GetObject"}, "sum": {"requests": 9000}},
        {"dimensions": {"actionType": "HeadObject"}, "sum": {"requests": 1000}},
        {"dimensions": {"actionType": "DeleteObject"}, "sum": {"requests": 400}},      # free
    ]))
    res = collect.collect_r2_operations("cf-token", "acct123", http, NOW)
    s = by_metric(res)
    assert res.status == "ok"
    assert (s["r2_class_a_ops"].used, s["r2_class_a_ops"].quota) == (750, 1_000_000)
    assert (s["r2_class_b_ops"].used, s["r2_class_b_ops"].quota) == (10_000, 10_000_000)
    method, url, headers, body = http.calls[0]
    assert (method, url) == ("POST", "https://api.cloudflare.com/client/v4/graphql")
    assert headers["Authorization"] == "Bearer cf-token"
    variables = json.loads(body)["variables"]
    assert variables == {"accountTag": "acct123", "start": "2026-10-01T00:00:00Z", "end": "2026-10-20T15:30:00Z"}


def test_r2_operations_without_a_token_is_not_configured_and_never_calls_out():
    http = FakeHttp()
    assert collect.collect_r2_operations(None, "acct", http, NOW).status == "not_configured"
    assert http.calls == []


@pytest.mark.parametrize("status,body,reason", [
    (401, b'{"errors":[{"message":"bad"}]}', "http_401"),
    (200, b'{"data":null,"errors":[{"message":"no access"}]}', "graphql_error"),
    (200, b'{"data":{"viewer":{"accounts":[]}}}', "bad_shape"),
    (200, b"not json", "bad_shape"),
])
def test_r2_operations_errors_are_unavailable(status, body, reason):
    res = collect.collect_r2_operations("t", "acct", FakeHttp(status, body), NOW)
    assert (res.status, res.reason) == ("unavailable", reason)


def jsonl(*records):
    return "\n".join(json.dumps(r) for r in records).encode() + b"\n"


def rec(service, qty, unit, category="Usage"):
    return {"ServiceName": service, "ConsumedQuantity": qty, "ConsumedUnit": unit, "ChargeCategory": category,
            "BilledCost": 0, "Tags": {"ProjectName": "secret-project"}}


def test_vercel_parser_sums_by_service_and_converts_units():
    body = jsonl(rec("Fluid Active CPU", 3600, "second"), rec("Fluid Active CPU", "1.5", "hour"),
                 rec("Function Invocations", 1200, "Invocations"), rec("Function Invocations", "300", "Invocations"),
                 rec("Fluid Provisioned Memory", 100, "gigabyte-hour"), rec("Fluid Provisioned Memory", 7200, "GB-Seconds"),
                 rec("Fluid Provisioned Memory (Custom Scaling)", 999, "gigabyte-hour"),
                 rec("Fast Data Transfer", 5, "gigabyte"), rec("Function Invocations", 50, "Invocations", category="Credit"))
    s = {x.metric: x for x in collect.parse_vercel_charges(body)}
    assert (s["vercel_active_cpu_hours"].used, s["vercel_active_cpu_hours"].quota) == (2.5, 4)
    assert (s["vercel_invocations"].used, s["vercel_invocations"].quota) == (1500, 1_000_000)
    assert (s["vercel_provisioned_memory_gb_hours"].used, s["vercel_provisioned_memory_gb_hours"].quota) == (102, 360)


def test_vercel_parser_reports_zero_for_a_service_with_no_records():
    s = {x.metric: x for x in collect.parse_vercel_charges(jsonl(rec("Fast Data Transfer", 5, "gigabyte")))}
    assert s["vercel_invocations"].used == 0 and s["vercel_active_cpu_hours"].used == 0


@pytest.mark.parametrize("body", [
    jsonl({"ConsumedQuantity": 1, "ConsumedUnit": "hour"}),                          # no ServiceName
    jsonl({"ServiceName": "Fluid Active CPU", "ConsumedUnit": "hour"}),              # no ConsumedQuantity
    jsonl(rec("Fluid Active CPU", 1, "fortnight")),                                  # a unit we cannot convert
    jsonl(rec("Function Invocations", "lots", "Invocations")),                       # not a number
    b"<html>nope</html>",
    b"[1, 2]\n",
])
def test_vercel_missing_or_odd_fields_make_the_source_unavailable(body):
    with pytest.raises(collect.SourceShapeError):
        collect.parse_vercel_charges(body)
    res = collect.collect_vercel("tok", "team_x", FakeHttp(body=body), NOW)
    assert res.status == "unavailable" and res.samples == ()


def test_vercel_collector_queries_month_to_date_for_the_team():
    http = FakeHttp(body=jsonl(rec("Function Invocations", 10, "Invocations")))
    res = collect.collect_vercel("vtok", "team_abc", http, NOW)
    assert res.status == "ok"
    method, url, headers, _ = http.calls[0]
    assert method == "GET" and headers["Authorization"] == "Bearer vtok"
    assert url == ("https://api.vercel.com/v1/billing/charges?from=2026-10-01T00%3A00%3A00.000Z"
                   "&to=2026-10-20T15%3A30%3A00.000Z&teamId=team_abc")
    assert collect.collect_vercel(None, "team_abc", http, NOW).status == "not_configured"
    assert collect.collect_vercel("vtok", None, FakeHttp(403, b"{}"), NOW).reason == "http_403"


def test_parse_inject():
    assert collect.parse_inject("vercel_invocations=700000/1000000") == Sample(
        "vercel_invocations", 700_000, 1_000_000, "injected", injected=True)
    for bad in ("nope", "x=1", "x=a/b", "Bad Name=1/2", "x=1/0"):
        with pytest.raises(ValueError):
            collect.parse_inject(bad)


# ---- thresholds -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("used,quota,expected", [(699, 1000, False), (69.9, 100, False), (70, 100, True),
                                                 (350, 500, True), (0.7, 1, True), (1, None, False), (5, 4, True)])
def test_threshold_boundary(used, quota, expected):
    assert alerts.is_alerting(Sample("m", used, quota, "bytes")) is expected


def test_evaluate_produces_one_state_per_kind():
    states = {a.kind: a for a in alerts.evaluate(
        [Sample("supabase_db_bytes", 360e6, 500e6, "bytes"), Sample("r2_storage_bytes", 1, 10e9, "bytes")],
        [DeadJob("0f0f0f0f-0000-4000-8000-000000000001", "lg_" + "a" * 22, "scheduled", "http_500", "2026-10-20T10:00Z")])}
    assert states["usage"].active and states["usage"].keys == ("supabase_db_bytes",)
    assert states["usage"].title == "Usage above 70%"
    assert states["dead_jobs"].active and states["dead_jobs"].keys == ("0f0f0f0f-0000-4000-8000-000000000001",)
    assert states["dead_jobs"].title == "Dead sync jobs"
    quiet = {a.kind: a for a in alerts.evaluate([], [])}
    assert not quiet["usage"].active and not quiet["dead_jobs"].active


def test_unknown_inputs_never_clear_an_alert():
    states = {a.kind: a for a in alerts.evaluate([], None, usage_known=False)}
    assert not states["usage"].known and not states["dead_jobs"].known


def test_dead_job_fields_are_sanitised():
    job = alerts.dead_job_from_row(("not-a-uuid", "My League Name", "scheduled", "Error: secret@example.com", None))
    assert job.job_id == "invalid" and job.league_id == "invalid" and job.error_code == "other"


# ---- GitHub issues (fake gh) -------------------------------------------------------------------------------------

class FakeGh:
    """A tiny in-memory GitHub: `gh issue list/create/edit/comment/close` and `gh label create`."""

    def __init__(self):
        self.issues, self.calls, self.next = {}, [], 1

    def __call__(self, args):
        self.calls.append(list(args))
        opt = {args[i]: args[i + 1] for i in range(len(args) - 1) if args[i].startswith("--")}
        if args[:2] == ["issue", "list"]:
            assert opt["--label"] == "platform-alert" and opt["--state"] == "open"
            return json.dumps([{"number": n, "title": i["title"], "body": i["body"]}
                               for n, i in self.issues.items() if i["state"] == "open"])
        if args[:2] == ["issue", "create"]:
            n, self.next = self.next, self.next + 1
            self.issues[n] = {"title": opt["--title"], "body": opt["--body"], "state": "open", "comments": [],
                              "labels": [opt["--label"]]}
            return f"https://github.com/hhavynn/fantasy/issues/{n}\n"
        n = int(args[2])
        if args[:2] == ["issue", "edit"]:
            self.issues[n]["body"] = opt["--body"]
        elif args[:2] == ["issue", "comment"]:
            self.issues[n]["comments"].append(opt["--body"])
        elif args[:2] == ["issue", "close"]:
            self.issues[n]["state"] = "closed"
            self.issues[n]["comments"].append(opt["--comment"])
        return ""


def usage_state(*metrics, known=True):
    return next(a for a in alerts.evaluate([Sample(m, 80, 100, "bytes") for m in metrics], [], usage_known=known)
                if a.kind == "usage")


def test_an_alert_opens_one_issue_then_comments_only_when_the_set_grows():
    gh = FakeGh()
    assert github.sync_alert(usage_state("supabase_db_bytes"), gh) == "opened"
    issue = gh.issues[1]
    assert issue["title"] == "Usage above 70%" and issue["labels"] == ["platform-alert"]
    assert "<!-- state: supabase_db_bytes -->" in issue["body"]
    assert github.sync_alert(usage_state("supabase_db_bytes"), gh) == "unchanged"
    assert issue["comments"] == [] and len(gh.issues) == 1
    assert github.sync_alert(usage_state("supabase_db_bytes", "vercel_invocations"), gh) == "commented"
    assert len(issue["comments"]) == 1 and "vercel_invocations" in issue["comments"][0]
    assert "supabase_db_bytes,vercel_invocations" in issue["body"]
    assert github.sync_alert(usage_state("vercel_invocations"), gh) == "unchanged"     # shrinking set: no email
    assert len(issue["comments"]) == 1


def test_an_alert_closes_with_a_comment_when_it_clears():
    gh = FakeGh()
    github.sync_alert(usage_state("supabase_db_bytes"), gh)
    assert github.sync_alert(usage_state(), gh) == "closed"
    assert gh.issues[1]["state"] == "closed" and "Cleared" in gh.issues[1]["comments"][-1]
    assert github.sync_alert(usage_state(), gh) == "none"
    assert github.sync_alert(usage_state("supabase_db_bytes"), gh) == "opened"          # a new episode, a new issue
    assert len(gh.issues) == 2


def test_an_unknown_state_never_closes_the_issue():
    gh = FakeGh()
    github.sync_alert(usage_state("supabase_db_bytes"), gh)
    assert github.sync_alert(usage_state(known=False), gh) == "unknown"
    assert gh.issues[1]["state"] == "open"


def test_issues_of_other_kinds_are_left_alone():
    gh = FakeGh()
    github.sync_alert(usage_state("supabase_db_bytes"), gh)
    dead = next(a for a in alerts.evaluate([], []) if a.kind == "dead_jobs")
    assert github.sync_alert(dead, gh) == "none"
    assert gh.issues[1]["state"] == "open"


def test_marker_round_trip():
    assert github.parse_marker("text\n" + github.marker(("a", "b"))) == {"a", "b"}
    assert github.parse_marker(github.marker(())) == set()
    assert github.parse_marker("no marker") is None


# ---- the script and the public-output audit ------------------------------------------------------------------

def load_script():
    if str(SCRIPT.parent) not in sys.path:          # the script imports its _bootstrap sibling
        sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("platform_monitor", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SECRET_NAME = "Hidden Valley Ranch Dynasty"
SECRET_EMAIL = "someone.private@example.com"


@pytest.fixture
def world(pg):
    with pg.connect() as su:
        su.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s)", [U1, SECRET_EMAIL])
        su.execute("INSERT INTO fi.app_users (id) VALUES (%s)", [U1])
        lid = seed_league(su, name=SECRET_NAME)
        job = seed_job(su, lid, status="dead", kind="scheduled", error_code="http_500")
        su.execute("UPDATE fi.sync_jobs SET finished_at = now(), error_detail = %s WHERE id = %s",
                   [f"{SECRET_NAME} {SECRET_EMAIL}", job])
        su.commit()
    return {"league": lid, "job": job}


def run_script(pg, gh, *argv, http=None):
    mod = load_script()
    out = io.StringIO()
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as con:
        code = mod.run(mod.parse_args(list(argv)), sync_con=con, http=http or FakeHttp(500, b""), gh=gh,
                       r2_lister=lambda: [("leagues/x.duckdb", 123)], env={}, out=out, now=NOW)
    return code, out.getvalue()


def test_the_monitor_stores_samples_alerts_and_stays_public_safe(pg, world):
    gh = FakeGh()
    code, text = run_script(pg, gh, "--inject", "vercel_invocations=800000/1000000")
    assert code == 0
    with pg.connect() as su:
        stored = {r[0] for r in su.execute("SELECT metric FROM fi.usage_samples")}
    assert {"supabase_db_bytes", "supabase_accounts", "r2_storage_bytes", "r2_objects"} <= stored
    assert "vercel_invocations" not in stored            # an injected sample proves the alert; it is never stored
    titles = {i["title"]: i for i in gh.issues.values()}
    assert set(titles) == {"Usage above 70%", "Dead sync jobs"}
    assert world["job"] in titles["Dead sync jobs"]["body"] and world["league"] in titles["Dead sync jobs"]["body"]
    assert "vercel_invocations" in titles["Usage above 70%"]["body"]
    public = text + json.dumps(gh.calls)
    for secret in (SECRET_NAME, SECRET_EMAIL, "secret-project", "@"):
        assert secret not in public
    assert "r2_operations: not_configured" in text and "vercel: not_configured" in text


def test_the_monitor_prunes_old_samples_and_dry_run_never_calls_gh(pg, world):
    with pg.connect() as su:
        su.execute("INSERT INTO fi.usage_samples (metric, used, quota, unit, sampled_at) "
                   "VALUES ('r2_objects', 1, NULL, 'objects', now() - interval '100 days')")
        su.commit()
    gh = FakeGh()
    code, text = run_script(pg, gh, "--dry-run")
    assert code == 0 and gh.calls == []
    with pg.connect() as su:
        assert su.execute("SELECT count(*) FROM fi.usage_samples WHERE sampled_at < now() - interval '90 days'"
                          ).fetchone() == (0,)
    assert "dry run" in text


def test_a_second_run_with_the_same_state_does_not_comment_and_a_cleared_run_closes(pg, world):
    gh = FakeGh()
    run_script(pg, gh, "--inject", "vercel_invocations=800000/1000000")
    run_script(pg, gh, "--inject", "vercel_invocations=800000/1000000")
    assert all(i["comments"] == [] for i in gh.issues.values())
    with pg.connect() as su:
        su.execute("UPDATE fi.sync_jobs SET status = 'succeeded'")
        su.commit()
    run_script(pg, gh)
    assert all(i["state"] == "closed" for i in gh.issues.values())


def test_bad_inject_is_a_usage_error():
    mod = load_script()
    with pytest.raises(SystemExit):
        mod.parse_args(["--inject", "League Name=1/2"])


def test_unconfigured_monitor_does_nothing(capsys):
    mod = load_script()
    assert mod.main([], env={}) == 0
    assert "not configured" in capsys.readouterr().out


def _audit_module():
    spec = importlib.util.spec_from_file_location("audit_drain_log", SCRIPT.parent / "audit_drain_log.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_line_the_monitor_prints_passes_the_public_log_audit(pg, world):
    audit = _audit_module()
    gh = FakeGh()
    texts = [run_script(pg, gh, "--inject", "vercel_invocations=800000/1000000")[1],
             run_script(pg, gh, "--dry-run")[1]]
    with pg.connect() as su:
        su.execute("UPDATE fi.sync_jobs SET status = 'succeeded'")
        su.commit()
    texts.append(run_script(pg, gh)[1])
    for text in texts:
        assert audit.audit(text.splitlines(), audit.MONITOR_ALLOWED) == [], text
    assert audit.audit(["a league named Secret Club"], audit.MONITOR_ALLOWED) != []
    assert audit.audit(texts[0].splitlines()) != [], "the drain profile does not accept monitor lines"
