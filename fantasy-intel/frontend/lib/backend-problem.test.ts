// BackendProblem's wording is safe by default: without the server's `local` flag (a client render such as
// app/error.tsx, or any hosted page) it never shows the backend address, a server command, a sync command or the
// backend's raw 5xx text. Only the server wrapper (components/BackendProblem.tsx) can turn developer help on.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { backendProblemCopy, type ProblemInput } from "./backend-problem.ts";
import { api } from "./api.ts";
import { isFirstSync } from "./api-errors.ts";
import { computeSyncSummary } from "./sync-status.ts";

const HOST = "http://10.20.30.40:8000";
const LEAKS = /uvicorn|sync_espn|sync_nfl|scripts\/|cd backend|10\.20\.30\.40|https?:|MotherDuck|r2:|snapshots\/|Traceback|\.env/;

test("a no-snapshot 409 reaches the page and pill as no data, without claiming a job is running (#333)", async () => {
  const realFetch = globalThis.fetch;
  try {
    // league_scope._authorize returns the same detail for no job, queued, running and failed first syncs.
    for (const state of ["no_data", "first_sync_queued", "first_sync_running", "first_sync_failed"]) {
      globalThis.fetch = (async () => new Response(JSON.stringify({ detail: "First sync in progress." }), {
        status: 409, headers: { "X-FI-Sync-State": state },
      })) as typeof fetch;
      const res = await api("/v2/leagues/lg_SyntheticBlankLeague/api/status");
      assert.equal(res.ok, false);
      if (res.ok) continue;
      for (const local of [true, false, undefined]) {
        const copy = backendProblemCopy({ ...res, local });
        assert.equal(copy.variant, "first-sync");
        assert.ok("title" in copy && /no data yet/i.test(String(copy.title)));
        assert.doesNotMatch(JSON.stringify(copy), /still syncing|within a few minutes|409|First sync in progress/);
        assert.doesNotMatch(JSON.stringify(copy), LEAKS);
      }
      const pill = computeSyncSummary(null, undefined, "espn", false, isFirstSync(res.status, res.error));
      assert.equal(pill.text, "No league data yet");
      assert.notEqual(pill.state, "offline");
    }
  } finally {
    globalThis.fetch = realFetch;
  }
});

const FAILURES: ProblemInput[] = [
  { kind: "offline", error: `Cannot reach the backend at ${HOST}. Start it with: cd backend && uv run uvicorn app.main:app --reload` },
  { kind: "schema", status: 503, error: "Database schema v2 is older than v4. Run `uv run python ../scripts/sync_nfl.py`" },
  { kind: "http", status: 500, error: "Traceback (most recent call last): /srv/app/main.py" },
  { kind: "http", status: 503, error: "Cannot load the published snapshot: r2: NoSuchKey snapshots/lg_abc/manifest.json" },
  { kind: "http", status: 503, error: "Cannot reach MotherDuck database 'fi_prod': IO Error" },
  { kind: "http", status: 502, error: "<html>Bad gateway</html>" },
  { kind: "unknown", status: 200, error: `The backend at ${HOST} returned a response that is not valid JSON.` },
];

test("a client render with no server flag shows no address, server or sync command, or raw 5xx text", () => {
  for (const input of FAILURES) {
    for (const copy of [backendProblemCopy(input), backendProblemCopy({ ...input, local: false })]) {
      assert.equal(copy.variant, "notice", input.error);
      const text = JSON.stringify(copy);
      assert.doesNotMatch(text, LEAKS, input.error);
      if (copy.variant === "notice") {
        assert.deepEqual(copy.blocks.map((b) => b.type), ["text"], input.error);   // no commands, detail or setup steps
      }
    }
  }
});

test("without the flag, a busy database and a 404, 409 or 422 still say something useful", () => {
  const busy = backendProblemCopy({ kind: "http", status: 503, error: "Database is busy (a sync is probably running). Retry in a few seconds." });
  assert.equal(busy.variant === "notice" && busy.title, "The league data is busy");
  for (const status of [404, 409, 422]) {
    const copy = backendProblemCopy({ kind: "http", status, error: "Team 77 is not in this league." });
    assert.ok(copy.variant === "notice" && copy.blocks.some((b) => b.type === "text" && b.text === "Team 77 is not in this league."), `${status}`);
  }
  const forbidden = backendProblemCopy({ kind: "http", status: 403, error: "Invalid token: kid abc" });
  assert.doesNotMatch(JSON.stringify(forbidden), /kid abc/);
});

test("the first-run setup guide needs the server's local flag; without it a missing database is a plain 503", () => {
  const noDatabase = { kind: "http" as const, status: 503, error: "No database yet. Run scripts/sync_espn.py and scripts/sync_nfl.py first." };
  for (const local of [false, undefined]) {
    const copy = backendProblemCopy({ ...noDatabase, local });
    assert.equal(copy.variant, "notice", String(local));
    assert.doesNotMatch(JSON.stringify(copy), /\.env|ESPN_LEAGUE_ID|ESPN_S2|SWID|uvicorn|sync/i, String(local));
    assert.ok(copy.variant === "notice" && copy.blocks.every((b) => b.type === "text"));
  }
  assert.equal(backendProblemCopy({ ...noDatabase, local: true }).variant, "first-run");
  // The first-sync notice is fixed visitor copy (no setup), so it shows either way.
  for (const local of [true, false, undefined]) {
    assert.equal(backendProblemCopy({ kind: "http", status: 409, error: "First sync in progress.", local }).variant, "first-sync");
  }
});

test("a local run (the server says so) keeps the developer's error and commands", () => {
  const offline = backendProblemCopy({ ...FAILURES[0], local: true });
  assert.ok(offline.variant === "notice");
  assert.deepEqual(offline.blocks.map((b) => b.type), ["text", "commands", "detail", "first-run"]);
  assert.match(JSON.stringify(offline), /uvicorn/);
  const schema = backendProblemCopy({ ...FAILURES[1], local: true });
  assert.match(JSON.stringify(schema), /sync_espn/);
  const http = backendProblemCopy({ ...FAILURES[2], local: true });
  assert.match(JSON.stringify(http), /Traceback/);
});

/** Every .ts/.tsx source file under the frontend's app/ and components/ folders. */
function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx?$/.test(name) && !/\.test\.ts$/.test(name) ? [path] : [];
  });
}

test("only the server decides `local`: client code never imports the server wrapper or reads the environment for it", () => {
  const root = new URL("..", import.meta.url).pathname;
  const view = readFileSync(join(root, "components/BackendProblemView.tsx"), "utf8");
  const copy = readFileSync(join(root, "lib/backend-problem.ts"), "utf8");
  assert.doesNotMatch(view + copy, /process\.env/);
  for (const file of ["components/BackendProblem.tsx", "lib/deployment.server.ts"]) {
    assert.match(readFileSync(join(root, file), "utf8"), /^import "server-only";/m, file);
  }
  assert.match(readFileSync(join(root, "components/SyncPill.tsx"), "utf8"), /isLocalRun\(\)/);
  for (const file of [...sources(join(root, "app")), ...sources(join(root, "components"))]) {
    const text = readFileSync(file, "utf8");
    if (!/^"use client";/m.test(text)) continue;
    assert.doesNotMatch(text, /@\/components\/BackendProblem"|deployment\.server|isHostedDeployment/, file);
    assert.doesNotMatch(text, /<BackendProblemView[^>]*\blocal\b/, file);
  }
});
