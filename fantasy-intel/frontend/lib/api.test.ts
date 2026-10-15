import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { api, classifyHttpError } from "./api.ts";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

const respond = (status: number, body: unknown, statusText = "") =>
  (async () => new Response(typeof body === "string" ? body : JSON.stringify(body), { status, statusText })) as typeof fetch;

test("classifyHttpError: the stale-schema detail is a schema problem", () => {
  assert.equal(classifyHttpError("Database schema v2 is older than v4. Run `uv run python ../scripts/sync_nfl.py`"), "schema");
  assert.equal(classifyHttpError("Database is busy (a sync is probably running)."), "http");
  assert.equal(classifyHttpError("No database yet. Run scripts/sync_espn.py first."), "http");
});

test("api: a successful response keeps the {ok, data, error} shape", async () => {
  globalThis.fetch = respond(200, { hello: "world" });
  assert.deepEqual(await api("/x"), { ok: true, data: { hello: "world" }, error: null });
});

test("api: a network failure is offline and keeps the start-the-backend message", async () => {
  globalThis.fetch = (async () => { throw new TypeError("fetch failed"); }) as typeof fetch;
  const res = await api("/x");
  assert.equal(res.ok, false);
  assert.equal(res.ok === false && res.kind, "offline");
  assert.match(res.ok === false ? res.error : "", /Cannot reach the backend at .* uv run uvicorn app\.main:app/);
});

test("api: a stale schema 503 is kind schema with the backend's detail", async () => {
  globalThis.fetch = respond(503, { detail: "Database schema v2 is older than v4. Run it." });
  const res = await api("/x");
  assert.deepEqual(res, { ok: false, data: null, error: "Database schema v2 is older than v4. Run it.", kind: "schema", status: 503 });
});

test("api: other error statuses are kind http with status and detail", async () => {
  globalThis.fetch = respond(404, { detail: "Player not found" });
  assert.deepEqual(await api("/x"), { ok: false, data: null, error: "Player not found", kind: "http", status: 404 });
});

test("api: an error without a JSON body falls back to status text", async () => {
  globalThis.fetch = respond(500, "<html>boom</html>", "Internal Server Error");
  const res = await api("/x");
  assert.equal(res.ok === false && res.error, "500 Internal Server Error");
  assert.equal(res.ok === false && res.kind, "http");
});

test("api: a 200 that is not JSON is kind unknown", async () => {
  globalThis.fetch = respond(200, "not json");
  const res = await api("/x");
  assert.equal(res.ok === false && res.kind, "unknown");
});

test("api: POST sends a JSON body", async () => {
  let seen: RequestInit | undefined;
  globalThis.fetch = (async (_url: unknown, init?: RequestInit) => { seen = init; return new Response("{}"); }) as typeof fetch;
  await api("/x", { a: 1 });
  assert.equal(seen?.method, "POST");
  assert.equal(seen?.body, '{"a":1}');
});
