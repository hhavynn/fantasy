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

test("a local Supabase needs SUPABASE_LOCAL=1, a loopback address, and never runs on Vercel", () => {
  const local = { ...ENV, SUPABASE_URL: "http://127.0.0.1:54321" };
  assert.equal(authConfig(local), null);
  assert.equal(authConfig({ ...local, SUPABASE_LOCAL: "1" })?.url, "http://127.0.0.1:54321");
  assert.equal(authConfig({ ...local, SUPABASE_LOCAL: "1", VERCEL: "1" }), null);
  assert.equal(authConfig({ ...local, SUPABASE_LOCAL: "1", SUPABASE_URL: "http://evil.example:80" }), null);
});

test("after sign-in the redirect stays inside the site", () => {
  assert.equal(safeNext("/l/lg_abcdefghijklmnop/waivers"), "/l/lg_abcdefghijklmnop/waivers");
  assert.equal(safeNext("/leagues?host=espn&step=details"), "/leagues?host=espn&step=details");
  assert.equal(safeNext("/%2F%2Fevil.example"), "/%2F%2Fevil.example", "an encoded slash stays a path on this site");
  for (const bad of [null, "", "https://evil.example", "//evil.example", "/\\evil.example", "leagues",
    // The URL parser strips tabs and newlines and reads a backslash as a slash: each of these would leave the site.
    "/\t/evil.example", "/\n/evil.example", "/\r//evil.example", "/\t\\evil.example", "/x\\..\\..\\evil", "/\u0000/evil",
    "javascript:alert(1)", `/${"a".repeat(2048)}`]) {
    assert.equal(safeNext(bad), "/leagues", JSON.stringify(bad));
  }
  // Whatever passes resolves to this origin.
  for (const ok of ["/", "/terms", "/l/lg_abcdefghijklmnop/waivers?team=2#x", "/%5Cevil.example"]) {
    assert.equal(new URL(safeNext(ok), "https://app.example").origin, "https://app.example", ok);
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
