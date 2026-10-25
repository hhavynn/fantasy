import test from "node:test";
import assert from "node:assert/strict";
import {
  carriedParams, moveText, optionLabel, readWhatIf, restrictionLabel, shownDelta, shownTotal, whatIfHref, withWhatIf,
  WHATIF_MAX_LEN, type WhatIfMove, type WhatIfOption, type WhatIfPlayer, type WhatIfSlot,
} from "./what-if.ts";

const player = (id: number, name: string, position: string, proj: number | null): WhatIfPlayer => ({
  player_id: id, name, position, pro_team: "NYG", projected_points: proj, projection_origin: null, lock_status: "UNLOCKED",
  kickoff_utc: null, opponent: "DAL", is_home: true, injury_status: null, nfl_ineligible: false, roster_spot: "bench",
  recommended_slot_id: null, scenario_slot_id: null, movable: true, restriction: null, restriction_reason: null,
});
const slot = (id: string, label: string): WhatIfSlot => ({
  slot_id: id, slot: id.split("_")[0], label, is_flex: id.startsWith("FLEX"), eligible_positions: ["RB", "WR", "TE"],
  locked: false, lock_reason: null, recommended_player_id: 7, scenario_player_id: 7, changed: false, options: [], blocked: [],
});
const players = new Map([[8, player(8, "Wally Bench", "WR", 10)], [7, player(7, "Walt Slot", "WR", 11)], [14, player(14, "Will Unknown", "WR", null)], [9, player(9, "Ted End", "TE", 8)]]);
const slots = new Map([["FLEX_0", slot("FLEX_0", "FLEX")], ["TE_0", slot("TE_0", "TE")]]);
const option = (o: Partial<WhatIfOption>): WhatIfOption => ({
  player_id: 8, from_slot_id: null, displaced_player_id: 7, displaced_to_slot_id: null, total: 104, known_total: 104,
  complete: true, delta: -1, known_delta: -1, whatif: "w7t1~FLEX_0.8", ...o,
});

test("readWhatIf forwards a bounded scenario and flags anything else", () => {
  assert.deepEqual(readWhatIf({}), { kind: "none" });
  assert.deepEqual(readWhatIf({ whatif: "" }), { kind: "none" });
  assert.deepEqual(readWhatIf({ whatif: "w7t1~FLEX_0.8,D/ST_0.-16002" }), { kind: "value", value: "w7t1~FLEX_0.8,D/ST_0.-16002" });
  assert.deepEqual(readWhatIf({ whatif: ["w7t1", "w8t1"] }), { kind: "value", value: "w7t1" });
  assert.deepEqual(readWhatIf({ whatif: "<script>" }), { kind: "invalid" });
  assert.deepEqual(readWhatIf({ whatif: "w7t1 ~FLEX_0.8" }), { kind: "invalid" });
  assert.deepEqual(readWhatIf({ whatif: "w".repeat(WHATIF_MAX_LEN + 1) }), { kind: "invalid" });
});

test("whatIfHref sets or removes the scenario and keeps the other parameters", () => {
  assert.equal(whatIfHref("/start-sit", { team: "3", week: "7", whatif: "w7t3" }, "w7t3~FLEX_0.8"), "/start-sit?team=3&week=7&whatif=w7t3%7EFLEX_0.8");
  assert.equal(whatIfHref("/start-sit", { team: "3", whatif: "w7t3~FLEX_0.8" }, null), "/start-sit?team=3");
  assert.equal(whatIfHref("/start-sit", {}, null), "/start-sit");
  assert.equal(whatIfHref("/start-sit", new URLSearchParams("week=8"), "w8t1~D/ST_0.-16002"), "/start-sit?week=8&whatif=w8t1%7ED%2FST_0.-16002");
  assert.deepEqual(carriedParams({ team: "3", whatif: "x", sort: ["a", "b"], none: undefined }), [["team", "3"], ["sort", "a"], ["sort", "b"]]);
});

test("withWhatIf adds the scenario to a backend path", () => {
  assert.equal(withWhatIf("/api/start-sit/what-if?week=7", null), "/api/start-sit/what-if?week=7");
  assert.equal(withWhatIf("/api/start-sit/what-if?", "w7t1"), "/api/start-sit/what-if?whatif=w7t1");
  assert.equal(withWhatIf("/api/start-sit/what-if?week=7&team_id=2", "w7t2~FLEX_0.8"), "/api/start-sit/what-if?week=7&team_id=2&whatif=w7t2%7EFLEX_0.8");
});

test("totals and deltas with a missing projection are known-only, never a zero", () => {
  const full = { total: 104, known_total: 104, complete: true, missing_projection_ids: [], starters: 9, starters_with_projection: 9 };
  assert.deepEqual(shownTotal(full), { value: 104, partial: false });
  assert.deepEqual(shownTotal({ ...full, total: null, known_total: 94, complete: false, missing_projection_ids: [14] }), { value: 94, partial: true });
  assert.deepEqual(shownDelta(-1, -1), { value: -1, partial: false });
  assert.deepEqual(shownDelta(null, -11), { value: -11, partial: true });
});

test("option labels say who, the projection, the change and any swap", () => {
  assert.equal(optionLabel(option({}), players, slots), "Wally Bench (WR, NYG) · 10.0 · −1.0 vs best");
  assert.equal(optionLabel(option({ player_id: 14, total: null, delta: null, known_delta: -11 }), players, slots),
    "Will Unknown (WR, NYG) · no projection · −11.0 known-only vs best");
  assert.equal(optionLabel(option({ player_id: 9, from_slot_id: "TE_0", delta: 0, known_delta: 0 }), players, slots),
    "Ted End (TE, NYG) · 8.0 · 0.0 vs best · swaps with TE");
  assert.equal(optionLabel(option({ delta: 2.26, known_delta: 2.26 }), players, slots), "Wally Bench (WR, NYG) · 10.0 · +2.3 vs best");
});

test("moves read as plain sentences", () => {
  const m: WhatIfMove = { index: 0, slot_id: "FLEX_0", player_id: 8, from_slot_id: null, displaced_player_id: 7,
    displaced_to_slot_id: null, removable: true, whatif_without: null, blocked_by: null };
  assert.equal(moveText(m, players, slots), "Wally Bench into FLEX; Walt Slot to the bench");
  assert.equal(moveText({ ...m, player_id: 9, from_slot_id: "TE_0", displaced_to_slot_id: "TE_0" }, players, slots), "Ted End into FLEX; Walt Slot to TE");
  assert.equal(moveText({ ...m, displaced_player_id: null }, players, slots), "Wally Bench into FLEX");
  assert.equal(restrictionLabel("LOCKED_PLAYER"), "Locked");
  assert.equal(restrictionLabel("RESERVED"), "Cannot start");
  assert.equal(restrictionLabel(null), null);
});
