import assert from "node:assert/strict";
import test from "node:test";
import {
  appPath, crossesLeagueScope, leagueRouteHref, leaguePath, proxyDecision, scopeApiPath, splitLeaguePath, withLeague,
} from "./league-scope.ts";

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

test("a league URL drops ?league=clear from the rewrite, so the page's own links stay in the league", () => {
  const OTHER = "lg_" + "b".repeat(20);
  assert.deepEqual(proxyDecision({ enabled: true, pathname: `/l/${LG}/start-sit`, search: "?team=3&league=clear&week=7", cookie: OTHER }),
    { kind: "rewrite", path: "/start-sit?team=3&week=7", league: LG });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: `/l/${LG}/rankings`, search: "?league=clear" }),
    { kind: "rewrite", path: "/rankings", league: LG });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: `/l/${LG}`, search: "?league=clear" }), { kind: "rewrite", path: "/", league: LG });
  // Repeated, the param is dropped whichever value comes first.
  assert.deepEqual(proxyDecision({ enabled: true, pathname: `/l/${LG}/data`, search: "?league=x&league=clear" }),
    { kind: "rewrite", path: "/data", league: LG });
  // Without league=clear the query passes through byte for byte (no re-encoding).
  assert.deepEqual(proxyDecision({ enabled: true, pathname: `/l/${LG}/players`, search: "?q=a%20b&league=x" }),
    { kind: "rewrite", path: "/players?q=a%20b&league=x", league: LG });
  // The rewritten page's links built from its params are scoped again.
  const rewritten = (proxyDecision({ enabled: true, pathname: `/l/${LG}/start-sit`, search: "?team=3&league=clear" }) as { path: string }).path;
  assert.equal(withLeague(rewritten, LG), `/l/${LG}/start-sit?team=3`);
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
  // The query and the hash both survive (review #250: a limited split dropped the hash after a query).
  assert.equal(withLeague("/schedule-intelligence?team=2#kdst-streaming", LG), `/l/${LG}/schedule-intelligence?team=2#kdst-streaming`);
  assert.equal(withLeague("/schedule-intelligence#kdst-streaming", LG), `/l/${LG}/schedule-intelligence#kdst-streaming`);
  assert.equal(withLeague("/kickers?week=9&team=3", LG), `/l/${LG}/kickers?week=9&team=3`);
  assert.equal(withLeague(`/l/${LG}/schedule-intelligence?team=2#kdst-streaming`, LG), `/l/${LG}/schedule-intelligence?team=2#kdst-streaming`);
  assert.equal(withLeague("/waivers?league=clear#x", LG), "/waivers?league=clear#x");
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
    for (const pathname of ["/account", "/admin/usage", "/terms", "/privacy"]) {
      for (const cookie of [LG, null]) {
        assert.deepEqual(proxyDecision({ enabled: true, pathname, search: "", cookie, classic }), { kind: "next" },
          `${pathname} classic=${classic} cookie=${cookie}`);
      }
    }
  }
  assert.equal(withLeague("/terms", LG), "/terms");
  assert.equal(withLeague("/privacy", LG), "/privacy");
});

test("a client navigation crosses the league scope into, out of, or between leagues, never within one", () => {
  const A = "/l/lg_KxH_Y1Y56aM7qnCCseKvs1sU", B = "/l/lg_5776f7eff90e4f45ac2c84eee0670db2";
  assert.equal(crossesLeagueScope(null, `${A}/waivers`), false, "the first render needs no refresh");
  assert.equal(crossesLeagueScope("/leagues", `${A}`), true);
  assert.equal(crossesLeagueScope(`${A}/waivers`, "/terms"), true);
  assert.equal(crossesLeagueScope(`${A}/waivers`, `${B}/waivers`), true);
  assert.equal(crossesLeagueScope(`${A}/waivers`, `${A}/trades`), false);
  assert.equal(crossesLeagueScope("/terms", "/privacy"), false);
});

test("route-handler fetches from a league page stay in that league (the trade evidence panel)", () => {
  const src = "/api/trade-evidence?team_id=2&mode=targeted&key=1-for-1%3A3%3A204%3A208";
  const scoped = leagueRouteHref(src, LG);
  assert.equal(scoped, `/l/${LG}${src}`);
  // proxy.ts rewrites it to the handler with the league header (a bare /api fetch would be unscoped and lose it)
  const url = new URL(scoped, "http://x");
  assert.deepEqual(proxyDecision({ enabled: true, pathname: url.pathname, search: url.search }),
    { kind: "rewrite", path: src, league: LG });
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/api/trade-evidence", search: url.search, cookie: LG }),
    { kind: "next" });
  // classic site, a bad id, and anything that is not a route handler are unchanged
  assert.equal(leagueRouteHref(src, null), src);
  assert.equal(leagueRouteHref(src, "lg_bad"), src);
  assert.equal(leagueRouteHref("/trades", LG), "/trades");
  assert.equal(leagueRouteHref("/apiary", LG), "/apiary");
  assert.equal(leagueRouteHref("https://example.com/api/x", LG), "https://example.com/api/x");
});

test("client navigations from a league page stay in that league end to end", async () => {
  const { toQuery } = await import("./trade-builder.ts");
  const { withTeam } = await import("./team-perspective.ts");
  const { compareHref, swapHref } = await import("./compare.ts");
  const { whatIfHref } = await import("./what-if.ts");
  const evaluate = withTeam(`/trades?${toQuery({ ta: 1, tb: 4, a: [204], b: [208, 311], sw: null })}`, "1");
  const hrefs = [
    evaluate,                                                              // TradeBuilder "Evaluate"
    compareHref(204, 311), swapHref(204, 311), "/compare",                  // ComparePicker pick, swap, clear
    "/luck-lab?team=2&schedule=5", "/luck-lab",                              // ScheduleSelector
    whatIfHref("/start-sit", new URLSearchParams("team=1&week=7"), "3:204"), // WhatIfPicker
  ];
  for (const href of hrefs) {
    const scoped = withLeague(href, LG);
    assert.equal(scoped, `/l/${LG}${href}`, href);
    // proxy.ts serves it for that league, whatever league the last-viewed cookie names ...
    const url = new URL(scoped, "http://x");
    assert.deepEqual(proxyDecision({ enabled: true, pathname: url.pathname, search: url.search, cookie: "lg_" + "z".repeat(24) }),
      { kind: "rewrite", path: href, league: LG }, href);
    // ... while the bare href follows that cookie (another tab's league), which is the drift this prevents.
    const bare = new URL(href, "http://x");
    assert.equal(proxyDecision({ enabled: true, pathname: bare.pathname, search: bare.search, cookie: "lg_" + "z".repeat(24) }).kind,
      "redirect", href);
    // The classic site keeps the unprefixed href.
    assert.equal(withLeague(href, null), href);
  }
  // The evaluated trade keeps the analyzed team and every side's players.
  const e = new URL(withLeague(evaluate, LG), "http://x").searchParams;
  assert.equal(e.get("team"), "1");
  assert.deepEqual(e.getAll("a"), ["204"]);
});
