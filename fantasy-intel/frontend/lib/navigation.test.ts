import assert from "node:assert/strict";
import { test } from "node:test";
import { ALL_NAV_PAGES, NAV_GROUPS, getActiveRoute } from "./navigation.ts";

const group = (path: string) => getActiveRoute(path).activeGroup?.label ?? null;

test("root route ('/') activates Overview in My team", () => {
  assert.equal(getActiveRoute("/").activeHref, "/");
  assert.equal(group("/"), "My team");
});

test("the matchup page sits after Overview in My team", () => {
  assert.equal(getActiveRoute("/matchup").activeHref, "/matchup");
  assert.equal(group("/matchup"), "My team");
  assert.deepEqual(NAV_GROUPS[0].pages.map((p) => p.href).slice(0, 2), ["/", "/matchup"]);
});

test("nested trade routes: the longest match wins", () => {
  assert.equal(getActiveRoute("/trades").activeHref, "/trades");
  assert.equal(getActiveRoute("/trades/discover").activeHref, "/trades/discover");
  assert.equal(getActiveRoute("/trades/discover/something").activeHref, "/trades/discover");
  assert.equal(group("/trades/discover"), "Trades");
});

test("the weekly recap sits in League after Schedule, so the palette lists it", () => {
  assert.equal(getActiveRoute("/weekly-recap").activeHref, "/weekly-recap");
  assert.equal(group("/weekly-recap"), "League");
  assert.deepEqual(NAV_GROUPS[3].pages.map((p) => p.href).slice(0, 2), ["/schedule-intelligence", "/weekly-recap"]);
});

test("projection movers sits in Players right after the Research lab", () => {
  const hrefs = NAV_GROUPS[1].pages.map((p) => p.href);
  assert.equal(hrefs[hrefs.indexOf("/research") + 1], "/movers");
  assert.equal(getActiveRoute("/movers").activeHref, "/movers");
  assert.equal(group("/movers"), "Players");
});

test("Compare sits right after Players in the Players group", () => {
  assert.equal(getActiveRoute("/compare").activeHref, "/compare");
  assert.equal(group("/compare"), "Players");
  const hrefs = NAV_GROUPS[1].pages.map((p) => p.href);
  assert.equal(hrefs[hrefs.indexOf("/players") + 1], "/compare");
  assert.equal(getActiveRoute("/compare/").activeHref, "/compare");
  assert.equal(getActiveRoute("/comparex").activeHref, null);
});

test("detail pages activate their list page", () => {
  assert.equal(getActiveRoute("/managers/123").activeHref, "/managers");
  assert.equal(group("/managers/123"), "League");
  assert.equal(getActiveRoute("/players/4262921").activeHref, "/players");
  assert.equal(group("/players/4262921"), "Players");
});

test("matching respects path boundaries and trailing slashes", () => {
  assert.equal(getActiveRoute("/managersx").activeHref, null);
  assert.equal(getActiveRoute("/start-sit/").activeHref, "/start-sit");
  assert.equal(group("/start-sit"), "My team");
});

test("unknown routes and Data quality activate nothing (never Overview)", () => {
  for (const path of ["/unknown", "/another-fake-page/nested", "/data", "", null]) {
    assert.deepEqual(getActiveRoute(path), { activeHref: null, activeGroup: null });
  }
});

test("every page appears once and every group has pages", () => {
  const hrefs = ALL_NAV_PAGES.map((p) => p.href);
  assert.equal(hrefs.length, new Set(hrefs).size, "duplicate hrefs in navigation");
  assert.deepEqual(NAV_GROUPS.map((g) => g.label), ["My team", "Players", "Trades", "League"]);
  assert.ok(NAV_GROUPS.every((g) => g.pages.length > 0));
  for (const href of ["/", "/matchup", "/start-sit", "/my-team/value", "/players", "/compare", "/movers", "/waivers", "/trades", "/trades/discover",
    "/schedule-intelligence", "/weekly-recap", "/luck-lab", "/playoff-race", "/managers", "/power-rankings",
    "/injury-impact"]) {
    assert.ok(hrefs.includes(href), `${href} missing from navigation`);
  }
});

test("the kicker outlook sits in Players", () => {
  assert.equal(getActiveRoute("/kickers").activeHref, "/kickers");
  assert.equal(group("/kickers"), "Players");
  assert.ok(ALL_NAV_PAGES.some((p) => p.href === "/kickers"));
});

test("power rankings sit last in League, and a team card highlights them", () => {
  assert.equal(group("/power-rankings"), "League");
  assert.equal(NAV_GROUPS[3].pages.at(-1)?.href, "/power-rankings");
  assert.equal(getActiveRoute("/power-rankings/4").activeHref, "/power-rankings");
});

test("injury impact is the last page in My team", () => {
  assert.equal(getActiveRoute("/injury-impact").activeHref, "/injury-impact");
  assert.equal(group("/injury-impact"), "My team");
  const hrefs = NAV_GROUPS[0].pages.map((p) => p.href);
  assert.equal(hrefs[hrefs.length - 1], "/injury-impact");
});
