import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { api } from "./api.ts";
import { FAAB_UNAVAILABLE, faabRouteResponse } from "./faab-route.ts";
import type { FaabResponse } from "./faab.ts";

const A = "lg_aaaaaaaaaaaaaaaa";
const ok = new URLSearchParams({ team_id: "2", claims: "309:206", share: "25" });
const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

test("an offline backend never leaks its address or the start-the-server hint", async () => {
  globalThis.fetch = (async () => { throw new TypeError("fetch failed"); }) as typeof fetch;
  const raw = await api<FaabResponse>("/api/waivers/faab");
  assert.match(raw.ok ? "" : raw.error, /uvicorn/);                  // what api() itself says
  const res = await faabRouteResponse(A, ok, (p) => api<FaabResponse>(p));
  const text = await res.text();
  assert.equal(res.status, 502);
  assert.equal(res.headers.get("cache-control"), "private, no-store");
  assert.deepEqual(JSON.parse(text), { error: FAAB_UNAVAILABLE });
  assert.doesNotMatch(text, /uvicorn|127\.0\.0\.1|localhost|http:/);
});

test("a non-JSON or 5xx backend answer is the fixed message; 404, 409 and 422 forward the backend's own", async () => {
  globalThis.fetch = (async () => new Response("<html>", { status: 200 })) as typeof fetch;
  const bad = await faabRouteResponse(A, ok, (p) => api<FaabResponse>(p));
  assert.equal(bad.status, 502);
  assert.doesNotMatch(await bad.text(), /http:|127\.0\.0\.1/);
  globalThis.fetch = (async () => new Response(JSON.stringify({ detail: "Internal trace" }), { status: 500 })) as typeof fetch;
  const five = await faabRouteResponse(A, ok, (p) => api<FaabResponse>(p));
  assert.equal(five.status, 502);
  assert.deepEqual(await five.json(), { error: FAAB_UNAVAILABLE });
  for (const status of [404, 409, 422]) {
    globalThis.fetch = (async () => new Response(JSON.stringify({ detail: "Team 77 is not in this league." }), { status })) as typeof fetch;
    const res = await faabRouteResponse(A, ok, (p) => api<FaabResponse>(p));
    assert.equal(res.status, status);
    assert.equal(res.headers.get("cache-control"), "private, no-store");
    assert.deepEqual(await res.json(), { error: "Team 77 is not in this league.", kind: "http" });
  }
});

test("refusals are private too, and nothing is fetched for them", async () => {
  let called = 0;
  const call = async () => { called += 1; return { ok: false as const, data: null, error: "x" }; };
  const noLeague = await faabRouteResponse(null, ok, call);
  assert.equal(noLeague.status, 404);
  assert.equal(noLeague.headers.get("cache-control"), "private, no-store");
  const malformed = await faabRouteResponse(A, new URLSearchParams({ team_id: "2", claims: "309:206:5" }), call);
  assert.equal(malformed.status, 400);
  assert.equal(malformed.headers.get("cache-control"), "private, no-store");
  assert.equal(called, 0);
});

test("a good answer carries the league it was read from and the degraded flag", async () => {
  const data = { contract: "waiver-faab/v1" } as unknown as FaabResponse;
  const res = await faabRouteResponse(A, ok, async () => ({ ok: true, data, error: null, stale: true }));
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "private, no-store");
  assert.deepEqual(await res.json(), { contract: "waiver-faab/v1", scope: A, stale: true });
});
