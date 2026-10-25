import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { api } from "./api.ts";
import {
  CLAIM_PLAN_SIGN_IN, CLAIM_PLAN_UNAVAILABLE, EVIDENCE_SIGN_IN, EVIDENCE_UNAVAILABLE,
  claimPlanRouteResponse, tradeEvidenceRouteResponse, tradeScenariosRouteResponse,
} from "./api-routes.ts";
import { FAAB_UNAVAILABLE, faabRouteResponse } from "./faab-route.ts";
import { searchResponse } from "./palette.ts";
import { SIGN_IN_AGAIN, routeErrorResponse } from "./route-error.ts";
import { COMPARE_UNAVAILABLE } from "./trade-scenarios.ts";

const A = "lg_aaaaaaaaaaaaaaaa";
const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

const offline = () => { globalThis.fetch = (async () => { throw new TypeError("fetch failed"); }) as typeof fetch; };
const answering = (status: number, body: string) => {
  globalThis.fetch = (async () => new Response(body, { status })) as typeof fetch;
};
/** The internal backend address api() names in its offline error (whatever API_URL is in this run). */
async function backendHost(): Promise<string> {
  offline();
  const raw = await api("/api/anything");
  assert.equal(raw.ok, false);
  const host = /https?:\/\/[^\s/]+/.exec(raw.ok ? "" : raw.error)?.[0];
  assert.ok(host, "api() names the backend address in its offline error");
  assert.match(raw.ok ? "" : raw.error, /uvicorn/);
  return host;
}

// Every route, called the way its route.ts calls it, with a well-formed request.
const ROUTES: { name: string; unavailable: string; signIn: string; run: () => Promise<Response> }[] = [
  { name: "waiver-claim-plan", unavailable: CLAIM_PLAN_UNAVAILABLE, signIn: CLAIM_PLAN_SIGN_IN,
    run: () => claimPlanRouteResponse(A, new URLSearchParams({ team_id: "2", claims: "309:206" }), (p) => api(p)) },
  { name: "trade-evidence", unavailable: EVIDENCE_UNAVAILABLE, signIn: EVIDENCE_SIGN_IN,
    run: () => tradeEvidenceRouteResponse(new URLSearchParams({ team_id: "2", key: "r1" }), (p) => api(p)) },
  { name: "waiver-faab", unavailable: FAAB_UNAVAILABLE, signIn: "",
    run: () => faabRouteResponse(A, new URLSearchParams({ team_id: "2", claims: "309:206", share: "25" }), (p) => api(p)) },
  { name: "trade-scenarios", unavailable: COMPARE_UNAVAILABLE, signIn: "",
    run: () => tradeScenariosRouteResponse(A, new URLSearchParams({ scenarios: "2:1:207.204:202" }), (p) => api(p)) },
];

test("an offline backend: no route sends the internal address or the start-the-server hint, and none is cached", async () => {
  const host = await backendHost();
  for (const route of ROUTES) {
    offline();
    const res = await route.run();
    const text = await res.text();
    assert.equal(res.status, 502, route.name);
    assert.equal(res.headers.get("cache-control"), "private, no-store", route.name);
    assert.equal(text.includes(host), false, `${route.name}: ${text}`);
    assert.doesNotMatch(text, /uvicorn|127\.0\.0\.1|localhost|http:/, route.name);
    assert.equal(JSON.parse(text).error, route.unavailable, route.name);
  }
  offline();
  const search = searchResponse(await api("/api/players?q=al"));
  const text = await search.text();
  assert.equal(search.status, 502);
  assert.equal(search.headers.get("cache-control"), "private, no-store");
  assert.equal(text.includes(host), false);
  assert.doesNotMatch(text, /uvicorn/);
});

test("an answer that is not JSON, or a 5xx, is never forwarded either", async () => {
  const host = await backendHost();
  for (const route of ROUTES) {
    answering(200, "<html>");
    const bad = await route.run();
    const badText = await bad.text();
    assert.equal(bad.status, 502, route.name);                       // a 2xx without JSON is never a success status
    assert.equal(bad.headers.get("cache-control"), "private, no-store", route.name);
    assert.equal(badText.includes(host), false, route.name);
    assert.equal(JSON.parse(badText).error, route.unavailable, route.name);
    answering(500, JSON.stringify({ detail: "Traceback at /srv/app/main.py" }));
    const five = await route.run();
    // 502 on the shared helper; the scenario route's own mapping keeps the backend's 5xx (an error status either way).
    assert.equal(five.status, route.name === "trade-scenarios" ? 500 : 502, route.name);
    assert.equal(five.headers.get("cache-control"), "private, no-store", route.name);
    assert.equal(JSON.parse(await five.text()).error, route.unavailable, route.name);
  }
});

test("the backend's own 404, 409 and 422 message is forwarded, privately", async () => {
  for (const route of ROUTES) {
    for (const status of [404, 409, 422]) {
      answering(status, JSON.stringify({ detail: "Team 77 is not in this league." }));
      const res = await route.run();
      assert.equal(res.status, status, route.name);
      assert.equal(res.headers.get("cache-control"), "private, no-store", route.name);
      assert.deepEqual(await res.json(), { error: "Team 77 is not in this league.", kind: "http" }, route.name);
    }
  }
});

test("a refused sign-in is a 401 in fixed words on the shared helper's routes", async () => {
  for (const route of ROUTES.filter((r) => r.signIn)) {
    answering(401, JSON.stringify({ detail: "Invalid token: kid abc" }));
    const res = await route.run();
    assert.equal(res.status, 401, route.name);
    assert.equal(res.headers.get("cache-control"), "private, no-store", route.name);
    assert.deepEqual(await res.json(), { error: route.signIn }, route.name);
  }
  const res = routeErrorResponse({ ok: false, data: null, error: "Invalid token: kid abc", kind: "http", status: 401 }, "x");
  assert.deepEqual(await res.json(), { error: SIGN_IN_AGAIN });
});

test("refusals without a league or with a malformed request are private, and nothing is fetched for them", async () => {
  let called = 0;
  const call = async () => { called += 1; return { ok: false as const, data: null, error: "x" }; };
  const refusals: [string, Promise<Response>, number][] = [
    ["claim plan, no league", claimPlanRouteResponse(null, new URLSearchParams({ team_id: "2", claims: "309:206" }), call), 404],
    ["claim plan, malformed", claimPlanRouteResponse(A, new URLSearchParams({ team_id: "2", claims: "309:206:5:1" }), call), 400],
    ["scenarios, no league", tradeScenariosRouteResponse("lg_short", new URLSearchParams({ scenarios: "2:1:207.204:202" }), call), 404],
    ["scenarios, malformed", tradeScenariosRouteResponse(A, new URLSearchParams({ scenarios: "nope" }), call), 400],
    ["evidence, no row key", tradeEvidenceRouteResponse(new URLSearchParams({ team_id: "2" }), call), 400],
  ];
  for (const [name, pending, status] of refusals) {
    const res = await pending;
    assert.equal(res.status, status, name);
    assert.equal(res.headers.get("cache-control"), "private, no-store", name);
    assert.equal(typeof (await res.json()).error, "string", name);
  }
  assert.equal(called, 0);
});

test("good answers are private too and carry the league they were read from", async () => {
  const plan = await claimPlanRouteResponse(A, new URLSearchParams({ team_id: "2", claims: "309:206" }),
    async () => ({ ok: true, data: { contract: "waiver-claim-plan/v1" } as never, error: null, stale: true }));
  assert.equal(plan.headers.get("cache-control"), "private, no-store");
  assert.deepEqual(await plan.json(), { contract: "waiver-claim-plan/v1", scope: A, stale: true });
  const evidence = await tradeEvidenceRouteResponse(new URLSearchParams({ key: "r1" }),
    async () => ({ ok: true, data: { key: "r1" } as never, error: null }));
  assert.equal(evidence.headers.get("cache-control"), "private, no-store");
  assert.deepEqual(await evidence.json(), { key: "r1" });
  const compare = await tradeScenariosRouteResponse(A, new URLSearchParams({ scenarios: "2:1:207.204:202" }),
    async () => ({ ok: true, data: { contract: "trade-scenario-compare/v1" } as never, error: null }));
  assert.equal(compare.headers.get("cache-control"), "private, no-store");
  assert.deepEqual(await compare.json(), { contract: "trade-scenario-compare/v1", scope: A, stale: false });
});
