// Server actions (app/leagues/actions.ts, app/account/actions.ts) show a failed backend call through
// `actionErrorMessage`: the backend's own 4xx words, or fixed text. Never the internal API_URL, a start-the-server hint,
// a raw fetch error or a server trace. And a hosted deployment's `api()` / `apiRequest()` errors name no address at all.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, test } from "node:test";
import { api, apiRequest } from "./api.ts";
import { connectProblem } from "./onboarding.ts";
import { ACTION_503_MESSAGES, SIGN_IN_AGAIN, actionErrorMessage } from "./route-error.ts";

const realFetch = globalThis.fetch;
const realEnv = { API_KEY: process.env.API_KEY, VERCEL: process.env.VERCEL };
afterEach(() => {
  globalThis.fetch = realFetch;
  for (const [k, v] of Object.entries(realEnv)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

const FALLBACK = "The league could not be added right now.";
const LEAKS = /uvicorn|127\.0\.0\.1|localhost|https?:|\.internal|fetch failed|ECONNREFUSED|Traceback|\/srv\//;

const offline = () => {
  globalThis.fetch = (async () => { throw new TypeError("fetch failed: connect ECONNREFUSED 10.1.2.3:8000"); }) as typeof fetch;
};
const answering = (status: number, body: string) => {
  globalThis.fetch = (async () => new Response(body, { status })) as typeof fetch;
};
const failure = async (path = "/v2/leagues/import") => {
  const res = await apiRequest(path, { body: {} });
  assert.equal(res.ok, false);
  return res as Extract<typeof res, { ok: false }>;
};

/** The internal backend address a local run's apiRequest names in its offline error (whatever API_URL is here). */
async function localBackendHost(): Promise<string> {
  delete process.env.API_KEY;
  delete process.env.VERCEL;
  offline();
  const raw = await failure();
  const host = /https?:\/\/[^\s/]+/.exec(raw.error)?.[0];
  assert.ok(host, "a local run names the backend address for the developer");
  return host;
}

test("a network failure never reaches an action's message: no API_URL, host or fetch error", async () => {
  const host = await localBackendHost();
  offline();
  const res = await failure();
  const message = actionErrorMessage(res, FALLBACK);
  assert.equal(message, FALLBACK);
  assert.equal(message.includes(host), false);
  assert.doesNotMatch(message, LEAKS);
  // The add-league card built from it (title, message, hint) names nothing internal either.
  const card = connectProblem(res.status, message, "espn");
  assert.equal(card.next, "retry_later");
  assert.doesNotMatch(JSON.stringify(card), LEAKS);
  assert.equal(JSON.stringify(card).includes(host), false);
});

test("a 5xx, a stale schema, a non-JSON answer and a 2xx that is not JSON get the fixed words", async () => {
  const host = await localBackendHost();
  const cases: [number, string][] = [
    [500, JSON.stringify({ detail: `Traceback at /srv/app/main.py calling ${host}` })],
    [502, "<html>Bad gateway from upstream.internal</html>"],
    [503, JSON.stringify({ detail: "The control plane is unavailable." })],
    [503, JSON.stringify({ detail: "Database schema v2 is older than v4. Run the sync." })],
    [200, "<html>not json</html>"],
  ];
  for (const [status, body] of cases) {
    answering(status, body);
    const res = await failure();
    const message = actionErrorMessage(res, FALLBACK);
    assert.equal(message, FALLBACK, `${status} ${body}`);
    assert.equal(message.includes(host), false);
  }
});

test("a 401 says to sign in again in fixed words (its backend text can name a key id)", async () => {
  answering(401, JSON.stringify({ detail: "Invalid token: kid abc123" }));
  assert.equal(actionErrorMessage(await failure(), FALLBACK), SIGN_IN_AGAIN);
  assert.equal(actionErrorMessage(await failure(), FALLBACK, "Sign in first."), "Sign in first.");
});

test("the backend's own 400, 403, 404, 409, 422 and 429 message still shows, so the flows keep their meaning", async () => {
  const flows: [number, string, string][] = [
    [400, "ESPN did not accept these cookies for this league.", "cookies"],
    [403, "Your ESPN account does not own a team in this league. Ask a member for an invite.", "invite"],
    [404, "ESPN has no league with that id for that season.", "fix_input"],
    [404, "This invite is not valid (used, expired or revoked).", "fix_input"],
    [409, "You already follow 25 leagues. Unpin one first.", "manage"],
    [409, "This league is public on ESPN: import it without cookies.", "fix_input"],
    [422, "The season must be a year.", "fix_input"],
    [429, "You have added too many leagues today. Try again tomorrow.", "retry_later"],
  ];
  for (const [status, detail, next] of flows) {
    answering(status, JSON.stringify({ detail }));
    const res = await failure();
    const message = actionErrorMessage(res, FALLBACK);
    assert.equal(message, detail, `${status}`);
    assert.equal(connectProblem(res.status, message, "espn").next, next, detail);
  }
  // FastAPI's validation list keeps its joined messages.
  answering(422, JSON.stringify({ detail: [{ msg: "Field required" }] }));
  assert.equal(actionErrorMessage(await failure(), FALLBACK), "Field required");
});

test("only the add-league flow's exact fixed 503 sentences are forwarded; any other 503 text is not", async () => {
  for (const detail of ACTION_503_MESSAGES) {
    answering(503, JSON.stringify({ detail }));
    assert.equal(actionErrorMessage(await failure(), FALLBACK), detail);
  }
  // The deployment cannot take private leagues at all: the card says so, and never "try again later".
  answering(503, JSON.stringify({ detail: "Private leagues are not available in this deployment." }));
  const res = await failure();
  const card = connectProblem(res.status, actionErrorMessage(res, FALLBACK), "espn");
  assert.equal(card.next, "none");
  assert.equal(card.title, "Private ESPN leagues are not available here");
  assert.doesNotMatch(JSON.stringify(card), /try again/i);
  // Near misses and other 503s get the fixed words.
  for (const detail of ["Private leagues are not available in this deployment. Traceback: /srv/app",
    "private leagues are not available in this deployment.", "The control plane is unavailable.",
    "Cannot load the published snapshot: r2 key snapshots/lg_x/manifest.json"]) {
    answering(503, JSON.stringify({ detail }));
    assert.equal(actionErrorMessage(await failure(), FALLBACK), FALLBACK, detail);
  }
  answering(500, JSON.stringify({ detail: "Private leagues are not available in this deployment." }));
  assert.equal(actionErrorMessage(await failure(), FALLBACK), FALLBACK);
});

test("a hosted deployment's api() and apiRequest() errors name no address and no server command", async () => {
  const host = await localBackendHost();
  for (const env of [{ VERCEL: "1" }, { API_KEY: "synthetic-test-key" }]) {
    Object.assign(process.env, env);
    offline();
    const reads = [await api("/api/anything"), await failure()];
    answering(200, "<html>");
    reads.push(await api("/api/anything"), await failure());
    for (const res of reads) {
      assert.equal(res.ok, false);
      const text = res.ok ? "" : res.error;
      assert.equal(text.includes(host), false, text);
      assert.doesNotMatch(text, LEAKS);
    }
    // app/error.tsx still recognises the offline message.
    assert.match(reads[0].ok ? "" : reads[0].error, /^cannot reach the backend/i);
    delete process.env.API_KEY;
    delete process.env.VERCEL;
  }
});

test("the server actions show failures only through actionErrorMessage, never res.error itself", () => {
  for (const file of ["app/leagues/actions.ts", "app/account/actions.ts"]) {
    const source = readFileSync(new URL(`../${file}`, import.meta.url), "utf8")
      .replace(/\/private\/i\.test\(res\.error\)/g, "");          // the 409 "private" check routes, it never displays
    assert.doesNotMatch(source, /res\.error/, file);
    assert.match(source, /actionErrorMessage\(res, /, file);
  }
});
