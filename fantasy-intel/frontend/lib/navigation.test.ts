import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ALL_NAV_ITEMS,
  NAV_GROUPS,
  PRIMARY_NAV_ITEMS,
  getActiveRoute,
} from "./navigation.ts";

test("root route ('/') activates Overview only", () => {
  const result = getActiveRoute("/");
  assert.equal(result.activeHref, "/");
  assert.equal(result.activeGroupId, null);
});

test("nested trade routes: /trades activates Trades, /trades/discover activates Trade Finder", () => {
  const trades = getActiveRoute("/trades");
  assert.equal(trades.activeHref, "/trades");
  assert.equal(trades.activeGroupId, null);

  const discover = getActiveRoute("/trades/discover");
  assert.equal(discover.activeHref, "/trades/discover");
  assert.equal(discover.activeGroupId, null);

  // Sub-path of discover still activates discover
  const discoverSub = getActiveRoute("/trades/discover/something");
  assert.equal(discoverSub.activeHref, "/trades/discover");
});

test("my team value: /my-team/value activates My Team", () => {
  const myTeam = getActiveRoute("/my-team/value");
  assert.equal(myTeam.activeHref, "/my-team/value");
  assert.equal(myTeam.activeGroupId, null);
});

test("managers routes: /managers and /managers/123 activate Managers and More group", () => {
  const list = getActiveRoute("/managers");
  assert.equal(list.activeHref, "/managers");
  assert.equal(list.activeGroupId, "more");

  const detail = getActiveRoute("/managers/123");
  assert.equal(detail.activeHref, "/managers");
  assert.equal(detail.activeGroupId, "more");
});

test("insights routes: /luck-lab, /schedule-intelligence, /playoff-race activate respective items and Insights group", () => {
  const luck = getActiveRoute("/luck-lab");
  assert.equal(luck.activeHref, "/luck-lab");
  assert.equal(luck.activeGroupId, "insights");

  const sched = getActiveRoute("/schedule-intelligence");
  assert.equal(sched.activeHref, "/schedule-intelligence");
  assert.equal(sched.activeGroupId, "insights");

  const playoff = getActiveRoute("/playoff-race");
  assert.equal(playoff.activeHref, "/playoff-race");
  assert.equal(playoff.activeGroupId, "insights");
});

test("data quality route: /data activates Data Quality and More group", () => {
  const data = getActiveRoute("/data");
  assert.equal(data.activeHref, "/data");
  assert.equal(data.activeGroupId, "more");
});

test("unknown routes do not activate Overview or any group", () => {
  const unknown1 = getActiveRoute("/unknown");
  assert.equal(unknown1.activeHref, null);
  assert.equal(unknown1.activeGroupId, null);

  const unknown2 = getActiveRoute("/another-fake-page/nested");
  assert.equal(unknown2.activeHref, null);
  assert.equal(unknown2.activeGroupId, null);
});

test("no duplicate navigation destinations across primary and grouped items", () => {
  const allHrefs = ALL_NAV_ITEMS.map((item) => item.href);
  const uniqueHrefs = new Set(allHrefs);
  assert.equal(allHrefs.length, uniqueHrefs.size, "Duplicate hrefs found in navigation");
});

test("navigation hierarchy contains all required pages", () => {
  const primaryHrefs = PRIMARY_NAV_ITEMS.map((i) => i.href);
  assert.deepEqual(primaryHrefs, [
    "/",
    "/my-team/value",
    "/players",
    "/waivers",
    "/trades",
    "/trades/discover",
  ]);

  const insightsGroup = NAV_GROUPS.find((g) => g.id === "insights");
  assert.ok(insightsGroup);
  assert.deepEqual(
    insightsGroup.items.map((i) => i.href),
    ["/schedule-intelligence", "/playoff-race", "/luck-lab"]
  );

  const moreGroup = NAV_GROUPS.find((g) => g.id === "more");
  assert.ok(moreGroup);
  assert.deepEqual(
    moreGroup.items.map((i) => i.href),
    ["/managers", "/data"]
  );
});
