// Run: npm test (node --test with type stripping).
import assert from "node:assert/strict";
import { test } from "node:test";
import { sortRows } from "./sort.ts";
import {
  API_SORTS, defaultSort, discoverQuery, evidencePath, evidenceSrc, isApiSort, resolveSort, sortValue, type SortableTrade,
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
  assert.equal(discoverQuery({}).toString(), "");
  assert.equal(
    discoverQuery({ team: "3", mode: "all", sort: "min_side", dir: "asc", kdst: "1", exact: "1", shape: ["1-for-1", "2-for-1"], limit: "20" }).toString(),
    "team_id=3&mode=all&limit=20&sort=min_side&include_kdst=true&heuristic=false&shapes=1-for-1%2C2-for-1",
  );
  // Table-only sorts and unknown keys never reach the API.
  assert.equal(discoverQuery({ sort: "partner", kdst: "0", foo: "bar" }).toString(), "");
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
