import test from "node:test";
import assert from "node:assert/strict";
import {
  addDropHref, carriedParams, headline, optionLabel, readAddDrop, shownChange, windowLabel, withAddDrop,
  type AddDropResponse,
} from "./add-drop.ts";
import { TEAM_SPECIFIC_PARAMS } from "./team-perspective.ts";

test("readAddDrop accepts player ids only, either side optional, never the same player twice", () => {
  assert.deepEqual(readAddDrop({}), { kind: "none" });
  assert.deepEqual(readAddDrop({ add: "", drop: "" }), { kind: "none" });
  assert.deepEqual(readAddDrop({ add: "209", drop: "-16002" }), { kind: "pair", add: 209, drop: -16002 });
  assert.deepEqual(readAddDrop({ add: "209" }), { kind: "pair", add: 209, drop: null });
  assert.deepEqual(readAddDrop({ drop: ["206", "207"] }), { kind: "pair", add: null, drop: 206 });
  assert.deepEqual(readAddDrop({ add: "209", drop: "209" }), { kind: "invalid" });
  for (const bad of ["<script>", "1e5", "12.5", "1".repeat(13), "--1", "209 "]) {
    if (bad.trim() === "209") continue;
    assert.deepEqual(readAddDrop({ add: bad }), { kind: "invalid" }, bad);
  }
  assert.deepEqual(readAddDrop({ add: "209", drop: "x" }), { kind: "invalid" });
});

test("addDropHref keeps every other parameter, replaces the scenario and lands on the panel", () => {
  assert.equal(addDropHref("/waivers", { team: "3", view: "ALL", add: "1" }, { add: 209, drop: 206 }),
    "/waivers?team=3&view=ALL&add=209&drop=206#add-drop");
  assert.equal(addDropHref("", { team: "3", add: "209", drop: "206" }, { add: null, drop: null }), "?team=3#add-drop");
  assert.equal(addDropHref("", { add: "209" }, { add: null, drop: null }), "?#add-drop");
  assert.equal(addDropHref("/waivers", new URLSearchParams("pos=RB"), { add: null, drop: -16002 }, ""),
    "/waivers?pos=RB&drop=-16002");
});

test("withAddDrop sends only a validated pair", () => {
  assert.equal(withAddDrop("/api/waivers/add-drop", { kind: "none" }), "/api/waivers/add-drop");
  assert.equal(withAddDrop("/api/waivers/add-drop", { kind: "invalid" }), "/api/waivers/add-drop");
  assert.equal(withAddDrop("/api/waivers/add-drop?team_id=2", { kind: "pair", add: 209, drop: null }),
    "/api/waivers/add-drop?team_id=2&add=209");
});

test("carriedParams keeps the page's filters but not the scenario or the page offset", () => {
  assert.deepEqual(carriedParams({ team: "3", add: "1", drop: "2", offset: "25", pos: ["RB", "WR"], none: undefined }),
    [["team", "3"], ["pos", "RB"], ["pos", "WR"]]);
});

test("changing team drops the scenario", () => {
  assert.ok(TEAM_SPECIFIC_PARAMS.includes("add") && TEAM_SPECIFIC_PARAMS.includes("drop"));
});

test("a missing projection is never a zero: the change is known-only or missing", () => {
  assert.deepEqual(shownChange(3, 3), { value: 3, partial: false });
  assert.deepEqual(shownChange(null, 2.5), { value: 2.5, partial: true });
  assert.deepEqual(shownChange(null, null), { value: null, partial: false });
  assert.deepEqual(shownChange(0, 0), { value: 0, partial: false });
});

test("labels", () => {
  assert.equal(windowLabel([3, 4, 5]), "Weeks 3–5");
  assert.equal(windowLabel([17]), "Week 17");
  assert.equal(windowLabel([]), null);
  assert.equal(optionLabel({ espn_player_id: 309, name: "Waiver WR", position: "WR", pro_team: "BUF", injury_status: null,
    known_projected_points: 105, projected_weeks: 15, availability_status: "WAIVERS" }), "Waiver WR (WR, BUF) · 105.0 proj · waivers");
  assert.equal(optionLabel({ espn_player_id: 207, name: null, position: "RB", pro_team: null, injury_status: "OUT",
    known_projected_points: null, projected_weeks: 0, on_ir: true }), "Player 207 (RB, FA) · no projection · IR");
});

const response = (o: Partial<AddDropResponse>): AddDropResponse => ({
  contract: "waiver-add-drop/v1", status: "evaluated", team: { id: 2, name: "Team 2" }, request: { add: 209, drop: 206 },
  evaluation: { start_week: 3, final_week: 17, last_regular_season_week: 14, weeks: [3, 4], earliest_actionable_week: 3,
    uncertain: false, warning: null, start_week_reason: {}, evaluated_at: null },
  projection_source: "ESPN", problems: [], notes: [],
  players: {
    add: { espn_player_id: 209, name: "Waiver RB", position: "RB", pro_team: "KC", roster_status: "free_agent" },
    drop: { espn_player_id: 206, name: "Juliet RB", position: "RB", pro_team: "NYJ", roster_status: "own_roster" },
  },
  options: { add: [], add_total: 0, add_limit: 60, drop: [] },
  roster: { active_capacity: 12, position_limits: {}, active_before: 11, active_after: 11, open_spots_before: 1,
    open_spots_after: 1, legal: true, drop_required: false, ir_slot_blockers: [], suggested_drop: null },
  depth: [],
  impact: { first_week: null, weeks: [], rest_of_season: { ros_delta: 45, known_ros_delta: 45, regular_season_delta: 36,
    playoff_delta: 9, average_weekly_delta: 3, weeks_improved: 15, weeks_worsened: 0, weeks_unchanged: 0,
    weeks_incomplete: [], projection_complete: true } },
  acquisition: null, assumptions: [], limitations: [], ...o,
});

test("headline states the move and the direction, marking known-only totals", () => {
  assert.equal(headline(response({})), "Add Waiver RB, drop Juliet RB: a projected lineup gain");
  const partial = response({});
  partial.impact!.rest_of_season = { ...partial.impact!.rest_of_season, ros_delta: null, known_ros_delta: -4 };
  assert.equal(headline(partial), "Add Waiver RB, drop Juliet RB: a projected lineup loss (known projections only)");
  assert.equal(headline(response({ status: "blocked", players: { add: null, drop: { espn_player_id: 1, name: "X", position: "RB",
    pro_team: null, roster_status: "other_roster" } } })), "Drop X: not possible as asked");
  assert.equal(headline(response({ status: "options" })), "Choose a player to add, one to drop, or both");
});
