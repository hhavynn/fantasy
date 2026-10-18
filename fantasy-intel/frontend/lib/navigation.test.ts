import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ALL_NAV_PAGES, NAV_GROUPS, getActiveRoute, navClusters, parseTabMemory, tabMemoryEntry, tabTarget,
} from "./navigation.ts";

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

test("the weekly recap leads League, so the palette lists it", () => {
  assert.equal(getActiveRoute("/weekly-recap").activeHref, "/weekly-recap");
  assert.equal(group("/weekly-recap"), "League");
  assert.equal(NAV_GROUPS[3].pages[0].href, "/weekly-recap");
});

test("projection movers sits in the Players research cluster", () => {
  const movers = NAV_GROUPS[1].pages.find((p) => p.href === "/movers");
  assert.equal(movers?.section, "Research");
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
  assert.equal(getActiveRoute("/defenses").activeHref, "/defenses");
  assert.equal(group("/defenses"), "Players");
});

// ---------- Task-based groups (frontend UX overhaul) ----------

const groupOf = (label: string) => NAV_GROUPS.find((g) => g.label === label)!;

test("My team holds this week's decisions for the analyzed roster, Waivers and Injury impact included", () => {
  assert.deepEqual(groupOf("My team").pages.map((p) => p.href), ["/", "/matchup", "/start-sit", "/waivers", "/my-team/value", "/injury-impact"]);
  assert.equal(group("/waivers"), "My team");
});

test("Players leads with Rankings; the advanced tools sit in one labelled Research cluster after the everyday pages", () => {
  const players = groupOf("Players");
  assert.equal(players.pages[0].href, "/rankings");
  const clusters = navClusters(players);
  assert.deepEqual(clusters.map((c) => c.title), [null, "Research"]);
  assert.deepEqual(clusters[0].pages.map((p) => p.href), ["/rankings", "/players", "/compare", "/kickers", "/defenses"]);
  assert.deepEqual(clusters[1].pages.map((p) => p.href).sort(),
    ["/movers", "/projection-performance", "/research", "/schedule-intelligence"].sort());
  assert.equal(group("/schedule-intelligence"), "Players");
  assert.equal(getActiveRoute("/research/wr").activeHref, "/research");
});

test("groups without a labelled cluster are one cluster", () => {
  for (const label of ["My team", "Trades", "League"]) {
    assert.deepEqual(navClusters(groupOf(label)).map((c) => c.title), [null], label);
  }
});

test("a group's clusters are contiguous, so the sub-nav never repeats a label", () => {
  for (const g of NAV_GROUPS) {
    const titles = navClusters(g).map((c) => c.title);
    assert.equal(titles.length, new Set(titles).size, g.label);
  }
});

test("every page carries a one-line summary for link titles and search", () => {
  for (const p of ALL_NAV_PAGES) assert.ok(p.summary && p.summary.length > 10 && !p.summary.endsWith("."), p.href);
});

test("the feature-flagged league connections page is not advertised in the nav", () => {
  assert.equal(getActiveRoute("/leagues").activeHref, null);
});

// ---------- Phone tab bar memory ----------

test("the tab memory records a page's path and filters, without the team", () => {
  assert.deepEqual(tabMemoryEntry("/rankings", "pos=WR&team=4", "4"), { group: "Players", entry: { href: "/rankings?pos=WR", team: "4" } });
  assert.deepEqual(tabMemoryEntry("/trades/discover", "", null), { group: "Trades", entry: { href: "/trades/discover", team: null } });
  assert.deepEqual(tabMemoryEntry("/players", "id=201", null), { group: "Players", entry: { href: "/players?id=201", team: null } });
  assert.equal(tabMemoryEntry("/data", "", null), null);
  assert.equal(tabMemoryEntry("/leagues", "", null), null);
});

test("a tab reopens the remembered page; the active group's tab returns to the group's first page", () => {
  const trades = groupOf("Trades");
  const remembered = { href: "/trades/discover?mode=balanced", team: null };
  assert.equal(tabTarget(trades, false, remembered, null), "/trades/discover?mode=balanced");
  assert.equal(tabTarget(trades, true, remembered, null), "/trades");
  assert.equal(tabTarget(trades, false, undefined, null), "/trades");
});

test("a page remembered under another team keeps only its path, so a trade or opponent is never replayed for another team", () => {
  const trades = groupOf("Trades");
  const remembered = { href: "/trades?ta=4&tb=2&a=11", team: "4" };
  assert.equal(tabTarget(trades, false, remembered, "4"), "/trades?ta=4&tb=2&a=11");
  assert.equal(tabTarget(trades, false, remembered, null), "/trades");
  assert.equal(tabTarget(trades, false, remembered, "7"), "/trades");
});

test("a remembered page outside the group (a stale or moved entry) falls back to the group's first page", () => {
  assert.equal(tabTarget(groupOf("Players"), false, { href: "/waivers?pos=RB", team: null }, null), "/rankings");
  assert.equal(tabTarget(groupOf("League"), false, { href: "/gone", team: null }, null), "/weekly-recap");
});

test("stored tab memory is parsed defensively", () => {
  assert.deepEqual(parseTabMemory(null), {});
  assert.deepEqual(parseTabMemory("not json"), {});
  assert.deepEqual(parseTabMemory("[1,2]"), {});
  assert.deepEqual(parseTabMemory(JSON.stringify({
    Players: { href: "/rankings?pos=WR", team: null },
    Trades: { href: "//evil.example/trades", team: null },
    League: { href: "https://evil.example", team: null },
    "My team": { href: "/waivers", team: 4 },
  })), { Players: { href: "/rankings?pos=WR", team: null } });
});

test("power rankings sit in League right after the weekly recap, and a team card highlights them", () => {
  assert.equal(group("/power-rankings"), "League");
  assert.deepEqual(NAV_GROUPS[3].pages.map((p) => p.href).slice(0, 2), ["/weekly-recap", "/power-rankings"]);
  assert.equal(getActiveRoute("/power-rankings/4").activeHref, "/power-rankings");
});

test("injury impact is the last page in My team", () => {
  assert.equal(getActiveRoute("/injury-impact").activeHref, "/injury-impact");
  assert.equal(group("/injury-impact"), "My team");
  const hrefs = NAV_GROUPS[0].pages.map((p) => p.href);
  assert.equal(hrefs[hrefs.length - 1], "/injury-impact");
});
