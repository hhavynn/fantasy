import assert from "node:assert/strict";
import { test } from "node:test";
import { readStashHorizon, stashGain } from "./stash-horizon.ts";

test("stash horizon only allows the backend's 2, 4 and 6 weeks", () => {
  assert.equal(readStashHorizon("2"), 2);
  assert.equal(readStashHorizon("4"), 4);
  assert.equal(readStashHorizon("6"), 6);
  assert.equal(readStashHorizon(["6", "2"]), 6);
  for (const bad of [undefined, "", "9", "02", "2.0", "NaN", "0", "-1"]) {
    assert.equal(readStashHorizon(bad), 4);
  }
});

test("stash gain preserves measured zero and never invents a value when unknown", () => {
  assert.equal(stashGain(2.37), "+2.4");
  assert.equal(stashGain(-0.75), "-0.8");
  assert.equal(stashGain(0), "0.0");
  assert.equal(stashGain(null), "—");
  assert.equal(stashGain(undefined), "—");
  assert.equal(stashGain(Number.NaN), "—");
});
