"""Run one claimed sync job (spec §3.3–3.7). Called by scripts/sync_drain.py inside GitHub Actions.

    probe                 anonymous visibility check (restrict at once on "private"; restore only after a public answer)
    initial / scheduled / manual
                          fetch (cookies only for a private league's own endpoint) -> build -> publish (fenced)
    recompute             the previous version's raw payloads against the current global file, no fetch

A heartbeat thread (its own connection) extends the lease every 30 s; when the lease is lost the job stops at the
next stage and publishes nothing. Outcomes follow app/sync/failures.py; nothing here deletes a credential or data.
Every line printed goes through the redacting Printer and carries only ids, durations and codes.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psycopg

from ..db.connection import utcnow
from ..db.league_files import global_key, league_key
from ..db.league_split import VERSION_RE
from . import espn_source, queue
from .build import build_league_file
from .failures import classify
from .publish import publish_job, raw_key
from .raw_store import dump_payloads, load_payloads
from .redact import Printer

HEARTBEAT_S = 30


@dataclass
class Context:
    sync_dsn: str
    objects: Any                                  # ObjectWriter with fetch/get_bytes (R2Writer or DirWriter)
    printer: Printer
    owner: str                                    # lease owner: "<run id>-<worker n>", never a hostname
    keyring: Any = None                           # credential_crypto.PrivateKeyring, None when no key is configured
    client: Callable = espn_source.espn_client    # (external_id, season, credential) -> context manager of a client
    control: Callable[[int], bool] = espn_source.control_request
    probe: Callable[[int, int], str] = espn_source.probe
    heartbeat_s: float = HEARTBEAT_S
    work_root: Path | None = None
    _globals: dict = field(default_factory=dict)  # version -> local path, shared by this worker's jobs


def private_keyring_from_env(environ=os.environ):
    from ..control_plane.credential_crypto import PrivateKeyring
    keys = {f"v{m[1]}": v for k, v in environ.items() if (m := re.fullmatch(r"CREDENTIALS_PRIVKEY_V(\d{1,4})", k)) and v}
    return PrivateKeyring(keys) if keys else None


def connect(dsn: str):
    return psycopg.connect(dsn, prepare_threshold=None, connect_timeout=15)


class _Lease:
    """Heartbeats on its own connection; `lost` is set when the lease is gone."""

    def __init__(self, ctx: Context, claim: queue.Claim):
        self.ctx, self.claim = ctx, claim
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            with connect(self.ctx.sync_dsn) as con:
                while not self._stop.wait(self.ctx.heartbeat_s):
                    if not queue.heartbeat(con, self.claim):
                        self.lost.set()
                        return
        except Exception:   # noqa: BLE001 - a heartbeat we cannot send is a lease we cannot trust
            self.lost.set()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(5)

    def check(self, stage: str) -> None:
        if self.lost.is_set():
            raise LeaseLost(stage)


class LeaseLost(Exception):
    pass


def _fetch_version(objects, key: str, version: str, dest: Path) -> Path:
    m = VERSION_RE.fullmatch(version)
    if m is None:
        raise ValueError("malformed version")
    objects.fetch(key, dest, sha256_prefix=m["sha"], size=int(m["bytes"]))
    return dest


def _global_file(ctx: Context, version: str) -> Path:
    path = ctx._globals.get(version)
    if path is None or not path.exists():
        root = Path(tempfile.mkdtemp(prefix="fi-global-", dir=ctx.work_root))
        path = _fetch_version(ctx.objects, global_key(version), version, root / "global.duckdb")
        ctx._globals[version] = path
    return path


@contextmanager
def _credential(ctx: Context, con, claim: queue.Claim, league_id: str):
    """The decrypted cookies of a private league's live credential, masked before use; (None, None) if none."""
    from ..control_plane.credential_crypto import EncryptedCredential
    row = con.execute("SELECT credential_id::text, owner_user_id::text, ciphertext, nonce, aad_format, wrapped_data_key, "
                      "wrap_key_version FROM fi.sync_credential(%s, %s)", [claim.job_id, claim.fence]).fetchone()
    con.rollback()
    if row is None:
        yield None, None
        return
    if ctx.keyring is None:
        raise RuntimeError("no credential private key configured")
    cookies = ctx.keyring.decrypt(EncryptedCredential(bytes(row[2]), bytes(row[3]), row[4], bytes(row[5]), row[6]),
                                  league_id=league_id, credential_id=row[0], owner_user_id=row[1])
    ctx.printer.mask(cookies["espn_s2"])
    ctx.printer.mask(cookies["swid"])
    try:
        yield cookies, row[0]
    finally:
        cookies.clear()


def run_job(ctx: Context, con, claim: queue.Claim) -> str:
    """Run `claim` to an outcome and record it. Returns a short outcome code (printed by the drain)."""
    lg = con.execute("SELECT provider, external_id, season, visibility FROM fi.leagues WHERE id = %s",
                     [claim.league_id]).fetchone()
    con.rollback()
    provider, external_id, season, visibility = lg
    ctx.printer.sensitive([external_id])
    if provider != "espn":
        queue.fail(con, claim, "provider_unsupported", retry=False)
        return "provider_unsupported"
    if claim.kind == "probe":
        return _run_probe(ctx, con, claim, int(external_id), season, visibility)
    stage, credential_id = "fetch", None
    try:
        with _Lease(ctx, claim) as lease, _credential(ctx, con, claim, claim.league_id) as (cookies, credential_id):
            if visibility == "private" and cookies is None:
                queue.fail(con, claim, "no_credential", retry=False)
                return "no_credential"
            gv = con.execute("SELECT global_version FROM fi.global_snapshot").fetchone()
            prev = con.execute("SELECT data_version FROM fi.league_snapshots WHERE league_id = %s",
                               [claim.league_id]).fetchone()
            con.rollback()
            if gv is None:
                queue.fail(con, claim, "no_global", retry=True)
                return "no_global"
            with tempfile.TemporaryDirectory(dir=ctx.work_root) as tmp:
                tmp = Path(tmp)
                stage = "fetch"
                global_path = _global_file(ctx, gv[0])
                previous = _fetch_version(ctx.objects, league_key(claim.league_id, prev[0]), prev[0],
                                          tmp / "previous.duckdb") if prev else None
                if claim.kind == "recompute" and prev:
                    payloads = load_payloads(ctx.objects.get_bytes(raw_key(claim.league_id, prev[0])))
                else:
                    with ctx.client(int(external_id), season, cookies) as client:
                        from ..espn.sync import fetch_all
                        payloads = fetch_all(client)
                ctx.printer.sensitive_from(payloads.league)
                ctx.printer.sensitive_from(payloads.pro_players)
                ctx.printer.sensitive_from(payloads.pool)
                lease.check("fetch")
                stage = "build"
                raw = dump_payloads(payloads)
                work = tmp / "work"
                work.mkdir()
                built = build_league_file(work, global_path=global_path, previous=previous, payloads=payloads,
                                          owner_swid=cookies["swid"] if cookies else None,
                                          stamp=utcnow().isoformat(timespec="seconds"))
                lease.check("build")
                if not queue.heartbeat(con, claim):
                    raise LeaseLost("publish")
                stage = "publish"
                applied, _ = publish_job(ctx.objects, con, claim, built, raw=raw, global_version=gv[0])
            _after_success(con, claim, visibility, credential_id, built.name)
            queue.complete(con, claim, global_version=gv[0])
            return "published" if applied else "superseded"
    except LeaseLost:
        return "lease_lost"
    except Exception as err:   # noqa: BLE001 - every failure is classified and recorded; nothing escapes to the log
        con.rollback()
        return _record_failure(ctx, con, claim, err, stage, credential_id, season, visibility)


def _after_success(con, claim: queue.Claim, visibility: str, credential_id: str | None, name: str | None) -> None:
    with con.transaction():
        con.execute("SELECT fi.record_league_found(%s, %s)", [claim.job_id, claim.fence])
        if name:
            con.execute("UPDATE fi.leagues SET name = %s WHERE id = %s AND name IS DISTINCT FROM %s",
                        [name[:200], claim.league_id, name[:200]])
        if visibility == "public":   # a successful anonymous fetch is also a fresh visibility check
            con.execute("UPDATE fi.leagues SET visibility_checked_at = now() WHERE id = %s", [claim.league_id])
    if credential_id:
        con.execute("SELECT fi.mark_credential(%s, %s, %s, 'active')", [claim.job_id, claim.fence, credential_id])
        con.commit()


def _restrict(con, league_id: str, has_credential: bool) -> None:
    """The provider says private: restrict now (one transaction, epoch + 1); a public import nobody can read
    privately becomes unavailable. Data is kept (retention decides)."""
    with con.transaction():
        con.execute("""UPDATE fi.leagues SET visibility = 'private', visibility_epoch = visibility_epoch + 1,
                              visibility_checked_at = now(),
                              status = CASE WHEN %s THEN status ELSE 'unavailable' END
                       WHERE id = %s AND visibility = 'public'""", [has_credential, league_id])


def _record_failure(ctx: Context, con, claim: queue.Claim, err: BaseException, stage: str, credential_id: str | None,
                    season: int, visibility: str) -> str:
    out = classify(err, authenticated=credential_id is not None, stage=stage)
    if out.action == "auth":
        status = con.execute("SELECT fi.record_credential_failure(%s, %s, %s, %s)",
                             [claim.job_id, claim.fence, credential_id, ctx.control(season)]).fetchone()[0]
        con.commit()
        queue.fail(con, claim, out.code, retry=status != "invalid")
        return f"{out.code}:{status}"
    if out.action == "private":
        _restrict(con, claim.league_id, has_credential=False)
        queue.fail(con, claim, out.code, retry=False)
        return out.code
    if out.action == "missing":
        status = con.execute("SELECT fi.record_league_missing(%s, %s, %s)",
                             [claim.job_id, claim.fence, ctx.control(season)]).fetchone()[0]
        con.commit()
        queue.fail(con, claim, out.code, retry=status != "unavailable")
        return f"{out.code}:{status}"
    queue.fail(con, claim, out.code, retry=out.action == "retry", detail=f"{stage}:{type(err).__name__}")
    return out.code


def _run_probe(ctx: Context, con, claim: queue.Claim, external_id: int, season: int, visibility: str) -> str:
    verdict = ctx.probe(external_id, season)
    if verdict == "public":
        with con.transaction():
            # Restoring public access needs this successful anonymous fetch; a league someone connected privately
            # (it has a live credential) is never made public by a probe.
            has_cred = con.execute("SELECT count(*) FROM fi.sync_credential(%s, %s)",
                                   [claim.job_id, claim.fence]).fetchone()[0] > 0
            if not has_cred:
                con.execute("""UPDATE fi.leagues SET visibility_checked_at = now(),
                                      visibility_epoch = visibility_epoch + CASE WHEN visibility = 'private' THEN 1 ELSE 0 END,
                                      status = CASE WHEN visibility = 'private' AND status = 'unavailable' THEN 'active'
                                                    ELSE status END,
                                      visibility = 'public'
                               WHERE id = %s""", [claim.league_id])
            con.execute("SELECT fi.record_league_found(%s, %s)", [claim.job_id, claim.fence])
        queue.complete(con, claim)
        return "probe_public"
    if verdict == "private":
        has_cred = con.execute("SELECT count(*) FROM fi.sync_credential(%s, %s)", [claim.job_id, claim.fence]).fetchone()[0] > 0
        con.rollback()
        _restrict(con, claim.league_id, has_credential=has_cred)
        queue.complete(con, claim)
        return "probe_private"
    if verdict == "missing":
        status = con.execute("SELECT fi.record_league_missing(%s, %s, %s)",
                             [claim.job_id, claim.fence, ctx.control(season)]).fetchone()[0]
        con.commit()
        queue.complete(con, claim)
        return f"probe_missing:{status}"
    queue.fail(con, claim, "probe_unknown", retry=True)   # keeps the league closed until a probe succeeds
    return "probe_unknown"


def drain(ctx: Context, *, budget_s: float, providers: tuple[str, ...] = ("espn",)) -> list[tuple[str, str, str, float]]:
    """Claim and run jobs until none is due or the budget is used. Returns (job id, league id, outcome, seconds)."""
    done = []
    deadline = time.monotonic() + budget_s
    with connect(ctx.sync_dsn) as con:
        while time.monotonic() < deadline:
            claim = queue.claim(con, ctx.owner, providers=providers)
            if claim is None:
                break
            started = time.monotonic()
            outcome = run_job(ctx, con, claim)
            secs = time.monotonic() - started
            ctx.printer(f"job {claim.job_id} league {claim.league_id} kind {claim.kind} attempt {claim.attempt} "
                        f"-> {outcome} in {secs:.1f}s")
            done.append((claim.job_id, claim.league_id, outcome, secs))
    return done
