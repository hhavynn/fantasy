// Run: npm test (node --test with type stripping).
import assert from "node:assert/strict";
import { test } from "node:test";
import { sortRows } from "./sort.ts";
import {
  API_SORTS, analyzerHref, defaultSort, discoverQuery, evidencePath, evidenceSrc, horizonOptions, isApiSort, resolveSort, sortValue,
  targetHrefParams, targetQuery, type SortableTrade,
} from "./trade-discovery.ts";

const D = (ros: number | null, reg: number | null = ros, po: number | null = ros) => ({ ros_delta: ros, regular_season_delta: reg, playoff_delta: po });
const T = (over: Partial<SortableTrade> = {}): SortableTrade => ({
  shape: "1-for-1", opponent_name: "Tortas", mine: D(4), theirs: D(2), combined_ros_delta: 6, min_side_ros_delta: 2,
  asset_balance: { ratio: 0.9 }, ...over,
});

test("the API's sort keys are all table sort keys and nothing else is passed to the API", () => {
  for (const k of API_SORTS) assert.equal(isApiSort(k), true);
  assert.equal(isApiSort("shape"), false);
  assert.equal(isApiSort("rank"), false);
  assert.equal(isApiSort(undefined), false);
});

test("default order: API order, except All candidates which is best gain first", () => {
  assert.deepEqual(defaultSort("practical"), { key: "rank", dir: "asc" });
  assert.deepEqual(defaultSort("upside"), { key: "rank", dir: "asc" });
  assert.deepEqual(defaultSort("all"), { key: "my_gain", dir: "desc" });
});

test("sort state comes from the URL; unknown keys fall back to the mode default", () => {
  assert.deepEqual(resolveSort({ sort: "their_gain", dir: "asc" }, "practical"), { key: "their_gain", dir: "asc" });
  assert.deepEqual(resolveSort({ sort: "shape", dir: "desc" }, "practical"), { key: "shape", dir: "desc" });
  assert.deepEqual(resolveSort({ sort: "bogus", dir: "asc" }, "practical"), { key: "rank", dir: "asc" });
  assert.deepEqual(resolveSort({}, "all"), { key: "my_gain", dir: "desc" });
});

test("an older ?sort=<api sort> link without dir still means best first", () => {
  assert.deepEqual(resolveSort({ sort: "combined" }, "all"), { key: "combined", dir: "desc" });
  assert.deepEqual(resolveSort({ sort: "min_side_playoff" }, "practical"), { key: "min_side_playoff", dir: "desc" });
  assert.deepEqual(resolveSort({ sort: "shape" }, "practical"), { key: "shape", dir: "asc" });
});

test("column values: gains, smaller-side across phases, and missing data", () => {
  const r = T({ mine: D(4, 3, 1), theirs: D(2, -1, 5), combined_ros_delta: 6, min_side_ros_delta: 2 });
  assert.equal(sortValue(r, "my_gain", 0), 4);
  assert.equal(sortValue(r, "their_gain", 0), 2);
  assert.equal(sortValue(r, "combined", 0), 6);
  assert.equal(sortValue(r, "min_side", 0), 2);
  assert.equal(sortValue(r, "playoff_gain", 0), 1);
  assert.equal(sortValue(r, "min_side_regular", 0), -1);
  assert.equal(sortValue(r, "min_side_playoff", 0), 1);
  assert.equal(sortValue(r, "partner", 0), "Tortas");
  assert.equal(sortValue(r, "asset", 0), 0.9);
  assert.equal(sortValue(r, "rank", 7), 7);
  const incomplete = T({ mine: D(null, null, null), asset_balance: { ratio: null } });
  assert.equal(sortValue(incomplete, "my_gain", 0), null);
  assert.equal(sortValue(incomplete, "min_side_regular", 0), null);
  assert.equal(sortValue(incomplete, "asset", 0), null);
});

test("sorting keeps rows with missing values last in both directions", () => {
  const rows = [T({ shape: "a", mine: D(1) }), T({ shape: "b", mine: D(null) }), T({ shape: "c", mine: D(3) })];
  const bySort = (dir: "asc" | "desc") => sortRows(rows.map((r, i) => ({ r, i })), (x) => sortValue(x.r, "my_gain", x.i), dir).map((x) => x.r.shape);
  assert.deepEqual(bySort("desc"), ["c", "a", "b"]);
  assert.deepEqual(bySort("asc"), ["a", "c", "b"]);
});

test("the page URL maps onto the backend query the context, summary and evidence endpoints share", () => {
  assert.equal(discoverQuery({}).toString(), "mode=mutual_upgrades");
  assert.equal(
    discoverQuery({ team: "3", mode: "all", sort: "min_side", dir: "asc", kdst: "1", exact: "1", shape: ["1-for-1", "2-for-1"], limit: "20" }).toString(),
    "team_id=3&mode=all&limit=20&sort=min_side&include_kdst=true&heuristic=false&shapes=1-for-1%2C2-for-1",
  );
  // Table-only sorts and unknown keys never reach the API.
  assert.equal(discoverQuery({ sort: "partner", kdst: "0", foo: "bar" }).toString(), "mode=mutual_upgrades");
});

test("a URL without a mode sends the page default; legacy and explicit modes pass through", () => {
  assert.equal(discoverQuery({}).get("mode"), "mutual_upgrades");
  assert.equal(discoverQuery({ mode: "" }).get("mode"), "mutual_upgrades");
  assert.equal(discoverQuery({ mode: ["fair", "all"] }).get("mode"), "fair");
  for (const m of ["practical", "mutual", "mutual_upgrades", "fair", "upside", "consolidation", "playoff", "all"]) {
    assert.equal(discoverQuery({ mode: m }).get("mode"), m);
  }
  // The default does not disturb the other parameters or their order.
  assert.equal(discoverQuery({ team: "3", limit: "20" }).toString(), "team_id=3&mode=mutual_upgrades&limit=20");
});

test("the verdict column sorts by the backend's verdict order, best first, with no verdict last", () => {
  const V = (verdict: string | null) => T({ shape: verdict ?? "none", dual_value_summary: verdict ? { verdict } : null });
  const rows = [V("one_sided"), V("sensible"), V(null), V("unlikely_mutual"), V("incomplete"), V("sensible_with_concerns"), V("fair_no_lineup_gain")];
  const bySort = (dir: "asc" | "desc") => sortRows(rows.map((r, i) => ({ r, i })), (x) => sortValue(x.r, "verdict", x.i), dir).map((x) => x.r.shape);
  assert.deepEqual(bySort("asc"), ["sensible", "sensible_with_concerns", "unlikely_mutual", "fair_no_lineup_gain", "one_sided", "incomplete", "none"]);
  assert.deepEqual(bySort("desc"), ["incomplete", "one_sided", "fair_no_lineup_gain", "unlikely_mutual", "sensible_with_concerns", "sensible", "none"]);
  assert.equal(sortValue(T(), "verdict", 0), null);                 // a legacy row without dual_value_summary
  assert.deepEqual(resolveSort({ sort: "verdict", dir: "asc" }, "mutual_upgrades"), { key: "verdict", dir: "asc" });
  assert.deepEqual(resolveSort({ sort: "verdict" }, "mutual_upgrades"), { key: "verdict", dir: "asc" });
  assert.equal(isApiSort("verdict"), false);
  assert.equal(discoverQuery({ mode: "fair", sort: "verdict" }).get("sort"), null);
});

test("lens modes default to the backend order", () => {
  for (const m of ["mutual_upgrades", "fair", "upside", "consolidation", "playoff", "practical", "mutual"]) assert.deepEqual(defaultSort(m), { key: "rank", dir: "asc" });
});

test("a row's evidence request keeps the view's query and only known parameters reach the backend", () => {
  const src = evidenceSrc("team_id=3&mode=upside&shapes=1-for-1%2C2-for-1", "2|1-for-1|10|20");
  const url = new URL(src, "http://x");
  assert.equal(url.pathname, "/api/trade-evidence");
  assert.equal(evidencePath(url.searchParams),
    "/api/trades/discover/evidence?team_id=3&mode=upside&shapes=1-for-1%2C2-for-1&key=2%7C1-for-1%7C10%7C20");
  assert.equal(evidencePath(new URLSearchParams("key=k&positions=K&evil=1")), "/api/trades/discover/evidence?key=k");
  assert.equal(evidencePath(new URLSearchParams("mode=all")), null);
});

// ------------------------------------------------------------------------------- targeted discovery --

test("a saved link without a target sends no target parameter (original modes stay as they were)", () => {
  for (const mode of ["practical", "mutual", "mutual_upgrades", "fair", "upside", "consolidation", "playoff", "all"]) {
    const q = discoverQuery({ mode, team: "2", opponent: "3", shape: ["1-for-1", "2-for-1"] });
    for (const k of ["target_player", "target_position", "need", "weeks"]) assert.equal(q.get(k), null, `${mode} ${k}`);
  }
  assert.equal(discoverQuery({}).toString(), "mode=mutual_upgrades");
});

test("a targeted URL becomes the API's target parameters", () => {
  const q = discoverQuery({ mode: "targeted", team: "2", target: "204", pos: "RB", weeks: "7-9" });
  assert.equal(q.get("target_player"), "204");
  assert.equal(q.get("target_position"), "RB");
  assert.equal(q.get("weeks"), "7-9");
  assert.equal(q.get("need"), null);
});

test("a roster need wins over the field it chooses; blank fields are no target", () => {
  assert.deepEqual(targetQuery({ need: "weakest", pos: "WR" }, "targeted"), { need: "weakest" });
  assert.deepEqual(targetQuery({ need: "bye", weeks: "7" }, "targeted"), { need: "bye" });
  assert.deepEqual(targetQuery({ target: "", pos: "", need: "", weeks: "" }, "targeted"), {});
  assert.deepEqual(targetQuery({ target: " 12 " }, "targeted"), { target_player: "12" });
});

test("a horizon is only sent to the targeted mode; a player or position filters any mode", () => {
  assert.deepEqual(targetQuery({ weeks: "7", need: "bye", pos: "TE", target: "9" }, "mutual_upgrades"),
    { target_player: "9", target_position: "TE" });
  assert.deepEqual(targetQuery({ need: "weakest" }, "fair"), { need: "weakest" });
});

test("target parameters survive into a row's evidence request", () => {
  const q = discoverQuery({ mode: "targeted", pos: "RB", weeks: "5" }).toString();
  const path = evidencePath(new URL(evidenceSrc(q, "k1"), "http://x").searchParams)!;
  const p = new URL(path, "http://x").searchParams;
  assert.equal(p.get("target_position"), "RB");
  assert.equal(p.get("weeks"), "5");
  assert.equal(p.get("mode"), "targeted");
  assert.equal(p.get("key"), "k1");
});

test("the query echo becomes the URL keys that keep a view's target", () => {
  assert.deepEqual(targetHrefParams({ target_player: 204, target_position: null, need: "bye", weeks: null }),
    { target: "204", pos: undefined, need: "bye", weeks: undefined });
  assert.deepEqual(targetHrefParams({}), { target: undefined, pos: undefined, need: undefined, weeks: undefined });
});

test("horizon choices stay inside the evaluation window", () => {
  assert.deepEqual(horizonOptions(7, 17).map((o) => o.value), ["", "7", "7-9"]);
  assert.deepEqual(horizonOptions(16, 17).map((o) => o.value), ["", "16", "16-17"]);
  assert.equal(horizonOptions(16, 17)[2].label, "Next 2 weeks (weeks 16–17)");
  assert.deepEqual(horizonOptions(17, 17).map((o) => o.value), ["", "17"]);
  assert.deepEqual(horizonOptions(null, null).map((o) => o.value), [""]);
});

test("every discovered trade links to the analyzer with exactly its teams and packages", () => {
  const href = analyzerHref({ anchor_team_id: 2, opponent_team_id: 3, sends: [204, 205], receives: [208] });
  const u = new URL(href, "http://x");
  assert.equal(u.pathname, "/trades");
  assert.equal(u.searchParams.get("ta"), "2");
  assert.equal(u.searchParams.get("tb"), "3");
  assert.deepEqual(u.searchParams.getAll("a"), ["204", "205"]);
  assert.deepEqual(u.searchParams.getAll("b"), ["208"]);
  assert.deepEqual([...u.searchParams.keys()].sort(), ["a", "a", "b", "ta", "tb"]);
});

test("a row's evidence request from a league page stays in that league end to end", async () => {
  const { leagueRouteHref, proxyDecision } = await import("./league-scope.ts");
  const LG = "lg_" + "b".repeat(24);
  const src = evidenceSrc(discoverQuery({ mode: "targeted", team: "2", pos: "RB" }).toString(), "1-for-1:3:204:208");
  // What TradeEvidence fetches on /l/<lg>/trades/discover: the league-prefixed route handler URL ...
  const fetched = new URL(leagueRouteHref(src, LG), "http://x");
  assert.equal(fetched.pathname, `/l/${LG}/api/trade-evidence`);
  // ... which proxy.ts rewrites to the handler with that league's header (api() then calls /v2/leagues/<lg>/...) ...
  const d = proxyDecision({ enabled: true, pathname: fetched.pathname, search: fetched.search });
  assert.equal(d.kind, "rewrite");
  assert.equal(d.kind === "rewrite" && d.league, LG);
  // ... carrying only the view's own discovery parameters to the backend.
  const path = evidencePath(new URL(d.kind === "rewrite" ? d.path : "", "http://x").searchParams)!;
  assert.match(path, /^\/api\/trades\/discover\/evidence\?/);
  assert.equal(new URL(path, "http://x").searchParams.get("target_position"), "RB");
  // The old bare URL is unscoped: the proxy passes it on without a league (and strips any league header the browser
  // sends), so it could only ever reach the single-league classic path.
  const bare = new URL(src, "http://x");
  assert.deepEqual(proxyDecision({ enabled: true, pathname: bare.pathname, search: bare.search, cookie: LG }), { kind: "next" });
  // The classic site keeps its unprefixed URL.
  assert.equal(leagueRouteHref(src, null), src);
});
