import assert from "node:assert/strict";
import test from "node:test";
import { appPath, leaguePath, proxyDecision, scopeApiPath, splitLeaguePath, withLeague } from "./league-scope.ts";

const LG = "lg_" + "a".repeat(24);

test("splitLeaguePath recognises only our league ids", () => {
  assert.deepEqual(splitLeaguePath(`/l/${LG}/waivers`), { league: LG, path: "/waivers" });
  assert.deepEqual(splitLeaguePath(`/l/${LG}`), { league: LG, path: "/" });
  assert.deepEqual(splitLeaguePath("/l/lg_short/waivers"), { league: null, path: "/l/lg_short/waivers" });
  assert.deepEqual(splitLeaguePath("/waivers"), { league: null, path: "/waivers" });
  assert.equal(appPath(`/l/${LG}/players/12`), "/players/12");
});

test("leaguePath and scopeApiPath", () => {
  assert.equal(leaguePath(LG, "/"), `/l/${LG}`);
  assert.equal(leaguePath(LG, "/trades"), `/l/${LG}/trades`);
  assert.equal(scopeApiPath("/api/overview?team_id=2", LG), `/v2/leagues/${LG}/api/overview?team_id=2`);
  assert.equal(scopeApiPath("/api/overview", null), "/api/overview");
  assert.equal(scopeApiPath("/v2/providers", LG), "/v2/providers");
  assert.equal(scopeApiPath("/api/overview", "lg_bad"), "/api/overview");
});

test("with the flag off nothing changes", () => {
  assert.deepEqual(proxyDecision({ enabled: false, pathname: `/l/${LG}/waivers`, search: "", cookie: LG }), { kind: "next" });
  assert.deepEqual(proxyDecision({ enabled: false, pathname: "/waivers", search: "", cookie: LG }), { kind: "next" });
});

test("league URLs rewrite to the page with the league in a header", () => {
  assert.deepEqual(proxyDecision({ enabled: true, pathname: `/l/${LG}/waivers`, search: "?team=2" }),
    { kind: "rewrite", path: "/waivers?team=2", league: LG });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: `/l/${LG}`, search: "" }), { kind: "rewrite", path: "/", league: LG });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/l/lg_bad/waivers", search: "" }), { kind: "next" });
});

test("old URLs go to the last-viewed league, and ?league=clear returns to the classic site", () => {
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/trades", search: "?a=1", cookie: LG }),
    { kind: "redirect", location: `/l/${LG}/trades?a=1` });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/", search: "", cookie: LG }),
    { kind: "redirect", location: `/l/${LG}` });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/trades", search: "", cookie: null }), { kind: "next" });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/trades", search: "?league=clear&x=1", cookie: LG }),
    { kind: "clear", location: "/trades?x=1" });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/trades", search: "", cookie: "lg_bad" }), { kind: "next" });
});

test("assets, Next internals, route handlers and the connections page are never redirected", () => {
  for (const p of ["/_next/static/x.js", "/api/thing", "/leagues", "/leagues/x", "/favicon.ico", "/icon.svg", "/apple-icon", "/robots.txt"]) {
    assert.deepEqual(proxyDecision({ enabled: true, pathname: p, search: "", cookie: LG }), { kind: "next" }, p);
  }
});

test("withLeague prefixes in-app links for the current league and leaves the rest alone", () => {
  assert.equal(withLeague("/waivers?team=2", LG), `/l/${LG}/waivers?team=2`);
  assert.equal(withLeague("/", LG), `/l/${LG}`);
  assert.equal(withLeague("/players/12#log", LG), `/l/${LG}/players/12#log`);
  assert.equal(withLeague("/waivers", null), "/waivers");
  for (const href of ["/leagues", "/favicon.ico", "https://espn.com/x", "//evil.example/x", `/l/${LG}/trades`, "/?league=clear", "/data?league=clear"]) {
    assert.equal(withLeague(href, LG), href, href);
  }
});

test("sign-in, its callback and invites are never scoped to the cookie's league", () => {
  for (const pathname of ["/login", "/auth/callback", "/invite"]) {
    assert.deepEqual(proxyDecision({ enabled: true, pathname, search: "?next=/leagues", cookie: LG }), { kind: "next" }, pathname);
  }
});

test("with the classic site retired an unscoped page always goes to a league", () => {
  const off = { enabled: true, search: "", classic: false };
  assert.deepEqual(proxyDecision({ ...off, pathname: "/waivers", cookie: LG }), { kind: "redirect", location: `/l/${LG}/waivers` });
  assert.deepEqual(proxyDecision({ ...off, pathname: "/waivers", cookie: null }), { kind: "redirect", location: "/leagues" });
  assert.deepEqual(proxyDecision({ ...off, pathname: "/leagues", cookie: null }), { kind: "next" });
  assert.deepEqual(proxyDecision({ ...off, pathname: "/login", cookie: null }), { kind: "next" });
  assert.deepEqual(proxyDecision({ ...off, pathname: `/l/${LG}/waivers`, cookie: null }), { kind: "rewrite", path: "/waivers", league: LG });
  // the classic site stays as it is until then
  assert.deepEqual(proxyDecision({ enabled: true, search: "", pathname: "/waivers", cookie: null }), { kind: "next" });
});

test("account settings, the terms and the privacy policy are never scoped, with the classic site on or retired", () => {
  for (const classic of [true, false]) {
    for (const pathname of ["/account", "/terms", "/privacy"]) {
      for (const cookie of [LG, null]) {
        assert.deepEqual(proxyDecision({ enabled: true, pathname, search: "", cookie, classic }), { kind: "next" },
          `${pathname} classic=${classic} cookie=${cookie}`);
      }
    }
  }
  assert.equal(withLeague("/terms", LG), "/terms");
  assert.equal(withLeague("/privacy", LG), "/privacy");
});
