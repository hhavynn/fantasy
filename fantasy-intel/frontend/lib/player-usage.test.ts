import assert from "node:assert/strict";
import { test } from "node:test";
import { alignToAxis, hasData, pctTick, usageChartSpecs, valueDomain, weekAxis } from "./player-usage.ts";

const ids = (position: string | null) => usageChartSpecs(position).map((c) => c.id);

test("usageChartSpecs: charts follow the position", () => {
  assert.deepEqual(ids("WR"), ["snap", "target", "points"]);
  assert.deepEqual(ids("TE"), ["snap", "target", "points"]);
  assert.deepEqual(ids("RB"), ["snap", "rush", "target", "points"]);
  assert.deepEqual(ids("QB"), ["snap", "rush", "points"]);
});

test("usageChartSpecs: a WR has no rushing chart and an RB has no air yards", () => {
  const fields = (p: string) => usageChartSpecs(p).flatMap((c) => c.series.map((s) => s.field));
  assert.ok(!fields("WR").includes("team_rush_share"));
  assert.ok(!fields("WR").includes("backfield_rush_share"));
  assert.ok(!fields("RB").includes("air_yards_share"));
  assert.ok(fields("RB").includes("backfield_rush_share"));
  assert.ok(!fields("QB").includes("target_share"));
});

test("usageChartSpecs: kickers, defenses and unknown positions get no usage charts", () => {
  assert.deepEqual(ids("K"), []);
  assert.deepEqual(ids("D/ST"), []);
  assert.deepEqual(ids(null), []);
  assert.deepEqual(ids(undefined), []);
});

test("weekAxis: runs from week 1 to the latest record or completed week", () => {
  assert.deepEqual(weekAxis([1, 4, 5]), [1, 2, 3, 4, 5]);
  assert.deepEqual(weekAxis([1, 2], 5), [1, 2, 3, 4, 5]);
  assert.deepEqual(weekAxis([1, 7], 5), [1, 2, 3, 4, 5, 6, 7]);
  assert.deepEqual(weekAxis([], null), []);
  assert.deepEqual(weekAxis([], 0), []);
});

test("alignToAxis: weeks without a row, and null values, are gaps rather than zero", () => {
  const rows = [
    { week: 1, snap: 0.7 },
    { week: 4, snap: 0 },
    { week: 5, snap: null as number | null },
  ];
  const out = alignToAxis(rows, [1, 2, 3, 4, 5, 6], (r) => r.snap);
  assert.deepEqual(out, [0.7, null, null, 0, null, null]);
});

test("alignToAxis: ignores non-finite values", () => {
  const out = alignToAxis([{ week: 1, v: Number.NaN }, { week: 2, v: Infinity }], [1, 2], (r) => r.v);
  assert.deepEqual(out, [null, null]);
});

test("hasData: needs at least one real value", () => {
  assert.equal(hasData([null, null]), false);
  assert.equal(hasData([]), false);
  assert.equal(hasData([null, 0]), true);
});

test("pctTick rounds a share to a whole percent", () => {
  assert.equal(pctTick(0.35), "35%");
  assert.equal(pctTick(1), "100%");
  assert.equal(pctTick(0), "0%");
});

test("valueDomain: shares start at zero and top out at the next 10%", () => {
  assert.deepEqual(valueDomain([0.12, 0.31, null], "pct"), [0, 0.4]);
  assert.deepEqual(valueDomain([0.7, 1], "pct"), [0, 1]);
  assert.deepEqual(valueDomain([0.02], "pct"), [0, 0.1]);
});

test("valueDomain: points start at zero unless a value is negative, with headroom above", () => {
  assert.deepEqual(valueDomain([12.4, 27.7, 37.7], "pts"), [0, 42]);
  assert.deepEqual(valueDomain([-2.5, 10], "pts"), [-3, 11]);
  assert.deepEqual(valueDomain([0], "pts"), [0, 1]);
});

test("valueDomain: nothing to chart gives no domain", () => {
  assert.equal(valueDomain([null, null], "pct"), undefined);
  assert.equal(valueDomain([], "pts"), undefined);
});
