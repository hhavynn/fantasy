import assert from "node:assert/strict";
import test from "node:test";
import { EMPTY_RANGES, rangeFor, rangeLabel, rangeMap, rangeText, type RangesResponse } from "./projection-ranges.ts";

const resp = (over: Partial<RangesResponse> = {}): RangesResponse => ({
  schema_version: "projection-ranges/v1", available: true, reason: null, season: 2026, week: 7, coverage: 0.8,
  definition: "floor = 10th percentile", positions: { QB: { served: false, reason: "held" }, WR: { served: true, reason: null } },
  run: { run_id: "r", model_version: "ifp-2.1.0", forecast_horizon: 0 },
  ranges: {
    "11": { position: "WR", projection: 14.96, floor: 4.36, median: 13.5, ceiling: 27.95 },
    "22": { position: "RB", projection: 8.0, floor: 0, median: 6.9, ceiling: 17.04 },
  },
  ...over,
});

test("rangeMap keys by numeric player id and is empty when nothing is served", () => {
  const m = rangeMap(resp());
  assert.equal(m.size, 2);
  assert.equal(m.get(11)?.ceiling, 27.95);
  assert.equal(rangeMap(resp({ available: false })), EMPTY_RANGES);
  assert.equal(rangeMap(null), EMPTY_RANGES);
});

test("rangeFor shows a range only next to our own number", () => {
  const m = rangeMap(resp());
  assert.equal(rangeFor(m, 11)?.floor, 4.36);
  assert.equal(rangeFor(m, 11, "ours")?.floor, 4.36);
  assert.equal(rangeFor(m, 11, "espn"), null);        // ESPN's number stands in: no range around it
  assert.equal(rangeFor(m, 11, "ours_zero"), null);   // bye or unavailable
  assert.equal(rangeFor(m, 99), null);
  assert.equal(rangeFor(m, null), null);
});

test("range text is compact with one decimal and an en dash; the label names floor and ceiling", () => {
  assert.equal(rangeText({ floor: 4.36, ceiling: 27.95 }), "4.4–28.0");
  assert.equal(rangeText({ floor: 0, ceiling: 17.04 }), "0.0–17.0");
  assert.equal(rangeLabel({ floor: 4.36, ceiling: 27.95 }), "Floor 4.4, ceiling 28.0");
});
