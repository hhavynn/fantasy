import assert from "node:assert/strict";
import test from "node:test";
import { MAX_RECENT, dataAsOf, parseRecent, rememberLeague } from "./league-switcher.ts";

const id = (n: number) => `lg_${String(n).padStart(24, "0")}`;

test("rememberLeague keeps the most recent first, deduplicated and bounded", () => {
  let list = [] as ReturnType<typeof parseRecent>;
  for (let i = 0; i < 12; i++) list = rememberLeague(list, { id: id(i), name: `L${i}` });
  list = rememberLeague(list, { id: id(9), name: "L9 renamed" });
  assert.equal(list.length, MAX_RECENT);
  assert.deepEqual(list[0], { id: id(9), name: "L9 renamed" });
  assert.equal(list.filter((x) => x.id === id(9)).length, 1);
  assert.deepEqual(rememberLeague(list, { id: "lg_bad", name: "x" }), list);
});

test("parseRecent drops anything malformed", () => {
  assert.deepEqual(parseRecent(null), []);
  assert.deepEqual(parseRecent("not json"), []);
  assert.deepEqual(parseRecent(JSON.stringify({ id: id(1) })), []);
  assert.deepEqual(parseRecent(JSON.stringify([{ id: id(1), name: "A" }, { id: "../x", name: "B" }, { id: id(2) }])),
    [{ id: id(1), name: "A" }]);
});

test("dataAsOf formats the served version's time", () => {
  assert.equal(dataAsOf(null), null);
  assert.equal(dataAsOf("garbage"), null);
  assert.equal(dataAsOf("2026-10-20T12:00:00Z", "UTC"), "Data as of Oct 20, 12:00 PM");
});
