import assert from "node:assert/strict";
import test from "node:test";
import { parseProjectionSource, showingOurs, withSource } from "./projection-source.ts";

test("parseProjectionSource defaults to ESPN for anything but 'ours'", () => {
  assert.equal(parseProjectionSource("ours"), "ours");
  assert.equal(parseProjectionSource("espn"), "espn");
  assert.equal(parseProjectionSource(undefined), "espn");
  assert.equal(parseProjectionSource("independent"), "espn");
});

test("withSource adds source=independent only for ours", () => {
  assert.equal(withSource("/api/matchup", "espn"), "/api/matchup");
  assert.equal(withSource("/api/matchup", "ours"), "/api/matchup?source=independent");
  assert.equal(withSource("/api/start-sit?week=6", "ours"), "/api/start-sit?week=6&source=independent");
  assert.equal(withSource("/api/start-sit?", "ours"), "/api/start-sit?source=independent");
});

test("showingOurs needs an available independent run", () => {
  const base = { reason: null, run: null, model_status: null, counts: null };
  assert.equal(showingOurs({ ...base, source: "independent", available: true }), true);
  assert.equal(showingOurs({ ...base, source: "independent", available: false }), false);
  assert.equal(showingOurs({ ...base, source: "espn", available: true }), false);
  assert.equal(showingOurs(undefined), false);
});
