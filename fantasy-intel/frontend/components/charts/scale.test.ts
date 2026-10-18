import assert from "node:assert/strict";
import { test } from "node:test";
import { extent, linearScale, niceStep, niceTicks, ticksWithin } from "./scale.ts";

test("linearScale maps domain ends to range ends and interpolates", () => {
  const s = linearScale([0, 10], [100, 200]);
  assert.equal(s(0), 100);
  assert.equal(s(10), 200);
  assert.equal(s(2.5), 125);
});

test("linearScale supports inverted ranges (SVG y axes)", () => {
  const s = linearScale([0, 100], [300, 0]);
  assert.equal(s(0), 300);
  assert.equal(s(100), 0);
  assert.equal(s(50), 150);
});

test("linearScale centres a degenerate domain", () => {
  assert.equal(linearScale([5, 5], [0, 100])(5), 50);
});

test("extent ignores null, undefined and non-finite values", () => {
  assert.deepEqual(extent([3, null, -2, undefined, NaN, 10, Infinity]), [-2, 10]);
  assert.equal(extent([null, undefined]), null);
  assert.equal(extent([]), null);
});

test("niceStep picks 1/2/5 × 10^k", () => {
  assert.equal(niceStep(100, 5), 20);
  assert.equal(niceStep(97, 4), 20);
  assert.equal(niceStep(1, 4), 0.2);
  assert.equal(niceStep(7, 4), 2);
  assert.equal(niceStep(430, 4), 100);
});

test("niceTicks covers the data with round numbers", () => {
  const t = niceTicks(3.2, 97, 5);
  assert.deepEqual(t.ticks, [0, 20, 40, 60, 80, 100]);
  assert.equal(t.min, 0);
  assert.equal(t.max, 100);
  assert.equal(t.step, 20);
});

test("niceTicks handles negatives and straddles zero", () => {
  const t = niceTicks(-12, 31, 4);
  assert.ok(t.min <= -12 && t.max >= 31);
  assert.ok(t.ticks.includes(0));
  assert.deepEqual(t.ticks, [-20, -10, 0, 10, 20, 30, 40]);
});

test("niceTicks has no floating-point noise for fractional steps", () => {
  assert.deepEqual(niceTicks(0, 1, 5).ticks, [0, 0.2, 0.4, 0.6, 0.8, 1]);
  assert.deepEqual(niceTicks(0.1, 0.35, 5).ticks, [0.1, 0.15, 0.2, 0.25, 0.3, 0.35]);
  assert.deepEqual(niceTicks(0, 0.3, 3).ticks, [0, 0.1, 0.2, 0.3]);
});

test("niceTicks widens a degenerate range instead of collapsing", () => {
  const t = niceTicks(42, 42, 4);
  assert.ok(t.min < 42 && t.max > 42);
  assert.ok(t.ticks.length >= 3);
  const z = niceTicks(0, 0, 4);
  assert.ok(z.min < 0 && z.max > 0);
});

test("niceTicks swaps reversed input and survives non-finite input", () => {
  assert.deepEqual(niceTicks(100, 0, 5).ticks, niceTicks(0, 100, 5).ticks);
  assert.deepEqual(niceTicks(NaN, 5).ticks, [0, 1]);
});

test("niceTicks ticks are strictly increasing and evenly spaced", () => {
  for (const [lo, hi] of [[0, 3], [-5, 5], [0.002, 0.009], [1200, 98000], [-0.3, 0.9]] as const) {
    const { ticks, step } = niceTicks(lo, hi, 5);
    for (let i = 1; i < ticks.length; i++) {
      assert.ok(ticks[i] > ticks[i - 1]);
      assert.ok(Math.abs(ticks[i] - ticks[i - 1] - step) < step * 1e-6);
    }
    assert.ok(ticks[0] <= lo && ticks[ticks.length - 1] >= hi);
  }
});

test("ticksWithin stays inside a pinned domain", () => {
  assert.deepEqual(ticksWithin(0, 30, 3), [0, 10, 20, 30]);
  assert.deepEqual(ticksWithin(3, 47, 4), [10, 20, 30, 40]);
  assert.deepEqual(ticksWithin(5, 5), [5]);
});
