import assert from "node:assert/strict";
import { test } from "node:test";
import { leagueRouteHref, proxyDecision, scopeApiPath } from "./league-scope.ts";
import { ALL_NAV_PAGES, NAV_GROUPS } from "./navigation.ts";
import {
  FUZZY_SCORE, LEGACY_RECENT_KEY, QUICK_ACTIONS, RECENT_LIMIT, buildSections, flattenSections, normalize, pageItems, parseLeagueRecents,
  parseRecents, playerItem, playerItems, pushRecent, readLeagueRecents, recentsKey, writeLeagueRecents,
  rankItems, scoreItem, searchPath, searchResponse, searchSrc, wrapIndex, type PaletteItem,
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

test("rankItems: a subsequence-only match is returned only when nothing matches better", () => {
  const dataCheck = QUICK_ACTIONS.find((a) => a.href === "/data")!;
  const chase = { id: "p:1", kind: "player" as const, label: "Chase Brown", href: "/players?id=1" };
  assert.equal(scoreItem("chase", dataCheck), FUZZY_SCORE);
  assert.deepEqual(rankItems("chase", [dataCheck, chase]).map((i) => i.id), ["p:1"]);
  assert.deepEqual(rankItems("chase", [dataCheck]).map((i) => i.id), [dataCheck.id]);
});

test("a name that only fuzzy-matches a page puts the players first and drops the weak page match", () => {
  const chase = [playerItem({ id: 3, name: "Ja'Marr Chase", position: "WR", team: "CIN", owner: "los changitos" })];
  const sections = buildSections("chase", pages, chase, []);
  assert.deepEqual(sections.map((s) => s.key), ["players"]);
  assert.equal(flattenSections(sections)[0].label, "Ja'Marr Chase");
});

test("while players are still loading, a weak page match is not offered (Enter must not open it)", () => {
  assert.deepEqual(buildSections("chase", pages, [], [], true), []);
  // Once the search settles with no player, the weak match is the best remaining answer.
  assert.deepEqual(flattenSections(buildSections("chase", pages, [], [], false)).map((i) => i.href), ["/data"]);
});

test("a strong page match is unaffected by pending players or players that also matched", () => {
  assert.equal(flattenSections(buildSections("wai", pages, [], [], true))[0].href, "/waivers");
  const sections = buildSections("wai", pages, players, [], false);
  assert.deepEqual(sections.map((s) => s.key), ["pages", "players"]);
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

test("a player's recent keeps only his position: never this league's holder, roster status, injury or NFL team", () => {
  const live = playerItem({ id: 9, name: "Echo RB", position: "RB", team: "KC", owner: "Team 1", availability: "ONTEAM", injury: "OUT" });
  assert.equal(live.hint, "RB · KC · Out · Team 1");
  const [recent] = pushRecent([], live);
  assert.deepEqual(recent, { id: "player:9", kind: "player", label: "Echo RB", hint: "RB", href: "/players?id=9" });
  for (const availability of ["WAIVERS", "FREEAGENT"] as const) {
    const [r] = pushRecent([], playerItem({ id: 9, name: "Echo RB", position: "RB", team: "KC", owner: null, availability, injury: null }));
    assert.equal(r.hint, "RB");
  }
  // A player item built elsewhere without a recentHint keeps no hint at all; pages keep theirs.
  assert.equal(pushRecent([], { id: "player:3", kind: "player", label: "X", hint: "WR · Team 4", href: "/players?id=3" })[0].hint, undefined);
  assert.equal(pushRecent([], QUICK_ACTIONS[0])[0].hint, QUICK_ACTIONS[0].hint);
  // A stored player hint that is anything but a position is dropped on read.
  const stored = JSON.stringify([{ id: "player:3", kind: "player", label: "X", hint: "WR · BAL · Gridiron Ghosts", href: "/players?id=3" },
    { id: "player:4", kind: "player", label: "Y", hint: "D/ST", href: "/players?id=4" }]);
  assert.deepEqual(parseRecents(stored).map((r) => r.hint), [undefined, "D/ST"]);
});

test("recents are kept per league, refused under another league's key, never outside a league; the legacy list is ignored and removed", () => {
  const A = "lg_aaaaaaaaaaaaaaaa";
  const B = "lg_bbbbbbbbbbbbbbbb";
  const map = new Map<string, string>();
  const io = { getItem: (k: string) => map.get(k) ?? null, setItem: (k: string, v: string) => { map.set(k, v); }, removeItem: (k: string) => { map.delete(k); } };
  assert.equal(recentsKey(A), "fi:palette-recent:v1:lg_aaaaaaaaaaaaaaaa");
  for (const lg of [null, undefined, "", "12345"]) assert.equal(recentsKey(lg), null);

  // The pre-#200 browser-wide list (with a holder in a player hint) is never read, and goes on the first write.
  map.set(LEGACY_RECENT_KEY, JSON.stringify([{ id: "player:2", kind: "player", label: "Zay Flowers", hint: "WR · BAL · Gridiron Ghosts", href: "/players?id=2" }]));
  assert.deepEqual(readLeagueRecents(io, A), []);
  const live = playerItem({ id: 309, name: "Waiver WR", position: "WR", team: "LAR", owner: null, availability: "WAIVERS", injury: null });
  assert.equal(writeLeagueRecents(io, A, pushRecent([], live)), true);
  assert.equal(map.has(LEGACY_RECENT_KEY), false);
  assert.deepEqual(readLeagueRecents(io, A), [{ id: "player:309", kind: "player", label: "Waiver WR", hint: "WR", href: "/players?id=309" }]);
  assert.doesNotMatch(map.get(recentsKey(A)!)!, /waivers|Free agent|LAR/i);

  // League B sees nothing, even with A's payload copied under its key; nothing is stored outside a league.
  assert.deepEqual(readLeagueRecents(io, B), []);
  map.set(recentsKey(B)!, map.get(recentsKey(A)!)!);
  assert.deepEqual(readLeagueRecents(io, B), []);
  assert.equal(writeLeagueRecents(io, null, [live]), false);
  assert.deepEqual(readLeagueRecents(io, null), []);
  for (const raw of ["{", "null", "[]", JSON.stringify({ v: 2, league: A, items: [] }), JSON.stringify({ v: 1, league: A, items: "x" })])
    assert.deepEqual(parseLeagueRecents(raw, A), [], raw);
  const blocked = { getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("quota"); }, removeItem: () => {} };
  assert.deepEqual(readLeagueRecents(blocked, A), []);
  assert.equal(writeLeagueRecents(blocked, A, [live]), false);
});

test("before the provider is confirmed for the league searched, results use only provider-neutral words", () => {
  const p = (availability: "ONTEAM" | "WAIVERS" | "FREEAGENT" | null, owner: string | null = null) =>
    ({ id: 9, name: "Echo RB", position: "RB", team: "KC", owner, availability, injury: null });
  assert.equal(playerItem(p("FREEAGENT"), null).hint, "RB · KC · Not rostered");
  assert.equal(playerItem(p("ONTEAM", "Team 1"), null).hint, "RB · KC · Team 1");
  assert.equal(playerItem(p("WAIVERS"), null).hint, "RB · KC · On waivers");
  assert.equal(playerItem(p(null), null).hint, "RB · KC · Roster status unknown");
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
  // No NFL team is said in words: "FA" would read as a fantasy free agent.
  assert.equal(playerItem({ id: 3, name: "X", position: "RB", team: null, owner: null }).hint, "RB · No NFL team");
});

test("player results say who holds him in this league, in the provider's terms, with any injury designation", () => {
  const p = (availability: "ONTEAM" | "WAIVERS" | "FREEAGENT" | null, owner: string | null = null, injury: string | null = null) =>
    ({ id: 9, name: "Echo RB", position: "RB", team: "KC", owner, availability, injury });
  assert.equal(playerItem(p("ONTEAM", "Team 1")).hint, "RB · KC · Team 1");
  assert.equal(playerItem(p("ONTEAM")).hint, "RB · KC · Rostered");
  assert.equal(playerItem(p("WAIVERS")).hint, "RB · KC · On waivers");
  assert.equal(playerItem(p("FREEAGENT")).hint, "RB · KC · Free agent");
  // Sleeper says only whether he is rostered: an unrostered player may be on waivers, so he is never "Free agent".
  assert.equal(playerItem(p("FREEAGENT"), "sleeper").hint, "RB · KC · Not rostered");
  assert.equal(playerItem(p(null)).hint, "RB · KC · Roster status unknown");
  assert.equal(playerItem(p("FREEAGENT", null, "OUT")).hint, "RB · KC · Out · Free agent");
  assert.equal(playerItem(p("ONTEAM", "Team 3", "INJURY_RESERVE")).hint, "RB · KC · IR · Team 3");
  assert.equal(playerItem(p("ONTEAM", "Team 3", "ACTIVE")).hint, "RB · KC · Team 3");
});

test("same-name players stay distinguishable; only identical lines gain the player ID", () => {
  const twins = playerItems([
    { id: 11, name: "Daniel Jones", position: "QB", team: "NYG", owner: "Team 1", availability: "ONTEAM", injury: null },
    { id: 12, name: "Daniel Jones", position: "WR", team: null, owner: null, availability: "FREEAGENT", injury: null },
    { id: 13, name: "José Núñez", position: "TE", team: "LV", owner: null, availability: "FREEAGENT", injury: null },
    { id: 14, name: "Jose Nunez", position: "TE", team: "LV", owner: null, availability: "FREEAGENT", injury: null },
  ]);
  assert.deepEqual(twins.map((i) => i.id), ["player:11", "player:12", "player:13", "player:14"]);
  assert.equal(twins[0].hint, "QB · NYG · Team 1");
  assert.equal(twins[1].hint, "WR · No NFL team · Free agent");
  // Accents aside these two read the same, so each shows its ID.
  assert.equal(twins[2].hint, "TE · LV · Free agent · ID 13");
  assert.equal(twins[3].hint, "TE · LV · Free agent · ID 14");
  assert.equal(normalize(twins[2].label), normalize(twins[3].label));
});

test("empty and accented queries: nothing is sent for blank text, accents reach the backend intact", () => {
  for (const q of ["", "   ", "\t\n"]) assert.equal(searchPath(new URLSearchParams({ q })), null);
  const path = searchPath(new URLSearchParams({ q: "  José Núñez " }))!;
  assert.equal(new URL(path, "http://x").searchParams.get("q"), "José Núñez");
  // The palette matches pages and recents without accents too.
  assert.ok(scoreItem("nunez", { id: "p", kind: "player", label: "José Núñez", href: "/players?id=1" }) > 0);
});

test("pages in a labelled cluster say so in their hint, and their summary finds them", () => {
  const movers = pages.find((p) => p.href === "/movers")!;
  assert.equal(movers.hint, "Players · Research");
  assert.equal(rankItems("schedule luck", [...QUICK_ACTIONS, ...pages])[0]?.href, "/luck-lab");
  assert.equal(rankItems("opponents", [...QUICK_ACTIONS, ...pages]).some((i) => i.href === "/schedule-intelligence"), true);
});


test("hidden routes leave quick actions, recents and pages alike", () => {
  const hidden = new Set(["/research", "/projection-performance"]);
  const recents = [{ id: "r1", kind: "page", label: "WR lab", href: "/research/wr?view=momentum" },
                   { id: "r2", kind: "page", label: "Waivers", href: "/waivers" }] as PaletteItem[];
  const pages = pageItems(NAV_GROUPS);
  const hrefs = (secs: ReturnType<typeof buildSections>) => secs.flatMap((s) => s.items.map((i) => i.href));
  const shown = hrefs(buildSections("", pages, [], recents, false, hidden));
  assert.ok(!shown.some((h) => h.startsWith("/research") || h.startsWith("/projection-performance")), shown.join(","));
  assert.ok(shown.includes("/waivers"));
  assert.ok(!hrefs(buildSections("research", pages, [], recents, false, hidden)).some((h) => h.startsWith("/research")));
  assert.ok(hrefs(buildSections("", pages, [], recents)).includes("/research"));   // nothing hidden: unchanged
});

test("a player search from a league page stays in that league end to end", () => {
  const LG = "lg_" + "c".repeat(24);
  const src = searchSrc("Zay F&lowers");
  // What the command palette and the compare pickers fetch on /l/<lg>/...: the league-prefixed route handler URL ...
  const fetched = new URL(leagueRouteHref(src, LG), "http://x");
  assert.equal(fetched.pathname, `/l/${LG}/api/search`);
  // ... which proxy.ts rewrites to app/api/search with that league's header ...
  const d = proxyDecision({ enabled: true, pathname: fetched.pathname, search: fetched.search });
  assert.equal(d.kind, "rewrite");
  assert.equal(d.kind === "rewrite" && d.league, LG);
  // ... whose backend path carries only the query, and api() moves it to the league's /v2 mount.
  const path = searchPath(new URL(d.kind === "rewrite" ? d.path : "", "http://x").searchParams)!;
  assert.equal(path, "/api/players?q=Zay%20F%26lowers&limit=8");
  assert.equal(scopeApiPath(path, d.kind === "rewrite" ? d.league : null), `/v2/leagues/${LG}/api/players?q=Zay%20F%26lowers&limit=8`);
  // The old bare URL is unscoped: the proxy passes it on without a league, even with a last-viewed-league cookie, so it
  // could only reach the classic league (or a 404 once the classic site is off).
  assert.deepEqual(proxyDecision({ enabled: true, pathname: "/api/search", search: fetched.search, cookie: LG }), { kind: "next" });
  // The classic site keeps its unprefixed URL and its unscoped backend path.
  assert.equal(leagueRouteHref(src, null), src);
  assert.equal(scopeApiPath(path, null), path);
});

test("searchPath forwards only a trimmed, capped query", () => {
  assert.equal(searchPath(new URLSearchParams("q=%20%20")), null);
  assert.equal(searchPath(new URLSearchParams("")), null);
  assert.equal(searchPath(new URLSearchParams("q=%20josh%20&limit=100&team_id=3")), "/api/players?q=josh&limit=8");
  const long = searchPath(new URLSearchParams({ q: "x".repeat(200) }))!;
  assert.equal(new URL(long, "http://x").searchParams.get("q"), "x".repeat(80));
});

test("searchPath caps by code points, so an emoji at the cap never splits into a lone surrogate", () => {
  const q = "a".repeat(79) + "😀" + "b";
  // The old .slice(0, 80) cut the emoji in half and encodeURIComponent threw (URIError: a 500 from the route).
  assert.throws(() => encodeURIComponent(q.slice(0, 80)), URIError);
  const path = searchPath(new URLSearchParams({ q }))!;
  assert.equal(new URL(path, "http://x").searchParams.get("q"), "a".repeat(79) + "😀");
  assert.doesNotThrow(() => searchPath(new URLSearchParams({ q: "😀".repeat(200) })));
});

test("search responses are never shared-cacheable: results, empty query and backend failure", async () => {
  const ok = searchResponse({ ok: true, error: null, data: [
    { espn_player_id: 201, full_name: "Echo RB", position: "RB", pro_team: "KC", fantasy_team: "Team 1", availability_status: "ONTEAM",
      injury_status: "QUESTIONABLE" },
    { espn_player_id: 7, full_name: null, position: "WR", pro_team: null, fantasy_team: null },
    { espn_player_id: 202, full_name: "Foxtrot RB", position: "RB", pro_team: null, fantasy_team: null, availability_status: "SOMETHING",
      injury_status: "<b>free text</b>" },
  ] });
  assert.equal(ok.status, 200);
  assert.equal(ok.headers.get("cache-control"), "private, no-store");
  // Unknown roster states and anything but a short designation code are sent as null, never passed through.
  assert.deepEqual(await ok.json(), { players: [
    { id: 201, name: "Echo RB", position: "RB", team: "KC", owner: "Team 1", availability: "ONTEAM", injury: "QUESTIONABLE" },
    { id: 202, name: "Foxtrot RB", position: "RB", team: null, owner: null, availability: null, injury: null },
  ] });

  const empty = searchResponse(null);
  assert.equal(empty.status, 200);
  assert.equal(empty.headers.get("cache-control"), "private, no-store");
  assert.deepEqual(await empty.json(), { players: [] });

  // A league the visitor cannot see answers 404 upstream: the same 502 as any other failure, so nothing is revealed.
  const failed = searchResponse({ ok: false, data: null, error: "Not Found", kind: "http", status: 404 });
  assert.equal(failed.status, 502);
  assert.equal(failed.headers.get("cache-control"), "private, no-store");
  assert.deepEqual(await failed.json(), { players: [], error: "http" });
});
