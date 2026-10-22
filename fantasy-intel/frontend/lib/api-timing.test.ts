import assert from "node:assert/strict";
import { test } from "node:test";
import { apiPathTemplate, backendTotalMs, recordTiming, timingLine, timingRate } from "./api-timing.ts";

const LG = "lg_" + "a".repeat(24);

test("apiPathTemplate drops league ids, numeric ids and the query string", () => {
  assert.equal(apiPathTemplate(`/v2/leagues/${LG}/api/overview?team_id=3`), "/v2/leagues/{league}/api/overview");
  assert.equal(apiPathTemplate(`/v2/leagues/${LG}/api/players/4262921/forward?source=ours`),
    "/v2/leagues/{league}/api/players/{id}/forward");
  assert.equal(apiPathTemplate("/api/managers/7"), "/api/managers/{id}");
  assert.equal(apiPathTemplate("/api/waivers?view=BEST&limit=3"), "/api/waivers");
});

test("timingRate is off unless explicitly set to 1, true or a fraction", () => {
  assert.equal(timingRate(undefined), 0);
  assert.equal(timingRate(""), 0);
  assert.equal(timingRate("0"), 0);
  assert.equal(timingRate("yes"), 0);
  assert.equal(timingRate("-1"), 0);
  assert.equal(timingRate("1"), 1);
  assert.equal(timingRate("TRUE"), 1);
  assert.equal(timingRate("0.05"), 0.05);
  assert.equal(timingRate("7"), 1);
});

test("backendTotalMs reads the app phase only", () => {
  assert.equal(backendTotalMs('auth;dur=3.1, files;dur=40.0;desc="cold", app;dur=123.4;desc="s"'), 123.4);
  assert.equal(backendTotalMs("auth;dur=3.1"), null);
  assert.equal(backendTotalMs(null), null);
});

test("a timing line names the template, status and durations, never the league", () => {
  const line = timingLine("GET", `/v2/leagues/${LG}/api/matchup?team_id=2`, 200, 87.6, "app;dur=41.2");
  assert.equal(line, "api GET /v2/leagues/{league}/api/matchup 200 88ms backend=41ms");
  assert.ok(!line.includes(LG));
});

test("recordTiming logs only when sampled", () => {
  const lines: string[] = [];
  recordTiming("GET", "/api/status", 200, 5, null, 0, () => 0, (l) => lines.push(l));
  recordTiming("GET", "/api/status", 200, 5, null, 0.1, () => 0.5, (l) => lines.push(l));
  recordTiming("GET", "/api/status", "offline", 5, null, 0.1, () => 0.05, (l) => lines.push(l));
  assert.deepEqual(lines, ["api GET /api/status offline 5ms"]);
});
