import test from "node:test";
import assert from "node:assert/strict";
import {
  activeRosterText, capacityFacts, dropCostText, legalityLabel, noDropText, partialSlotsNote, slotList, unsupportedSlotsTitle,
  type RosterLegality,
} from "./lineup-coverage.ts";
import { headline, type AddDropResponse } from "./add-drop.ts";

test("a total that leaves out unsupported slots is labelled partial and names them", () => {
  assert.equal(partialSlotsNote(["LB"]), "*Partial: leaves out LB (no engine fills this slot)");
  assert.equal(partialSlotsNote(["DL", "LB"]), "*Partial: leaves out DL, LB (no engine fills these slots)");
  assert.equal(partialSlotsNote([]), null);
  assert.equal(partialSlotsNote(undefined), null);
  assert.equal(slotList(["P", "HC"]), "P, HC");
  assert.equal(unsupportedSlotsTitle(["P", "HC"]), "P, HC: slots no engine fills");
  assert.equal(unsupportedSlotsTitle(["LB"]), "LB: a slot no engine fills");
});

const legality: RosterLegality = {
  status: "unverified", code: "ROSTER_UNVERIFIED", unsupported_slots: ["DL", "LB", "DP"], players_missing: 3,
  reason: "3 rostered players are not in the synced player data. Roster legality is unverified.",
};

test("an unverified roster is never shown as legal", () => {
  assert.deepEqual(legalityLabel(legality), { label: "Unverified", reason: legality.reason });
  assert.equal(legalityLabel(undefined), null);
});

test("the add/drop headline says when roster legality is unverified", () => {
  const base = {
    status: "evaluated", players: { add: { name: "FX WR 1" }, drop: null },
    impact: { rest_of_season: { ros_delta: 4.5, known_ros_delta: 4.5 } },
    roster: { legality },
  } as unknown as AddDropResponse;
  assert.equal(headline(base), "Add FX WR 1: a projected lineup gain; roster legality unverified");
  const unknown = { ...base, impact: { rest_of_season: { ros_delta: null, known_ros_delta: null } } } as unknown as AddDropResponse;
  assert.equal(headline(unknown), "Add FX WR 1: roster legality unverified");
  const plain = { ...base, roster: {} } as unknown as AddDropResponse;
  assert.equal(headline(plain), "Add FX WR 1: a projected lineup gain");
});

test("an unverified roster never renders a bare count, 'within capacity' or 'no drop needed'", () => {
  // Sleeper IDP: 14 counted of 17, the 3 invisible players make the count wrong
  const ro = { active_before: 14, active_after: 15, active_capacity: 17 };
  assert.equal(activeRosterText(ro, legality), "unverified");
  assert.equal(activeRosterText({ active_before: null, active_after: null, active_capacity: 17 }, undefined), "unverified");
  assert.equal(activeRosterText(ro, undefined), "14 → 15 of 17");
  const iss = { active_before_drops: 15, active_capacity: 17, over_capacity_by: 0 };
  assert.equal(capacityFacts(iss, legality), null);
  assert.equal(capacityFacts({ ...iss, active_before_drops: null, over_capacity_by: null }, undefined), null);
  assert.deepEqual(capacityFacts(iss, undefined), { used: 15, capacity: 17, over: 0 });
  assert.equal(noDropText(legality), "none found on an unverified roster count");
  assert.equal(noDropText(undefined), "none");
});

test("an unvalued drop never shows a 0.0 cost", () => {
  assert.equal(dropCostText(3.25, 3.25), "3.3");
  assert.equal(dropCostText(null, 1.5), "1.5 known only");
  assert.equal(dropCostText(null, null), "not valued (a player no engine values)");
});
