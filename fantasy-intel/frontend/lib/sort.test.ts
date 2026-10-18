import assert from "node:assert/strict";
import { test } from "node:test";
import { nextDir, parseSort, sortHref, sortRows } from "./sort.ts";

type Row = { id: string; n: number | null; s?: string | null };
const ids = (rows: Row[]) => rows.map((r) => r.id).join("");

test("sortRows: numbers ascending and descending", () => {
  const rows: Row[] = [{ id: "a", n: 3 }, { id: "b", n: 1 }, { id: "c", n: 2 }];
  assert.equal(ids(sortRows(rows, (r) => r.n, "asc")), "bca");
  assert.equal(ids(sortRows(rows, (r) => r.n, "desc")), "acb");
});

test("sortRows: nulls are last in both directions, and a null is not zero", () => {
  const rows: Row[] = [{ id: "a", n: null }, { id: "b", n: 0 }, { id: "c", n: -1 }, { id: "d", n: null }, { id: "e", n: 5 }];
  assert.equal(ids(sortRows(rows, (r) => r.n, "asc")), "cbead");
  assert.equal(ids(sortRows(rows, (r) => r.n, "desc")), "ebcad");
});

test("sortRows: undefined and NaN are missing too", () => {
  const rows = [{ id: "a", v: undefined }, { id: "b", v: 2 }, { id: "c", v: NaN }, { id: "d", v: 1 }] as { id: string; v: number | undefined }[];
  assert.deepEqual(sortRows(rows, (r) => r.v, "asc").map((r) => r.id), ["d", "b", "a", "c"]);
  assert.deepEqual(sortRows(rows, (r) => r.v, "desc").map((r) => r.id), ["b", "d", "a", "c"]);
});

test("sortRows: stable for ties in both directions", () => {
  const rows: Row[] = [{ id: "a", n: 1 }, { id: "b", n: 2 }, { id: "c", n: 1 }, { id: "d", n: 2 }, { id: "e", n: 1 }];
  assert.equal(ids(sortRows(rows, (r) => r.n, "asc")), "acebd");
  assert.equal(ids(sortRows(rows, (r) => r.n, "desc")), "bdace");
});

test("sortRows: strings use localeCompare, missing strings last", () => {
  const rows: Row[] = [{ id: "a", n: 0, s: "banana" }, { id: "b", n: 0, s: "Apple" }, { id: "c", n: 0, s: "cherry" }, { id: "d", n: 0, s: null }];
  assert.equal(ids(sortRows(rows, (r) => r.s, "asc")), "bacd");
  assert.equal(ids(sortRows(rows, (r) => r.s, "desc")), "cabd");
});

test("sortRows: mixed numbers and strings put numbers first ascending, missing last", () => {
  const rows = [{ id: "a", v: "x" }, { id: "b", v: 2 }, { id: "c", v: null }, { id: "d", v: 1 }] as { id: string; v: number | string | null }[];
  assert.deepEqual(sortRows(rows, (r) => r.v, "asc").map((r) => r.id), ["d", "b", "a", "c"]);
  assert.deepEqual(sortRows(rows, (r) => r.v, "desc").map((r) => r.id), ["a", "b", "d", "c"]);
});

test("sortRows: does not mutate its input", () => {
  const rows: Row[] = [{ id: "a", n: 2 }, { id: "b", n: 1 }];
  sortRows(rows, (r) => r.n, "asc");
  assert.equal(ids(rows), "ab");
});

test("parseSort: valid key and direction", () => {
  assert.deepEqual(parseSort({ sort: "pf", dir: "asc" }, ["pf", "pa"], { key: "pa", dir: "desc" }), { key: "pf", dir: "asc" });
});

test("parseSort: unknown, missing or hostile keys fall back", () => {
  const fb = { key: "pa", dir: "desc" } as const;
  assert.deepEqual(parseSort({ sort: "nope" }, ["pf", "pa"], fb), fb);
  assert.deepEqual(parseSort({}, ["pf", "pa"], fb), fb);
  assert.deepEqual(parseSort(undefined, ["pf", "pa"], fb), fb);
  assert.deepEqual(parseSort({ sort: "__proto__" }, ["pf"], fb), fb);
});

test("parseSort: a bad direction uses the fallback direction only for the fallback key", () => {
  const fb = { key: "pa", dir: "desc" } as const;
  assert.deepEqual(parseSort({ sort: "pa", dir: "sideways" }, ["pf", "pa"], fb), { key: "pa", dir: "desc" });
  assert.deepEqual(parseSort({ sort: "pf", dir: "sideways" }, ["pf", "pa"], fb), { key: "pf", dir: "asc" });
});

test("parseSort: array params use the first value; named tables use their own params", () => {
  const fb = { key: "a", dir: "asc" } as const;
  assert.deepEqual(parseSort({ sort: ["b", "a"], dir: ["desc"] }, ["a", "b"], fb), { key: "b", dir: "desc" });
  assert.deepEqual(parseSort({ moves: "b", moves_dir: "desc", sort: "a" }, ["a", "b"], fb, "moves"), { key: "b", dir: "desc" });
});

test("sortHref: preserves other params (including repeated) and replaces the sort", () => {
  assert.equal(sortHref({ q: "jj", pos: ["WR", "TE"], sort: "pf", dir: "asc" }, "pa", "desc"), "?q=jj&pos=WR&pos=TE&sort=pa&dir=desc");
  assert.equal(sortHref(undefined, "pf", "asc"), "?sort=pf&dir=asc");
  assert.equal(sortHref({ sort: "pf", moves: "x" }, "y", "asc", "moves"), "?sort=pf&moves=y&moves_dir=asc");
});

test("nextDir: flips the active column, uses the default for a new one", () => {
  assert.equal(nextDir({ key: "pf", dir: "desc" }, "pf", "desc"), "asc");
  assert.equal(nextDir({ key: "pf", dir: "asc" }, "pf", "desc"), "desc");
  assert.equal(nextDir({ key: "pf", dir: "asc" }, "pa", "desc"), "desc");
});
