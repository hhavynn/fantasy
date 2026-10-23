import assert from "node:assert/strict";
import test from "node:test";
import {
  callLine, costRangeText, costText, exclusionLines, feedbackPath, hitRateText, inputLines, parseFeedbackSource, pts,
  tallyText, utcText, type DfCall, type DfTally,
} from "./decision-feedback.ts";
import { parseRecapQuery, recapHref } from "./weekly-recap.ts";

const tally = (over: Partial<DfTally> = {}): DfTally => ({
  calls: 4, graded: 3, excluded: 1, correct: 1, missed: 2, push: 0, hit_rate: 0.333, close_calls_missed: 0,
  opportunity_cost_lower: 12, opportunity_cost_upper: null, ...over,
});

const input = { source: "ESPN", snapshot_id: "espn_projection_history:2026-09-10", captured_at: "2026-09-10T12:00:00+00:00",
  kickoff: "2026-09-13T17:00:00+00:00", model_version: null, scoring_version: "league", content_sha256: null, artifact_id: null,
  coverage_status: null };

test("feedbackPath never carries a team and keeps only week and source", () => {
  assert.equal(feedbackPath(null, null), "/api/weekly-recap/feedback");
  assert.equal(feedbackPath(3, null), "/api/weekly-recap/feedback?week=3");
  assert.equal(feedbackPath(3, "independent"), "/api/weekly-recap/feedback?week=3&source=independent");
  assert.ok(!feedbackPath(2, "espn").includes("team"));
});

test("source parsing accepts only the known sources and recap links keep it", () => {
  assert.equal(parseFeedbackSource({ source: "espn" }), "espn");
  assert.equal(parseFeedbackSource({ source: ["independent", "espn"] }), "independent");
  assert.equal(parseFeedbackSource({ source: "ours" }), null);
  assert.deepEqual(parseRecapQuery({ week: "2", source: "independent" }), { week: 2, players: "top", moves: "adds", source: "independent" });
  assert.deepEqual(parseRecapQuery({ source: "nope" }), { week: null, players: "top", moves: "adds" });
  assert.equal(recapHref({ week: 2, players: "top", moves: "adds", source: "espn" }, { moves: "trades" }), "/weekly-recap?week=2&moves=trades&source=espn");
});

test("cost bounds read as a range, a single value or a floor when the upper bound is unknown", () => {
  assert.equal(costText({ lower: 7, upper: 7, upper_unknown: 0 }), "7.00");
  assert.equal(costText({ lower: 5, upper: 9.5, upper_unknown: 0 }), "5.00 to 9.50");
  assert.equal(costText({ lower: 5, upper: null, upper_unknown: 1 }), "at least 5.00");
  assert.equal(costText(null), "—");
  assert.equal(costRangeText(12, null), "at least 12.00");
  assert.equal(costRangeText(null, null), "—");
  assert.equal(costRangeText(12, 17), "12.00 to 17.00");
});

test("tallies always carry their count; a rate never stands alone", () => {
  assert.equal(tallyText(tally()), "1 of 3 decided calls worked, 1 left ungraded");
  assert.equal(tallyText(tally({ correct: 1, missed: 0, push: 1, excluded: 0 })), "1 of 1 decided call worked, 1 push");
  assert.equal(tallyText(tally({ graded: 0, calls: 0 })), "No calls to grade");
  assert.equal(tallyText(tally({ graded: 0, calls: 2 })), "2 calls, none with recorded points for both players");
  assert.equal(hitRateText(tally()), "33% of 3");
  assert.equal(hitRateText(tally({ hit_rate: null })), "—");
});

test("inputs name the snapshot, its capture time and the kickoff in UTC", () => {
  assert.deepEqual(inputLines(input), ["ESPN snapshot espn_projection_history:2026-09-10",
    "captured 2026-09-10 12:00 UTC, kickoff 2026-09-13 17:00 UTC", "scoring league"]);
  const ours = inputLines({ ...input, source: "Ours", snapshot_id: "r1", model_version: "m", content_sha256: "c".repeat(64) });
  assert.ok(ours.includes("model m") && ours.includes(`content ${"c".repeat(16)}`));
  assert.equal(utcText(null), "unknown");
  assert.equal(utcText("not a date"), "unknown");
});

test("missing values stay a dash, exclusions read largest first", () => {
  assert.equal(pts(null), "—");
  assert.equal(pts(0), "0.00");
  assert.deepEqual(exclusionLines({ bye: 1, no_pregame_forecast: 3, reserve_slot: 0 }, { bye: "no game", no_pregame_forecast: "no forecast" }),
    ["3 players: no forecast", "1 player: no game"]);
  const side = { espn_player_id: 1, full_name: "Ann", position: "QB", pro_team: null, forecast: 20, actual: 18, input };
  const call: DfCall = { week: 2, slot: "QB", start: side, over: { ...side, espn_player_id: 2, full_name: "Bea" }, forecast_gap: 5,
    close_call: false, verdict: "missed", margin: -7, opportunity_cost: { lower: 7, upper: 7, upper_unknown: 0 }, excluded: null, alternatives: 1 };
  assert.equal(callLine(call), "Start Ann over Bea (QB)");
});
