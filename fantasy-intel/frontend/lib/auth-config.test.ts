import assert from "node:assert/strict";
import test from "node:test";
import { EMAIL_RE, authConfig, hasSessionCookie, safeNext, siteOrigin } from "./auth-config.ts";

const ENV = { MULTILEAGUE_UI_ENABLED: "1", SUPABASE_URL: "https://kdikrcmvfuzqsgahqaxf.supabase.co/", SUPABASE_PUBLISHABLE_KEY: "sb_publishable_x" };

test("sign-in needs the UI flag and both Supabase settings; sign-ups stay closed unless opened", () => {
  assert.deepEqual(authConfig(ENV), { url: "https://kdikrcmvfuzqsgahqaxf.supabase.co", key: "sb_publishable_x", signupsOpen: false });
  assert.equal(authConfig({ ...ENV, MULTILEAGUE_UI_ENABLED: undefined }), null);
  assert.equal(authConfig({ ...ENV, SUPABASE_PUBLISHABLE_KEY: "" }), null);
  assert.equal(authConfig({ ...ENV, SUPABASE_URL: "https://evil.example" }), null);
  assert.equal(authConfig({ ...ENV, SIGNUPS_OPEN: "1" })?.signupsOpen, true);
});

test("after sign-in the redirect stays inside the site", () => {
  assert.equal(safeNext("/l/lg_abcdefghijklmnop/waivers"), "/l/lg_abcdefghijklmnop/waivers");
  for (const bad of [null, "", "https://evil.example", "//evil.example", "/\\evil.example", "leagues"]) {
    assert.equal(safeNext(bad), "/leagues", String(bad));
  }
});

test("only Supabase's session cookies count as a session", () => {
  assert.equal(hasSessionCookie(["fi_league", "sb-kdikrcmvfuzqsgahqaxf-auth-token"]), true);
  assert.equal(hasSessionCookie(["sb-kdikrcmvfuzqsgahqaxf-auth-token.0"]), true);
  assert.equal(hasSessionCookie(["fi_league", "sb-code-verifier", "theme"]), false);
});

test("the email link returns to the configured site, else to the request's host", () => {
  const headers = (h: Record<string, string>) => (n: string) => h[n] ?? null;
  assert.equal(siteOrigin(headers({ host: "x.vercel.app" }), { SITE_URL: "https://fantasy.example/" }), "https://fantasy.example");
  assert.equal(siteOrigin(headers({ "x-forwarded-host": "x.vercel.app", "x-forwarded-proto": "https" }), {}), "https://x.vercel.app");
  assert.equal(siteOrigin(headers({ host: "localhost:3000" }), {}), "http://localhost:3000");
});

test("email addresses are checked for shape only", () => {
  assert.ok(EMAIL_RE.test("havyn@example.com"));
  for (const bad of ["", "no-at", "a@b", "a b@c.de"]) assert.ok(!EMAIL_RE.test(bad), bad);
});
