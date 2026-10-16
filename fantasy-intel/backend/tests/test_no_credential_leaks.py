"""No external-service credential is committed, logged, stored, echoed, or left in a repr / exception chain.

Three layers:
  1. a scan of every file git tracks (and every untracked, non-ignored file, so a secret is caught before it is
     committed) for credential shapes, with an explicit allowlist of the synthetic values the tests use;
  2. a logging / response / storage capture through a connect flow that fails with an exception whose message
     contains fake espn_s2 / SWID / token values;
  3. repr / str / exception-chain checks on the objects that carry credentials.

A new fake credential in a test is fine, but must be added to ALLOWED_SYNTHETIC on purpose: that is the point.
`test_secN_...` tests reproduce fixed findings of the adversarial review and now guard against regressions.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import pickle
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.api.leagues_v2 import provider_registry
from app.config import get_settings
from app.leagues.auth import HEADER, sign_user_assertion
from app.leagues.capabilities import Capability, CapabilityState, ProviderCapabilities, Support
from app.leagues.context import AccessMethod, LeagueKey, Principal, Provider
from app.leagues.credentials import EspnCookies, OwnerEnvCredentials, Secret
from app.leagues.models import (
    LeagueSettings, LineupRules, LineupSlot, PlayoffSettings, Provenance, Roster, RosterEntry, ScoringRules,
    SlotKind, Sourced, Team, TradeSettings, WaiverSettings,
)
from app.leagues.identity import ProviderPlayerId
from app.leagues.providers import registry as real_registry
from app.leagues.providers.base import LeagueAccess, LeagueProvider, ProviderError, ProviderUnavailable
from app.leagues.store import TenancyStore, sanitize_error

REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET = "u" * 48
API_KEY = "service-api-key-" + "k" * 32

# Fake credentials used by this file (and by the tests of the sanitizer). They look real enough to trigger a
# redaction heuristic, which is the point, and are allowlisted for the repository scan below.
FAKE_S2 = "AEBfakeespns2cookie%2Bvalue0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop0123456789"
FAKE_SWID = "{ABCDEF01-2345-6789-ABCD-EF0123456789}"
FAKE_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlLW1vdGhlcmR1Y2sifQ.ZmFrZXNpZ25hdHVyZWZha2VzaWduYXR1cmU"
FAKE_BEARER = "bearer-token-Qw3rT7yU1oP5aS9dF2gH6jK0lZ4xC8vB"
SECRET_VALUES = (FAKE_S2, FAKE_SWID, FAKE_TOKEN, FAKE_BEARER)

# ---- 1. repository scan ------------------------------------------------------------------------

# Synthetic values that legitimately appear in tracked tests, copied verbatim. Anything else that matches a
# credential shape fails the scan, including a new fake: add it here on purpose.
ALLOWED_SYNTHETIC = {
    FAKE_S2, FAKE_SWID, FAKE_TOKEN,
    # tests/test_leagues_api.py, tests/test_leagues_store.py, tests/test_multileague_isolation.py
    "AEBsupersecretcookievalue0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghij",
    "AEBkYWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXowMTIzNDU2Nzg5QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo",
    "{B1A2C3D4-0000-4321-8765-0123456789AB}",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlc2lnbmF0dXJlMTIzNDU2",
    "AEBfakeespns2cookie%2Bvalue0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop0123456789",
}

_PLACEHOLDER = re.compile(r"(?i)^(?:|\.\.\.|x+|\*+|changeme|change-me|your[-_].*|<.*>|\$.*|\{\{.*|example.*|todo|none|null|"
                          r"secret|password|redacted|replace[-_].*|test.*)$")

PATTERNS: dict[str, re.Pattern] = {
    "espn_s2 value": re.compile(r"""(?i)\bespn_s2["']?\s*[=:]\s*["']?([A-Za-z0-9%+/=_-]{40,})"""),
    "espn_s2 shaped string": re.compile(r"\bAE[A-D][A-Za-z0-9%+/=_-]{60,}"),
    "SWID guid in braces": re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}"),
    "JWT (MotherDuck token shape)": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "Bearer token": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "API secret key (sk-)": re.compile(r"\bsk-[A-Za-z0-9_-]{32,}"),
}
# `NAME=value` with a real-looking value (.env files, docs, CI, shell). Python / TypeScript sources are
# excluded from this one because they assign test constants all the time; the shaped patterns above cover them.
ENV_ASSIGNMENT = re.compile(
    r"""(?m)\b(FANTASY_API_KEY|API_KEY|SITE_PASSWORD|MOTHERDUCK_TOKEN|motherduck_token|ESPN_S2|SWID|"""
    r"""FANTASY_USER_ASSERTION_SECRET|YAHOO_CLIENT_SECRET)\b["']?[ \t]*[=:][ \t]*["']?([^\s"'<>$#`),]+)""")
ENV_SCAN_SKIP_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs", ".lock", ".json", ".svg", ".css")
MAX_SCAN_BYTES = 2_000_000


def tracked_and_new_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             cwd=REPO_ROOT, capture_output=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available; the repository scan needs `git ls-files`")
    return [REPO_ROOT / name for name in out.decode().split("\0") if name]


def scan_text(text: str, name: str, *, env_assignments: bool = True) -> list[str]:
    found = []
    for label, pattern in PATTERNS.items():
        for m in pattern.finditer(text):
            value = m.group(1) if pattern.groups else m.group(0)
            if value in ALLOWED_SYNTHETIC or m.group(0) in ALLOWED_SYNTHETIC:
                continue
            found.append(f"{name}: {label}: {m.group(0)[:12]}...{m.group(0)[-4:]}")
    if env_assignments:
        for m in ENV_ASSIGNMENT.finditer(text):
            if not _PLACEHOLDER.match(m.group(2)) and m.group(2) not in ALLOWED_SYNTHETIC:
                found.append(f"{name}: {m.group(1)} assigned a literal value: {m.group(2)[:6]}...")
    return found


def test_no_credentials_are_committed_to_the_repository():
    files = tracked_and_new_files()
    assert len(files) > 100, "git ls-files returned suspiciously few files"
    findings: list[str] = []
    for path in files:
        try:
            if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue                                                  # binary or unreadable: not a text credential
        rel = path.relative_to(REPO_ROOT).as_posix()
        findings += scan_text(text, rel, env_assignments=not rel.endswith(ENV_SCAN_SKIP_SUFFIXES))
    assert not findings, ("credential-shaped strings found in the repository (remove them, or if they are fake "
                          "test values add them to ALLOWED_SYNTHETIC on purpose):\n  " + "\n  ".join(findings))


def test_the_env_example_files_ship_empty_credentials():
    examples = [p for p in tracked_and_new_files() if re.search(r"(^|/)\.env[^/]*\.(example|sample|template)$|(^|/)\.env\.example$", p.as_posix())]
    assert examples, "expected a tracked .env.example"
    for path in examples:
        for m in ENV_ASSIGNMENT.finditer(path.read_text()):
            assert _PLACEHOLDER.match(m.group(2)), f"{path.name}: {m.group(1)} has a value"


def test_the_scanner_catches_each_credential_shape_and_honours_the_allowlist():
    """Positive samples are assembled at run time so this file never contains a matching literal itself."""
    uuid = "-".join(["12345678", "9abc", "def0", "1234", "56789abcdef0"])
    samples = {
        "espn_s2 value": "espn_s2=" + "A" * 50,
        "espn_s2 shaped string": "AEB" + "z9%2B" * 25,
        "SWID guid in braces": "{" + uuid + "}",
        "JWT (MotherDuck token shape)": ".".join(["eyJ" + "a" * 20, "eyJ" + "b" * 20, "c" * 20]),
        "Bearer token": "Authorization: Bearer " + "q" * 30,
        "private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
        "GitHub token": "ghp_" + "a" * 36,
        "AWS access key": "AKIA" + "A" * 16,
        "Slack token": "xoxb-" + "1" * 12,
        "API secret key (sk-)": "sk-" + "a" * 40,
    }
    assert set(samples) == set(PATTERNS)
    for label, sample in samples.items():
        assert any(label in f for f in scan_text(sample, "sample.txt")), label
    env_samples = ["FANTASY_API_KEY=" + "v" * 24, "MOTHERDUCK_TOKEN: " + "t" * 30, "SITE_PASSWORD='hunter" + "2'", "SWID=" + uuid]
    for sample in env_samples:
        assert scan_text(sample, "x.env"), sample
    # placeholders and empty values are fine; allowlisted fakes are fine
    for ok in ("FANTASY_API_KEY=", "ESPN_S2=\nSWID=", "FANTASY_API_KEY=<your key>", "FANTASY_API_KEY=${KEY}",
               "API_KEY=changeme", "SITE_PASSWORD=your-password", FAKE_SWID, f'espn_s2="{FAKE_S2}"'):
        assert scan_text(ok, "x.env") == [], ok


# ---- 2. capture: logs, responses, stored errors ---------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("SLEEPER_SOURCE", "ESPN_PUBLIC_IMPORT_ENABLED", "YAHOO_CLIENT_ID", "MULTILEAGUE_UI_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "1")
    monkeypatch.setenv("FANTASY_TENANCY_DB_PATH", str(tmp_path / "tenancy.duckdb"))
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("FANTASY_API_KEY", API_KEY)
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "owner")
    monkeypatch.setenv("ESPN_S2", FAKE_S2)
    monkeypatch.setenv("SWID", FAKE_SWID)
    real_registry.reset_providers()
    yield monkeypatch
    real_registry.reset_providers()


class LeakyEspn(LeagueProvider):
    """An ESPN adapter whose upstream failure messages echo the request (cookies, tokens) like a careless
    HTTP library would."""
    provider = Provider.ESPN

    def __init__(self):
        self.fail_with: Exception | None = None
        self.seen: list[object] = []

    def capabilities(self):
        ok = CapabilityState(Support.SUPPORTED)
        return ProviderCapabilities(Provider.ESPN, True, None,
                                    {c: ok for c in (Capability.LEAGUE_SETTINGS, Capability.TEAMS, Capability.ROSTERS)})

    def _go(self, key, access):
        self.seen.append(access.credential)
        if self.fail_with is not None:
            raise self.fail_with
        return Provenance(Provider.ESPN, "espn:test", datetime(2026, 9, 1, 12))

    def fetch_settings(self, key, access):
        prov = self._go(key, access)
        lineup = LineupRules((LineupSlot("QB", 1, SlotKind.STARTER, frozenset({"QB"}), "QB"),))
        return Sourced(LeagueSettings(key, "Owner League", 2, lineup, ScoringRules({"53": 1.0}, {"rec": 1.0}, ()),
                                      WaiverSettings(None), TradeSettings(), PlayoffSettings(), None, False), prov)

    def fetch_teams(self, key, access):
        return Sourced([Team(key, "1", "Team 1", ("espn:sha256:abc",)), Team(key, "2", "Team 2", ())],
                       self._go(key, access))

    def fetch_rosters(self, key, access):
        entry = RosterEntry(ProviderPlayerId(Provider.ESPN, "1"), SlotKind.STARTER, "QB", name="QB One", position="QB")
        return Sourced([Roster(key, "1", (entry,)), Roster(key, "2", ())], self._go(key, access))

    def _no(self, *a, **k):
        raise NotImplementedError

    discover_leagues = fetch_scoring = fetch_matchups = fetch_standings = _no
    fetch_free_agents = fetch_transactions = fetch_projections = _no


class Registry:
    def __init__(self, espn):
        self.espn = espn

    def get_provider(self, provider):
        if provider is Provider.ESPN:
            return self.espn
        raise ProviderUnavailable("not enabled")

    def all_capabilities(self):
        return [self.espn.capabilities()]


@pytest.fixture
def leaky(env):
    from app.main import app
    espn = LeakyEspn()
    app.dependency_overrides[provider_registry] = lambda: Registry(espn)
    yield espn, TestClient(app)
    app.dependency_overrides.clear()


def hdr(user, **kw):
    return {"X-API-Key": API_KEY, HEADER: sign_user_assertion(user, SECRET, time.time(), **kw)}


def dump_tenancy_db() -> str:
    path = get_settings().tenancy_db_path
    con = duckdb.connect(str(path), read_only=True)
    try:
        chunks = []
        for (table,) in con.execute("SELECT table_name FROM duckdb_tables()").fetchall():
            chunks.append(f"{table}: {con.execute(f'SELECT * FROM {table}').fetchall()!r}")
        return "\n".join(chunks)
    finally:
        con.close()


def assert_clean(text: str, where: str, extra=()):
    for secret in (*SECRET_VALUES, SECRET, API_KEY, *extra):
        assert secret not in text, f"{secret[:10]}... leaked into {where}"


FAILURES = [
    ("cookie header", lambda: RuntimeError(f"HTTPSConnectionPool: request failed; Cookie: espn_s2={FAKE_S2}; SWID={FAKE_SWID}")),
    ("keyed secrets", lambda: ValueError(f"bad response for espn_s2={FAKE_S2} SWID={FAKE_SWID} access_token={FAKE_BEARER}")),
    ("bearer + jwt", lambda: KeyError(f"upstream 401 Authorization: Bearer {FAKE_BEARER} token={FAKE_TOKEN}")),
    ("bare cookie value", lambda: OSError(f"connection reset while sending {FAKE_S2} and {FAKE_SWID}")),
    ("chained", lambda: _chained()),
    ("multi-line", lambda: RuntimeError(f"first line is harmless\nsecond line: espn_s2={FAKE_S2}\nthird {FAKE_SWID}")),
]


def _chained():
    try:
        try:
            raise OSError(f"inner Cookie: espn_s2={FAKE_S2}")
        except OSError as inner:
            raise RuntimeError(f"outer SWID={FAKE_SWID}") from inner
    except RuntimeError as err:
        return err


@pytest.mark.parametrize("label,make", FAILURES, ids=[f[0] for f in FAILURES])
def test_a_failing_connect_leaks_no_credential_into_logs_responses_or_storage(leaky, caplog, label, make):
    espn, client = leaky
    caplog.set_level(logging.DEBUG)                                      # root logger, every level
    ok = client.post("/v2/leagues", json={"provider": "espn", "external_league_id": "777", "season": 2026},
                     headers=hdr("owner"))
    assert ok.status_code == 201 and ok.json()["access"] == "owner_env"
    assert isinstance(espn.seen[0], EspnCookies)                         # the owner's cookies really were in play
    lid = ok.json()["league_id"]

    espn.fail_with = make()
    bad = client.post("/v2/leagues", json={"provider": "espn", "external_league_id": "777", "season": 2026},
                      headers=hdr("owner"))
    assert bad.status_code in (502, 503, 404), bad.text
    assert_clean(bad.text, "the HTTP error response")
    assert_clean(json.dumps(dict(bad.headers)), "the response headers")

    view = client.get(f"/v2/leagues/{lid}", headers=hdr("owner")).json()
    assert view["sync"]["status"] == "failed" and view["sync"]["last_error"]
    assert_clean(json.dumps(view), "the league view")
    stored = rows_last_error()
    assert stored and all(len(e) <= 240 for e in stored)
    assert_clean(" ".join(stored), "league_sync_status.last_error")
    assert_clean(dump_tenancy_db(), "the tenancy database")
    for path in ("", "/settings", "/teams"):
        assert_clean(client.get(f"/v2/leagues/{lid}{path}", headers=hdr("owner")).text, f"GET league{path}")
    assert_clean(client.get("/v2/leagues", headers=hdr("owner")).text, "league list")
    assert_clean(client.get("/v2/providers", headers=hdr("owner")).text, "providers")

    assert_clean(caplog.text, "captured logs")
    for record in caplog.records:
        assert_clean(record.getMessage(), f"log record {record.name}")
        assert_clean(repr(record.args), "log record args")
        assert record.exc_info is None or record.exc_info[1] is None, "an exception traceback was logged"
        assert not record.exc_text, "a formatted traceback was logged"
    assert any("league connect failed" in r.getMessage() for r in caplog.records), "the failure was not logged at all"


def rows_last_error() -> list[str]:
    con = duckdb.connect(str(get_settings().tenancy_db_path), read_only=True)
    try:
        return [r[0] for r in con.execute("SELECT last_error FROM league_sync_status WHERE last_error IS NOT NULL").fetchall()]
    finally:
        con.close()


def test_a_user_who_is_not_connected_cannot_write_error_text_into_anyones_status(leaky):
    espn, client = leaky
    lid = client.post("/v2/leagues", json={"provider": "espn", "external_league_id": "777", "season": 2026},
                      headers=hdr("owner")).json()["league_id"]
    espn.fail_with = RuntimeError(f"stranger-controlled text espn_s2={FAKE_S2}")
    # a stranger is not the owner: public access to a private league never reaches storage
    r = client.post("/v2/leagues", json={"provider": "espn", "external_league_id": "777", "season": 2026},
                    headers=hdr("mallory"))
    assert r.status_code in (502, 503) and FAKE_S2 not in r.text
    assert client.get(f"/v2/leagues/{lid}", headers=hdr("owner")).json()["sync"]["last_error"] is None
    assert rows_last_error() == []


def test_the_assertion_secret_api_key_and_headers_are_never_logged_or_stored(leaky, caplog):
    espn, client = leaky
    caplog.set_level(logging.DEBUG)
    good = hdr("owner")
    bad = {"X-API-Key": API_KEY, HEADER: good[HEADER][:-4] + "AAAA"}
    expired = {"X-API-Key": API_KEY, HEADER: sign_user_assertion("owner", SECRET, time.time() - 5000)}
    client.post("/v2/leagues", json={"provider": "espn", "external_league_id": "777", "season": 2026}, headers=good)
    for headers in (bad, expired, {"X-API-Key": "wrong-key-value"}):
        client.get("/v2/leagues", headers=headers)
    blob = caplog.text + dump_tenancy_db()
    for value in (good[HEADER], bad[HEADER], expired[HEADER], SECRET, API_KEY, "wrong-key-value"):
        assert value not in blob
        assert value.split(".")[-1] not in blob or len(value.split(".")[-1]) < 8


@pytest.mark.parametrize("channel", ["body", "cookie header", "query", "header", "nested"])
def test_user_supplied_cookies_are_rejected_or_ignored_in_every_channel(leaky, channel):
    espn, client = leaky
    payload = {"provider": "espn", "external_league_id": "777", "season": 2026}
    kwargs: dict = {"headers": hdr("alice")}                              # Alice is NOT the owner
    espn.fail_with = None
    path = "/v2/leagues"
    if channel == "body":
        payload.update(espn_s2=FAKE_S2, SWID=FAKE_SWID)
    elif channel == "nested":
        payload.update(credentials={"espn_s2": FAKE_S2, "swid": FAKE_SWID})
    elif channel == "cookie header":
        kwargs["headers"]["Cookie"] = f"espn_s2={FAKE_S2}; SWID={FAKE_SWID}"
    elif channel == "query":
        path += f"?espn_s2={FAKE_S2}&swid={FAKE_SWID}"
    else:
        kwargs["headers"].update({"X-ESPN-S2": FAKE_S2, "X-ESPN-SWID": FAKE_SWID, "Authorization": f"Bearer {FAKE_BEARER}"})
    r = client.post(path, json=payload, **kwargs)
    if channel in ("body", "nested"):
        assert r.status_code == 400, r.text                              # unknown fields are refused outright
    else:
        # ignored: Alice is not the owner, so her connect is a PUBLIC attempt on a private league
        assert r.status_code == 422, r.text
    assert_clean(r.text, f"the response ({channel})")
    assert all(cred is None for cred in espn.seen if cred is not None) or not espn.seen
    assert not any(isinstance(cred, EspnCookies) for cred in espn.seen), "user input reached the ESPN client as credentials"
    assert_clean(dump_tenancy_db() if get_settings().tenancy_db_path.exists() else "", "the tenancy database")
    assert client.get("/v2/leagues", headers=hdr("alice")).json() == {"leagues": []}


def test_the_owner_cookies_come_only_from_the_server_environment_and_only_for_the_owner(leaky):
    espn, client = leaky
    client.post("/v2/leagues", json={"provider": "espn", "external_league_id": "777", "season": 2026}, headers=hdr("owner"))
    cookies = espn.seen[0]
    assert isinstance(cookies, EspnCookies) and cookies.espn_s2 == FAKE_S2 and cookies.swid == FAKE_SWID
    espn.seen.clear()
    client.post("/v2/leagues", json={"provider": "espn", "external_league_id": "778", "season": 2026}, headers=hdr("alice"))
    assert not any(isinstance(c, EspnCookies) for c in espn.seen)


# ---- 3. repr / str / exception chains ---------------------------------------------------------------

def test_credential_objects_never_reveal_their_values(env):
    cookies = EspnCookies(FAKE_S2, FAKE_SWID)
    secret = Secret(FAKE_TOKEN)
    access = LeagueAccess(AccessMethod.OWNER_ENV, cookies)
    holders = [cookies, secret, access, LeagueAccess(AccessMethod.OWNER_ENV, secret), [cookies], {"c": cookies},
               (access, secret)]
    for obj in holders:
        for text in (repr(obj), str(obj), f"{obj}", f"{obj!r}", "%s" % (obj,), "%r" % (obj,), format(obj, ""), ascii(obj)):
            assert_clean(text, f"repr/str of {type(obj).__name__}")
    for fn in (pickle.dumps, copy.copy, copy.deepcopy, json.dumps, dataclasses.asdict):
        for obj in (cookies, secret, access):
            try:
                result = fn(obj)
            except (TypeError, AttributeError):
                continue                                                  # refusing to serialize / deep-copy is the goal
            # a shallow copy of the holder is harmless, but nothing may come out as data
            assert not isinstance(result, (str, bytes, bytearray, dict, list)), f"{fn.__name__} serialized {type(obj).__name__}"
            assert_clean(repr(result), f"{fn.__name__}({type(obj).__name__})")
    assert not hasattr(cookies, "__dict__") and not hasattr(secret, "__dict__")
    with pytest.raises(AttributeError):
        cookies.espn_s2 = "x"                                             # type: ignore[misc]
    assert OwnerEnvCredentials().for_owner(Principal("owner"), Provider.ESPN).espn_s2 == FAKE_S2   # the one way out


def test_a_license_to_inspect_a_context_does_not_expose_credentials(env, tmp_path):
    store = TenancyStore(tmp_path / "s.duckdb", capabilities=lambda: None)
    ctx = store.connect(Principal("owner"), LeagueKey(Provider.ESPN, "777", 2026), AccessMethod.OWNER_ENV, name="L",
                        is_public=False, data_origin="live")
    assert_clean(repr(ctx) + str(ctx) + repr(ctx.freshness) + repr(ctx.principal), "LeagueContext repr")
    assert_clean(dump_tenancy_db_at(store.path), "the store file")


def dump_tenancy_db_at(path) -> str:
    con = duckdb.connect(str(path), read_only=True)
    try:
        return "\n".join(f"{t}: {con.execute(f'SELECT * FROM {t}').fetchall()!r}"
                         for (t,) in con.execute("SELECT table_name FROM duckdb_tables()").fetchall())
    finally:
        con.close()


def _chain(err: BaseException):
    seen = []
    while err is not None and err not in seen:
        seen.append(err)
        err = err.__cause__ or err.__context__
    return seen


def _chained_oserror():
    try:
        try:
            raise ValueError(f"inner Cookie: espn_s2={FAKE_S2}")
        except ValueError as inner:
            raise OSError(f"outer SWID={FAKE_SWID}") from inner
    except OSError as err:
        return err


# Exception types the ESPN adapter classifies (ValueError/KeyError/TypeError/AttributeError, OSError)
CLASSIFIED = [f for f in FAILURES if f[0] in ("keyed secrets", "bearer + jwt", "bare cookie value")] + [
    ("chained oserror", _chained_oserror)]
# Anything else an upstream library might raise (RuntimeError, custom errors, ...)
UNCLASSIFIED = [f for f in FAILURES if f[0] in ("cookie header", "chained", "multi-line")]


def _espn_calls(adapter, key, access):
    return (lambda: adapter.fetch_settings(key, access), lambda: adapter.fetch_teams(key, access),
            lambda: adapter.fetch_rosters(key, access), lambda: adapter.fetch_scoring(key, access),
            lambda: adapter.fetch_matchups(key, access, week=1), lambda: adapter.fetch_standings(key, access),
            lambda: adapter.fetch_free_agents(key, access), lambda: adapter.fetch_transactions(key, access, week=1),
            lambda: adapter.fetch_projections(key, access, week=1))


def _exploding_espn(make):
    from app.leagues.providers.espn import EspnProvider

    class Boom:
        def __getattr__(self, name):
            def call(*a, **k):
                raise make()
            return call

    return EspnProvider(lambda k, a: Boom(), public_import_enabled=False, owner_credentials_configured=True)


def _owner_access():
    return LeagueAccess(AccessMethod.OWNER_ENV, EspnCookies(FAKE_S2, FAKE_SWID))


@pytest.mark.parametrize("label,make", CLASSIFIED, ids=[f[0] for f in CLASSIFIED])
def test_the_espn_adapter_raises_sanitized_errors_with_no_cause_or_context(env, label, make):
    adapter = _exploding_espn(make)
    key = LeagueKey(Provider.ESPN, "777", 2026)
    for call in _espn_calls(adapter, key, _owner_access()):
        with pytest.raises(ProviderError) as caught:
            call()
        err = caught.value
        for link in _chain(err):
            assert_clean(f"{type(link).__name__}: {link}", "an exception message")
            assert_clean(repr(link), "an exception repr")
        assert err.__cause__ is None, "the upstream exception is chained"
        assert err.__context__ is None, "the upstream exception is kept as __context__"


@pytest.mark.parametrize("label,make", UNCLASSIFIED, ids=[f[0] for f in UNCLASSIFIED])
def test_sec8_the_espn_adapter_converts_unrecognised_exceptions_to_provider_errors(env, label, make):
    adapter = _exploding_espn(make)
    key = LeagueKey(Provider.ESPN, "777", 2026)
    for call in _espn_calls(adapter, key, _owner_access()):
        with pytest.raises(ProviderError) as caught:
            call()
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert_clean(str(caught.value), "an exception message")


def test_the_sleeper_adapter_raises_sanitized_errors_with_no_cause_or_context(env):
    from app.leagues.providers.sleeper import SleeperProvider
    from app.sleeper.client import SleeperClient

    def transport(url, timeout):
        raise OSError(f"cannot reach {url} Cookie: espn_s2={FAKE_S2} Bearer {FAKE_BEARER}")

    client = SleeperClient(transport=transport, sleep=lambda s: None, cache_dir="/nonexistent/none", max_retries=0)
    provider = SleeperProvider(client=client, idmap=object(), source="fixture")
    with pytest.raises(ProviderError) as err:
        provider.fetch_settings(LeagueKey(Provider.SLEEPER, "1180000000000000001", 2026), LeagueAccess(AccessMethod.PUBLIC))
    assert_clean(str(err.value), "the Sleeper error")
    assert err.value.__cause__ is None and err.value.__context__ is None


# ---- sanitizer regression ----------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    f"Cookie: espn_s2={FAKE_S2}; SWID={FAKE_SWID}",
    f"set-cookie: espn_s2={FAKE_S2}",
    f"Bearer {FAKE_BEARER}",
    f"token={FAKE_BEARER}",
    f"url https://x/y?api_key={FAKE_BEARER}&x=1",
    f"{FAKE_TOKEN}",
    f"password: hunter2hunter2",
    f"x-api-key: {API_KEY}",
    f"swid {FAKE_SWID}",
])
def test_sanitize_error_removes_credentials(message):
    out = sanitize_error(RuntimeError(message))
    assert_clean(out, "sanitize_error output")
    for fragment in ("hunter2hunter2",):
        assert fragment not in out


def test_sanitize_error_is_bounded_single_line_and_keeps_the_error_class():
    out = sanitize_error(ValueError("first line\n" + "second line espn_s2=" + FAKE_S2 + "\n" + "x" * 5000))
    assert out.startswith("ValueError: first line") and "\n" not in out and len(out) <= 240
    long_text = "the provider said no " * 400
    assert sanitize_error(long_text).endswith("…") and len(sanitize_error(long_text)) <= 240


@pytest.mark.parametrize("message,secret", [
    ("401 Unauthorized. Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==", "dXNlcjpwYXNzd29yZDEyMw=="),
    ("request failed with Authorization: Token abcdef0123456789ghij", "abcdef0123456789ghij"),
    ("retry with Authorization: Digest response=3f6e9a1c2b", "3f6e9a1c2b"),
])
def test_sec4_sanitize_error_removes_values_that_follow_an_authorization_scheme(message, secret):
    assert secret not in sanitize_error(RuntimeError(message))


def test_sec7_settings_repr_does_not_print_credentials(env):
    text = repr(get_settings()) + str(get_settings())
    for secret in (FAKE_S2, FAKE_SWID, API_KEY, SECRET):
        assert secret not in text
