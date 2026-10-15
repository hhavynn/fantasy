import test from "node:test";
import assert from "node:assert/strict";
import { formatGain, actionabilityBadgeTone, lockStatusBadge } from "./start-sit-helpers.ts";

test("formatGain formats positive, zero, and missing gains", () => {
  assert.equal(formatGain(4.2), "+4.2 pts");
  assert.equal(formatGain(0.0), "0.0 pts");
  assert.equal(formatGain(-1.5), "-1.5 pts");
  assert.equal(formatGain(null), "—");
  assert.equal(formatGain(undefined), "—");
});

test("actionabilityBadgeTone maps categories to design system tones", () => {
  assert.equal(actionabilityBadgeTone("ACTIONABLE"), "accent");
  assert.equal(actionabilityBadgeTone("LOCKED"), "mute");
  assert.equal(actionabilityBadgeTone("UNCERTAIN"), "warn");
  assert.equal(actionabilityBadgeTone("IR_BLOCKED"), "warn");
  assert.equal(actionabilityBadgeTone("NO_CHANGE"), "info");
});

test("lockStatusBadge formats final score, kickoff, bye, and unknown", () => {
  const finalBadge = lockStatusBadge("LOCKED", "2026-10-15T17:00:00Z", true, 28, 31);
  assert.equal(finalBadge.label, "Final (31-28)");
  assert.equal(finalBadge.tone, "mute");

  const lockedNoScore = lockStatusBadge("LOCKED", "2026-10-15T17:00:00Z", false);
  assert.equal(lockedNoScore.label, "Locked");
  assert.equal(lockedNoScore.tone, "mute");

  const byeBadge = lockStatusBadge("BYE", null);
  assert.equal(byeBadge.label, "NFL Bye");
  assert.equal(byeBadge.tone, "mute");

  const unknownBadge = lockStatusBadge("UNKNOWN", null);
  assert.equal(unknownBadge.label, "Kickoff Unknown");
  assert.equal(unknownBadge.tone, "warn");
});
