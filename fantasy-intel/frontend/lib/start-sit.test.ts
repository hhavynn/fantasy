import test from "node:test";
import assert from "node:assert/strict";
import {
  formatGain, actionabilityBadgeTone, lockStatusBadge, lineupTotal, startSitVerdict, emptyStartingSlots, actionabilityBadge, slotChange,
  NO_PROJECTIONS, noProjections, projectionsMeta,
} from "./start-sit-helpers.ts";

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

test("lineupTotal never presents a partial sum as a full total", () => {
  assert.deepEqual(lineupTotal(false, true, 10, 10), { value: null, partial: false });
  assert.deepEqual(lineupTotal(true, true, 114.36, 114.36), { value: 114.36, partial: false });
  assert.deepEqual(lineupTotal(true, false, null, 90.1), { value: 90.1, partial: true });
  assert.deepEqual(lineupTotal(true, true, null, 90.1), { value: 90.1, partial: true });
});

const base = { status: "optimal", startersCount: 9, projectedGain: 0, missingProjections: 0, scoringPeriod: 6, moves: 0, actionableMoves: 0, emptySlots: [] as string[] };

test("startSitVerdict opens with the decision", () => {
  assert.equal(startSitVerdict(base).tone, "positive");
  assert.match(startSitVerdict(base).headline, /optimal/);
  const swaps = startSitVerdict({ ...base, status: "upgrade_available", projectedGain: 3.91, moves: 1, actionableMoves: 1 });
  assert.equal(swaps.headline, "1 recommended move for +3.9 pts");
  assert.equal(swaps.detail, "All can be made now.");
  const locked = startSitVerdict({ ...base, status: "upgrade_available", projectedGain: 2, moves: 2, actionableMoves: 0 });
  assert.match(locked.detail, /None can be made yet/);
});

test("startSitVerdict makes no gain claim without a lineup or complete projections", () => {
  const preview = startSitVerdict({ ...base, status: "future_preview", startersCount: 0, projectedGain: null, scoringPeriod: 7 });
  assert.match(preview.headline, /Week 7 preview/);
  assert.equal(preview.tone, "info");
  const partial = startSitVerdict({ ...base, status: "incomplete_data", projectedGain: null, missingProjections: 2 });
  assert.equal(partial.tone, "caution");
  assert.doesNotMatch(partial.headline, /\+\d/);
  assert.equal(startSitVerdict({ ...base, status: "upgrade_available", startersCount: 0, projectedGain: null }).headline, "No submitted lineup for this week");
});

test("actionabilityBadge pairs a text label with a tone", () => {
  assert.deepEqual(actionabilityBadge("ACTIONABLE"), { label: "Actionable", tone: "good" });
  assert.deepEqual(actionabilityBadge("IR_BLOCKED"), { label: "IR blocked", tone: "warn" });
  assert.deepEqual(actionabilityBadge("LOCKED"), { label: "Locked", tone: "" });
});

test("slotChange separates real promotions from slot relabelling", () => {
  assert.equal(slotChange({ slot: "QB", playerId: 1, isCurrentlyStarting: true, currentSlot: "QB" }).kind, "same");
  assert.equal(slotChange({ slot: "WR", playerId: 1, isCurrentlyStarting: true, currentSlot: "FLEX" }).kind, "relabel");
  assert.equal(slotChange({ slot: "D/ST", playerId: 2, isCurrentlyStarting: false, currentSlot: "BE" }).text, "Promoted from bench");
  assert.equal(slotChange({ slot: "D/ST", playerId: null, isCurrentlyStarting: false, currentSlot: null }).kind, "empty");
});

test("an empty starting slot is flagged instead of called optimal", () => {
  assert.deepEqual(emptyStartingSlots([{ slot: "QB", player_id: 1 }, { slot: "D/ST", player_id: null }]), ["D/ST"]);
  assert.deepEqual(emptyStartingSlots([{ slot: "QB", player_id: 1 }]), []);
  const none = startSitVerdict({ ...base, emptySlots: ["D/ST"] });
  assert.equal(none.tone, "caution");
  assert.match(none.headline, /leaves D\/ST empty/);
  assert.match(none.detail, /No swap improves/);
  const swaps = startSitVerdict({ ...base, status: "upgrade_available", projectedGain: 3.9, moves: 1, actionableMoves: 1, emptySlots: ["D/ST"] });
  assert.equal(swaps.tone, "caution");
  assert.match(swaps.headline, /\+3\.9 pts/);
  assert.match(swaps.detail, /D\/ST stays empty/);
});

test("a week without any projection has no recommended total, never 0.0", () => {
  const row = (player_id: number | null, projected_points: number | null) => ({ player_id, projected_points });
  assert.equal(noProjections(NO_PROJECTIONS, [row(1, 12.5)]), true);                  // the backend says so
  assert.equal(noProjections("Ours", [row(null, null), row(2, null)]), true);         // nobody projected
  assert.equal(noProjections("ESPN", []), true);
  assert.equal(noProjections("ESPN", [row(null, null), row(2, 0)]), false);           // a projected 0 is a number
  assert.equal(projectionsMeta(NO_PROJECTIONS), "no projections");
  assert.equal(projectionsMeta("ESPN"), "ESPN projections");
});
