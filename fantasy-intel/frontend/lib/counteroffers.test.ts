import assert from "node:assert/strict";
import { test } from "node:test";
import {
  counterofferAnalyzerHref, counterofferHref, coverageText, editText, parseLocks, statusTitle, toggleLockHref,
  type Counteroffer, type CounterofferSearch,
} from "./counteroffers.ts";

const SEL = { ta: 1, tb: 2, a: [105], b: [204], sw: null };
const R = { names: { "105": { name: "Ray Back", position: "RB", pro_team: "KC" },
  "102": { name: "Lead Back", position: "RB", pro_team: "KC" },
  "208": { name: "Tight End", position: "TE", pro_team: "BUF" } }, team_names: { "1": "Alpha", "2": null } };

test("locks keep only offer players, once, in order", () => {
  assert.deepEqual(parseLocks([204, 999, 105, 204], [105, 204]), [105, 204]);
  assert.deepEqual(parseLocks([], [105]), []);
  assert.equal(parseLocks(Array.from({ length: 30 }, (_, i) => i), Array.from({ length: 30 }, (_, i) => i)).length, 20);
});

test("the panel URL carries the selection, the open flag and the locks; toggling flips one lock", () => {
  assert.equal(counterofferHref(SEL, [204, 105]), "/trades?ta=1&tb=2&a=105&b=204&co=1&lock=105&lock=204");
  assert.equal(toggleLockHref(SEL, [105], 105), "/trades?ta=1&tb=2&a=105&b=204&co=1");
  assert.equal(toggleLockHref(SEL, [105], 204), "/trades?ta=1&tb=2&a=105&b=204&co=1&lock=105&lock=204");
  const c = { analyzer_link: { ta: 1, tb: 2, a: [102], b: [204, 208] } } as Counteroffer;
  assert.equal(counterofferAnalyzerHref(c), "/trades?ta=1&tb=2&a=102&b=204&b=208");
});

test("edits read as plain changes to the offer, with fallbacks for unknown names", () => {
  assert.equal(editText(R, { kind: "swap", team_id: 1, out: 105, in: 102 }), "Alpha sends Lead Back instead of Ray Back");
  assert.equal(editText(R, { kind: "add", team_id: 2, out: null, in: 208 }), "Team 2 also sends Tight End");
  assert.equal(editText(R, { kind: "remove", team_id: 1, out: 777, in: null }), "Alpha keeps Player 777");
});

test("titles and coverage say what happened without hedges", () => {
  assert.equal(statusTitle("found", 1), "1 counteroffer helps the side this offer treats worse");
  assert.equal(statusTitle("found", 3), "3 counteroffers help the side this offer treats worse");
  assert.match(statusTitle("original_unknown", 0), /unknown/);
  const s: CounterofferSearch = { packages_evaluated: 28, packages_not_evaluated: 0, termination: "all_candidates_evaluated",
    exhaustive: true, work_spent: 90, work_budget: 15000, stop_reason: null, scope: "single edit of the offer" };
  assert.equal(coverageText(s), "28 packages evaluated exactly: every single edit of the offer within the limits.");
  assert.equal(coverageText({ ...s, packages_evaluated: 1, packages_not_evaluated: 4, exhaustive: false, termination: "work_budget",
    stop_reason: "work budget of 60 lineup totals reached" }),
  "1 package evaluated exactly; 4 more not evaluated (work budget of 60 lineup totals reached).");
  assert.equal(coverageText({ ...s, exhaustive: false, scope: "single edit of the offer, plus one more addition to the best few" }),
    "28 packages evaluated exactly: single edit of the offer, plus one more addition to the best few.");
  assert.equal(statusTitle("none_known", 0), "No counteroffer with a known impact");
});
