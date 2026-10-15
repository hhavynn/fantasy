// Run: npm test (node --test with type stripping).
import assert from "node:assert/strict";
import { test } from "node:test";
import { joinList, resultText, resultTone, signedText } from "./trade-format.ts";

test("result labels become sentences with the team names", () => {
  assert.equal(resultText("MUTUAL_PROJECTED_GAIN", "A", "B"), "Both lineups project to gain");
  assert.equal(resultText("BOTH_LOSE", "A", "B"), "Both lineups project to lose");
  assert.equal(resultText("NO_PROJECTED_CHANGE", "A", "B"), "Neither lineup changes");
  assert.equal(resultText("TEAM_A_GAIN_TEAM_B_LOSS", "Jonnie Blue", "Tortas"), "Jonnie Blue gains · Tortas loses");
  assert.equal(resultText("TEAM_A_UNCHANGED_TEAM_B_GAIN", "A", "B"), "A is unchanged · B gains");
  assert.equal(resultText("SOMETHING_NEW", "A", "B"), "SOMETHING_NEW");
});

test("result tone is good only for a mutual gain", () => {
  assert.equal(resultTone("MUTUAL_PROJECTED_GAIN"), "good");
  assert.equal(resultTone("BOTH_LOSE"), "bad");
  assert.equal(resultTone("INCOMPLETE_DATA"), "warn");
  assert.equal(resultTone("TEAM_A_GAIN_TEAM_B_LOSS"), "info");
});

test("signedText signs non-zero values and leaves measured zeros and missing values alone", () => {
  assert.equal(signedText(1.234), "+1.2");
  assert.equal(signedText(-1.25, 2), "−1.25");
  assert.equal(signedText(0), "0.0");
  assert.equal(signedText(-0.04), "0.0");
  assert.equal(signedText(null), "—");
  assert.equal(signedText(undefined), "—");
});

test("joinList reads naturally", () => {
  assert.equal(joinList([]), "");
  assert.equal(joinList(["a"]), "a");
  assert.equal(joinList(["a", "b"]), "a and b");
  assert.equal(joinList(["a", "b", "c"]), "a, b and c");
});
