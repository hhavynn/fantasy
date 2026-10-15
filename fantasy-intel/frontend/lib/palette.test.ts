import assert from "node:assert/strict";
import { test } from "node:test";
import { ALL_NAV_PAGES, NAV_GROUPS } from "./navigation.ts";
import {
  QUICK_ACTIONS, RECENT_LIMIT, buildSections, flattenSections, pageItems, parseRecents, playerItem, pushRecent,
  rankItems, scoreItem, wrapIndex,
} from "./palette.ts";

const pages = pageItems(NAV_GROUPS);
const players = [
  playerItem({ id: 1, name: "Lloyd Cushenberry", position: "C", team: "TEN", owner: null }),
  playerItem({ id: 2, name: "Zay Flowers", position: "WR", team: "BAL", owner: "Gridiron Ghosts" }),
];

test("every nav page and Data quality is reachable, as a page or through the action that stands in for it", () => {
  const hrefs = new Set([...pages, ...QUICK_ACTIONS].map((i) => i.href));
  for (const p of ALL_NAV_PAGES) assert.ok(hrefs.has(p.href), p.href);
  assert.ok(hrefs.has("/data"));
});

test("no destination is listed twice among actions and pages", () => {
  const hrefs = [...QUICK_ACTIONS, ...pages].map((i) => i.href);
  assert.equal(new Set(hrefs).size, hrefs.length);
});

test("scoreItem: exact beats prefix beats word start beats substring; no match scores 0", () => {
  const item = { id: "x", kind: "page" as const, label: "Playoff race", href: "/playoff-race" };
  const s = (q: string) => scoreItem(q, item);
  assert.equal(s("playoff race"), 100);
  assert.ok(s("play") > s("race"));
  assert.ok(s("race") > s("off"));
  assert.ok(s("off") > 0);
  assert.equal(s("zzz"), 0);
});

test("scoreItem ignores case and accents", () => {
  const item = { id: "x", kind: "player" as const, label: "José Núñez", href: "/players?id=1" };
  assert.ok(scoreItem("jose", item) >= 85);
  assert.ok(scoreItem("NUNEZ", item) >= 70);
});

test("scoreItem: keywords and hint find an item when the label does not", () => {
  const trade = QUICK_ACTIONS.find((a) => a.href === "/trades")!;
  assert.ok(scoreItem("analyzer", trade) > 0);
  assert.equal(scoreItem("analyzer", { ...trade, keywords: [], hint: undefined }), 0);
});

test("rankItems orders by score", () => {
  const ranked = rankItems("wa", [...QUICK_ACTIONS, ...pages]).map((i) => i.href);
  assert.equal(ranked[0], "/waivers");
});

test("empty query: recents, then actions, then pages, with recents not repeated", () => {
  const recent = pushRecent([], QUICK_ACTIONS[2]);
  const sections = buildSections("", pages, [], recent);
  assert.deepEqual(sections.map((s) => s.key), ["recent", "actions", "pages"]);
  const flat = flattenSections(sections);
  assert.equal(flat.filter((i) => i.href === QUICK_ACTIONS[2].href).length, 1);
  assert.deepEqual(buildSections("", pages, [], []).map((s) => s.key), ["actions", "pages"]);
});

test("a query that names a page puts it ahead of players", () => {
  const sections = buildSections("wai", pages, players, []);
  assert.deepEqual(sections.map((s) => s.key), ["pages", "players"]);
  assert.equal(sections[0].items[0].href, "/waivers");
});

test("a query only players match leads with players and omits the empty section", () => {
  const sections = buildSections("lloyd", pages, players, []);
  assert.deepEqual(sections.map((s) => s.key), ["players"]);
  assert.equal(sections[0].items[0].label, "Lloyd Cushenberry");
});

test("a query nothing matches yields no sections", () => {
  assert.deepEqual(buildSections("qqqq", pages, [], []), []);
});

test("wrapIndex wraps both ways and starts from the ends when nothing is active", () => {
  assert.equal(wrapIndex(0, 1, 3), 1);
  assert.equal(wrapIndex(2, 1, 3), 0);
  assert.equal(wrapIndex(0, -1, 3), 2);
  assert.equal(wrapIndex(-1, 1, 3), 0);
  assert.equal(wrapIndex(-1, -1, 3), 2);
  assert.equal(wrapIndex(0, 1, 0), -1);
  assert.equal(wrapIndex(1, 1, 1), 0);
});

test("pushRecent de-duplicates by destination, puts the newest first and caps the list", () => {
  let r = pushRecent([], players[0]);
  r = pushRecent(r, players[1]);
  r = pushRecent(r, players[0]);
  assert.deepEqual(r.map((i) => i.id), ["player:1", "player:2"]);
  for (let i = 0; i < 10; i++) r = pushRecent(r, { id: `p${i}`, kind: "page", label: `P${i}`, href: `/p${i}` });
  assert.equal(r.length, RECENT_LIMIT);
  assert.equal(r[0].id, "p9");
});

test("parseRecents survives garbage and rejects off-site destinations", () => {
  assert.deepEqual(parseRecents(null), []);
  assert.deepEqual(parseRecents("not json"), []);
  assert.deepEqual(parseRecents('{"a":1}'), []);
  const ok = { id: "a", kind: "page", label: "A", href: "/a" };
  const evil = { id: "b", kind: "page", label: "B", href: "//evil.example" };
  const abs = { id: "c", kind: "page", label: "C", href: "https://evil.example" };
  assert.deepEqual(parseRecents(JSON.stringify([ok, evil, abs, 5, null])).map((i) => i.id), ["a"]);
});

test("playerItem builds the profile link and a compact hint", () => {
  assert.equal(players[0].href, "/players?id=1");
  assert.equal(players[0].hint, "C · TEN");
  assert.equal(players[1].hint, "WR · BAL · Gridiron Ghosts");
  assert.equal(playerItem({ id: 3, name: "X", position: "RB", team: null, owner: null }).hint, "RB · FA");
});
