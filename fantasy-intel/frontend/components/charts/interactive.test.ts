import assert from "node:assert/strict";
import { test } from "node:test";
import { bubbleRadius, chartHeight, nearestPoint, tooltipPosition } from "./interactive.ts";

test("nearest point within reach, measured from the marker edge", () => {
  const pts = [{ px: 10, py: 10, r: 4 }, { px: 50, py: 50, r: 12 }];
  assert.equal(nearestPoint(pts, 12, 12, 10), 0);
  assert.equal(nearestPoint(pts, 30, 30, 10), null);     // 28.3 px from the centre minus r 12 = 16.3 > 10
  assert.equal(nearestPoint(pts, 30, 30, 18), 1);        // the big marker's edge is nearer than the small one's
  assert.equal(nearestPoint(pts, 200, 200, 18), null);
});

test("bubble radius is area-true and handles missing sizes", () => {
  assert.equal(bubbleRadius(null, 20), 4);
  assert.equal(bubbleRadius(20, 20), 14);
  assert.ok(Math.abs(bubbleRadius(5, 20) - (4 + 10 * 0.5)) < 1e-9);
  assert.equal(bubbleRadius(40, 20), 14);
});

test("tooltip flips side and stays inside the chart", () => {
  assert.deepEqual(tooltipPosition(20, 100, 200, 80, 400, 300), { left: 34, top: 60 });
  assert.deepEqual(tooltipPosition(390, 5, 200, 80, 400, 300), { left: 176, top: 4 });
  const narrow = tooltipPosition(150, 290, 232, 120, 300, 300);
  assert.ok(narrow.left >= 4 && narrow.left + 232 <= 300 && narrow.top + 120 <= 296);
});

test("charts get taller on phones", () => {
  assert.equal(chartHeight(1000), 520);
  assert.ok(chartHeight(343) >= 300);
});
