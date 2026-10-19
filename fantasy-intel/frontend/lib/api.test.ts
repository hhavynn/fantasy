import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { api, apiRequest, classifyHttpError, formatDetail, isFirstRun } from "./api.ts";

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

test("apiRequest: defaults to GET, sends extra headers and keeps the API key", async () => {
  let seen: RequestInit | undefined;
  globalThis.fetch = (async (_url: unknown, init?: RequestInit) => { seen = init; return new Response("{}"); }) as typeof fetch;
  await apiRequest("/x", { headers: { "X-Fantasy-User-Assertion": "v1.a.b" } });
  assert.equal(seen?.method, "GET");
  assert.equal(seen?.body, undefined);
  assert.deepEqual(seen?.headers, { "X-Fantasy-User-Assertion": "v1.a.b" });
});

test("apiRequest: PUT and DELETE keep their method; a body adds a JSON content type", async () => {
  let seen: RequestInit | undefined;
  globalThis.fetch = (async (_url: unknown, init?: RequestInit) => { seen = init; return new Response("{}"); }) as typeof fetch;
  await apiRequest("/x", { method: "PUT", body: { a: 1 } });
  assert.equal(seen?.method, "PUT");
  assert.equal(seen?.body, '{"a":1}');
  assert.deepEqual(seen?.headers, { "Content-Type": "application/json" });
  await apiRequest("/x", { method: "DELETE" });
  assert.equal(seen?.method, "DELETE");
});

test("apiRequest: a 204 is ok with null data", async () => {
  globalThis.fetch = (async () => new Response(null, { status: 204 })) as typeof fetch;
  assert.deepEqual(await apiRequest("/x", { method: "DELETE" }), { ok: true, data: null, error: null });
});

test("apiRequest: errors carry the backend detail, including validation lists", async () => {
  globalThis.fetch = respond(404, { detail: "League not found" });
  assert.deepEqual(await apiRequest("/x"), { ok: false, data: null, error: "League not found", kind: "http", status: 404 });
  globalThis.fetch = respond(422, { detail: [{ loc: ["body", "season"], msg: "Input should be a valid integer" }] });
  const res = await apiRequest("/x", { method: "POST", body: {} });
  assert.equal(res.ok === false && res.error, "Input should be a valid integer");
  globalThis.fetch = (async () => { throw new TypeError("fetch failed"); }) as typeof fetch;
  const off = await apiRequest("/x");
  assert.equal(off.ok === false && off.kind, "offline");
});

test("formatDetail: strings, lists and anything else", () => {
  assert.equal(formatDetail("nope"), "nope");
  assert.equal(formatDetail([{ msg: "a" }, { msg: "b" }]), "a; b");
  assert.equal(formatDetail({ x: 1 }), null);
  assert.equal(formatDetail(""), null);
});

test("a missing database is a first run, not an error", () => {
  assert.equal(isFirstRun("No database yet. Run scripts/sync_espn.py and scripts/sync_nfl.py first."), true);
  assert.equal(isFirstRun("database is busy"), false);
  assert.equal(isFirstRun(null), false);
});
