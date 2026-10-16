import assert from "node:assert/strict";
import { test } from "node:test";
import { placeLabels, type PlacedLabel } from "./labels.ts";

const W = 300;
const H = 200;
const CHAR = 6;
const boxOf = (l: PlacedLabel) => {
  const w = l.text.length * CHAR;
  const x0 = l.anchor === "start" ? l.x : l.anchor === "end" ? l.x - w : l.x - w / 2;
  return { x0, x1: x0 + w, y0: l.y - 9, y1: l.y + 2 };
};
const overlap = (a: ReturnType<typeof boxOf>, b: ReturnType<typeof boxOf>) =>
  a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;

test("a lone label sits to the right of its point", () => {
  const [l] = placeLabels([{ id: 1, text: "Alpha", px: 100, py: 100 }], { width: W, height: H }, { charWidth: CHAR });
  assert.equal(l.anchor, "start");
  assert.equal(l.x, 109);
});

test("priority labels are placed first and centred above the point", () => {
  const out = placeLabels(
    [{ id: 1, text: "Other", px: 100, py: 100 }, { id: 2, text: "Mine", px: 100, py: 100, priority: true }],
    { width: W, height: H }, { charWidth: CHAR },
  );
  assert.equal(out[0].id, 2);
  assert.equal(out[0].anchor, "middle");
});

test("placed labels never overlap each other and stay inside the bounds", () => {
  const labels = Array.from({ length: 14 }, (_, i) => ({
    id: i, text: `Team ${i}`, px: 120 + (i % 4) * 9, py: 80 + Math.floor(i / 4) * 8, priority: i === 0,
  }));
  const out = placeLabels(labels, { width: W, height: H }, { charWidth: CHAR });
  const boxes = out.map(boxOf);
  for (let i = 0; i < boxes.length; i++) {
    assert.ok(boxes[i].x0 >= 0 && boxes[i].x1 <= W && boxes[i].y0 >= 0 && boxes[i].y1 <= H, `label ${out[i].id} inside bounds`);
    for (let j = i + 1; j < boxes.length; j++) assert.ok(!overlap(boxes[i], boxes[j]), `labels ${out[i].id} and ${out[j].id} overlap`);
  }
  assert.ok(out.length >= 4, "a dense cluster still gets several labels");
});

test("non-priority labels that cannot fit are dropped by default, priority never", () => {
  const labels = [
    { id: "p", text: "Priority", px: 150, py: 100, priority: true },
    ...Array.from({ length: 9 }, (_, i) => ({ id: i, text: "Crowded", px: 150, py: 100 })),
  ];
  const out = placeLabels(labels, { width: W, height: H }, { charWidth: CHAR });
  assert.ok(out.some((l) => l.id === "p"));
  assert.ok(out.length < labels.length, "some of the nine crowded labels are dropped");
  const boxes = out.map(boxOf);
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) assert.ok(!overlap(boxes[i], boxes[j]));
});

test("dropOnCollision false keeps every label", () => {
  const labels = Array.from({ length: 6 }, (_, i) => ({ id: i, text: "Same spot", px: 150, py: 100 }));
  assert.equal(placeLabels(labels, { width: W, height: H }, { charWidth: CHAR, dropOnCollision: false }).length, 6);
});

test("labels avoid other points' markers", () => {
  const out = placeLabels(
    [{ id: 1, text: "A", px: 100, py: 100 }, { id: 2, text: "B", px: 109, py: 99 }],
    { width: W, height: H }, { charWidth: CHAR, markerRadius: 5 },
  );
  const a = out.find((l) => l.id === 1)!;
  // The default right-hand slot would sit on B's marker, so A moves elsewhere.
  assert.ok(!(a.anchor === "start" && a.x === 109 && a.y === 104));
});

test("an edge label flips left with its full text, fully inside the bounds", () => {
  const [l] = placeLabels([{ id: 1, text: "Carted Off Field", px: 280, py: 100 }], { width: W, height: H }, { charWidth: CHAR });
  assert.equal(l.anchor, "end");
  assert.equal(l.text, "Carted Off Field");
  assert.ok(!l.truncated);
  const b = boxOf(l);
  assert.ok(b.x0 >= 0 && b.x1 <= W);
});

test("a label that fits on neither side is ellipsized, never clipped", () => {
  const [l] = placeLabels([{ id: 1, text: "A very long team name indeed", px: 140, py: 100 }], { width: 160, height: H }, { charWidth: CHAR });
  assert.ok(l.truncated);
  assert.ok(l.text.endsWith("…"));
  const b = boxOf(l);
  assert.ok(b.x0 >= 0 && b.x1 <= 160, "inside the bounds");
});

test("unlabeled points passed as obstacles are avoided", () => {
  const out = placeLabels([{ id: 1, text: "A", px: 100, py: 100 }], { width: W, height: H }, { charWidth: CHAR, markerRadius: 5, obstacles: [{ id: 2, px: 109, py: 99 }] });
  assert.ok(!(out[0].anchor === "start" && out[0].x === 109 && out[0].y === 104));
});

test("a priority label that cannot fit anywhere is clamped inside the bounds", () => {
  const [l] = placeLabels([{ id: 1, text: "Extremely long priority label", px: 40, py: 100, priority: true }], { width: 90, height: H }, { charWidth: CHAR });
  assert.ok(l.truncated);
  const b = boxOf(l);
  assert.ok(b.x0 >= 0 && b.x1 <= 90);
});

test("a label near the right edge flips to the left side", () => {
  const [l] = placeLabels([{ id: 1, text: "EdgeCase", px: 290, py: 100 }], { width: W, height: H }, { charWidth: CHAR });
  assert.equal(l.anchor, "end");
});
