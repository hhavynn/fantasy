import assert from "node:assert/strict";
import { test } from "node:test";
import { REFOCUS_TTL_MS, createRefocus } from "./tab-refocus.ts";

test("refocus: the tab that was activated takes focus once, when it arrives", () => {
  const r = createRefocus();
  r.arm("player-tabs", "?id=201&tab=usage", 1000);
  assert.equal(r.take("player-tabs", "?id=201&tab=usage", 1200), true);
  assert.equal(r.take("player-tabs", "?id=201&tab=usage", 1300), false);
});

test("refocus: nothing pending, nothing focused", () => {
  assert.equal(createRefocus().take("player-tabs", "?id=201", 0), false);
});

test("refocus: a failed navigation never steals focus on a later profile", () => {
  const r = createRefocus();
  r.arm("player-tabs", "?id=201&tab=usage", 1000);
  // The navigation errored (no tablist mounted); later another player's profile opens.
  assert.equal(r.take("player-tabs", "?id=305", 1500), false);
  // And the hand-off is used up, so even the original tab does not take focus afterwards.
  assert.equal(r.take("player-tabs", "?id=201&tab=usage", 1600), false);
});

test("refocus: an expired hand-off is dropped", () => {
  const r = createRefocus();
  r.arm("player-tabs", "?id=201&tab=usage", 1000);
  assert.equal(r.take("player-tabs", "?id=201&tab=usage", 1000 + REFOCUS_TTL_MS + 1), false);
});

test("refocus: another tablist leaves the hand-off pending", () => {
  const r = createRefocus();
  r.arm("player-tabs", "?id=201&tab=usage", 1000);
  assert.equal(r.take("other-tabs", "?id=201&tab=usage", 1100), false);
  assert.equal(r.take("player-tabs", "?id=201&tab=usage", 1200), true);
});
