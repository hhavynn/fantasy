import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { readStashHorizon, stashCoverage, stashGain, stashWindowText, type StashCounts } from "./stash-horizon.ts";

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

// The backend's own GET /api/my-team/stash?team_id=2 response for the synthetic e2e league, regenerated and compared by
// backend/tests/test_golden_legacy.py: the panel's wording is checked against what the endpoint really serves.
const golden = JSON.parse(readFileSync(new URL("../../backend/tests/golden/e2e/api_my_team_stash__team.json", import.meta.url), "utf8")
  .replace(/^\/\/.*\n/, "")).body as { horizon: number; weeks: number[]; later_weeks: number[]; counts: StashCounts };

test("every eligible free agent in the served plan is accounted for, not only the evaluated ones", () => {
  const c = golden.counts;
  const evaluated = c.evaluated ?? 0;
  const accounted = evaluated + (c.excluded_unprojected ?? 0) + (c.excluded_position_limit ?? 0)
    + (c.no_possible_gain ?? 0) + (c.not_evaluated_cap ?? 0);
  assert.equal(accounted, c.considered, "the backend's split of `considered` is exhaustive");
  const text = stashCoverage(c)!;
  assert.match(text, new RegExp(`^Compared ${c.considered} eligible free agents: `));
  assert.match(text, new RegExp(`${evaluated} evaluated exactly`));
  assert.match(text, new RegExp(`${c.no_possible_gain} with no possible gain over the window`));
});

test("missing projections, position limits, the evaluation cap and unknown values are each disclosed", () => {
  const text = stashCoverage({ considered: 12, evaluated: 4, unknown: 1, excluded_unprojected: 3, excluded_position_limit: 2,
    no_possible_gain: 2, not_evaluated_cap: 1 })!;
  assert.match(text, /4 evaluated exactly \(1 with an unknown value: a projection is missing\)/);
  assert.match(text, /3 excluded for a missing projection \(not counted as zero\)/);
  assert.match(text, /2 excluded by a roster position limit/);
  assert.match(text, /2 with no possible gain over the window/);
  assert.match(text, /1 beyond the per-position evaluation cap/);
  assert.equal(stashCoverage({}), null, "no later weeks: nothing was compared");
  assert.equal(stashCoverage({ considered: 0 }), "Compared 0 eligible free agents");
});

test("the window names the weeks the backend evaluated, including fewer than asked near the season's end", () => {
  assert.equal(stashWindowText(golden), "4 later weeks (weeks 4–7), plus the current week");
  assert.equal(stashWindowText({ horizon: 6, weeks: [15, 16, 17], later_weeks: [16, 17] }),
    "2 later weeks (weeks 16–17; only 2 of the 6 asked for remain), plus the current week");
  assert.equal(stashWindowText({ horizon: 2, weeks: [16, 17], later_weeks: [17] }),
    "1 later week (week 17; only 1 of the 2 asked for remain), plus the current week");
  assert.equal(stashWindowText({ horizon: 4, weeks: [17] }), "no later week remains");
  assert.equal(stashWindowText({ horizon: 4 }), "4 later weeks asked for, plus the current week");
});
