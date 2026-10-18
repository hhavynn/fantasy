import assert from "node:assert/strict";
import { test } from "node:test";
import { defaultFormat, phoneFont, seriesColor, signedFormat, thinStep, truncate } from "./shared.ts";

test("phoneFont grows with the chart's natural width but never below the base size", () => {
  assert.equal(phoneFont(480), 16);
  assert.ok(phoneFont(900) > phoneFont(480));
  assert.equal(phoneFont(100), 11);
});

test("seriesColor uses the explicit colour, else cycles the six chart tokens", () => {
  assert.equal(seriesColor(0), "var(--chart-1)");
  assert.equal(seriesColor(5), "var(--chart-6)");
  assert.equal(seriesColor(6), "var(--chart-1)");
  assert.equal(seriesColor(2, "#fff"), "#fff");
});

test("defaultFormat trims noise and uses a real minus; signedFormat adds plus", () => {
  assert.equal(defaultFormat(12.3456), "12.35");
  assert.equal(defaultFormat(12), "12");
  assert.equal(defaultFormat(1234.5), "1,234.5");
  assert.equal(defaultFormat(-3), "−3");
  assert.equal(signedFormat(3.2), "+3.2");
  assert.equal(signedFormat(-1), "−1");
  assert.equal(signedFormat(0), "0");
});

test("thinStep keeps labels that fit and thins those that do not", () => {
  assert.equal(thinStep(10, 400, 30), 1);
  assert.equal(thinStep(17, 400, 40), 2);
  assert.equal(thinStep(40, 400, 40), 4);
  assert.equal(thinStep(1, 400, 40), 1);
});

test("truncate adds an ellipsis only when needed", () => {
  assert.equal(truncate("Short", 10), "Short");
  assert.equal(truncate("A very long team name", 10), "A very lo…");
  assert.equal(truncate("anything", 1), "");
});
