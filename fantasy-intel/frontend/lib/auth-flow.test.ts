import assert from "node:assert/strict";
import test from "node:test";
import { authConfig } from "./auth-config.ts";
import { safeNext } from "./auth-config.ts";
import {
  CALLBACK_PROBLEMS, LINK_MESSAGES, RECENT_LINK_SECONDS, apiRefusedAccount, authHref, callbackFailureHref, callbackProblem,
  isRecentLink, linkOutcome, linkState, parseCallbackProblem, parseIntent, parseSignupStatus, recentLinkValue,
  sameSiteLocation, signupGate,
} from "./auth-flow.ts";

const ENV = { MULTILEAGUE_UI_ENABLED: "1", SUPABASE_URL: "https://kdikrcmvfuzqsgahqaxf.supabase.co", SUPABASE_PUBLISHABLE_KEY: "sb_publishable_x" };
const closedHere = authConfig(ENV);
const openHere = authConfig({ ...ENV, SIGNUPS_OPEN: "1" });

test("sign-up is open only when this app offers it and the API accepts new accounts", () => {
  assert.deepEqual(signupGate(openHere, true), { open: true, reason: "open" });
  // Every other combination is closed, whatever the other side says.
  assert.deepEqual(signupGate(null, true), { open: false, reason: "auth_off" });
  assert.deepEqual(signupGate(closedHere, true), { open: false, reason: "web_closed" }, "API open, this app closed");
  assert.deepEqual(signupGate(closedHere, false), { open: false, reason: "web_closed" });
  assert.deepEqual(signupGate(openHere, false), { open: false, reason: "api_closed" }, "this app open, API closed");
  assert.deepEqual(signupGate(openHere, null), { open: false, reason: "api_unknown" }, "no answer from the API fails closed");
});

test("the API's sign-up status is read strictly", () => {
  assert.equal(parseSignupStatus({ open: true }), true);
  assert.equal(parseSignupStatus({ open: false }), false);
  for (const bad of [null, undefined, {}, { open: "true" }, { open: 1 }, "open", []]) assert.equal(parseSignupStatus(bad), null, JSON.stringify(bad));
});

test("every answer that depends on the address having an account reads as sent", () => {
  // No account while sign-in only, Supabase's own sign-up switch, an address the project may not email, and a failure
  // sending the email are all indistinguishable from success outside the server log.
  assert.deepEqual(linkOutcome(null), { outcome: "sent", log: null });
  assert.deepEqual(linkOutcome({ status: 422, code: "otp_disabled" }), { outcome: "sent", log: null });
  assert.deepEqual(linkOutcome({ status: 422, code: "signup_disabled" }), { outcome: "sent", log: "signup_disabled" });
  assert.deepEqual(linkOutcome({ status: 400, code: "email_address_not_authorized" }), { outcome: "sent", log: "email_address_not_authorized" });
  assert.deepEqual(linkOutcome({ status: 500, code: "unexpected_failure" }), { outcome: "sent", log: "unexpected_failure" });
  assert.deepEqual(linkOutcome({ status: 400 }), { outcome: "sent", log: "http_400" });
  // The same text for Sign in and for Sign up, whatever the address.
  assert.equal(linkState("signin", "sent").message, LINK_MESSAGES.sent.signin);
  assert.equal(linkState("signup", "sent").message, LINK_MESSAGES.sent.signup);
  assert.match(LINK_MESSAGES.sent.signin, /^If this address has an account/);
  assert.match(LINK_MESSAGES.sent.signup, /^If this address can be used here/);
});

test("Supabase's email throttle, reached only by addresses that get emails, reads exactly like an unknown address", () => {
  // over_email_send_rate_limit (per address, or the project's email quota) is only ever reached by an address Supabase
  // emails: on Sign in that means it has an account. Its answer must equal the unknown-address answer, on both intents.
  for (const intent of ["signin", "signup"] as const) {
    const unknown = linkState(intent, linkOutcome({ status: 422, code: "otp_disabled" }).outcome);
    for (const e of [{ status: 429, code: "over_email_send_rate_limit" }, { status: 400, code: "over_email_send_rate_limit" }, { status: 429 }]) {
      assert.deepEqual(linkState(intent, linkOutcome(e).outcome), unknown, `${intent} ${JSON.stringify(e)}`);
    }
  }
  assert.deepEqual(linkOutcome({ status: 429, code: "over_email_send_rate_limit" }), { outcome: "sent", log: "over_email_send_rate_limit" });
  assert.deepEqual(linkOutcome({ status: 429 }), { outcome: "sent", log: "http_429" });
});

test("the per-IP request limit, a malformed address and an unreachable service get their own guidance", () => {
  assert.equal(linkOutcome({ status: 429, code: "over_request_rate_limit" }).outcome, "rate_limited");
  assert.deepEqual(linkState("signin", "rate_limited"), { status: "error", message: LINK_MESSAGES.rateLimited });
  assert.equal(linkOutcome({ status: 400, code: "email_address_invalid" }).outcome, "invalid_email");
  assert.deepEqual(linkState("signup", "invalid_email"), { status: "error", message: LINK_MESSAGES.invalidEmail, field: "email" });
  for (const e of [{ status: 0, name: "AuthRetryableFetchError" }, { name: "AuthRetryableFetchError" }, { status: 502 }, { status: 503 }, { status: 504 }]) {
    assert.equal(linkOutcome(e).outcome, "unavailable", JSON.stringify(e));
  }
  assert.deepEqual(linkState("signin", "unavailable"), { status: "error", message: LINK_MESSAGES.unavailable });
  // No message names an address or says whether it has an account.
  for (const m of [LINK_MESSAGES.rateLimited, LINK_MESSAGES.recent, LINK_MESSAGES.unavailable, LINK_MESSAGES.invalidEmail]) {
    assert.doesNotMatch(m, /no account|not registered|already registered|does not exist/i, m);
  }
});

test("a repeated request for one address within a minute is recognised before Supabase is asked", () => {
  const hash = "a".repeat(32);
  const v = recentLinkValue(hash, 1_000);
  assert.equal(v, `${hash}.1000`);
  assert.equal(isRecentLink(v, hash, 1_000), true);
  assert.equal(isRecentLink(v, hash, 1_000 + RECENT_LINK_SECONDS - 1), true);
  assert.equal(isRecentLink(v, hash, 1_000 + RECENT_LINK_SECONDS), false, "after a minute it may be asked again");
  assert.equal(isRecentLink(v, "b".repeat(32), 1_001), false, "another address is not throttled");
  assert.equal(isRecentLink(v, hash, 999), false, "a cookie from the future is ignored");
  for (const bad of [null, undefined, "", "junk", `${hash}`, `${hash}.x`, `Z${hash.slice(1)}.1000`]) {
    assert.equal(isRecentLink(bad, hash, 1_000), false, String(bad));
  }
});

test("an email link that did not sign in gets a specific reason", () => {
  assert.equal(callbackProblem("otp_expired"), "expired");
  assert.equal(callbackProblem("flow_state_expired"), "expired");
  assert.equal(callbackProblem("flow_state_not_found"), "used");
  assert.equal(callbackProblem("bad_code_verifier"), "used");
  assert.equal(callbackProblem("pkce_code_verifier_not_found"), "browser");
  for (const other of [null, undefined, "", "access_denied", "unexpected_failure"]) assert.equal(callbackProblem(other), "invalid");
  for (const p of ["expired", "used", "browser", "invalid", "closed"] as const) {
    assert.equal(parseCallbackProblem(p), p);
    assert.ok(CALLBACK_PROBLEMS[p].title && CALLBACK_PROBLEMS[p].body, p);
  }
  // The older generic value and anything unexpected show the generic message; nothing is echoed back.
  assert.equal(parseCallbackProblem("1"), "invalid");
  assert.equal(parseCallbackProblem("<script>"), "invalid");
  assert.equal(parseCallbackProblem(["expired", "used"]), "expired");
  assert.equal(parseCallbackProblem(undefined), null);
  assert.equal(parseCallbackProblem(""), null);
});

test("Sign in and Sign up link to each other and keep an explicit destination", () => {
  assert.equal(authHref("/signup", "/leagues"), "/signup");
  assert.equal(authHref("/login", "/leagues"), "/login");
  assert.equal(authHref("/signup", "/l/lg_abcdefghijklmnop/waivers?team=2"), "/signup?next=%2Fl%2Flg_abcdefghijklmnop%2Fwaivers%3Fteam%3D2");
  assert.equal(new URL(authHref("/login", "/leagues?host=espn"), "https://x.example").searchParams.get("next"), "/leagues?host=espn");
});

// The reviewer's dot-segment vectors (F1) and the earlier ones: none may ever become a protocol-relative Location.
const HOSTILE_NEXT = [
  "/.//evil.example", "/..//evil.example", "/a/..//evil.example", "/%2e//evil.example", "/%2E%2E//evil.example",
  "/%2e%2e/%2e%2e//evil.example", "/a/./..//evil.example", "/x/%2e%2e//evil.example?x=1", "/%2E/%2F/evil.example",
  "//evil.example", "/\\evil.example", "/\t/evil.example", "/\n/evil.example", "https://evil.example", "/%zz",
];

test("no next value, however written, becomes a protocol-relative or off-site Location", () => {
  for (const raw of HOSTILE_NEXT) {
    const next = safeNext(raw);
    const location = sameSiteLocation(next);
    assert.ok(location.startsWith("/") && !location.startsWith("//"), `${JSON.stringify(raw)} -> ${location}`);
    assert.equal(new URL(location, "https://app.example").origin, "https://app.example", JSON.stringify(raw));
    // And through the failure redirect, which carries next as a query value.
    const fail = sameSiteLocation(callbackFailureHref("signup", "expired", next));
    assert.ok(fail.startsWith("/signup?") && !fail.startsWith("//"), fail);
  }
  for (const raw of ["/.//evil.example", "/..//evil.example", "/a/..//evil.example", "/%2e//evil.example", "/%2E%2E//evil.example"]) {
    assert.equal(safeNext(raw), "/leagues", raw);
  }
  // sameSiteLocation on its own refuses what a caller might pass unchecked.
  for (const raw of ["//evil.example", "/.//evil.example", "https://evil.example/x", "/\\evil.example"]) {
    assert.equal(sameSiteLocation(raw), "/leagues", raw);
  }
  assert.equal(sameSiteLocation("/l/lg_abcdefghijklmnop/waivers?team=2#x"), "/l/lg_abcdefghijklmnop/waivers?team=2#x");
  assert.equal(sameSiteLocation("/terms?q=é"), "/terms?q=%C3%A9", "percent-encoded for a header value");
});

test("/v2/me refusing the account is its status, whatever the wording", () => {
  assert.equal(apiRefusedAccount({ ok: false, status: 403 }), true);
  assert.equal(apiRefusedAccount({ ok: false, status: 401 }), false);
  assert.equal(apiRefusedAccount({ ok: false, status: 404 }), false, "API flag off");
  assert.equal(apiRefusedAccount({ ok: false }), false, "offline");
  assert.equal(apiRefusedAccount({ ok: true, status: 200 }), false);
});

test("a failed link returns to the page it was asked for from; intent is only ever signup or signin", () => {
  assert.equal(parseIntent("signup"), "signup");
  for (const other of [null, undefined, "", "signin", "SIGNUP", "signup ", "//evil.example", "admin"]) assert.equal(parseIntent(other), "signin");
  assert.equal(callbackFailureHref("signup", "expired", "/terms"), "/signup?error=expired&next=%2Fterms");
  assert.equal(callbackFailureHref("signin", "browser", "/l/lg_abcdefghijklmnop/waivers"), "/login?error=browser&next=%2Fl%2Flg_abcdefghijklmnop%2Fwaivers");
});
