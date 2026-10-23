import assert from "node:assert/strict";
import test from "node:test";
import {
  currentAdvice, opponentText, planSummary, positionPageLink, streamParams, streamingPath, weekAction, weekAnchor,
  weekPoints, type StreamPositionPlan, type StreamStart, type StreamWeek,
} from "./kdst-streaming.ts";

const LG = "lg_abcdefghijklmnop";

const start = (over: Partial<StreamStart> = {}): StreamStart => ({
  espn_player_id: 9002, name: "Bravo K", pro_team: "BUF", injury_status: "ACTIVE", provider_status: "WAIVERS",
  points: 12, state: "plays", detail: null, opponent: "NYJ", home: true, kickoff: null, eligibility: "waiver_claim",
  conflicts: [], model: null, action: "add", ...over,
});

const week = (over: Partial<StreamWeek> = {}): StreamWeek => ({
  week: 9, horizon: 0, phase: "regular", outlook: false, schedule_loaded: true, candidates_with_game: 4,
  projected_candidates: 4, planned: true, reason: null, baseline: null,
  hold: { player: { espn_player_id: 502, name: "Zulu K", pro_team: "BUF", injury_status: "ACTIVE", provider_status: null }, points: 5, state: "plays", detail: null, opponent: "NE", home: false, kickoff: null },
  action: "add", start: start(), points: 12, alternatives: [], gap: null, reasons: [], ...over,
});

const plan = (over: Partial<StreamPositionPlan> = {}): StreamPositionPlan => ({
  position: "K", slots: 1, status: "planned", incumbents: [], weeks: [week()], planned_weeks: [9, 10, 11], capped_weeks: [],
  plan: { moves: 3, move_cost: 0, cost_total: 0, projected_points: 34, net_points: 34, hold_points: 10, gain_vs_hold: 24, hold_unknown_weeks: [] },
  no_plan_reason: null, this_week: { week: 9, best: start(), best_confirmed: null, hold_points: 5 }, pool: null, excluded: [], model: {},
  ...over,
});

test("links go back to the kicker and defense pages in the same league and week", () => {
  assert.equal(positionPageLink("K", 9, LG), `/l/${LG}/kickers?week=9`);
  assert.equal(positionPageLink("D/ST", 10, null), "/defenses?week=10");
  assert.equal(weekAnchor("D/ST", 9), "stream-dst-week-9");
  assert.equal(weekAnchor("K", 9), "stream-k-week-9");
});

test("a week the backend did not plan has no player and no number", () => {
  const capped = week({ planned: false, action: null, start: null, points: undefined, reason: "Only 1 free agent is projected for week 12 (at least 3 needed)." });
  assert.equal(weekAction(capped, "K"), "Not planned");
  assert.equal(weekPoints(capped), null);
  assert.equal(weekAction(week(), "K"), "Add Bravo K");
  assert.equal(weekAction(week({ action: "keep" }), "K"), "Keep Bravo K");
  assert.equal(weekAction(week({ action: "hold", start: null }), "K"), "Keep Zulu K");
  assert.equal(weekAction(week({ action: "hold", start: null, hold: { ...week().hold, player: null } }), "D/ST"), "No D/ST rostered");
});

test("current advice restates the plan and names a different best single-week streamer", () => {
  assert.equal(currentAdvice(plan()), "Week 9: Add Bravo K (12.0 projected).");
  const other = plan({ this_week: { week: 9, best: start({ espn_player_id: 1, name: "Other K", points: 13 }), best_confirmed: null, hold_points: 5 } });
  assert.equal(currentAdvice(other), "Week 9: Add Bravo K (12.0 projected). Best for this week alone: Other K (13.0).");
  const thin = plan({ status: "no_weeks", plan: null, weeks: [week({ planned: false, action: null, start: null, reason: "No free agent is projected for week 9 (at least 3 needed)." })] });
  assert.equal(currentAdvice(thin), "Week 9: no plan. No free agent is projected for week 9 (at least 3 needed).");
  assert.equal(currentAdvice(plan({ status: "not_started", no_plan_reason: "The league does not start a K." })), "The league does not start a K.");
});

test("plan summary uses the backend's numbers and keeps an unknown comparison unknown", () => {
  assert.equal(planSummary(plan()), "3 weeks · 3 add/drops · 34.0 projected · +24.0 vs keeping");
  const costly = plan({ plan: { moves: 1, move_cost: 5, cost_total: 5, projected_points: 25.5, net_points: 20.5, hold_points: null, gain_vs_hold: null, hold_unknown_weeks: [10] } });
  assert.equal(planSummary(costly), "3 weeks · 1 add/drop · 25.5 projected · 20.5 after costs · keeping cannot be compared");
  assert.equal(planSummary(plan({ plan: null, planned_weeks: [] })), "No week planned");
});

test("planner params are validated before they reach the backend", () => {
  assert.deepEqual(streamParams({ stream_weeks: "4", max_moves: "0", move_cost: "1.5" }), { weeks: 4, maxMoves: 0, moveCost: 1.5 });
  assert.deepEqual(streamParams({ stream_weeks: "9", max_moves: "x", move_cost: "-2" }), {});
  assert.deepEqual(streamParams({ max_moves: "", move_cost: "" }), {});
  assert.equal(streamingPath("3", { stream_weeks: "2", max_moves: "1" }), "/api/schedule-intelligence/kdst-streaming?team_id=3&weeks=2&max_moves=1");
  assert.equal(streamingPath(undefined, {}), "/api/schedule-intelligence/kdst-streaming");
});

test("opponent text", () => {
  assert.equal(opponentText({ opponent: "NYJ", home: true }), "vs NYJ");
  assert.equal(opponentText({ opponent: "NYJ", home: false }), "@ NYJ");
  assert.equal(opponentText({ opponent: null, home: null }), null);
});
