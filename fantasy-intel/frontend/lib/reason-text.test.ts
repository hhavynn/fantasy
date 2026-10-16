import assert from "node:assert/strict";
import { test } from "node:test";
import { metricLabel, tidyReason, weekRanges } from "./reason-text.ts";

test("metricLabel names the usage metrics and falls back to a spaced, capitalised key", () => {
  assert.equal(metricLabel("offense_pct"), "Snap share");
  assert.equal(metricLabel("xfp"), "xFP");
  assert.equal(metricLabel("red_zone_looks"), "Red zone looks");
});

test("weekRanges collapses consecutive weeks and keeps gaps", () => {
  assert.equal(weekRanges([1, 2, 3]), "1–3");
  assert.equal(weekRanges([4, 5]), "4–5");
  assert.equal(weekRanges([1, 2, 3, 5]), "1–3, 5");
  assert.equal(weekRanges([7]), "7");
  assert.equal(weekRanges([5, 3, 4]), "3–5");
});

test("tidyReason rewrites identifiers, week arrays and arrows without touching the numbers", () => {
  assert.equal(
    tidyReason("offense_pct down: 94% -> 55% (weeks [1, 2, 3] vs [4, 5])"),
    "Snap share down: 94% → 55% (weeks 1–3 vs 4–5)",
  );
  assert.equal(tidyReason("xfp down: 15.0 -> 12.8 (weeks [1, 2, 3] vs [4, 5])"), "xFP down: 15.0 → 12.8 (weeks 1–3 vs 4–5)");
});

test("tidyReason leaves ordinary sentences alone", () => {
  const s = "PPR 16.0/g vs expected 14.1/g (+1.9).";
  assert.equal(tidyReason(s), s);
  const t = "Usage has dipped from its earlier level but the recent role is still high-volume: monitor for a sustained decline before selling.";
  assert.equal(tidyReason(t), t);
});
